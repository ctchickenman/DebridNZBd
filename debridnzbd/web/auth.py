"""Web UI authentication for DebridNZBd.

Implements cookie-based session authentication for the web interface.
Sessions are stored in memory with a configurable inactivity timeout.

When username/password credentials are configured in misc.username and
misc.password, all web UI pages require authentication. When no credentials
are configured, the web UI is accessible without authentication (for
backward compatibility and local-only deployments).

On first launch with no credentials, temporary credentials are generated
and displayed in the log. The user is then forced through a setup wizard
to set permanent credentials.

Trusted networks (CIDR ranges) can be configured to bypass authentication
for requests from those networks. This bypass is disabled when temporary
credentials are active (setup must be completed first).

This is separate from the SABnzbd API key auth (in api/auth.py) and the
qBittorrent SID auth (in api/qbittorrent/auth.py).
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import secrets
import time
from collections import defaultdict
from urllib.parse import quote

from fastapi import Request, Response
from fastapi.responses import RedirectResponse

from debridnzbd.core.config_store import ConfigStore

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
#  In-memory session store                                              #
# ------------------------------------------------------------------ #

# session_id -> {username, last_access}
_web_sessions: dict[str, dict] = {}

# 8-hour session timeout — longer than qBittorrent's 1-hour SID timeout
# because web UI sessions are typically longer-lived (users keep tabs open).
WEB_SESSION_TIMEOUT = 28800  # seconds

# Rate limiting for web login attempts.
# Prevents brute-force attacks on the login form.
_web_login_failures: dict[str, list[float]] = defaultdict(list)
MAX_WEB_LOGIN_FAILURES = 10
WEB_LOGIN_RATE_WINDOW = 300  # 5 minutes


async def create_web_session(username: str) -> str:
    """Create a new web session and return the session ID.

    The session ID is a 192-bit random token (48 hex characters) stored in
    an HttpOnly, SameSite=Lax cookie.
    """
    session_id = secrets.token_hex(24)  # 48 hex chars (192-bit)
    _web_sessions[session_id] = {
        "username": username,
        "last_access": time.time(),
    }
    logger.info("Web auth: session created for user '%s'", username)
    return session_id


async def validate_web_session(session_id: str) -> dict | None:
    """Validate a web session. Returns session dict if valid, None if expired/invalid.

    Updates the last access time on each validation to implement
    inactivity timeout (sessions expire after WEB_SESSION_TIMEOUT seconds
    of inactivity, not a fixed lifetime).
    """
    if session_id not in _web_sessions:
        return None

    session = _web_sessions[session_id]
    if time.time() - session["last_access"] > WEB_SESSION_TIMEOUT:
        del _web_sessions[session_id]
        logger.info(
            "Web auth: expired session removed for user '%s'",
            session["username"],
        )
        return None

    # Update last access time
    session["last_access"] = time.time()
    return session


async def destroy_web_session(session_id: str) -> None:
    """Destroy a web session by its session ID."""
    session = _web_sessions.pop(session_id, None)
    if session:
        logger.info(
            "Web auth: session destroyed for user '%s'",
            session.get("username", "unknown"),
        )


def _check_web_rate_limit(ip: str) -> bool:
    """Check if an IP is rate-limited for web login. Returns True if blocked."""
    now = time.time()
    cutoff = now - WEB_LOGIN_RATE_WINDOW
    _web_login_failures[ip] = [t for t in _web_login_failures[ip] if t > cutoff]
    return len(_web_login_failures[ip]) >= MAX_WEB_LOGIN_FAILURES


def _record_web_failure(ip: str) -> None:
    """Record a failed web login attempt for rate limiting."""
    _web_login_failures[ip].append(time.time())


# ------------------------------------------------------------------ #
#  Trusted network bypass                                               #
# ------------------------------------------------------------------ #

# Cache parsed CIDR networks to avoid re-parsing on every request.
# Refreshed every 60 seconds.
_trusted_networks_cache: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
_trusted_networks_cache_time: float = 0.0
_TRUSTED_NETWORKS_CACHE_TTL = 60.0  # seconds


async def _is_trusted_network(config: ConfigStore, client_ip: str) -> bool:
    """Check if a client IP is in a trusted network.

    Parses ``misc.trusted_networks`` (comma-separated CIDR list) and
    caches the result for 60 seconds. Returns False if trusted_networks
    is empty, the IP is invalid, or the config is unavailable.
    """
    global _trusted_networks_cache_time

    now = time.time()
    if now - _trusted_networks_cache_time < _TRUSTED_NETWORKS_CACHE_TTL:
        networks = _trusted_networks_cache
    else:
        # Refresh cache
        raw = await config.get("misc", "trusted_networks", "")
        networks = []
        if raw:
            for entry in raw.split(","):
                entry = entry.strip()
                if not entry:
                    continue
                try:
                    networks.append(ipaddress.ip_network(entry, strict=False))
                except ValueError:
                    logger.warning("Web auth: invalid trusted network CIDR: %s", entry)
        _trusted_networks_cache.clear()
        _trusted_networks_cache.extend(networks)
        _trusted_networks_cache_time = now

    if not networks:
        return False

    try:
        ip = ipaddress.ip_address(client_ip)
    except ValueError:
        return False

    return any(ip in network for network in networks)


# ------------------------------------------------------------------ #
#  Paths exempt from web auth                                           #
# ------------------------------------------------------------------ #

# Exact paths that are exempt from web session auth.
# - /api: SABnzbd API endpoint (has its own API key auth)
# - /login: Must be accessible for unauthenticated users
# - /logout: Must be accessible to destroy sessions
AUTH_EXEMPT_EXACT = {"/api", "/login", "/logout"}

# Path prefixes that are exempt from web session auth.
# - /api/v2/: qBittorrent API (has its own SID-based auth)
# - /static/: Static assets (CSS, JS, images) don't need auth
AUTH_EXEMPT_PREFIXES = ("/api/v2/", "/static/")


def _requires_web_auth(path: str) -> bool:
    """Check if a request path requires web UI authentication.

    Returns True for web UI paths that need session auth.
    Returns False for paths that have their own auth or are public assets.
    """
    # Strip trailing slash for consistent comparison
    path = path.rstrip("/")
    if not path:
        path = "/"

    # Check exact exempt paths
    if path in AUTH_EXEMPT_EXACT:
        return False

    # Check exempt prefixes
    for prefix in AUTH_EXEMPT_PREFIXES:
        if path.startswith(prefix):
            return False
        # Handle paths like /api/v2/auth/login (with trailing content)
        if path + "/" == prefix or (path + "/").startswith(prefix):
            return False

    return True


async def web_auth_middleware(request: Request, call_next):
    """Middleware that enforces web UI authentication.

    Flow:
    1. Skip auth for exempt paths (API endpoints, static assets, login)
    2. Trusted network bypass (only when temp_credentials is NOT active)
    3. If no credentials are configured, allow access without auth
    4. If credentials are configured, check for a valid web_session cookie
    5. If session is valid but setup is not complete, redirect to /setup
    6. Redirect GET requests to /login if not authenticated
    7. Return 403 for non-GET requests if not authenticated
    """
    path = request.url.path

    # Step 1: Skip auth for non-web paths (API, static assets)
    if not _requires_web_auth(path):
        return await call_next(request)

    # Check if credentials are configured
    config: ConfigStore | None = getattr(request.app.state, "config", None)
    if config is None:
        # Config not ready during startup — allow through
        return await call_next(request)

    username = await config.get("misc", "username")
    password = await config.get("misc", "password")
    temp_creds = await config.get_bool("misc", "temp_credentials", False)

    # Step 2: Trusted network bypass (disabled when temp_credentials active)
    # Must complete setup before trusted network bypass is allowed.
    if not temp_creds:
        client_ip = request.client.host if request.client else ""
        if client_ip and await _is_trusted_network(config, client_ip):
            request.state.web_user = None
            return await call_next(request)

    # Step 3: If no credentials configured, allow access without authentication.
    # This maintains backward compatibility for local-only deployments.
    if not username and not password:
        request.state.web_user = None
        return await call_next(request)

    # Step 4: Check for valid session cookie
    session_id = request.cookies.get("web_session")
    if session_id:
        session = await validate_web_session(session_id)
        if session:
            request.state.web_user = session["username"]

            # Step 5: If setup is not complete, force redirect to /setup.
            # /setup itself is exempt from this redirect so the user can
            # actually complete the wizard.
            setup_complete = await config.get_bool("misc", "setup_complete", False)
            if not setup_complete:
                normalized_path = path.rstrip("/") or "/"
                if normalized_path != "/setup":
                    if request.method == "GET":
                        return RedirectResponse(url="/setup", status_code=303)
                    return Response(
                        status_code=403,
                        content="Setup must be completed first",
                    )

            return await call_next(request)

    # Step 6: No valid session — redirect GET requests to login page
    if request.method == "GET":
        next_url = request.url.path
        if request.url.query:
            next_url += "?" + request.url.query
        return RedirectResponse(
            url=f"/login?next={quote(next_url)}",
            status_code=303,
        )

    # Step 7: Non-GET requests without auth return 403
    return Response(status_code=403, content="Authentication required")