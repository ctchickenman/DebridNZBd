"""qBittorrent authentication endpoints and session management.

Implements cookie-based SID authentication matching qBittorrent's WebUI API.
Sessions are stored in memory with a configurable inactivity timeout.

qBittorrent clients authenticate by POSTing username/password to
/api/v2/auth/login, receiving a Set-Cookie header with a SID value,
then including that cookie in all subsequent requests.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import time
from collections import defaultdict

from fastapi import APIRouter, Depends, Request, Response

from debridnzbd.api.qbittorrent.dependencies import get_config, require_sid

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["qBittorrent Auth"])

# ------------------------------------------------------------------ #
#  In-memory session store                                              #
# ------------------------------------------------------------------ #

_sessions: dict[str, float] = {}  # sid -> last_access_time
SESSION_TIMEOUT = 3600  # 1 hour inactivity timeout

# Rate limiting: track failed login attempts per IP
_login_failures: dict[str, list[float]] = defaultdict(list)
MAX_LOGIN_FAILURES = 5  # per minute
LOGIN_RATE_WINDOW = 60  # seconds


async def create_session() -> str:
    """Create a new session and return the SID value."""
    sid = secrets.token_hex(20)  # 40 hex chars
    _sessions[sid] = time.time()
    logger.info("Created new SID session")
    return sid


async def validate_session(sid: str) -> bool:
    """Check if a session is valid and not expired.

    Updates the last access time if valid. Removes expired sessions.
    """
    if sid not in _sessions:
        return False

    last_access = _sessions[sid]
    if time.time() - last_access > SESSION_TIMEOUT:
        del _sessions[sid]
        logger.info("Expired SID session removed")
        return False

    # Update last access time
    _sessions[sid] = time.time()
    return True


async def destroy_session(sid: str) -> None:
    """Destroy a session by its SID value."""
    _sessions.pop(sid, None)


def _check_rate_limit(ip: str) -> bool:
    """Check if an IP is rate-limited. Returns True if blocked."""
    now = time.time()
    # Clean old entries
    cutoff = now - LOGIN_RATE_WINDOW
    _login_failures[ip] = [t for t in _login_failures[ip] if t > cutoff]
    return len(_login_failures[ip]) >= MAX_LOGIN_FAILURES


def _record_failure(ip: str) -> None:
    """Record a failed login attempt for rate limiting."""
    _login_failures[ip].append(time.time())


# ------------------------------------------------------------------ #
#  Endpoints                                                           #
# ------------------------------------------------------------------ #


@router.post("/login")
async def auth_login(request: Request):
    """Authenticate and create a session.

    qBittorrent clients POST username/password as form data.
    On success: returns plain text 'Ok.' with a Set-Cookie header.
    On failure: returns plain text 'Fails.' with 403 status.
    """
    config = request.app.state.config

    # Rate limit check
    client_ip = request.client.host if request.client else "unknown"
    if _check_rate_limit(client_ip):
        logger.warning("Rate-limited login attempt from %s", client_ip)
        return Response(content="Fails.", status_code=403)

    # Parse form data
    form = await request.form()
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))

    # Get configured credentials
    configured_username = await config.get("misc", "username")
    configured_password = await config.get("misc", "password")

    # Determine whether to set the Secure flag on cookies
    https_enabled = await config.get_bool("misc", "https_enabled", False)

    # If both are empty (no auth configured), accept any credentials
    if not configured_username and not configured_password:
        sid = await create_session()
        response = Response(content="Ok.", media_type="text/plain")
        response.set_cookie(
            "SID",
            sid,
            max_age=SESSION_TIMEOUT,
            path="/",
            httponly=True,
            samesite="lax",
            secure=https_enabled,
        )
        return response

    # Validate credentials with constant-time comparison
    username_ok = hmac.compare_digest(username, configured_username)
    password_ok = hmac.compare_digest(password, configured_password)

    if username_ok and password_ok:
        sid = await create_session()
        response = Response(content="Ok.", media_type="text/plain")
        response.set_cookie(
            "SID",
            sid,
            max_age=SESSION_TIMEOUT,
            path="/",
            httponly=True,
            samesite="lax",
            secure=https_enabled,
        )
        return response

    # Failed login
    _record_failure(client_ip)
    logger.warning("Failed login attempt from %s", client_ip)
    return Response(content="Fails.", status_code=403, media_type="text/plain")


@router.api_route("/logout", methods=["GET", "POST"])
async def auth_logout(
    request: Request,
    sid: str = Depends(require_sid),
):
    """Destroy the current session."""
    await destroy_session(sid)
    response = Response(content="Ok.", media_type="text/plain")
    response.delete_cookie("SID", path="/")
    return response