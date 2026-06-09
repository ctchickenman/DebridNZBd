"""qBittorrent sync endpoints for incremental data polling.

Provides the maindata endpoint that qBittorrent clients use for
efficient polling. The initial implementation returns full snapshots
on every request; incremental updates can be added as an optimization
later.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from debridnzbd.api.qbittorrent.dependencies import get_config, get_db, require_sid
from debridnzbd.api.qbittorrent.mappers import build_torrent_info
from debridnzbd.core.config_store import ConfigStore
from debridnzbd.db.database import Database

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sync", tags=["qBittorrent Sync"])

# Monotonic rid counter — incremented on each poll.
# Reset on app restart (causes clients to get a full refresh).
_maindata_rid: int = 0


@router.get("/maindata")
async def sync_maindata(
    request: Request,
    rid: str = Query("0"),
    sid: str = Depends(require_sid),
    db: Database = Depends(get_db),
    config: ConfigStore = Depends(get_config),
):
    """Get main data snapshot for qBittorrent clients.

    The rid (request ID) parameter enables incremental polling:
    - rid=0 or absent: full snapshot
    - rid>0: in this implementation, always returns full snapshot

    Clients should store the returned rid and pass it in the next request.
    """
    global _maindata_rid
    _maindata_rid += 1
    current_rid = _maindata_rid

    if not db or not db.conn:
        return JSONResponse(content={
            "rid": current_rid,
            "full_update": True,
            "torrents": {},
            "categories": {},
            "tags": [],
            "server_state": {},
        })

    show_all_types = (await config.get("torbox", "qbit_show_all_types", "0")) == "1"

    # Fetch all jobs
    if show_all_types:
        cursor = await db.conn.execute(
            """SELECT nzo_id, filename, nzo_url, category, priority, status,
                      size, sizeleft, percentage, time_added, time_completed,
                      torbox_id, torbox_type, torbox_hash, speed, tags, position
               FROM jobs ORDER BY position"""
        )
    else:
        cursor = await db.conn.execute(
            """SELECT nzo_id, filename, nzo_url, category, priority, status,
                      size, sizeleft, percentage, time_added, time_completed,
                      torbox_id, torbox_type, torbox_hash, speed, tags, position
               FROM jobs WHERE torbox_type = 'torrent' ORDER BY position"""
        )

    rows = await cursor.fetchall()

    # Build torrents dict keyed by hash
    torrents = {}
    for row in rows:
        info = build_torrent_info(row)
        torrents[info["hash"]] = info

    # Build categories
    complete_dir = await config.get("folders", "complete_dir", "downloads/complete")
    cat_cursor = await db.conn.execute("SELECT name, dir FROM categories ORDER BY name")
    cat_rows = await cat_cursor.fetchall()
    categories = {}
    for name, cat_dir in cat_rows:
        categories[name] = {"name": name, "savePath": cat_dir if cat_dir else f"{complete_dir}/{name}"}

    # Build tags list
    tag_cursor = await db.conn.execute("SELECT tags FROM jobs WHERE tags IS NOT NULL AND tags != ''")
    tag_rows = await tag_cursor.fetchall()
    all_tags: set[str] = set()
    for tag_row in tag_rows:
        if tag_row[0]:
            for tag in tag_row[0].split(","):
                tag = tag.strip()
                if tag:
                    all_tags.add(tag)

    # Aggregate speed stats
    total_speed = sum(t.get("dspeed", 0) for t in torrents.values())
    dl_limit = int(await config.get("torbox", "qbit_dl_limit", "0"))

    server_state = {
        "dl_info_speed": total_speed,
        "up_info_speed": 0,
        "dl_rate_limit": dl_limit,
        "up_rate_limit": 0,
        "dht_nodes": 0,
        "connection_status": "connected",
        "queueing": True,
        "use_alt_speed_limits": False,
        "refresh_interval": 1500,
    }

    response = {
        "rid": current_rid,
        "full_update": True,
        "torrents": torrents,
        "categories": categories,
        "categories_removed": [],
        "tags": sorted(all_tags),
        "tags_removed": [],
        "server_state": server_state,
    }

    return JSONResponse(content=response)


@router.get("/torrentPeers")
async def sync_torrent_peers(
    request: Request,
    hash: str = Query(""),
    rid: str = Query("0"),
    sid: str = Depends(require_sid),
):
    """Get torrent peer data — stubbed (debrid has no peers)."""
    return JSONResponse(content={
        "rid": 0,
        "full_update": True,
        "peers": {},
    })