"""Tests for web UI authentication (debridnzbd.web.auth).

Tests session creation, validation, destruction, rate limiting,
and the web auth middleware.
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from httpx import ASGITransport, AsyncClient

from debridnzbd.web.auth import (
    WEB_SESSION_TIMEOUT,
    create_web_session,
    destroy_web_session,
    validate_web_session,
    web_auth_middleware,
    _check_web_rate_limit,
    _record_web_failure,
    _requires_web_auth,
    _web_sessions,
    _web_login_failures,
)


@pytest.fixture(autouse=True)
def clear_sessions():
    """Clear session and rate limit state between tests."""
    _web_sessions.clear()
    _web_login_failures.clear()
    # Clear trusted network cache between tests
    from debridnzbd.web.auth import _trusted_networks_cache, _trusted_networks_cache_time
    _trusted_networks_cache.clear()
    # Use module-level import to reset the global
    import debridnzbd.web.auth as _auth_mod
    _auth_mod._trusted_networks_cache_time = 0.0
    yield
    _web_sessions.clear()
    _web_login_failures.clear()
    _trusted_networks_cache.clear()
    _auth_mod._trusted_networks_cache_time = 0.0


# ------------------------------------------------------------------ #
#  Session management tests                                            #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_create_session():
    """Sessions should be created with a unique ID and stored."""
    sid = await create_web_session("admin")
    assert sid is not None
    assert len(sid) == 48  # 24 bytes = 48 hex chars
    assert sid in _web_sessions
    assert _web_sessions[sid]["username"] == "admin"


@pytest.mark.asyncio
async def test_create_session_unique():
    """Each session should get a unique ID."""
    sid1 = await create_web_session("user1")
    sid2 = await create_web_session("user2")
    assert sid1 != sid2


@pytest.mark.asyncio
async def test_validate_valid_session():
    """Valid sessions should be validated and update last access time."""
    sid = await create_web_session("admin")
    session = await validate_web_session(sid)
    assert session is not None
    assert session["username"] == "admin"
    # Last access should be recent (within last second)
    assert time.time() - session["last_access"] < 1


@pytest.mark.asyncio
async def test_validate_invalid_session():
    """Invalid session IDs should return None."""
    result = await validate_web_session("nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_validate_expired_session():
    """Expired sessions should be removed and return None."""
    sid = await create_web_session("admin")
    # Manually expire the session
    _web_sessions[sid]["last_access"] = time.time() - WEB_SESSION_TIMEOUT - 1
    result = await validate_web_session(sid)
    assert result is None
    assert sid not in _web_sessions  # Should be cleaned up


@pytest.mark.asyncio
async def test_destroy_session():
    """Destroying a session should remove it from the store."""
    sid = await create_web_session("admin")
    assert sid in _web_sessions
    await destroy_web_session(sid)
    assert sid not in _web_sessions


@pytest.mark.asyncio
async def test_destroy_nonexistent_session():
    """Destroying a non-existent session should not raise an error."""
    await destroy_web_session("nonexistent")  # Should not raise


# ------------------------------------------------------------------ #
#  Rate limiting tests                                                 #
# ------------------------------------------------------------------ #


def test_rate_limit_allows_initial_attempts():
    """Initial attempts should not be rate-limited."""
    assert not _check_web_rate_limit("192.168.1.1")


def test_rate_limit_blocks_after_max_failures():
    """After MAX_WEB_LOGIN_FAILURES, the IP should be blocked."""
    for _ in range(10):
        _record_web_failure("192.168.1.1")
    assert _check_web_rate_limit("192.168.1.1")


def test_rate_limit_separate_ips():
    """Different IPs should have separate rate limits."""
    for _ in range(10):
        _record_web_failure("192.168.1.1")
    assert _check_web_rate_limit("192.168.1.1")
    assert not _check_web_rate_limit("192.168.1.2")


def test_rate_limit_window_expiry():
    """Rate limit should expire after WEB_LOGIN_RATE_WINDOW."""
    # Record failures with old timestamps
    old_time = time.time() - 301  # 5+ minutes ago
    _web_login_failures["192.168.1.1"] = [old_time] * 10
    # Should not be rate-limited (old entries expired)
    assert not _check_web_rate_limit("192.168.1.1")


# ------------------------------------------------------------------ #
#  Path classification tests                                           #
# ------------------------------------------------------------------ #


def test_requires_web_auth_home():
    """Home page requires web auth."""
    assert _requires_web_auth("/") is True


def test_requires_web_auth_history():
    """History page requires web auth."""
    assert _requires_web_auth("/history") is True


def test_requires_web_auth_config():
    """Config pages require web auth."""
    assert _requires_web_auth("/config/general") is True


def test_requires_web_auth_status():
    """Status page requires web auth."""
    assert _requires_web_auth("/status") is True


def test_requires_web_auth_provider():
    """Provider page requires web auth."""
    assert _requires_web_auth("/provider") is True


def test_requires_web_auth_logs():
    """Logs page requires web auth."""
    assert _requires_web_auth("/logs") is True


def test_requires_web_auth_api_browse():
    """API browse endpoints require web auth (they're web UI endpoints)."""
    assert _requires_web_auth("/api/browse") is True


def test_exempt_api():
    """SABnzbd API endpoint is exempt (has its own auth)."""
    assert _requires_web_auth("/api") is False


def test_exempt_qbit_api():
    """qBittorrent API endpoints are exempt (have their own auth)."""
    assert _requires_web_auth("/api/v2/auth/login") is False
    assert _requires_web_auth("/api/v2/torrents/info") is False


def test_exempt_static():
    """Static assets are exempt from auth."""
    assert _requires_web_auth("/static/css/style.css") is False
    assert _requires_web_auth("/static/js/main.js") is False
    assert _requires_web_auth("/static/img/logo.svg") is False


def test_exempt_login():
    """Login page is exempt from auth."""
    assert _requires_web_auth("/login") is False


def test_exempt_logout():
    """Logout endpoint is exempt from auth."""
    assert _requires_web_auth("/logout") is False


# ------------------------------------------------------------------ #
#  Middleware integration tests                                         #
# ------------------------------------------------------------------ #


def _create_test_app(credentials_configured=True, setup_complete=True, temp_credentials=False):
    """Create a test FastAPI app with web auth middleware."""
    app = FastAPI()

    # Mock config store
    config = AsyncMock()
    if credentials_configured:
        config.get = AsyncMock(side_effect=lambda s, k, d="": {
            ("misc", "username"): "admin",
            ("misc", "password"): "secret123",
        }.get((s, k), d))
        config.get_bool = AsyncMock(side_effect=lambda s, k, d=False: {
            ("misc", "setup_complete"): setup_complete,
            ("misc", "temp_credentials"): temp_credentials,
        }.get((s, k), d))
    else:
        config.get = AsyncMock(side_effect=lambda s, k, d="": {
            ("misc", "username"): "",
            ("misc", "password"): "",
        }.get((s, k), d))
        config.get_bool = AsyncMock(return_value=False)

    app.state.config = config

    # Add web auth middleware
    app.middleware("http")(web_auth_middleware)

    # Add test routes
    @app.get("/")
    async def home():
        return HTMLResponse("<h1>Home</h1>")

    @app.get("/setup")
    async def setup_page():
        return HTMLResponse("<h1>Setup</h1>")

    @app.get("/login")
    async def login_page():
        return HTMLResponse("<h1>Login</h1>")

    @app.api_route("/logout", methods=["GET", "POST"])
    async def logout():
        return HTMLResponse("<h1>Logged Out</h1>")

    @app.get("/api")
    async def api():
        return {"status": True}

    return app


@pytest.mark.asyncio
async def test_middleware_no_credentials_configured():
    """When no credentials are configured, all pages should be accessible."""
    app = _create_test_app(credentials_configured=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/", follow_redirects=False)
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_middleware_redirects_to_login():
    """When credentials are configured, unauthenticated GET requests should redirect to /login."""
    app = _create_test_app(credentials_configured=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/", follow_redirects=False)
        assert response.status_code == 303
        assert "/login" in response.headers.get("location", "")


@pytest.mark.asyncio
async def test_middleware_post_returns_403():
    """When credentials are configured, unauthenticated POST requests should return 403."""
    app = _create_test_app(credentials_configured=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/")
        assert response.status_code == 403


@pytest.mark.asyncio
async def test_middleware_allows_with_valid_session():
    """When credentials are configured, valid sessions should allow access."""
    app = _create_test_app(credentials_configured=True)
    sid = await create_web_session("admin")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/", cookies={"web_session": sid})
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_middleware_rejects_invalid_session():
    """Invalid session cookies should be treated as unauthenticated."""
    app = _create_test_app(credentials_configured=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/", cookies={"web_session": "invalid"}, follow_redirects=False)
        assert response.status_code == 303
        assert "/login" in response.headers.get("location", "")


@pytest.mark.asyncio
async def test_middleware_exempt_login_page():
    """Login page should always be accessible (even without auth)."""
    app = _create_test_app(credentials_configured=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/login")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_middleware_exempt_api():
    """SABnzbd API endpoint should pass through (has its own auth)."""
    app = _create_test_app(credentials_configured=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api?mode=version")
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_middleware_exempt_static():
    """Static assets should pass through without auth."""
    app = _create_test_app(credentials_configured=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/static/css/style.css")
        # 404 is expected since no static files are mounted in test app,
        # but it should NOT be 303 (redirect to login)
        assert response.status_code != 303


# ------------------------------------------------------------------ #
#  Setup wizard redirect tests                                         #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_setup_incomplete_redirects_to_setup():
    """When setup_complete is False, authenticated GETs redirect to /setup."""
    app = _create_test_app(credentials_configured=True, setup_complete=False)
    sid = await create_web_session("admin")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/", cookies={"web_session": sid}, follow_redirects=False)
        assert response.status_code == 303
        assert "/setup" in response.headers.get("location", "")


@pytest.mark.asyncio
async def test_setup_incomplete_post_returns_403():
    """When setup_complete is False, authenticated POSTs return 403."""
    app = _create_test_app(credentials_configured=True, setup_complete=False)
    sid = await create_web_session("admin")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/", cookies={"web_session": sid})
        assert response.status_code == 403


@pytest.mark.asyncio
async def test_setup_page_not_redirected():
    """The /setup page itself should NOT be redirected even when setup_complete is False."""
    app = _create_test_app(credentials_configured=True, setup_complete=False)
    sid = await create_web_session("admin")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/setup", cookies={"web_session": sid}, follow_redirects=False)
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_setup_complete_allows_access():
    """When setup_complete is True, normal access is allowed."""
    app = _create_test_app(credentials_configured=True, setup_complete=True)
    sid = await create_web_session("admin")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/", cookies={"web_session": sid})
        assert response.status_code == 200


# ------------------------------------------------------------------ #
#  Trusted network bypass tests                                        #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_trusted_network_bypass():
    """Requests from trusted networks should bypass auth."""
    app = _create_test_app(credentials_configured=True, setup_complete=True)
    # Override get to return a trusted_networks value.
    # httpx ASGITransport uses "testserver" as client host by default,
    # so we can't match it with a real CIDR. Instead, we mock _is_trusted_network
    # to return True for this test.
    from debridnzbd.web import auth as auth_module
    original_fn = auth_module._is_trusted_network
    auth_module._is_trusted_network = AsyncMock(return_value=True)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/", follow_redirects=False)
            assert response.status_code == 200
    finally:
        auth_module._is_trusted_network = original_fn


@pytest.mark.asyncio
async def test_trusted_network_bypass_disabled_during_temp_creds():
    """Trusted network bypass should be disabled when temp_credentials is active."""
    app = _create_test_app(credentials_configured=True, setup_complete=False, temp_credentials=True)
    # Even if _is_trusted_network would return True, temp_credentials blocks bypass
    from debridnzbd.web import auth as auth_module
    original_fn = auth_module._is_trusted_network
    auth_module._is_trusted_network = AsyncMock(return_value=True)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/", follow_redirects=False)
            # Should redirect to login, not bypass
            assert response.status_code == 303
            assert "/login" in response.headers.get("location", "")
    finally:
        auth_module._is_trusted_network = original_fn


@pytest.mark.asyncio
async def test_non_trusted_network_requires_auth():
    """Requests from non-trusted networks should require auth."""
    app = _create_test_app(credentials_configured=True, setup_complete=True)
    # No trusted networks configured (default)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/", follow_redirects=False)
        assert response.status_code == 303
        assert "/login" in response.headers.get("location", "")


# ------------------------------------------------------------------ #
#  _is_trusted_network function tests                                  #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_is_trusted_network_match():
    """IP in a configured CIDR should match."""
    from debridnzbd.web.auth import _is_trusted_network, _trusted_networks_cache
    from debridnzbd.core.config_store import ConfigStore
    from debridnzbd.db.database import Database

    config = AsyncMock()
    config.get = AsyncMock(return_value="192.168.1.0/24")
    _trusted_networks_cache.clear()

    result = await _is_trusted_network(config, "192.168.1.100")
    assert result is True


@pytest.mark.asyncio
async def test_is_trusted_network_no_match():
    """IP not in a configured CIDR should not match."""
    from debridnzbd.web.auth import _is_trusted_network, _trusted_networks_cache

    config = AsyncMock()
    config.get = AsyncMock(return_value="192.168.1.0/24")
    _trusted_networks_cache.clear()

    result = await _is_trusted_network(config, "10.0.0.1")
    assert result is False


@pytest.mark.asyncio
async def test_is_trusted_network_empty_config():
    """Empty trusted_networks config should return False."""
    from debridnzbd.web.auth import _is_trusted_network, _trusted_networks_cache

    config = AsyncMock()
    config.get = AsyncMock(return_value="")
    _trusted_networks_cache.clear()

    result = await _is_trusted_network(config, "192.168.1.1")
    assert result is False


@pytest.mark.asyncio
async def test_is_trusted_network_multiple_cidrs():
    """Multiple CIDR ranges should all be checked."""
    from debridnzbd.web.auth import _is_trusted_network, _trusted_networks_cache

    config = AsyncMock()
    config.get = AsyncMock(return_value="10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16")
    _trusted_networks_cache.clear()

    assert await _is_trusted_network(config, "10.5.1.1") is True
    assert await _is_trusted_network(config, "172.20.0.1") is True
    assert await _is_trusted_network(config, "192.168.99.99") is True
    assert await _is_trusted_network(config, "8.8.8.8") is False


@pytest.mark.asyncio
async def test_is_trusted_network_ipv6():
    """IPv6 addresses should work with IPv6 CIDR ranges."""
    from debridnzbd.web.auth import _is_trusted_network, _trusted_networks_cache

    config = AsyncMock()
    config.get = AsyncMock(return_value="::1/128, fd00::/8")
    _trusted_networks_cache.clear()

    assert await _is_trusted_network(config, "::1") is True
    assert await _is_trusted_network(config, "fd00::1") is True
    assert await _is_trusted_network(config, "fe80::1") is False


@pytest.mark.asyncio
async def test_is_trusted_network_invalid_ip():
    """Invalid client IP should return False."""
    from debridnzbd.web.auth import _is_trusted_network, _trusted_networks_cache

    config = AsyncMock()
    config.get = AsyncMock(return_value="192.168.0.0/16")
    _trusted_networks_cache.clear()

    result = await _is_trusted_network(config, "not-an-ip")
    assert result is False


# ------------------------------------------------------------------ #
#  _requires_web_auth path classification — additional tests           #
# ------------------------------------------------------------------ #


def test_requires_web_auth_setup():
    """/setup requires web auth (but is exempt from setup-redirect in middleware)."""
    assert _requires_web_auth("/setup") is True