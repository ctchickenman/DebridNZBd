"""qBittorrent transfer management endpoints.

Provides global speed statistics and speed limit management.
Since DebridNZBd routes through Torbox (a debrid service),
upload speed is always 0 and speed limits are stored locally
but not actually enforced on the Torbox side.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse

from debridnzbd.api.qbittorrent.dependencies import get_config, get_db, require_sid
from debridnzbd.core.config_store import ConfigStore
from debridnzbd.db.database import Database

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/transfer", tags=["qBittorrent Transfer"])


@router.get("/info")
async def transfer_info(
    request: Request,
    sid: str = Depends(require_sid),
    db: Database = Depends(get_db),
    config: ConfigStore = Depends(get_config),
):
    """Get global transfer info (speeds, totals, connection status)."""
    total_speed = 0.0
    total_downloaded = 0.0

    if db and db.conn:
        try:
            # Only count torrent-type jobs — usenet/webdl are not shown in qBittorrent
            cursor = await db.conn.execute(
                "SELECT COALESCE(SUM(speed), 0), COALESCE(SUM(size - sizeleft), 0) FROM jobs WHERE status IN ('Downloading', 'Fetching') AND torbox_type = 'torrent'"
            )
            row = await cursor.fetchone()
            if row:
                total_speed = row[0] or 0
                total_downloaded = row[1] or 0
        except Exception:
            logger.exception("Failed to query transfer stats")

    dl_limit = int(await config.get("torbox", "qbit_dl_limit", "0"))

    info = {
        "dl_info_speed": int(total_speed),
        "dl_info_data": int(total_downloaded),
        "up_info_speed": 0,  # Debrid: no upload
        "up_info_data": 0,
        "dl_rate_limit": dl_limit,
        "up_rate_limit": 0,  # Debrid: no upload limit
        "dht_nodes": 0,  # Not applicable to debrid
        "connection_status": "connected",
    }

    return JSONResponse(content=info)


@router.get("/speedLimitsMode")
async def transfer_speed_limits_mode(
    request: Request,
    sid: str = Depends(require_sid),
):
    """Get alternative speed limits mode state. Always 0 (normal mode)."""
    return Response(content="0", media_type="text/plain")


@router.post("/toggleSpeedLimitsMode")
async def transfer_toggle_speed_limits_mode(
    request: Request,
    sid: str = Depends(require_sid),
):
    """Toggle alternative speed limits — accepted but not enforced."""
    return Response(content="Ok.", media_type="text/plain")


@router.get("/downloadLimit")
async def transfer_download_limit(
    request: Request,
    sid: str = Depends(require_sid),
    config: ConfigStore = Depends(get_config),
):
    """Get global download speed limit in bytes/s. 0 = unlimited."""
    limit = int(await config.get("torbox", "qbit_dl_limit", "0"))
    return Response(content=str(limit), media_type="text/plain")


@router.post("/setDownloadLimit")
async def transfer_set_download_limit(
    request: Request,
    sid: str = Depends(require_sid),
    config: ConfigStore = Depends(get_config),
):
    """Set global download speed limit in bytes/s. 0 = unlimited."""
    form = await request.form()
    limit = str(form.get("limit", "0"))
    try:
        limit_val = int(limit)
    except (ValueError, TypeError):
        limit_val = 0

    await config.set("torbox", "qbit_dl_limit", str(limit_val))
    return Response(content="Ok.", media_type="text/plain")


@router.get("/uploadLimit")
async def transfer_upload_limit(
    request: Request,
    sid: str = Depends(require_sid),
):
    """Get global upload speed limit. Always 0 (debrid = no upload)."""
    return Response(content="0", media_type="text/plain")


@router.post("/setUploadLimit")
async def transfer_set_upload_limit(
    request: Request,
    sid: str = Depends(require_sid),
):
    """Set global upload speed limit — accepted but ignored (debrid = no upload)."""
    return Response(content="Ok.", media_type="text/plain")


@router.post("/banPeers")
async def transfer_ban_peers(
    request: Request,
    sid: str = Depends(require_sid),
):
    """Ban peers — stubbed (not applicable to debrid)."""
    return Response(content="Ok.", media_type="text/plain")