"""Web UI routes for DebridNZBd.

Provides the browser-accessible interface with pages for the download
queue, history, configuration, and server status.

The web UI uses Jinja2 templates with a custom dark theme. Authentication
is separate from the API: the web UI uses username/password session auth
(cookie-based, see web/auth.py), while the API uses API key auth and the
qBittorrent API uses SID-based session auth.
"""

import asyncio
import hmac
import logging
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from debridnzbd.core.state_sync import _extract_download_name, _map_torbox_status
from debridnzbd.core.cdn_downloader import download_file as cdn_download_file, move_to_category_dir as cdn_move_to_category
from debridnzbd.torbox.client import TorboxClient
from debridnzbd.utils.nzo_id import generate_nzo_id
from debridnzbd.torbox.exceptions import (
    TorboxAuthError,
    TorboxConnectionError,
    TorboxError,
    TorboxRateLimitError,
)
from debridnzbd.utils.version import VERSION
from debridnzbd.utils.format import format_size, format_uptime, format_timestamp
from debridnzbd.web.auth import (
    create_web_session,
    destroy_web_session,
    validate_web_session,
    _check_web_rate_limit,
    _record_web_failure,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Template engine pointing to the web/templates/ directory
_templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))


# ------------------------------------------------------------------ #
#  Shared helpers                                                     #
# ------------------------------------------------------------------ #

async def _base_context(request: Request, **overrides) -> dict:
    """Build the common template context shared by all pages.

    Reads version, API key, speed, and paused state from app.state.config.
    Also includes authentication state (web_user, auth_configured) for
    the nav bar logout button.
    Additional keyword arguments override or extend the defaults.
    """
    config = getattr(request.app.state, "config", None)

    ctx = {
        "page": "",
        "version": VERSION,
        "api_key": "",
        "speed": "",
        "queue_paused": False,
        "flash_message": None,
        "flash_type": "info",
        "web_user": None,
        "auth_configured": False,
        "setup_complete": True,
    }

    if config is not None:
        try:
            ctx["api_key"] = await config.get("misc", "api_key")
        except Exception:
            pass

    # Authentication state from web auth middleware
    ctx["web_user"] = getattr(request.state, "web_user", None)

    # Check if authentication is configured (username or password set)
    if config is not None:
        username = await config.get("misc", "username")
        password = await config.get("misc", "password")
        ctx["auth_configured"] = bool(username) or bool(password)
        ctx["setup_complete"] = await config.get_bool("misc", "setup_complete", True)

    # Read flash messages from query params
    if request.query_params.get("saved"):
        ctx["flash_message"] = "Settings saved successfully."
        ctx["flash_type"] = "success"
    elif request.query_params.get("error"):
        ctx["flash_message"] = request.query_params.get("error")
        ctx["flash_type"] = "danger"

    ctx.update(overrides)
    return ctx


def _format_size(bytes_val: float) -> str:
    """Convert bytes to human-readable size string."""
    return format_size(bytes_val)


def _format_uptime(start_time: float) -> str:
    """Format seconds since start as 'Xd Xh Xm' string."""
    return format_uptime(start_time)


# ------------------------------------------------------------------ #
#  Generic config save handler                                        #
# ------------------------------------------------------------------ #

async def _save_config(request: Request, tab: str) -> RedirectResponse:
    """Generic config save handler — reads section.keyword form fields
    and writes each to ConfigStore. Protected/restricted keys are skipped.
    """
    config = getattr(request.app.state, "config", None)
    if config is None:
        return RedirectResponse(url=f"/config/{tab}", status_code=303)

    form = await request.form()
    saved_any = False

    for field_name, value in form.multi_items():
        if "." not in field_name:
            continue  # Skip non-config fields
        section, keyword = field_name.split(".", 1)
        str_value = str(value)
        try:
            await config.set(section, keyword, str_value)
            saved_any = True
            # Log config change, redacting sensitive values
            if keyword in ("api_key", "password", "email_password"):
                display_value = "***"
            else:
                display_value = str_value if len(str_value) < 100 else str_value[:97] + "..."
            logger.info("Config changed: %s.%s = %s", section, keyword, display_value)
        except ValueError as e:
            # Protected/restricted keywords are silently skipped
            logger.debug("Skipping protected config key %s.%s: %s", section, keyword, e)
            continue

    if saved_any:
        return RedirectResponse(url=f"/config/{tab}?saved=1", status_code=303)
    return RedirectResponse(url=f"/config/{tab}", status_code=303)


# ------------------------------------------------------------------ #
#  Login / Logout                                                      #
# ------------------------------------------------------------------ #


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    """Render the login page.

    If no credentials are configured, redirects to the home page
    (authentication is not required).
    """
    config = getattr(request.app.state, "config", None)
    if config is not None:
        username = await config.get("misc", "username")
        password = await config.get("misc", "password")
        # If no credentials are configured, skip login
        if not username and not password:
            return RedirectResponse(url="/", status_code=303)

    # If already logged in, redirect to home
    session_id = request.cookies.get("web_session")
    if session_id:
        session = await validate_web_session(session_id)
        if session:
            return RedirectResponse(url="/", status_code=303)

    next_url = request.query_params.get("next", "/")
    error = request.query_params.get("error", "")

    return templates.TemplateResponse(request, "login.html", {
        "next": next_url,
        "error": error,
        "last_username": "",
        "version": VERSION,
    })


@router.post("/login")
async def login_submit(request: Request) -> Response:
    """Process login form submission.

    Validates username/password against configured credentials.
    On success: creates a session and redirects to the next URL.
    On failure: redirects back to login with an error message.
    """
    config = getattr(request.app.state, "config", None)
    if config is None:
        return RedirectResponse(url="/login?error=Service+unavailable", status_code=303)

    # Rate limit check
    client_ip = request.client.host if request.client else "unknown"
    if _check_web_rate_limit(client_ip):
        logger.warning("Web auth: rate-limited login attempt from %s", client_ip)
        return RedirectResponse(url="/login?error=Too+many+failed+attempts.+Try+again+later.", status_code=303)

    # Parse form data
    form = await request.form()
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    next_url = str(form.get("next", "/"))

    # Get configured credentials
    configured_username = await config.get("misc", "username")
    configured_password = await config.get("misc", "password")

    # If no credentials are configured, allow access
    if not configured_username and not configured_password:
        return RedirectResponse(url=next_url, status_code=303)

    # Validate credentials with constant-time comparison
    username_ok = hmac.compare_digest(username, configured_username)
    password_ok = hmac.compare_digest(password, configured_password)

    if username_ok and password_ok:
        # Create session
        session_id = await create_web_session(username)

        # Determine if Secure flag should be set
        https_enabled = await config.get_bool("misc", "https_enabled", False)

        # If setup is not complete, force redirect to setup wizard
        setup_complete = await config.get_bool("misc", "setup_complete", True)
        if not setup_complete:
            next_url = "/setup"

        response = RedirectResponse(url=next_url, status_code=303)
        response.set_cookie(
            "web_session",
            session_id,
            max_age=28800,  # 8 hours, matches WEB_SESSION_TIMEOUT
            path="/",
            httponly=True,
            samesite="lax",
            secure=https_enabled,
        )
        return response

    # Failed login
    _record_web_failure(client_ip)
    logger.warning("Web auth: failed login attempt from %s for user '%s'", client_ip, username)

    # Re-render login page with error and last username
    return templates.TemplateResponse(request, "login.html", {
        "next": next_url,
        "error": "Invalid username or password.",
        "last_username": username,
        "version": VERSION,
    })


@router.api_route("/logout", methods=["GET", "POST"])
async def logout(request: Request) -> Response:
    """Destroy the current web session and redirect to login."""
    session_id = request.cookies.get("web_session")
    if session_id:
        await destroy_web_session(session_id)

    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("web_session", path="/")
    return response


# ------------------------------------------------------------------ #
#  Setup wizard                                                       #
# ------------------------------------------------------------------ #


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request) -> HTMLResponse:
    """Render the setup wizard page.

    This page is shown to users who logged in with temporary credentials.
    They must set permanent credentials before they can use the application.
    If setup is already complete, redirect to home.
    """
    config = getattr(request.app.state, "config", None)

    # If setup is already complete, redirect to home
    if config is not None:
        setup_complete = await config.get_bool("misc", "setup_complete", True)
        if setup_complete:
            return RedirectResponse(url="/", status_code=303)

    # Read current trusted networks value for pre-fill
    trusted_networks = ""
    temp_credentials = False
    if config is not None:
        trusted_networks = await config.get("misc", "trusted_networks", "")
        temp_credentials = await config.get_bool("misc", "temp_credentials", False)

    error = request.query_params.get("error", "")

    return templates.TemplateResponse(request, "setup.html", {
        "version": VERSION,
        "error": error,
        "trusted_networks": trusted_networks,
        "temp_credentials": temp_credentials,
    })


@router.post("/setup")
async def setup_submit(request: Request) -> Response:
    """Process the setup wizard form submission.

    Validates and saves permanent credentials, then marks setup as complete.
    Creates a new session with the new credentials and destroys the old one.
    """
    config = getattr(request.app.state, "config", None)
    if config is None:
        return RedirectResponse(url="/setup?error=Service+unavailable", status_code=303)

    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    password_confirm = str(form.get("password_confirm", ""))
    trusted_networks = str(form.get("trusted_networks", "")).strip()

    # Validate
    if len(username) < 3:
        return RedirectResponse(
            url="/setup?error=Username+must+be+at+least+3+characters", status_code=303
        )
    if len(password) < 6:
        return RedirectResponse(
            url="/setup?error=Password+must+be+at+least+6+characters", status_code=303
        )
    if password != password_confirm:
        return RedirectResponse(
            url="/setup?error=Passwords+do+not+match", status_code=303
        )

    try:
        await config.set_web_credentials(
            username=username,
            password=password,
            trusted_networks=trusted_networks,
        )
        logger.info("Setup wizard: credentials set for user '%s'", username)
    except ValueError as e:
        return RedirectResponse(
            url=f"/setup?error={str(e)}", status_code=303
        )

    # Destroy the old session (logged in with temp creds) and create a new one
    old_session_id = request.cookies.get("web_session")
    if old_session_id:
        await destroy_web_session(old_session_id)

    new_session_id = await create_web_session(username)

    # Determine if Secure flag should be set
    https_enabled = await config.get_bool("misc", "https_enabled", False)

    response = RedirectResponse(url="/?saved=1", status_code=303)
    response.set_cookie(
        "web_session",
        new_session_id,
        max_age=28800,
        path="/",
        httponly=True,
        samesite="lax",
        secure=https_enabled,
    )
    return response


# ------------------------------------------------------------------ #
#  Home page                                                          #
# ------------------------------------------------------------------ #

@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Home page — download queue with summary stats and add-NZB form.

    When the ``show_completed`` query parameter is present, also includes
    completed and failed items from the history table.
    """
    config = getattr(request.app.state, "config", None)
    db = getattr(request.app.state, "db", None)

    api_key = ""
    torbox_connected = False
    queue_items = []
    completed_items = []
    categories = []
    show_completed = bool(request.query_params.get("show_completed"))

    if config is not None:
        try:
            api_key = await config.get("misc", "api_key")
        except Exception:
            pass
        torbox_api_key = await config.get("torbox", "api_key")
        torbox_connected = bool(torbox_api_key)

    if db and db.conn:
        # Read queue items with torbox_state and stall tracking
        cursor = await db.conn.execute(
            "SELECT nzo_id, filename, status, category, priority, percentage, "
            "size, sizeleft, speed, download_time, torbox_state, stalled_since "
            "FROM jobs ORDER BY position"
        )
        rows = await cursor.fetchall()
        total_speed = 0
        total_sizeleft = 0
        now = time.time()
        for row in rows:
            status = row[2] or "Queued"
            stalled_since = row[11] or 0
            stalled = stalled_since > 0
            # If stalled, override display status
            display_status = "Stalled" if stalled else status
            status_label = (
                "stalled" if stalled else
                "downloading" if status == "Downloading" else
                "paused" if status == "Paused" else
                "fetching" if status == "Fetching" else
                "complete" if status == "Complete" else
                "failed" if status == "Failed" else
                "queued"
            )
            priority_label = {-100: "Paused", 1: "Low", 0: "Normal", 2: "High"}.get(row[4], "Normal")
            item_speed = row[8] or 0
            item_sizeleft = row[7] or 0
            total_speed += item_speed
            total_sizeleft += item_sizeleft
            # Compute stall duration string
            if stalled and stalled_since > 0:
                elapsed = now - stalled_since
                minutes = int(elapsed) // 60
                seconds = int(elapsed) % 60
                stall_duration = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"
            else:
                stall_duration = ""
            queue_items.append({
                "nzo_id": row[0], "filename": row[1], "status": display_status,
                "status_label": status_label, "cat": row[3] or "*",
                "priority": row[4], "priority_label": priority_label,
                "percentage": str(int(row[5] or 0)),
                "size": _format_size(row[6] or 0),
                "speed": _format_size(item_speed) + "/s" if item_speed and not stalled else ("Stalled " + stall_duration if stalled else "—"),
                "timeleft": "—",
                "torbox_state": row[10] or "",
                "stalled": stalled,
                "stall_duration": stall_duration,
            })

        # Read completed/failed items from history when toggled on
        if show_completed:
            cursor = await db.conn.execute(
                "SELECT nzo_id, name, status, size, category, download_time, "
                "completed, storage, fail_message, path, torbox_id, torbox_type "
                "FROM history ORDER BY completed DESC LIMIT 200"
            )
            rows = await cursor.fetchall()
            for row in rows:
                status = row[2] or "Completed"
                status_label = (
                    "complete" if status == "Completed" else
                    "failed" if status == "Failed" else
                    "downloading"
                )
                completed_items.append({
                    "nzo_id": row[0], "name": row[1], "status": status,
                    "status_label": status_label,
                    "size": _format_size(row[3] or 0),
                    "category": row[4] or "*",
                    "download_time": _format_duration(row[5] or 0),
                    "completed": _format_timestamp(row[6]) if row[6] else "—",
                    "storage": row[7] or "",
                    "path": row[9] or "",
                    "fail_message": row[8] or "",
                })

        # Read categories for the add form
        cursor = await db.conn.execute("SELECT name FROM categories ORDER BY order_index")
        categories = [r[0] for r in await cursor.fetchall()]

    # Calculate stats from queue items
    size_remaining = _format_size(total_sizeleft) if total_sizeleft else "0 B"
    speed_str = _format_size(total_speed) + "/s" if total_speed else "0 B/s"

    ctx = await _base_context(request, page="home",
        api_key=api_key, torbox_connected=torbox_connected,
        queue_count=len(queue_items), queue_items=queue_items,
        completed_items=completed_items, show_completed=show_completed,
        queue_paused=False, speed=speed_str,
        size_remaining=size_remaining, time_left="0:00:00",
        categories=categories)
    return templates.TemplateResponse(request, "index.html", ctx)


# ------------------------------------------------------------------ #
#  History page                                                       #
# ------------------------------------------------------------------ #

@router.get("/history", response_class=HTMLResponse)
async def history_page(request: Request) -> HTMLResponse:
    """History page — completed and failed downloads."""
    db = getattr(request.app.state, "db", None)

    history_items = []
    history_stats = {"today_completed": 0, "failed": 0, "total_downloaded": "0 B"}
    categories = []

    if db and db.conn:
        cursor = await db.conn.execute(
            "SELECT nzo_id, name, status, size, category, download_time, "
            "completed, storage, fail_message, path "
            "FROM history ORDER BY completed DESC LIMIT 200"
        )
        rows = await cursor.fetchall()
        for row in rows:
            status = row[2] or "Completed"
            history_items.append({
                "nzo_id": row[0], "name": row[1], "status": status,
                "size": _format_size(row[3] or 0), "category": row[4] or "*",
                "download_time": _format_duration(row[5] or 0),
                "completed": _format_timestamp(row[6]) if row[6] else "—",
                "storage": row[7] or "",
                "path": row[9] or "",
                "fail_message": row[8] or "",
            })

        # Stats
        cursor = await db.conn.execute(
            "SELECT COUNT(*) FROM history WHERE status='Completed'"
        )
        completed_row = await cursor.fetchone()
        history_stats["today_completed"] = completed_row[0] if completed_row else 0

        cursor = await db.conn.execute(
            "SELECT COUNT(*) FROM history WHERE status='Failed'"
        )
        failed_row = await cursor.fetchone()
        history_stats["failed"] = failed_row[0] if failed_row else 0

        cursor = await db.conn.execute("SELECT COALESCE(SUM(size), 0) FROM history")
        total_row = await cursor.fetchone()
        history_stats["total_downloaded"] = _format_size(total_row[0]) if total_row else "0 B"

        # Categories for filter
        cursor = await db.conn.execute("SELECT name FROM categories ORDER BY order_index")
        categories = [r[0] for r in await cursor.fetchall()]

    ctx = await _base_context(request, page="history",
        history_items=history_items, history_stats=history_stats,
        categories=categories)
    return templates.TemplateResponse(request, "history.html", ctx)


def _format_duration(seconds: float) -> str:
    """Format seconds as 'H:MM:SS'."""
    from debridnzbd.utils.format import format_timeleft
    return format_timeleft(seconds)


def _format_timestamp(ts: float) -> str:
    """Format a Unix timestamp as 'YYYY-MM-DD HH:MM'."""
    return format_timestamp(ts)


def _format_torbox_date(date_str: str) -> str:
    """Format a Torbox ISO date string as 'YYYY-MM-DD HH:MM'.

    Torbox returns created_at as ISO 8601 (e.g. '2024-01-15T12:30:00'
    or '2024-01-15T12:30:00.000Z'). This parser handles both formats.
    """
    if not date_str:
        return "—"
    try:
        # Strip trailing Z and try common ISO formats
        clean = date_str.rstrip("Z")
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(clean, fmt)
                return dt.strftime("%Y-%m-%d %H:%M")
            except ValueError:
                continue
        # Fallback: just return the first 16 chars (YYYY-MM-DDTHH:MM)
        return date_str[:16] if len(date_str) >= 16 else date_str
    except Exception:
        return date_str


# ------------------------------------------------------------------ #
#  Provider page                                                      #
# ------------------------------------------------------------------ #

@router.get("/provider", response_class=HTMLResponse)
async def provider_page(request: Request) -> HTMLResponse:
    """Provider page — all downloads on the user's Torbox account.

    Queries Torbox directly for usenet, torrent, webdl, and queued
    downloads and displays them in a unified table. Users can delete
    entries, download them locally, and assign categories.
    """
    config = getattr(request.app.state, "config", None)
    db = getattr(request.app.state, "db", None)

    provider_items = []
    provider_stats = {
        "total": 0, "usenet_count": 0, "torrent_count": 0,
        "webdl_count": 0, "queued_count": 0, "total_size": "0 B",
    }
    categories = ["*"]
    torbox_connected = False
    error = None
    total_bytes = 0

    # Build a lookup of (torbox_id, torbox_type) → (category, local_status)
    # from local jobs and history tables so we can auto-populate categories
    # and show tracked/completed status.
    local_lookup: dict[tuple[str, str], tuple[str, str]] = {}
    if db and db.conn:
        try:
            cursor = await db.conn.execute(
                "SELECT torbox_id, torbox_type, category, status FROM jobs "
                "WHERE torbox_id IS NOT NULL AND torbox_type IS NOT NULL"
            )
            for row in await cursor.fetchall():
                local_lookup[(str(row[0]), row[1])] = (row[2] or "*", row[3] or "Queued")

            cursor = await db.conn.execute(
                "SELECT torbox_id, torbox_type, category, status FROM history "
                "WHERE torbox_id IS NOT NULL AND torbox_type IS NOT NULL"
            )
            for row in await cursor.fetchall():
                key = (str(row[0]), row[1])
                if key not in local_lookup:
                    local_lookup[key] = (row[2] or "*", row[3] or "Complete")

            cursor = await db.conn.execute(
                "SELECT name FROM categories ORDER BY order_index"
            )
            categories = [r[0] for r in await cursor.fetchall()] or ["*"]
        except Exception:
            logger.exception("Provider page: failed to query local DB")

    if config:
        api_key = await config.get("torbox", "api_key")
        base_url = await config.get("torbox", "base_url", "https://api.torbox.app/v1")
        torbox_connected = bool(api_key)

        if torbox_connected:
            client = TorboxClient(api_key=api_key, base_url=base_url)
            try:
                # Fetch all four download types from Torbox
                usenet_list = await client.get_usenet_list(bypass_cache=True)
                torrent_list = await client.get_torrent_list(bypass_cache=True)
                webdl_list = await client.get_web_download_list(bypass_cache=True)
                queued_list = await client.get_queued_downloads()

                # Normalize usenet downloads
                for dl in usenet_list:
                    name = _extract_download_name(dl, "usenet") or f"Usenet #{dl.id}"
                    status = dl.status or ("cached" if dl.progress >= 1.0 else "queued")
                    torbox_key = (str(dl.id), "usenet")
                    local_cat, local_st = local_lookup.get(torbox_key, (None, None))
                    provider_items.append({
                        "id": dl.id,
                        "type": "usenet",
                        "name": name,
                        "status": status,
                        "status_label": _map_torbox_status(status, dl.progress),
                        "progress": dl.progress,
                        "progress_pct": str(int(dl.progress * 100)),
                        "size": _format_size(dl.size or 0),
                        "created_at": _format_torbox_date(dl.created_at),
                        "hash": dl.hash,
                        "local_category": local_cat,
                        "local_status": local_st,
                    })
                    total_bytes += dl.size or 0
                    provider_stats["usenet_count"] += 1

                # Normalize torrent downloads
                for dl in torrent_list:
                    name = dl.name or f"Torrent #{dl.id}"
                    status = dl.status or ("cached" if dl.progress >= 1.0 else "queued")
                    torbox_key = (str(dl.id), "torrent")
                    local_cat, local_st = local_lookup.get(torbox_key, (None, None))
                    provider_items.append({
                        "id": dl.id,
                        "type": "torrent",
                        "name": name,
                        "status": status,
                        "status_label": _map_torbox_status(status, dl.progress),
                        "progress": dl.progress,
                        "progress_pct": str(int(dl.progress * 100)),
                        "size": _format_size(dl.size or 0),
                        "created_at": _format_torbox_date(dl.created_at),
                        "hash": dl.hash,
                        "local_category": local_cat,
                        "local_status": local_st,
                    })
                    total_bytes += dl.size or 0
                    provider_stats["torrent_count"] += 1

                # Normalize web downloads
                for dl in webdl_list:
                    name = _extract_download_name(dl, "webdl") or f"WebDL #{dl.id}"
                    status = dl.status or ("cached" if dl.progress >= 1.0 else "queued")
                    torbox_key = (str(dl.id), "webdl")
                    local_cat, local_st = local_lookup.get(torbox_key, (None, None))
                    provider_items.append({
                        "id": dl.id,
                        "type": "webdl",
                        "name": name,
                        "status": status,
                        "status_label": _map_torbox_status(status, dl.progress),
                        "progress": dl.progress,
                        "progress_pct": str(int(dl.progress * 100)),
                        "size": _format_size(dl.size or 0),
                        "created_at": _format_torbox_date(dl.created_at),
                        "hash": dl.hash,
                        "local_category": local_cat,
                        "local_status": local_st,
                    })
                    total_bytes += dl.size or 0
                    provider_stats["webdl_count"] += 1

                # Normalize queued downloads
                for dl in queued_list:
                    dl_type = dl.type or "queued"
                    torbox_key = (str(dl.id), "queued")
                    local_cat, local_st = local_lookup.get(torbox_key, (None, None))
                    provider_items.append({
                        "id": dl.id,
                        "type": "queued",
                        "name": f"{dl_type.title()} #{dl.id}",
                        "status": "queued",
                        "status_label": "Queued",
                        "progress": 0,
                        "progress_pct": "0",
                        "size": "—",
                        "created_at": _format_torbox_date(dl.created_at),
                        "hash": dl.hash,
                        "local_category": local_cat,
                        "local_status": local_st,
                    })
                    provider_stats["queued_count"] += 1

                provider_stats["total"] = len(provider_items)
                provider_stats["total_size"] = _format_size(total_bytes)

            except (TorboxAuthError, TorboxConnectionError, TorboxRateLimitError, TorboxError) as e:
                error = f"Torbox API error: {e}"
                logger.warning("Provider page: failed to fetch Torbox data: %s", e)
            except Exception:
                error = "Failed to connect to Torbox API"
                logger.exception("Provider page: unexpected error")
            finally:
                await client.close()

    ctx = await _base_context(request, page="provider",
        provider_items=provider_items, provider_stats=provider_stats,
        torbox_connected=torbox_connected, error=error,
        categories=categories)
    return templates.TemplateResponse(request, "provider.html", ctx)


@router.post("/provider/delete")
async def provider_delete(request: Request) -> JSONResponse:
    """Delete one or more Torbox downloads and clean up local DB entries.

    Accepts a JSON body: ``{"items": ["usenet:123", "torrent:456", ...]}``
    Each item is ``type:torbox_id`` where type is usenet, torrent, webdl,
    or queued.

    For each item:
    1. Call the appropriate Torbox control API to delete it
    2. Delete matching entries from local jobs and history tables by torbox_id

    Returns JSON: ``{"status": true, "deleted_count": N, "errors": [...]}``
    """
    config = getattr(request.app.state, "config", None)
    db = getattr(request.app.state, "db", None)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"status": False, "error": "Invalid JSON body"},
        )

    items = body.get("items", [])
    if not items:
        return JSONResponse(
            content={"status": True, "deleted_count": 0, "errors": []},
        )

    api_key = ""
    base_url = "https://api.torbox.app/v1"
    if config:
        api_key = await config.get("torbox", "api_key")
        base_url = await config.get("torbox", "base_url", "https://api.torbox.app/v1")

    deleted_count = 0
    errors = []

    # Delete from Torbox
    if api_key:
        client = TorboxClient(api_key=api_key, base_url=base_url)
        try:
            for item in items:
                try:
                    parts = item.split(":", 1)
                    if len(parts) != 2:
                        errors.append(f"Invalid item format: {item}")
                        continue
                    dl_type, dl_id_str = parts
                    dl_id = int(dl_id_str)

                    if dl_type == "usenet":
                        result = await client.control_usenet_download(dl_id, "Delete")
                    elif dl_type == "torrent":
                        result = await client.control_torrent(dl_id, "Delete")
                    elif dl_type == "webdl":
                        result = await client.control_web_download(dl_id, "Delete")
                    elif dl_type == "queued":
                        result = await client.control_queued_download(dl_id, "Delete")
                    else:
                        errors.append(f"Unknown type: {dl_type}")
                        continue

                    if result.success:
                        deleted_count += 1
                        logger.info("Provider delete: deleted %s:%s from Torbox", dl_type, dl_id_str)
                    else:
                        detail = result.detail or "Unknown error"
                        # Treat "not found" (already deleted on Torbox side) as success
                        # — the item is gone from Torbox regardless.
                        if "not found" in detail.lower() or "does not exist" in detail.lower():
                            deleted_count += 1
                            logger.info("Provider delete: %s:%s already gone from Torbox (%s)", dl_type, dl_id_str, detail)
                        else:
                            errors.append(f"Failed to delete {item}: {detail}")
                            logger.warning("Provider delete: Torbox rejected delete for %s: %s", item, detail)
                except (TorboxError, TorboxAuthError, TorboxConnectionError, TorboxRateLimitError) as e:
                    errors.append(f"Failed to delete {item}: {e}")
                    logger.warning("Provider delete: Torbox error for %s: %s", item, e)
                except ValueError:
                    errors.append(f"Invalid ID: {dl_id_str}")
                except Exception:
                    errors.append(f"Error deleting {item}")
                    logger.exception("Provider delete: unexpected error for %s", item)
        finally:
            await client.close()
    else:
        errors.append("No Torbox API key configured")

    # Clean up local DB entries — delete from jobs and history by torbox_id
    if db and db.conn:
        db_deleted = 0
        for item in items:
            try:
                parts = item.split(":", 1)
                if len(parts) != 2:
                    continue
                dl_type, dl_id_str = parts
                cursor = await db.conn.execute(
                    "DELETE FROM jobs WHERE torbox_id = ? AND torbox_type = ?",
                    (dl_id_str, dl_type),
                )
                db_deleted += cursor.rowcount
                cursor = await db.conn.execute(
                    "DELETE FROM history WHERE torbox_id = ? AND torbox_type = ?",
                    (dl_id_str, dl_type),
                )
                db_deleted += cursor.rowcount
            except Exception:
                logger.exception("Provider delete: failed to clean up local DB for %s", item)
        await db.conn.commit()
        if db_deleted > 0:
            logger.info("Provider delete: removed %d local DB record(s)", db_deleted)

    return JSONResponse(content={
        "status": True,
        "deleted_count": deleted_count,
        "errors": errors,
    })


@router.post("/provider/download")
async def provider_download(request: Request) -> JSONResponse:
    """Create local jobs for one or more Torbox downloads.

    Accepts a JSON body:
    ``{"items": [{"id": "usenet:123", "category": "tv"}, ...]}``

    Each item is ``type:torbox_id`` with an optional category.
    For items that are already completed/cached on Torbox, the CDN
    link is requested immediately and the job is moved to history.
    Items already tracked locally (in jobs or history) are skipped.
    """
    config = getattr(request.app.state, "config", None)
    db = getattr(request.app.state, "db", None)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"status": False, "error": "Invalid JSON body"},
        )

    items = body.get("items", [])
    if not items:
        return JSONResponse(
            content={"status": True, "downloaded_count": 0, "skipped_count": 0, "errors": []},
        )

    if not db or not db.conn:
        return JSONResponse(
            status_code=500,
            content={"status": False, "error": "Database not available"},
        )

    api_key = ""
    base_url = "https://api.torbox.app/v1"
    if config:
        api_key = await config.get("torbox", "api_key")
        base_url = await config.get("torbox", "base_url", "https://api.torbox.app/v1")

    downloaded_count = 0
    skipped_count = 0
    errors = []

    # Build lookup of already-tracked torbox_ids from local DB
    tracked_jobs: set[tuple[str, str]] = set()
    tracked_history: set[tuple[str, str]] = set()
    try:
        cursor = await db.conn.execute(
            "SELECT torbox_id, torbox_type FROM jobs "
            "WHERE torbox_id IS NOT NULL AND torbox_type IS NOT NULL"
        )
        for row in await cursor.fetchall():
            tracked_jobs.add((str(row[0]), row[1]))

        cursor = await db.conn.execute(
            "SELECT torbox_id, torbox_type FROM history "
            "WHERE torbox_id IS NOT NULL AND torbox_type IS NOT NULL"
        )
        for row in await cursor.fetchall():
            tracked_history.add((str(row[0]), row[1]))
    except Exception:
        logger.exception("Provider download: failed to query local DB")
        return JSONResponse(
            status_code=500,
            content={"status": False, "error": "Database query failed"},
        )

    # Status values that mean the download is complete/cached on Torbox
    COMPLETED_STATUSES = {"completed", "cached", "seeding"}

    # Create a shared semaphore for CDN downloads across all items in this request
    cdn_semaphore = asyncio.Semaphore(max(1, int(await config.get("torbox", "cdn_download_concurrency", "2")) if config else 2))

    for item in items:
        item_id = item.get("id", "") if isinstance(item, dict) else str(item)
        category = (item.get("category", "*") if isinstance(item, dict) else "*") or "*"

        try:
            parts = item_id.split(":", 1)
            if len(parts) != 2:
                errors.append(f"Invalid item format: {item_id}")
                continue
            dl_type, dl_id_str = parts
            dl_id_int = int(dl_id_str)
        except (ValueError, AttributeError):
            errors.append(f"Invalid item: {item_id}")
            continue

        torbox_key = (dl_id_str, dl_type)

        # Skip if already in active jobs — it's being processed by state sync
        if torbox_key in tracked_jobs:
            skipped_count += 1
            logger.info("Provider download: skipping %s — already in active jobs", item_id)
            continue

        # If already in history, remove the old entry so we can re-download
        # with a fresh CDN link and updated category.
        if torbox_key in tracked_history:
            try:
                await db.conn.execute(
                    "DELETE FROM history WHERE torbox_id = ? AND torbox_type = ?",
                    (dl_id_str, dl_type),
                )
                await db.conn.commit()
                logger.info("Provider download: removed old history entry for %s to re-download", item_id)
            except Exception:
                errors.append(f"Failed to remove old history for {item_id}")
                logger.exception("Provider download: failed to remove history for %s", item_id)
                continue
            tracked_history.discard(torbox_key)

        # Fetch download details from Torbox to get name/status/size
        if not api_key:
            errors.append(f"No Torbox API key — cannot fetch details for {item_id}")
            continue

        client = TorboxClient(api_key=api_key, base_url=base_url)
        try:
            download_name = f"{dl_type.title()} #{dl_id_str}"
            download_status = "queued"
            download_size = 0

            if dl_type == "usenet":
                result = await client.get_usenet_list(bypass_cache=True)
                dl_obj = next((d for d in result if d.id == dl_id_int), None)
                if dl_obj:
                    download_name = _extract_download_name(dl_obj, "usenet") or download_name
                    download_status = dl_obj.status or ("cached" if dl_obj.progress >= 1.0 else "queued")
                    download_size = dl_obj.size or 0
            elif dl_type == "torrent":
                result = await client.get_torrent_list(bypass_cache=True)
                dl_obj = next((d for d in result if d.id == dl_id_int), None)
                if dl_obj:
                    download_name = dl_obj.name or download_name
                    download_status = dl_obj.status or ("cached" if dl_obj.progress >= 1.0 else "queued")
                    download_size = dl_obj.size or 0
            elif dl_type == "webdl":
                result = await client.get_web_download_list(bypass_cache=True)
                dl_obj = next((d for d in result if d.id == dl_id_int), None)
                if dl_obj:
                    download_name = _extract_download_name(dl_obj, "webdl") or download_name
                    download_status = dl_obj.status or ("cached" if dl_obj.progress >= 1.0 else "queued")
                    download_size = dl_obj.size or 0
            elif dl_type == "queued":
                download_name = f"Queued #{dl_id_str}"
                download_status = "queued"
            else:
                errors.append(f"Unknown type: {dl_type}")
                continue
        except (TorboxAuthError, TorboxConnectionError, TorboxRateLimitError, TorboxError) as e:
            errors.append(f"Failed to fetch details for {item_id}: {e}")
            logger.warning("Provider download: Torbox error for %s: %s", item_id, e)
            continue
        except Exception:
            errors.append(f"Failed to fetch details for {item_id}")
            logger.exception("Provider download: unexpected error for %s", item_id)
            continue
        finally:
            await client.close()

        # Create local job
        now = time.time()
        nzo_id = generate_nzo_id()

        try:
            # Get next position
            cursor = await db.conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM jobs"
            )
            row = await cursor.fetchone()
            position = row[0] if row else 0

            # Determine initial status
            is_completed = download_status.lower() in COMPLETED_STATUSES

            # Map torbox status to a SABnzbd-compatible local status
            local_status = _map_torbox_status(download_status, 1.0 if is_completed else 0.0)
            if local_status == "Complete" or is_completed:
                local_status = "Downloading"  # Will be completed shortly

            await db.conn.execute(
                """INSERT INTO jobs (
                    nzo_id, filename, password, nzo_url, category, script, priority, pp,
                    status, size, sizeleft, percentage, time_added, avg_age,
                    torbox_id, torbox_type, torbox_hash, position, torbox_state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    nzo_id,
                    download_name,
                    "",     # password
                    "",     # nzo_url — no original URL for Provider-initiated downloads
                    category,
                    "Default",  # script
                    0,      # priority
                    -1,     # pp (default)
                    local_status,
                    download_size,
                    0 if is_completed else download_size,  # sizeleft
                    100 if is_completed else 0,              # percentage
                    now,
                    "",     # avg_age
                    dl_id_str,
                    dl_type,
                    "",     # torbox_hash — not available from Provider page
                    position,
                    download_status,
                ),
            )
            await db.conn.commit()
            logger.info(
                "Provider download: created job %s for %s:%s (status=%s, category=%s)",
                nzo_id, dl_type, dl_id_str, local_status, category,
            )
        except Exception:
            errors.append(f"Failed to create local job for {item_id}")
            logger.exception("Provider download: failed to insert job for %s", item_id)
            continue

        # Mark as tracked so we don't double-create
        tracked_jobs.add(torbox_key)

        # If completed/cached on Torbox, request CDN link and move to history
        if is_completed and api_key:
            client = TorboxClient(api_key=api_key, base_url=base_url)
            try:
                cdn_link = ""
                try:
                    if dl_type == "usenet":
                        cdn_link = await client.request_usenet_dl(usenet_id=dl_id_int)
                    elif dl_type == "torrent":
                        cdn_link = await client.request_torrent_dl(torrent_id=dl_id_int)
                    elif dl_type == "webdl":
                        cdn_link = await client.request_web_dl(web_id=dl_id_int)
                except (TorboxError, TorboxConnectionError, TorboxAuthError) as e:
                    logger.warning(
                        "Provider download: failed to get CDN link for %s (type=%s): %s",
                        nzo_id, dl_type, e,
                    )
                    # Job stays in queue — state sync poller will retry

                # Download the file from CDN to the incomplete directory,
                # then move to the category-specific complete directory
                local_path: str | None = None
                if cdn_link:
                    logger.info("Provider download: got CDN link for %s (type=%s)", nzo_id, dl_type)
                    download_dir = await config.get("folders", "download_dir", "downloads/incomplete")
                    try:
                        local_path = await cdn_download_file(
                            url=cdn_link,
                            dest_dir=download_dir,
                            semaphore=cdn_semaphore,
                        )
                        if local_path:
                            # Move from incomplete to category-specific complete directory
                            final_path = await cdn_move_to_category(local_path, category, config)
                            if final_path:
                                local_path = final_path
                                logger.info("Provider download: moved file for %s to %s", nzo_id, local_path)
                            else:
                                logger.warning("Provider download: failed to move file for %s — keeping at %s", nzo_id, local_path)
                        else:
                            logger.warning("Provider download: CDN download returned no path for %s — CDN link stored as fallback", nzo_id)
                    except Exception:
                        logger.exception("Provider download: CDN download failed for %s — CDN link stored as fallback", nzo_id)
                        local_path = None

                # Mark complete and move to history
                await db.conn.execute(
                    "UPDATE jobs SET status = ?, cdn_link = ?, local_path = ?, percentage = 100, "
                    "sizeleft = 0, time_completed = ? WHERE nzo_id = ?",
                    ("Complete", cdn_link, local_path or "", now, nzo_id),
                )
                await db.conn.commit()

                # Move to history
                cursor = await db.conn.execute(
                    "SELECT nzo_id, filename, status, size, category, "
                    "time_added, download_time, cdn_link, torbox_id, torbox_type, "
                    "fail_message, nzo_url, local_path FROM jobs WHERE nzo_id = ?",
                    (nzo_id,),
                )
                row = await cursor.fetchone()
                if row:
                    await db.conn.execute(
                        """INSERT OR IGNORE INTO history
                        (nzo_id, name, status, size, category, download_time,
                         completed, time_added, storage, torbox_id, torbox_type,
                         fail_message, nzo_url, path)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            row[0], row[1], row[2], row[3], row[4],
                            row[6] or 0, now, row[5],
                            row[7] or row[11] or "",  # storage: cdn_link or nzo_url
                            row[8], row[9], row[10] or "", row[11] or "",
                            row[12] or "",  # path: local_path
                        ),
                    )
                    await db.conn.execute("DELETE FROM jobs WHERE nzo_id = ?", (nzo_id,))
                    await db.conn.commit()
                    logger.info("Provider download: completed %s — moved to history", nzo_id)

                # Update tracking: moved from jobs to history
                tracked_jobs.discard(torbox_key)
                tracked_history.add(torbox_key)
            except Exception:
                logger.exception(
                    "Provider download: error completing job %s — "
                    "state sync poller will retry", nzo_id,
                )
            finally:
                await client.close()

        downloaded_count += 1

    return JSONResponse(content={
        "status": True,
        "downloaded_count": downloaded_count,
        "skipped_count": skipped_count,
        "errors": errors,
    })


@router.post("/provider/set_category")
async def provider_set_category(request: Request) -> JSONResponse:
    """Set the category for one or more Torbox downloads in the local database.

    Accepts a JSON body:
    ``{"items": ["usenet:123", "torrent:456"], "category": "tv"}``

    Updates the category column in the local ``jobs`` and ``history`` tables
    for any items that are tracked locally. Items not tracked locally are
    silently skipped — their category will be set when they are downloaded.
    """
    db = getattr(request.app.state, "db", None)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"status": False, "error": "Invalid JSON body"},
        )

    items = body.get("items", [])
    category = body.get("category") or "*"

    if not items:
        return JSONResponse(
            content={"status": True, "updated_count": 0, "errors": []},
        )

    if not db or not db.conn:
        return JSONResponse(
            status_code=500,
            content={"status": False, "error": "Database not available"},
        )

    updated_count = 0
    errors = []

    for item in items:
        try:
            parts = item.split(":", 1)
            if len(parts) != 2:
                errors.append(f"Invalid item format: {item}")
                continue
            dl_type, dl_id_str = parts

            # Update in jobs table
            cursor = await db.conn.execute(
                "UPDATE jobs SET category = ? WHERE torbox_id = ? AND torbox_type = ?",
                (category, dl_id_str, dl_type),
            )
            if cursor.rowcount > 0:
                updated_count += 1

            # Update in history table
            cursor = await db.conn.execute(
                "UPDATE history SET category = ? WHERE torbox_id = ? AND torbox_type = ?",
                (category, dl_id_str, dl_type),
            )
            if cursor.rowcount > 0:
                updated_count += 1

        except Exception:
            errors.append(f"Error updating category for {item}")
            logger.exception("Provider set_category: failed for %s", item)

    await db.conn.commit()
    if updated_count > 0:
        logger.info("Provider set_category: set category=%s for %d record(s)", category, updated_count)

    return JSONResponse(content={
        "status": True,
        "updated_count": updated_count,
        "errors": errors,
    })


@router.get("/status", response_class=HTMLResponse)
async def status_page(request: Request) -> HTMLResponse:
    """Server status — connection, system info, disk space, warnings."""
    config = getattr(request.app.state, "config", None)
    db = getattr(request.app.state, "db", None)
    start_time = getattr(request.app.state, "start_time", time.time())

    # System info
    system_info = {
        "version": VERSION,
        "uptime": _format_uptime(start_time),
        "pid": os.getpid(),
    }
    try:
        loadavg = os.getloadavg()
        system_info["loadavg"] = f"{loadavg[0]:.2f} / {loadavg[1]:.2f} / {loadavg[2]:.2f}"
    except OSError:
        system_info["loadavg"] = "N/A"

    # Torbox connection
    torbox_connected = False
    if config:
        api_key = await config.get("torbox", "api_key")
        torbox_connected = bool(api_key)

    # Disk space
    disk_info = {"incomplete_path": "", "incomplete_free": "N/A",
                 "complete_path": "", "complete_free": "N/A"}
    if config:
        incomplete_dir = await config.get("folders", "download_dir", "downloads/incomplete")
        complete_dir = await config.get("folders", "complete_dir", "downloads/complete")
        disk_info["incomplete_path"] = incomplete_dir
        disk_info["complete_path"] = complete_dir
        for d, key in [(incomplete_dir, "incomplete_free"), (complete_dir, "complete_free")]:
            try:
                usage = shutil.disk_usage(d)
                disk_info[key] = f"{_format_size(usage.free)} free of {_format_size(usage.total)}"
            except (OSError, FileNotFoundError):
                disk_info[key] = "N/A"

    # Warnings
    warnings = []
    if db and db.conn:
        cursor = await db.conn.execute("SELECT text, type, time FROM warnings ORDER BY time DESC")
        for row in await cursor.fetchall():
            warnings.append({"text": row[0], "type": row[1], "time": row[2]})

    ctx = await _base_context(request, page="status",
        torbox_connected=torbox_connected, system_info=system_info,
        disk_info=disk_info, warnings=warnings)
    return templates.TemplateResponse(request, "status.html", ctx)


# ------------------------------------------------------------------ #
#  Config pages                                                       #
# ------------------------------------------------------------------ #

@router.get("/config", response_class=HTMLResponse)
async def config_redirect(request: Request) -> HTMLResponse:
    """Redirect /config to /config/general."""
    return RedirectResponse(url="/config/general", status_code=303)


@router.get("/config/general", response_class=HTMLResponse)
async def config_general(request: Request) -> HTMLResponse:
    config = getattr(request.app.state, "config", None)
    # The general config page needs the actual API key values for the
    # Show/Copy buttons (displayed behind type="password" inputs).
    # The password field always uses value="" to avoid sending the current
    # password to the browser.
    misc = await config.get_section("misc", redact_secrets=False) if config else {}
    ctx = await _base_context(request, page="config", active_tab="general",
        misc=misc)
    return templates.TemplateResponse(request, "config/general.html", ctx)


@router.post("/config/general", response_class=HTMLResponse)
async def config_general_save(request: Request) -> HTMLResponse:
    """Save general config, handling credential changes through set_web_credentials().

    Username and password are restricted keys that cannot be changed through
    the generic _save_config() path. This handler:
    1. Extracts username/password/trusted_networks from the form
    2. If username or password is non-empty, calls config.set_web_credentials()
    3. Delegates remaining fields to _save_config() (which silently skips
       restricted keys)
    """
    config = getattr(request.app.state, "config", None)
    if config is None:
        return RedirectResponse(url="/config/general", status_code=303)

    form = await request.form()
    new_username = str(form.get("misc.username", "")).strip()
    new_password = str(form.get("misc.password", "")).strip()
    trusted_networks = str(form.get("misc.trusted_networks", "")).strip()

    # Get current credentials to know if user is changing them
    current_username = await config.get("misc", "username")

    if new_username or new_password:
        # User is setting/changing credentials
        # If password is empty, keep current password (don't overwrite with blank)
        if not new_password:
            new_password = await config.get("misc", "password")
        # If username is empty, keep current username
        if not new_username:
            new_username = current_username

        try:
            await config.set_web_credentials(
                username=new_username,
                password=new_password,
                trusted_networks=trusted_networks,
            )
            logger.info("Config: web credentials updated for user '%s'", new_username)
        except ValueError as e:
            logger.warning("Config: failed to update credentials: %s", e)
            return RedirectResponse(
                url=f"/config/general?error={str(e)}", status_code=303
            )
    elif trusted_networks:
        # Only updating trusted networks, not credentials
        try:
            await config.set("misc", "trusted_networks", trusted_networks)
        except ValueError as e:
            return RedirectResponse(
                url=f"/config/general?error={str(e)}", status_code=303
            )

    # Save remaining (non-restricted) config fields
    return await _save_config(request, "general")


@router.get("/config/folders", response_class=HTMLResponse)
async def config_folders(request: Request) -> HTMLResponse:
    config = getattr(request.app.state, "config", None)
    folders = await config.get_section("folders", redact_secrets=False) if config else {}
    ctx = await _base_context(request, page="config", active_tab="folders", folders=folders)
    return templates.TemplateResponse(request, "config/folders.html", ctx)


@router.post("/config/folders", response_class=HTMLResponse)
async def config_folders_save(request: Request) -> HTMLResponse:
    return await _save_config(request, "folders")


@router.get("/config/torbox", response_class=HTMLResponse)
async def config_torbox(request: Request) -> HTMLResponse:
    config = getattr(request.app.state, "config", None)
    torbox = await config.get_section("torbox", redact_secrets=False) if config else {}
    torbox_connected = bool(torbox.get("api_key"))

    # Check for test result in query params
    test_result = request.query_params.get("test_result")
    ctx = await _base_context(request, page="config", active_tab="torbox",
        torbox=torbox, torbox_connected=torbox_connected, test_result=test_result)
    return templates.TemplateResponse(request, "config/torbox.html", ctx)


@router.post("/config/torbox", response_class=HTMLResponse)
async def config_torbox_save(request: Request) -> HTMLResponse:
    return await _save_config(request, "torbox")


@router.post("/config/torbox/test", response_class=HTMLResponse)
async def config_torbox_test(request: Request) -> HTMLResponse:
    """Test the Torbox connection using the submitted API key."""
    form = await request.form()
    api_key = str(form.get("torbox.api_key", ""))

    if not api_key:
        return RedirectResponse(url="/config/torbox?test_result=No+API+key+provided", status_code=303)

    try:
        logger.info("Torbox connection test requested")
        from debridnzbd.torbox.client import TorboxClient
        client = TorboxClient(api_key=api_key)
        result = await client.test_connection()
        if result:
            logger.info("Torbox connection test: Connected")
            return RedirectResponse(url="/config/torbox?test_result=Connected", status_code=303)
        else:
            logger.warning("Torbox connection test: failed")
            return RedirectResponse(url="/config/torbox?test_result=Connection+failed", status_code=303)
    except Exception as e:
        logger.warning("Torbox connection test failed: %s", e)
        return RedirectResponse(url=f"/config/torbox?test_result={str(e)[:80]}", status_code=303)


@router.get("/config/categories", response_class=HTMLResponse)
async def config_categories(request: Request) -> HTMLResponse:
    db = getattr(request.app.state, "db", None)
    config = getattr(request.app.state, "config", None)
    categories = []
    dir_full_paths: dict[str, str] = {}
    complete_dir = "downloads/complete"

    if config:
        complete_dir = await config.get("folders", "complete_dir", "downloads/complete")

    if db and db.conn:
        cursor = await db.conn.execute(
            "SELECT name, priority, pp, script, dir, newzbin FROM categories ORDER BY order_index"
        )
        for row in await cursor.fetchall():
            cat = {
                "name": row[0], "priority": row[1], "pp": row[2],
                "script": row[3], "dir": row[4], "newzbin": row[5],
            }
            categories.append(cat)
            # Compute the full resolved path for display/tooltips
            if cat["dir"]:
                dir_full_paths[cat["name"]] = str(Path(complete_dir) / cat["dir"])
            else:
                dir_full_paths[cat["name"]] = complete_dir

    ctx = await _base_context(request, page="config", active_tab="categories",
        categories=categories, dir_full_paths=dir_full_paths, complete_dir=complete_dir)
    return templates.TemplateResponse(request, "config/categories.html", ctx)


@router.post("/config/categories/add", response_class=HTMLResponse)
async def config_categories_add(request: Request) -> HTMLResponse:
    db = getattr(request.app.state, "db", None)
    if db and db.conn:
        form = await request.form()
        name = str(form.get("name", "new_category"))
        # Get max order_index
        cursor = await db.conn.execute("SELECT COALESCE(MAX(order_index), 0) + 1 FROM categories")
        row = await cursor.fetchone()
        order_index = row[0] if row else 0
        await db.conn.execute(
            "INSERT OR IGNORE INTO categories (name, priority, pp, script, dir, newzbin, order_index) "
            "VALUES (?, 0, -1, 'Default', '', '', ?)",
            (name, order_index),
        )
        await db.conn.commit()
        logger.info("Category added: %s", name)
    return RedirectResponse(url="/config/categories?saved=1", status_code=303)


@router.post("/config/categories/save", response_class=HTMLResponse)
async def config_categories_save(request: Request) -> HTMLResponse:
    db = getattr(request.app.state, "db", None)
    if db and db.conn:
        form = await request.form()
        name = str(form.get("name", ""))
        if name:
            priority = int(form.get("priority", 0))
            pp = int(form.get("pp", -1))
            script = str(form.get("script", ""))
            dir_val = str(form.get("dir", ""))
            # Validate dir_val for path traversal: reject paths that escape
            # the complete directory. Allow relative paths within complete_dir.
            if dir_val and ("/" in dir_val or "\\" in dir_val or dir_val.startswith(".")):
                logger.warning("Rejected category dir with path traversal: %s", dir_val)
                dir_val = ""  # Reset to empty (uses default complete_dir)
            newzbin = str(form.get("newzbin", ""))
            await db.conn.execute(
                "UPDATE categories SET priority=?, pp=?, script=?, dir=?, newzbin=? WHERE name=?",
                (priority, pp, script, dir_val, newzbin, name),
            )
            await db.conn.commit()
            logger.info("Category saved: %s (priority=%d, pp=%d, dir=%s)", name, priority, pp, dir_val)
    return RedirectResponse(url="/config/categories?saved=1", status_code=303)


@router.post("/config/categories/delete", response_class=HTMLResponse)
async def config_categories_delete(request: Request) -> HTMLResponse:
    db = getattr(request.app.state, "db", None)
    if db and db.conn:
        form = await request.form()
        name = str(form.get("name", ""))
        if name and name != "*":
            await db.conn.execute("DELETE FROM categories WHERE name=?", (name,))
            await db.conn.commit()
            logger.info("Category deleted: %s", name)
    return RedirectResponse(url="/config/categories?saved=1", status_code=303)


@router.get("/config/switches", response_class=HTMLResponse)
async def config_switches(request: Request) -> HTMLResponse:
    config = getattr(request.app.state, "config", None)
    switches = await config.get_section("switches", redact_secrets=False) if config else {}
    ctx = await _base_context(request, page="config", active_tab="switches", switches=switches)
    return templates.TemplateResponse(request, "config/switches.html", ctx)


@router.post("/config/switches", response_class=HTMLResponse)
async def config_switches_save(request: Request) -> HTMLResponse:
    return await _save_config(request, "switches")


@router.get("/config/sorting", response_class=HTMLResponse)
async def config_sorting(request: Request) -> HTMLResponse:
    config = getattr(request.app.state, "config", None)
    db = getattr(request.app.state, "db", None)
    sorting = await config.get_section("sorting", redact_secrets=False) if config else {}
    sorters = []

    if db and db.conn:
        cursor = await db.conn.execute(
            "SELECT id, name, sort_string, categories, enabled FROM sorters ORDER BY order_index"
        )
        for row in await cursor.fetchall():
            sorters.append({
                "id": row[0], "name": row[1], "sort_string": row[2],
                "categories": row[3], "enabled": row[4],
            })

    ctx = await _base_context(request, page="config", active_tab="sorting",
        sorting=sorting, sorters=sorters)
    return templates.TemplateResponse(request, "config/sorting.html", ctx)


@router.post("/config/sorting", response_class=HTMLResponse)
async def config_sorting_save(request: Request) -> HTMLResponse:
    return await _save_config(request, "sorting")


@router.get("/config/notifications", response_class=HTMLResponse)
async def config_notifications(request: Request) -> HTMLResponse:
    config = getattr(request.app.state, "config", None)
    notifications = await config.get_section("notifications", redact_secrets=False) if config else {}
    notification_events = [
        {"key": "notify_on_startup", "label": "Startup/Shutdown"},
        {"key": "notify_on_pause", "label": "Pause/Resume"},
        {"key": "notify_on_added", "label": "New NZB Added"},
        {"key": "notify_on_pp", "label": "Post-processing Started"},
        {"key": "notify_on_finished", "label": "Job Finished"},
        {"key": "notify_on_failed", "label": "Job Failed"},
        {"key": "notify_on_queue_finished", "label": "Queue Finished"},
    ]
    ctx = await _base_context(request, page="config", active_tab="notifications",
        notifications=notifications, notification_events=notification_events)
    return templates.TemplateResponse(request, "config/notifications.html", ctx)


@router.post("/config/notifications", response_class=HTMLResponse)
async def config_notifications_save(request: Request) -> HTMLResponse:
    return await _save_config(request, "notifications")


@router.get("/config/scheduling", response_class=HTMLResponse)
async def config_scheduling(request: Request) -> HTMLResponse:
    db = getattr(request.app.state, "db", None)
    schedules = []

    if db and db.conn:
        cursor = await db.conn.execute("SELECT id, minute, hour, day_of_week, action, argument FROM schedules ORDER BY id")
        for row in await cursor.fetchall():
            schedules.append({
                "id": row[0], "minute": row[1], "hour": row[2],
                "day_of_week": row[3], "action": row[4], "argument": row[5],
            })

    ctx = await _base_context(request, page="config", active_tab="scheduling",
        schedules=schedules)
    return templates.TemplateResponse(request, "config/scheduling.html", ctx)


@router.post("/config/scheduling/add", response_class=HTMLResponse)
async def config_scheduling_add(request: Request) -> HTMLResponse:
    db = getattr(request.app.state, "db", None)
    if db and db.conn:
        form = await request.form()
        day = str(form.get("day_of_week", "*"))
        time_str = str(form.get("time", "00:00"))
        action = str(form.get("action", "resume"))
        argument = str(form.get("argument", ""))

        parts = time_str.split(":")
        hour = int(parts[0]) if len(parts) > 0 else 0
        minute = int(parts[1]) if len(parts) > 1 else 0

        await db.conn.execute(
            "INSERT INTO schedules (minute, hour, day_of_week, action, argument) VALUES (?, ?, ?, ?, ?)",
            (minute, hour, day, action, argument),
        )
        await db.conn.commit()
        logger.info("Schedule added: %s %02d:%02d %s %s", day, hour, minute, action, argument)
    return RedirectResponse(url="/config/scheduling?saved=1", status_code=303)


@router.post("/config/scheduling/delete", response_class=HTMLResponse)
async def config_scheduling_delete(request: Request) -> HTMLResponse:
    db = getattr(request.app.state, "db", None)
    if db and db.conn:
        form = await request.form()
        schedule_id = str(form.get("id", ""))
        if schedule_id:
            await db.conn.execute("DELETE FROM schedules WHERE id=?", (int(schedule_id),))
            await db.conn.commit()
            logger.info("Schedule deleted: id=%s", schedule_id)
    return RedirectResponse(url="/config/scheduling?saved=1", status_code=303)


@router.get("/config/rss", response_class=HTMLResponse)
async def config_rss(request: Request) -> HTMLResponse:
    ctx = await _base_context(request, page="config", active_tab="rss")
    return templates.TemplateResponse(request, "config/rss.html", ctx)


@router.get("/config/special", response_class=HTMLResponse)
async def config_special(request: Request) -> HTMLResponse:
    config = getattr(request.app.state, "config", None)
    special = await config.get_section("special", redact_secrets=False) if config else {}
    ctx = await _base_context(request, page="config", active_tab="special", special=special)
    return templates.TemplateResponse(request, "config/special.html", ctx)


@router.post("/config/special", response_class=HTMLResponse)
async def config_special_save(request: Request) -> HTMLResponse:
    from debridnzbd.app import setup_logging

    result = await _save_config(request, "special")

    # Reconfigure logging immediately with the new log level
    config = getattr(request.app.state, "config", None)
    if config:
        log_level = await config.get("special", "log_level", "INFO")
        log_dir = await config.get("folders", "log_dir", "logs")
        setup_logging(log_dir=log_dir, log_level=log_level)

    return result


# ------------------------------------------------------------------ #
#  Logs page                                                          #
# ------------------------------------------------------------------ #

@router.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request) -> HTMLResponse:
    """View application logs.

    Reads the most recent log file from the configured log directory.
    Shows the last 500 lines by default; the `lines` query param adjusts.
    """
    config = getattr(request.app.state, "config", None)

    log_dir = "logs"
    if config:
        log_dir = await config.get("folders", "log_dir", "logs")

    max_lines = int(request.query_params.get("lines", "500"))

    log_content = ""
    log_file_path = ""

    # Find the most recent .log file in the log directory
    log_path = Path(log_dir)
    if log_path.is_dir():
        log_files = sorted(log_path.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        if log_files:
            log_file_path = str(log_files[0])
            try:
                text = log_files[0].read_text(errors="replace")
                lines = text.splitlines()
                log_content = "\n".join(lines[-max_lines:])
            except OSError:
                log_content = f"Unable to read log file: {log_file_path}"
        else:
            log_content = "No log files found in the log directory."
    else:
        log_content = f"Log directory '{log_dir}' does not exist yet. It will be created when the application writes its first log entry."

    ctx = await _base_context(request, page="logs",
        log_content=log_content, log_file_path=log_file_path, max_lines=max_lines)
    return templates.TemplateResponse(request, "logs.html", ctx)


# ------------------------------------------------------------------ #
#  Directory browser API (for folder picker UI)                       #
# ------------------------------------------------------------------ #

@router.get("/api/browse")
async def browse_directory(request: Request, path: str = ""):
    """List subdirectories of a given path for the folder picker UI.

    Returns JSON with the current path, parent path, and a list of
    subdirectories. Only paths within configured root directories
    (download_dir, complete_dir, admin_dir) are accessible.

    Args:
        path: Absolute or relative path to browse. If empty, lists
              the root allowed directories.

    Returns:
        JSONResponse with directory listing or error.
    """
    config = getattr(request.app.state, "config", None)
    if config is None:
        return JSONResponse(
            status_code=503,
            content={"error": "Configuration not available"},
        )

    # Build allowed root directories from config
    download_dir = await config.get("folders", "download_dir", "downloads/incomplete")
    complete_dir = await config.get("folders", "complete_dir", "downloads/complete")
    admin_dir = await config.get("folders", "admin_dir", "admin")

    # Collect unique resolved root directories. The "roots" are the
    # configured directories themselves. Paths are allowed if they are
    # within or equal to any root, OR if they are a parent of any root
    # (up to and including CWD). This allows users to navigate up from
    # a configured directory to its parents and back down.
    roots: list[Path] = []
    seen: set[Path] = set()
    for dir_path in [download_dir, complete_dir, admin_dir]:
        resolved = Path(dir_path).resolve()
        if resolved not in seen:
            roots.append(resolved)
            seen.add(resolved)

    cwd = Path.cwd().resolve()

    def _is_allowed(target: Path) -> bool:
        """Check that target is within or equal to a root, or is a parent of a root.

        Allows browsing paths that:
        - Are within a configured root directory (e.g., /data/downloads/complete/movies)
        - Are a parent of a configured root directory (e.g., /data/downloads is
          a parent of /data/downloads/complete)
        - But never allows browsing above CWD (prevents access to /etc, /home, etc.)
        """
        # Never allow browsing above CWD
        try:
            target.relative_to(cwd)
        except ValueError:
            # target is not within CWD — could be /etc, /home, etc.
            if target != cwd:
                return False

        for root in roots:
            # Target is within or equal to a root directory
            try:
                target.relative_to(root)
                return True
            except ValueError:
                pass
            # Root is within or equal to target (target is a parent of root)
            try:
                root.relative_to(target)
                return True
            except ValueError:
                pass
        return False

    def _to_relative(abs_path: Path) -> str:
        """Convert an absolute path to a relative path from CWD."""
        try:
            return str(abs_path.relative_to(cwd))
        except ValueError:
            return str(abs_path)

    # --- No path specified: list root directories ---
    if not path:
        root_entries = []
        seen_names: set[str] = set()
        for root in roots:
            rel = _to_relative(root)
            display_name = rel
            # Avoid duplicate display names
            if display_name in seen_names:
                continue
            seen_names.add(display_name)
            root_entries.append({
                "name": display_name,
                "path": str(root),
                "relative": rel,
                "exists": root.is_dir(),
            })

        return JSONResponse(content={
            "current_path": "",
            "relative_path": "",
            "parent": None,
            "directories": root_entries,
            "is_root": True,
        })

    # --- Path specified: validate and list subdirectories ---
    target = Path(path).resolve()

    if not _is_allowed(target):
        return JSONResponse(
            status_code=403,
            content={"error": f"Access denied: path is outside allowed directories."},
        )

    if not target.is_dir():
        return JSONResponse(
            status_code=404,
            content={"error": f"Directory not found: {path}"},
        )

    relative_path = _to_relative(target)

    # Compute parent — only if parent is also within allowed scope
    parent = target.parent
    parent_path = str(parent) if _is_allowed(parent) else None

    # List subdirectories (excluding hidden directories)
    directories = []
    try:
        for entry in sorted(target.iterdir()):
            if entry.is_dir() and not entry.name.startswith("."):
                directories.append({
                    "name": entry.name,
                    "path": str(entry),
                    "relative": _to_relative(entry),
                })
    except PermissionError:
        return JSONResponse(
            status_code=403,
            content={"error": f"Permission denied accessing directory."},
        )

    return JSONResponse(content={
        "current_path": str(target),
        "relative_path": relative_path,
        "parent": parent_path,
        "directories": directories,
        "is_root": False,
    })


@router.post("/api/browse/mkdir")
async def mkdir_directory(request: Request) -> JSONResponse:
    """Create a new directory within the allowed browse paths.

    Accepts a JSON body: ``{"path": "/absolute/path/to/parent", "name": "new_folder"}``

    The parent path must be within the configured allowed directories
    (download_dir, complete_dir, admin_dir). Returns the absolute path
    of the newly created directory on success.
    """
    config = getattr(request.app.state, "config", None)
    if config is None:
        return JSONResponse(
            status_code=503,
            content={"error": "Configuration not available"},
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid JSON body"},
        )

    parent_path = body.get("path", "")
    folder_name = body.get("name", "")

    if not parent_path or not folder_name:
        return JSONResponse(
            status_code=400,
            content={"error": "Both path and name are required"},
        )

    # Sanitize folder name — no path separators, no hidden dirs
    if "/" in folder_name or "\\" in folder_name or folder_name.startswith("."):
        return JSONResponse(
            status_code=400,
            content={"error": "Invalid folder name"},
        )

    # Build allowed root directories (same logic as browse_directory)
    download_dir = await config.get("folders", "download_dir", "downloads/incomplete")
    complete_dir = await config.get("folders", "complete_dir", "downloads/complete")
    admin_dir = await config.get("folders", "admin_dir", "admin")

    roots: list[Path] = []
    seen_roots: set[Path] = set()
    for dir_path in [download_dir, complete_dir, admin_dir]:
        resolved = Path(dir_path).resolve()
        if resolved not in seen_roots:
            roots.append(resolved)
            seen_roots.add(resolved)

    cwd = Path.cwd().resolve()

    def _is_allowed(target: Path) -> bool:
        try:
            target.relative_to(cwd)
        except ValueError:
            if target != cwd:
                return False
        for root in roots:
            try:
                target.relative_to(root)
                return True
            except ValueError:
                pass
            try:
                root.relative_to(target)
                return True
            except ValueError:
                pass
        return False

    parent = Path(parent_path).resolve()

    if not _is_allowed(parent):
        return JSONResponse(
            status_code=403,
            content={"error": "Access denied: parent path is outside allowed directories."},
        )

    if not parent.is_dir():
        return JSONResponse(
            status_code=404,
            content={"error": "Parent directory not found"},
        )

    new_dir = parent / folder_name

    # Prevent creating outside the parent (e.g. via symlinks)
    if not str(new_dir.resolve()).startswith(str(parent.resolve())):
        return JSONResponse(
            status_code=403,
            content={"error": "Access denied: new directory would be outside parent."},
        )

    if new_dir.exists():
        logger.info("Directory already exists, skipping creation: %s", new_dir)
        return JSONResponse(
            content={"status": True, "path": str(new_dir.resolve()),
                     "relative": str(new_dir.resolve().relative_to(cwd)),
                     "message": "Directory already exists"},
        )

    try:
        new_dir.mkdir(parents=False)
        logger.info("Created directory: %s", new_dir)
    except PermissionError:
        logger.warning("Permission denied creating directory: %s", new_dir)
        return JSONResponse(
            status_code=403,
            content={"error": "Permission denied creating directory"},
        )
    except OSError as e:
        logger.error("Failed to create directory %s: %s", new_dir, e)
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to create directory: {e}"},
        )

    return JSONResponse(content={
        "status": True,
        "path": str(new_dir.resolve()),
        "relative": str(new_dir.resolve().relative_to(cwd)),
    })