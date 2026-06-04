"""Authentication and authorization for DebridNZBd.

Implements SABnzbd-compatible API key validation with two levels:
1. **API Key** — Full access to all API modes and configuration
2. **NZB Key** — Restricted access: only addurl, addfile, and queue monitoring

Key validation follows SABnzbd's convention:
- Keys are passed as the `apikey` or `ma_username`/`ma_password` query parameters
- The `mode=version` and `mode=auth` endpoints do NOT require authentication
- All other API endpoints require a valid key

All secret comparisons use `hmac.compare_digest()` to prevent timing attacks.

Web UI authentication uses username/password stored in the config,
with session cookies managed by Starlette's session middleware.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any

from fastapi import Request
from starlette.responses import JSONResponse

from debridnzbd.core.config_store import ConfigStore

logger = logging.getLogger(__name__)

# API modes that do NOT require authentication — matches SABnzbd behavior.
# These are publicly accessible endpoints for version detection and auth checking.
PUBLIC_MODES = {"version", "auth"}

# API modes accessible with the NZB key (add-only + queue monitoring).
# The NZB key is intended for indexers that only need to submit NZBs
# and check queue status, not for full admin access.
# NOTE: "speedlimit" is intentionally excluded — it modifies global settings.
# NOTE: "pause" and "resume" are intentionally excluded — they operate on
# the global queue, not per-job. An indexer should not be able to pause
# or resume the entire download queue.
NZB_KEY_MODES = {
    "addurl", "addfile", "addlocalfile", "queue",
    "history",
    "get_cats",
}

# Keywords that should be redacted in logs — any config value whose
# keyword matches one of these will be logged as "***REDACTED***".
SENSITIVE_KEYWORDS = frozenset({
    "api_key", "nzb_key", "password", "email_password",
    "email_account", "socks5_proxy",
})


def redact_config_value(section: str, keyword: str, value: str) -> str:
    """Redact sensitive configuration values for logging.

    Args:
        section: Config section name.
        keyword: Config keyword name.
        value: The config value.

    Returns:
        The original value if not sensitive, or "***REDACTED***" if sensitive.
    """
    if keyword in SENSITIVE_KEYWORDS:
        return "***REDACTED***"
    return value


async def validate_api_key(
    request: Request, config: ConfigStore
) -> tuple[bool, str]:
    """Validate the API key from the request query parameters or form body.

    SABnzbd accepts the API key in three ways:
    1. Query parameter: ?apikey=...
    2. POST form field: apikey=... in the request body
    3. ma_username/ma_password query parameters (SABnzbd compatibility)

    Uses constant-time comparison (hmac.compare_digest) to prevent
    timing attacks. Returns (is_valid, auth_level) where auth_level
    is "full", "nzb", or "" if invalid.

    Args:
        request: The incoming HTTP request.
        config: The ConfigStore for reading API keys.

    Returns:
        Tuple of (is_valid, auth_level). If is_valid is False,
        auth_level is an empty string and the request should be rejected.
    """
    # Check if authentication is disabled (development mode)
    disable_api_key = await config.get_bool("special", "disable_api_key", False)
    if disable_api_key:
        logger.warning(
            "API authentication is DISABLED (special.disable_api_key=1). "
            "This should only be used for development."
        )
        return True, "full"

    # Extract the API key — check query params first, then POST form body
    api_key = request.query_params.get("apikey", "")

    if not api_key:
        # Also accept ma_username/ma_password format (SABnzbd compatibility)
        ma_username = request.query_params.get("ma_username", "")
        ma_password = request.query_params.get("ma_password", "")
        if ma_username and ma_password:
            api_key = f"{ma_username}:{ma_password}"

    # If no key in query params, check POST form body.
    # SABnzbd-compatible clients (and the web UI) send apikey as a form field.
    # Always parse the form for POST requests and cache it on request.state
    # so the API router can access it without re-reading the body stream.
    if request.method in ("POST", "PUT", "PATCH"):
        try:
            form = await request.form()
            request.state._form_data = form
            if not api_key:
                form_api_key = str(form.get("apikey", ""))
                if form_api_key:
                    api_key = form_api_key
        except Exception:
            pass  # Not a form request or parse error — skip form extraction

    # Reject empty keys — prevents "" == "" bypass
    if not api_key:
        return False, ""

    # Read configured keys
    configured_api_key = await config.get("misc", "api_key")
    configured_nzb_key = await config.get("misc", "nzb_key")

    # Also reject if the configured key itself is empty (not yet set up)
    if not configured_api_key:
        logger.error("API key not configured — authentication is broken")
        return False, ""

    # Constant-time comparison to prevent timing attacks
    if hmac.compare_digest(api_key, configured_api_key):
        return True, "full"

    if hmac.compare_digest(api_key, configured_nzb_key):
        return True, "nzb"

    # Log invalid attempt — only log the length, not any part of the key
    # to prevent character-by-character oracle attacks
    logger.warning(
        "Invalid API key attempted (length=%d)",
        len(api_key),
    )
    return False, ""


def check_auth_for_mode(auth_level: str, mode: str) -> bool:
    """Check if the given auth level is sufficient for the requested mode.

    Args:
        auth_level: "full", "nzb", or "public".
        mode: The SABnzbd API mode being requested.

    Returns:
        True if the auth level permits the mode, False otherwise.
    """
    # Public modes don't need any auth
    if mode in PUBLIC_MODES:
        return True

    # Full auth can do everything
    if auth_level == "full":
        return True

    # NZB key can only access restricted modes
    if auth_level == "nzb":
        return mode in NZB_KEY_MODES

    return False


async def auth_middleware(request: Request, call_next: Any) -> Any:
    """ASGI middleware that validates API key authentication for /api requests.

    Flow:
    1. If the path is not /api, pass through (web UI has its own auth)
    2. If the mode is in PUBLIC_MODES, pass through without auth
    3. Extract and validate the API key using constant-time comparison
    4. Check if the auth level permits the requested mode
    5. Set auth_level in request.state and continue, or return 403

    This function is designed to be used as a Starlette BaseHTTPMiddleware
    or as a standalone async function called from app middleware.
    """
    # Only authenticate /api endpoint — use exact path match to prevent
    # false positives on URLs like /foo/bar/api or missing /api/v2 routes
    path = request.url.path.rstrip("/")
    if path != "/api":
        return await call_next(request)

    # Get the config store from app state
    config: ConfigStore | None = getattr(request.app.state, "config", None)
    if config is None:
        # Config not yet initialized — deny access instead of granting full access.
        # This prevents a race condition during startup where an attacker could
        # gain unauthenticated admin access during the brief window before the
        # lifespan handler completes initialization.
        logger.warning(
            "Auth middleware running before config is initialized — "
            "returning 503 Service Unavailable. "
            "This should only happen during startup."
        )
        return JSONResponse(
            status_code=503,
            content={
                "status": False,
                "error": "Service starting up — please retry in a moment",
            },
        )

    mode = request.query_params.get("mode", "")
    output = request.query_params.get("output", "json")

    # Public modes don't require authentication
    if mode in PUBLIC_MODES:
        request.state.auth_level = "public"
        return await call_next(request)

    # Validate the API key
    is_valid, auth_level = await validate_api_key(request, config)

    if not is_valid:
        if auth_level == "":
            # No key provided
            return JSONResponse(
                status_code=403,
                content={"status": False, "error": "API key required"},
            )
        # This shouldn't happen since validate_api_key returns (False, "")
        # for invalid keys, but handle it defensively
        return JSONResponse(
            status_code=403,
            content={"status": False, "error": "Invalid API key"},
        )

    # Check if the auth level permits this mode
    if not check_auth_for_mode(auth_level, mode):
        if auth_level == "nzb":
            return JSONResponse(
                status_code=403,
                content={"status": False, "error": "NZB key does not have access to this mode"},
            )
        return JSONResponse(
            status_code=403,
            content={"status": False, "error": "Insufficient permissions"},
        )

    request.state.auth_level = auth_level
    return await call_next(request)


async def check_api_access(request: Request) -> str:
    """Dependency that checks if the request has at least API-level access.

    Returns the auth level ("full", "nzb", or "public").
    Raises HTTPException if auth_level is not set.
    """
    auth_level = getattr(request.state, "auth_level", None)
    if auth_level is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Authentication required")
    return auth_level


async def check_full_access(request: Request) -> str:
    """Dependency that requires full API key access (not NZB key).

    Returns "full" if the request has full access.
    Raises HTTPException if the auth level is "nzb" or not set.
    """
    from fastapi import HTTPException
    auth_level = await check_api_access(request)
    if auth_level == "nzb":
        raise HTTPException(status_code=403, detail="Full API key required for this operation")
    return auth_level