"""Main API router for DebridNZBd.

Dispatches incoming SABnzbd API requests (`/api?mode=XXX`) to the
appropriate handler module based on the `mode` query parameter.

The router follows SABnzbd's API convention:
- All requests go to `/api`
- The `mode` parameter determines the action
- The `output` parameter selects response format (json or xml)
- Authentication is handled by AuthMiddleware before reaching handlers
- Most modes require a valid `apikey` parameter

Handler modules are organized by functionality:
- `status.py`: version, auth, status, fullstatus, warnings, server_stats
- `queue.py`: addurl, addfile, queue, pause, resume, delete, switch, etc.
- `history.py`: history, retry, retry_all, mark_as_completed
- `config.py`: get_config, set_config, get_cats, get_scripts, speedlimit, etc.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile

from debridnzbd.api.queue import (
    handle_addurl,
    handle_addfile,
    handle_queue,
    handle_pause,
    handle_resume,
    handle_delete,
    handle_purge,
    handle_switch,
    handle_change_cat,
    handle_priority,
    handle_speedlimit,
)
from debridnzbd.api.history import handle_history, handle_retry, handle_retry_all
from debridnzbd.api.status import (
    handle_status,
    handle_fullstatus,
    handle_warnings,
    handle_server_stats,
)
from debridnzbd.api.config import (
    handle_get_config,
    handle_set_config,
    handle_del_config,
    handle_get_cats,
    handle_get_scripts,
)
from debridnzbd.utils.version import VERSION

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api")
@router.post("/api")
async def api_handler(
    request: Request,
    mode: str = Query(default="", description="SABnzbd API mode"),
    output: str = Query(default="json", description="Output format: json or xml"),
    apikey: str = Query(default="", description="API key for authentication"),
    name: str | None = Query(default=None, description="Sub-command name"),
    value: str | None = Query(default=None, description="Primary value parameter"),
    value2: str | None = Query(default=None, description="Secondary value parameter"),
    value3: str | None = Query(default=None, description="Tertiary value parameter"),
    start: int | None = Query(default=None, description="Start index for paginated results"),
    limit: int | None = Query(default=None, description="Limit for paginated results"),
    search: str | None = Query(default=None, description="Search filter"),
    cat: str | None = Query(default=None, description="Category filter"),
    category: str | None = Query(default=None, description="Category filter (alias)"),
    priority: int | None = Query(default=None, description="Priority filter"),
    status: str | None = Query(default=None, description="Status filter"),
    nzo_ids: str | None = Query(default=None, description="Comma-separated nzo IDs"),
    password: str | None = Query(default=None, description="Job password"),
    pp: int | None = Query(default=None, description="Post-processing option"),
    script: str | None = Query(default=None, description="Post-processing script"),
    nzbname: str | None = Query(default=None, description="Custom job name"),
    del_files: int | None = Query(default=None, description="Delete files flag (0 or 1)"),
    archive: int | None = Query(default=None, description="Archive flag (0 or 1)"),
    failed_only: int | None = Query(default=None, description="Show only failed (0 or 1)"),
    last_history_update: float | None = Query(default=None, description="Last update timestamp"),
    skip_dashboard: int | None = Query(default=None, description="Skip dashboard data (0 or 1)"),
    sort: str | None = Query(default=None, description="Sort field for queue"),
    dir: str | None = Query(default=None, description="Sort direction (asc/desc)"),
    section: str | None = Query(default=None, description="Config section"),
    keyword: str | None = Query(default=None, description="Config keyword"),
) -> JSONResponse:
    """Main SABnzbd API dispatcher.

    Routes requests based on the `mode` query parameter to the appropriate
    handler. This is the primary entry point for all SABnzbd-compatible
    API interactions.

    Args:
        request: The FastAPI request object (contains auth_level from middleware).
        mode: The SABnzbd API mode (e.g., "queue", "addurl", "history").
        All other parameters are passed through to handlers.

    Returns:
        JSONResponse matching SABnzbd's response format.
    """
    # Dispatch to handler based on mode
    handler = MODE_HANDLERS.get(mode)

    if handler is None:
        # Unknown mode — return a SABnzbd-compatible error
        logger.warning("Unknown API mode requested: %s", mode)
        return JSONResponse(
            status_code=400,
            content={"status": False, "error": f"Unknown mode: {mode}"},
        )

    # Build a params dict for the handler
    params = {
        "request": request,
        "mode": mode,
        "output": output,
        "apikey": apikey,
        "name": name,
        "value": value,
        "value2": value2,
        "value3": value3,
        "start": start,
        "limit": limit,
        "search": search,
        "cat": cat,
        "category": category,
        "priority": priority,
        "status": status,
        "nzo_ids": nzo_ids,
        "password": password,
        "pp": pp,
        "script": script,
        "nzbname": nzbname,
        "del_files": del_files,
        "archive": archive,
        "failed_only": failed_only,
        "last_history_update": last_history_update,
        "skip_dashboard": skip_dashboard,
        "sort": sort,
        "dir": dir,
        "section": section,
        "keyword": keyword,
    }

    # SABnzbd clients (and the web UI) often send parameters in the POST
    # form body rather than the query string. FastAPI's Query() parameters
    # only read from the URL query string, so we need to also read the form
    # body and merge any parameters that aren't already set from query params.
    # The auth middleware has already parsed and cached the form data on
    # request.state._form_data, so we use that instead of reading the body again.
    if request.method in ("POST", "PUT", "PATCH"):
        form = getattr(request.state, "_form_data", None)
        if form is not None:
            for key, value in form.multi_items():
                # Skip file uploads — they are extracted separately below
                if isinstance(value, UploadFile):
                    continue
                str_value = value if isinstance(value, str) else str(value)
                if key not in params or params[key] is None:
                    params[key] = str_value

            # Extract uploaded file data from multipart form.
            # SABnzbd uses "nzbfile" as the file parameter name, but we also
            # accept any file upload for robustness.
            for key, value in form.multi_items():
                if isinstance(value, UploadFile):
                    file_content = await value.read()
                    await value.close()
                    params["_upload_file_data"] = file_content
                    params["_upload_file_name"] = value.filename or "upload"
                    break  # Only one file upload per request

    try:
        return await handler(params)
    except Exception as e:
        # Log the full exception server-side for debugging
        logger.exception("Error handling API mode %s: %s", mode, e)
        # Return a generic error to the client — never expose internal details
        return JSONResponse(
            status_code=500,
            content={"status": False, "error": "Internal server error"},
        )


# ------------------------------------------------------------------ #
#  Handler functions for each API mode                                  #
# ------------------------------------------------------------------ #

async def handle_version(params: dict) -> JSONResponse:
    """Handle ?mode=version — return DebridNZBd version.

    This is a public endpoint that doesn't require authentication.
    """
    return JSONResponse(content={"status": True, "version": VERSION})


async def handle_auth(params: dict) -> JSONResponse:
    """Handle ?mode=auth — return authentication method.

    This is a public endpoint that doesn't require authentication.
    SABnzbd returns "apikey" to indicate that API key authentication is used.
    """
    return JSONResponse(content={"status": True, "auth": "apikey"})


# ------------------------------------------------------------------ #
#  Mode handler registry                                                #
# ------------------------------------------------------------------ #

# Maps SABnzbd API mode names to their handler functions.
MODE_HANDLERS: dict[str, callable] = {
    "version": handle_version,
    "auth": handle_auth,
    # Queue modes
    "addurl": handle_addurl,
    "addfile": handle_addfile,
    "queue": handle_queue,
    "pause": handle_pause,
    "resume": handle_resume,
    "delete": handle_delete,
    "purge": handle_purge,
    "switch": handle_switch,
    "change_cat": handle_change_cat,
    "priority": handle_priority,
    "speedlimit": handle_speedlimit,
    # History modes
    "history": handle_history,
    "retry": handle_retry,
    "retry_all": handle_retry_all,
    # Status modes
    "status": handle_status,
    "fullstatus": handle_fullstatus,
    "warnings": handle_warnings,
    "server_stats": handle_server_stats,
    # Config modes
    "get_config": handle_get_config,
    "set_config": handle_set_config,
    "del_config": handle_del_config,
    "get_cats": handle_get_cats,
    "get_scripts": handle_get_scripts,
    # Not yet implemented:
    # "addfile": handle_addfile,
    # "addlocalfile": handle_addlocalfile,
    # "change_script": handle_change_script,
    # "change_opts": handle_change_opts,
    # "rename": handle_rename,
    # "get_files": handle_get_files,
    # "sort": handle_sort_queue,
    # "mark_as_completed": handle_mark_as_completed,
    # "set_apikey": handle_set_apikey,
    # "set_nzbkey": handle_set_nzbkey,
    # "shutdown": handle_shutdown,
    # "restart": handle_restart,
}