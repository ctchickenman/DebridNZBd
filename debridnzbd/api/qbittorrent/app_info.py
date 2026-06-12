"""qBittorrent application information endpoints.

Provides version, preferences, and path information that clients
use for compatibility checks and settings display.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Query, Request, Response

from debridnzbd.api.qbittorrent.dependencies import get_config, require_sid
from debridnzbd.core.config_store import ConfigStore

from pathlib import Path

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/app", tags=["qBittorrent App"])

# Emulated qBittorrent version and WebAPI version
QBIT_APP_VERSION = "4.6.3"
QBIT_WEBAPI_VERSION = "2.11.2"


@router.get("/version")
async def app_version(
    request: Request,
    sid: str = Depends(require_sid),
):
    """Return the emulated qBittorrent application version as plain text."""
    return Response(content=QBIT_APP_VERSION, media_type="text/plain")


@router.get("/webapiVersion")
async def app_webapi_version(
    request: Request,
    sid: str = Depends(require_sid),
):
    """Return the emulated qBittorrent WebAPI version as plain text."""
    return Response(content=QBIT_WEBAPI_VERSION, media_type="text/plain")


@router.get("/defaultSavePath")
async def app_default_save_path(
    request: Request,
    config: ConfigStore = Depends(get_config),
    sid: str = Depends(require_sid),
):
    """Return the default save path for downloads as plain text.

    Returns an absolute path so that *arr clients can apply their own
    remote path mappings. Relative config values are resolved against
    the current working directory (e.g. ``downloads/complete`` →
    ``/data/downloads/complete`` in Docker).
    """
    save_path = await config.get("folders", "complete_dir", "downloads/complete")
    return Response(content=str(Path(save_path).resolve()), media_type="text/plain")


@router.get("/preferences")
async def app_preferences(
    request: Request,
    config: ConfigStore = Depends(get_config),
    sid: str = Depends(require_sid),
):
    """Return application preferences as JSON.

    Most fields are stubbed with defaults since DebridNZBd doesn't
    implement the full qBittorrent preferences model. Key fields
    (save_path, dl_limit, up_limit) are populated from config.
    """
    save_path = await config.get("folders", "complete_dir", "downloads/complete")
    temp_path = await config.get("folders", "download_dir", "downloads/incomplete")
    dl_limit = int(await config.get("torbox", "qbit_dl_limit", "0"))
    listen_port = int(await config.get("misc", "port", "8080"))

    # Resolve paths to absolute so *arr clients can apply remote path mappings.
    # A relative value like "downloads/complete" becomes "/data/downloads/complete"
    # when the app runs with WORKDIR=/data (Docker default).
    save_path_resolved = str(Path(save_path).resolve())
    temp_path_resolved = str(Path(temp_path).resolve())

    preferences = {
        # Paths — absolute paths are required for *arr remote path mappings
        "save_path": save_path_resolved,
        "temp_path": temp_path_resolved,
        "temp_path_enabled": False,
        # Connection
        "listen_port": listen_port,
        "upnp": False,
        "max_connec": 500,
        "max_connec_per_torrent": 100,
        "max_uploads": 20,
        "max_uploads_per_torrent": 4,
        # Speed limits (in KiB/s for preferences, unlike transfer API which uses bytes/s)
        "dl_limit": dl_limit // 1024 if dl_limit > 0 else 0,
        "up_limit": 0,
        "alt_dl_limit": 0,
        "alt_up_limit": 0,
        "scheduler_enabled": False,
        "schedule_from_hour": 8,
        "schedule_from_min": 0,
        "schedule_to_hour": 20,
        "schedule_to_min": 0,
        "scheduler_days": 0,
        # BitTorrent
        "dht": True,
        "pex": True,
        "lsd": True,
        "encryption": 0,
        "max_ratio_enabled": False,
        "max_ratio": -1,
        "max_seeding_time_enabled": False,
        "max_seeding_time": -1,
        # Queueing
        "queueing_enabled": True,
        "max_active_downloads": 3,
        "max_active_uploads": 3,
        "max_active_torrents": 5,
        "dont_count_slow_torrents": True,
        "slow_torrent_dl_rate_threshold": 2,
        "slow_torrent_ul_rate_threshold": 2,
        "slow_torrent_inactive_timer": 60,
        # Web UI
        "web_ui_username": await config.get("misc", "username", "admin"),
        "web_ui_port": listen_port,
        "web_ui_address": "*",
        "bittorrent_protocol": 0,
        # Misc
        "locale": "en",
        "create_subfolder_enabled": True,
        "start_paused_enabled": False,
        "auto_delete_mode": 0,
        "export_dir": "",
        "export_dir_fin": "",
        "mail_notification_enabled": False,
        "incomplete_files_ext": False,
        "rss_processing_enabled": False,
        "rss_auto_downloading_enabled": False,
        "rss_refresh_interval": 30,
        "rss_max_articles_per_feed": 50,
        "add_trackers": "",
        "add_trackers_enabled": False,
        "announce_ip": "",
        "announce_to_all_tiers": True,
        "announce_to_all_trackers": False,
        "anonymous_mode": False,
        "async_io_threads": 4,
        "bypass_auth_subnet": "",
        "bypass_local_auth": False,
        "check_interval": 60,
        "disk_cache": -1,
        "disk_cache_ttl": 60,
        "embedded_tracker_port": 0,
        "enable_coalesce_read_write": True,
        "enable_embedded_tracker": False,
        "enable_multi_connections_from_same_ip": True,
        "enable_piece_extent_affinity": True,
        "enable_upload_suggestions": False,
        "file_pool_size": 40,
        "max_ratio_act": 0,
        "send_upload_piece_suggestions": False,
        "stop_tracker_timeout": 2,
        "turn_off_interval": 0,
        "os_cache": True,
        "resolve_countries": False,
        "recheck_completed_torrents": False,
        "refresh_interval": 1500,
        "resolve_peer_countries": True,
        "save_resume_data_interval": 60,
        "send_buffer_watermark": 512,
        "send_buffer_low_watermark": 256,
        "send_buffer_watermark_factor": 50,
        "connection_speed": 0,
        "socket_backlog_size": 30,
        "current_interface_address": "",
        "current_network_interface": "",
        "ip_filter_enabled": False,
        "ip_filter_path": "",
        "ip_filter_trackers": False,
        "banned_IPs": "",
    }

    from fastapi.responses import JSONResponse
    return JSONResponse(content=preferences)


@router.post("/setPreferences")
async def app_set_preferences(
    request: Request,
    config: ConfigStore = Depends(get_config),
    sid: str = Depends(require_sid),
):
    """Accept preference changes. Only a few fields are applied.

    Most preferences are accepted but ignored since DebridNZBd
    doesn't control the actual download engine (Torbox does).
    """
    form = await request.form()
    json_str = str(form.get("json", ""))

    if json_str:
        try:
            prefs = json.loads(json_str)
        except (json.JSONDecodeError, TypeError):
            prefs = {}
    else:
        prefs = {}

    # Apply supported preferences
    if "save_path" in prefs and prefs["save_path"]:
        await config.set("folders", "complete_dir", str(prefs["save_path"]))

    if "dl_limit" in prefs:
        # Preferences use KiB/s; convert to bytes/s for storage
        limit_kib = int(prefs["dl_limit"])
        await config.set("torbox", "qbit_dl_limit", str(limit_kib * 1024))

    return Response(content="Ok.", media_type="text/plain")


@router.post("/shutdown")
async def app_shutdown(
    request: Request,
    sid: str = Depends(require_sid),
):
    """Shutdown is not supported. Accept but ignore."""
    return Response(content="Ok.", media_type="text/plain")