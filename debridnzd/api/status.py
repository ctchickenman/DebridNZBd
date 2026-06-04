"""SABnzbd-compatible API handlers for status operations.

Implements status, fullstatus, warnings, and server_stats modes
for server health, connection info, and active warnings.
"""

from __future__ import annotations

import logging
import os
import time

from fastapi.responses import JSONResponse

from debridnzd.db.models import ServerInfo, StatusResponse, WarningEntry, WarningsResponse
from debridnzd.utils.format import format_size, format_speed, format_uptime

logger = logging.getLogger(__name__)


async def handle_status(params: dict) -> JSONResponse:
    """Handle ?mode=status — return server status.

    Returns a summary of server status including Torbox connection
    info, speed, and disk space. This is an alias for fullstatus
    that most *arr clients use.

    Returns:
        JSONResponse with status info matching SABnzbd format.
    """
    return await _build_status_response(params)


async def handle_fullstatus(params: dict) -> JSONResponse:
    """Handle ?mode=fullstatus — return full server status.

    Identical to handle_status — SABnzbd treats status and fullstatus
    the same way for most clients.

    Returns:
        JSONResponse with full status info.
    """
    return await _build_status_response(params)


async def _build_status_response(params: dict) -> JSONResponse:
    """Build the common status response object.

    Reads from the database for active download stats and from config
    for Torbox connection info and settings.
    """
    request = params.get("request")
    config = getattr(request.app.state, "config", None) if request else None
    db = getattr(request.app.state, "db", None) if request else None
    start_time = getattr(request.app.state, "start_time", time.time()) if request else time.time()

    # Version
    from debridnzd.utils.version import VERSION

    # Torbox connection status
    torbox_connected = False
    if config:
        api_key = await config.get("torbox", "api_key")
        torbox_connected = bool(api_key)

    # Build server info
    servers = []
    if torbox_connected:
        servers.append(ServerInfo(
            servername="Torbox",
            servertotalconn=1,
            serverssl=True,
            serveractiveconn=1,
            serveroptional=False,
            serveractive=True,
            servererror="",
            serverpriority=0,
            serverbps="0",
        ))
    else:
        servers.append(ServerInfo(
            servername="Torbox",
            servertotalconn=0,
            serverssl=True,
            serveractiveconn=0,
            serveroptional=False,
            serveractive=False,
            servererror="API key not configured",
            serverpriority=0,
            serverbps="0",
        ))

    # Download stats from queue
    total_speed = 0.0
    speed_limit_pct = "100"
    if db and db.conn:
        cursor = await db.conn.execute(
            "SELECT COALESCE(SUM(speed), 0) FROM jobs WHERE status = 'Downloading'"
        )
        row = await cursor.fetchone()
        if row:
            total_speed = row[0]

    if config:
        speed_limit_pct = await config.get("misc", "speedlimit", "100")

    # Disk space
    diskspace1 = "0"
    diskspace2 = "0"
    if config:
        try:
            from debridnzd.utils.diskspace import get_disk_usage

            download_dir = await config.get("folders", "download_dir", "downloads/incomplete")
            complete_dir = await config.get("folders", "complete_dir", "downloads/complete")
            try:
                usage = get_disk_usage(download_dir)
                diskspace1 = format_size(usage["free"])
            except (FileNotFoundError, ValueError, OSError):
                pass
            try:
                usage = get_disk_usage(complete_dir)
                diskspace2 = format_size(usage["free"])
            except (FileNotFoundError, ValueError, OSError):
                pass
        except Exception:
            pass

    # Warnings
    warnings_list = []
    if db and db.conn:
        cursor = await db.conn.execute(
            "SELECT text, type, time FROM warnings ORDER BY time DESC LIMIT 50"
        )
        for row in await cursor.fetchall():
            warnings_list.append(WarningEntry(
                text=row[0],
                type=row[1] or "WARNING",
                time=row[2],
            ))

    # Build response
    status_response = StatusResponse(
        have_warnings=str(len(warnings_list)),
        uptime=format_uptime(start_time),
        speed=format_speed(total_speed),
        kbpersec=str(int(total_speed / 1024)),
        speedlimit=speed_limit_pct,
        speedlimit_abs=speed_limit_pct,
        paused=False,
        paused_all=False,
        servers=servers,
        warnings=[w.text for w in warnings_list],
        diskspace1=diskspace1,
        diskspace2=diskspace2,
        diskspacex1=diskspace1,
        diskspacex2=diskspace2,
        pid=os.getpid(),
    )

    # Folder config
    if config:
        status_response.downloaddir = await config.get("folders", "download_dir", "downloads/incomplete")
        status_response.completedir = await config.get("folders", "complete_dir", "downloads/complete")

    return JSONResponse(content={"status": True, **status_response.model_dump()})


async def handle_warnings(params: dict) -> JSONResponse:
    """Handle ?mode=warnings — return active warnings.

    Returns a list of active warnings from the database.
    """
    request = params.get("request")
    db = getattr(request.app.state, "db", None) if request else None

    warnings_list = []
    if db and db.conn:
        cursor = await db.conn.execute(
            "SELECT text, type, time FROM warnings ORDER BY time DESC"
        )
        for row in await cursor.fetchall():
            warnings_list.append(WarningEntry(
                text=row[0],
                type=row[1] or "WARNING",
                time=row[2],
            ))

    response = WarningsResponse(warnings=warnings_list)
    return JSONResponse(content=response.model_dump())


async def handle_server_stats(params: dict) -> JSONResponse:
    """Handle ?mode=server_stats — return Torbox connection stats.

    Returns server (Torbox) statistics in SABnzbd format.
    """
    request = params.get("request")
    config = getattr(request.app.state, "config", None) if request else None

    # Torbox connection status
    torbox_connected = False
    if config:
        api_key = await config.get("torbox", "api_key")
        torbox_connected = bool(api_key)

    server = ServerInfo(
        servername="Torbox",
        servertotalconn=1 if torbox_connected else 0,
        serverssl=True,
        serveractiveconn=1 if torbox_connected else 0,
        serveroptional=False,
        serveractive=torbox_connected,
        servererror="" if torbox_connected else "API key not configured",
        serverpriority=0,
        serverbps="0",
    )

    return JSONResponse(content={
        "status": True,
        "servers": [server.model_dump()],
    })