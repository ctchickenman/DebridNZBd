"""Shared FastAPI dependencies for the qBittorrent API.

Provides dependency injection functions for database, config, and
session authentication. All qBittorrent endpoints use these to access
app state and enforce authentication.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from fastapi import Depends, HTTPException, Request

from debridnzbd.core.config_store import ConfigStore
from debridnzbd.db.database import Database

logger = logging.getLogger(__name__)


async def get_db(request: Request) -> Database:
    """Extract the database from app state."""
    return request.app.state.db


async def get_config(request: Request) -> ConfigStore:
    """Extract the config store from app state."""
    return request.app.state.config


async def require_sid(request: Request) -> str:
    """Validate the SID cookie. Raise 403 if invalid or missing.

    All qBittorrent API endpoints (except login) require this dependency.
    Uses lazy import to avoid circular dependency with auth.py.
    """
    from debridnzbd.api.qbittorrent.auth import validate_session

    sid = request.cookies.get("SID", "")
    if not sid or not await validate_session(sid):
        raise HTTPException(status_code=403, detail="Forbidden")
    return sid


async def require_csrf(request: Request) -> None:
    """Enforce qBittorrent CSRF protection for mutating requests.

    qBittorrent requires the Referer or Origin header to match the Host
    header for all non-GET requests, except the login endpoint itself.
    This prevents cross-origin attacks from malicious websites.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return

    # Login endpoint is exempt from CSRF
    if request.url.path == "/api/v2/auth/login":
        return

    host = request.headers.get("host", "")
    origin = request.headers.get("origin", "")
    referer = request.headers.get("referer", "")

    if origin:
        parsed = urlparse(origin)
        if parsed.netloc != host:
            raise HTTPException(status_code=403, detail="Invalid Origin header")
    elif referer:
        parsed = urlparse(referer)
        if parsed.netloc != host:
            raise HTTPException(status_code=403, detail="Invalid Referer header")
    else:
        # Neither Origin nor Referer present on a mutating request — reject
        # to prevent CSRF bypass via header-stripping proxies or attackers
        raise HTTPException(
            status_code=403,
            detail="CSRF validation failed: missing Origin and Referer headers",
        )