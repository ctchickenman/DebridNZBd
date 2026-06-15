"""qBittorrent torrent management endpoints.

Implements the core torrent CRUD operations: listing, adding,
pausing, resuming, deleting, and property/file/tracker queries.
Also includes category and tag management endpoints.
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile

from debridnzbd.api.qbittorrent.dependencies import (
    get_config,
    get_db,
    require_csrf,
    require_sid,
)
from debridnzbd.api.qbittorrent.mappers import (
    build_torrent_info,
    debrid_status_to_qbit,
    matches_filter,
)
from debridnzbd.core.config_store import ConfigStore
from debridnzbd.db.database import Database
from debridnzbd.torbox.client import TorboxClient
from debridnzbd.utils.nzo_id import generate_nzo_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/torrents", tags=["qBittorrent Torrents"])

# ------------------------------------------------------------------ #
#  Hash lookup helper                                                   #
# ------------------------------------------------------------------ #


async def _find_jobs_by_hashes(
    db: Database, hashes: list[str],
) -> list[tuple]:
    """Look up jobs by torbox_hash (case-insensitive).

    Returns list of database rows matching any of the given hashes.
    """
    if not hashes:
        return []

    # Build parameterized IN clause
    placeholders = ",".join("?" for _ in hashes)
    # Also match synthesized hashes from nzo_id
    cursor = await db.conn.execute(
        f"""SELECT nzo_id, filename, nzo_url, category, priority, status,
                   size, sizeleft, percentage, time_added, time_completed,
                   torbox_id, torbox_type, torbox_hash, speed, tags, position,
                   stalled_since, local_path
            FROM jobs
            WHERE LOWER(torbox_hash) IN ({placeholders})
               OR nzo_id IN ({placeholders})""",
        [h.lower() for h in hashes] + [h for h in hashes],
    )
    return await cursor.fetchall()


async def _find_job_by_hash(db: Database, info_hash: str) -> tuple | None:
    """Look up a single job by torbox_hash."""
    rows = await _find_jobs_by_hashes(db, [info_hash])
    return rows[0] if rows else None


def _parse_hashes(hashes_str: str) -> list[str]:
    """Parse pipe-separated hashes string. 'all' returns empty list."""
    if not hashes_str or hashes_str.lower() == "all":
        return []
    return [h.strip().lower() for h in hashes_str.split("|") if h.strip()]


# ------------------------------------------------------------------ #
#  Torrent list                                                         #
# ------------------------------------------------------------------ #


@router.get("/info")
async def torrents_info(
    request: Request,
    filter: str = Query("all", alias="filter"),
    category: str = Query(""),
    tag: str = Query(""),
    sort: str = Query(""),
    reverse: str = Query(""),
    limit: str = Query("0"),
    offset: str = Query("0"),
    hashes: str = Query(""),
    sid: str = Depends(require_sid),
    db: Database = Depends(get_db),
    config: ConfigStore = Depends(get_config),
):
    """List torrents with optional filtering, sorting, and pagination."""
    if not db or not db.conn:
        return JSONResponse(content=[])

    # Resolve complete_dir to an absolute path so *arr clients can apply
    # their own remote path mappings.  A relative config value like
    # "downloads/complete" becomes "/data/downloads/complete" in Docker.
    complete_dir = str(Path(
        await config.get("folders", "complete_dir", "downloads/complete")
    ).resolve())

    # Only show torrent-type jobs in the qBittorrent API.
    # Usenet and webdl jobs are managed through the SABnzbd API.
    cursor = await db.conn.execute(
        """SELECT nzo_id, filename, nzo_url, category, priority, status,
                  size, sizeleft, percentage, time_added, time_completed,
                  torbox_id, torbox_type, torbox_hash, speed, tags, position,
                  stalled_since, local_path
           FROM jobs WHERE torbox_type = 'torrent' ORDER BY position"""
    )

    rows = await cursor.fetchall()
    torrents = [build_torrent_info(row, save_path=complete_dir) for row in rows]

    # Apply hash filter
    hash_list = _parse_hashes(hashes)
    if hash_list:
        torrents = [t for t in torrents if t["hash"].lower() in hash_list]

    # Apply state filter
    if filter and filter != "all":
        torrents = [t for t in torrents if matches_filter(t["state"], filter)]

    # Apply category filter
    if category:
        torrents = [t for t in torrents if t["category"] == category]

    # Apply tag filter
    if tag:
        torrents = [t for t in torrents if tag in (t.get("tags", "") or "").split(",")]

    # Apply sort
    if sort:
        reverse_flag = reverse.lower() in ("true", "1")
        key = sort
        if key in ("added_on", "size", "progress", "dlspeed", "upspeed", "ratio", "eta", "priority"):
            torrents.sort(key=lambda t: t.get(key, 0), reverse=reverse_flag)
        elif key == "name":
            torrents.sort(key=lambda t: t.get("name", ""), reverse=reverse_flag)
        elif key == "state":
            torrents.sort(key=lambda t: t.get("state", ""), reverse=reverse_flag)

    # Apply offset and limit
    offset_val = int(offset) if offset else 0
    limit_val = int(limit) if limit else 0
    if offset_val > 0:
        torrents = torrents[offset_val:]
    if limit_val > 0:
        torrents = torrents[:limit_val]

    return JSONResponse(content=torrents)


# ------------------------------------------------------------------ #
#  Add torrent                                                          #
# ------------------------------------------------------------------ #


@router.post("/add")
async def torrents_add(
    request: Request,
    sid: str = Depends(require_sid),
    db: Database = Depends(get_db),
    config: ConfigStore = Depends(get_config),
):
    """Add one or more torrents by URL, magnet, or .torrent file upload."""
    torbox_api_key = await config.get("torbox", "api_key")
    if not torbox_api_key:
        return Response(content="Fails.", status_code=500, media_type="text/plain")

    base_url = await config.get("torbox", "base_url", "https://api.torbox.app/v1")

    form = await request.form()
    urls_str = str(form.get("urls", ""))
    category = str(form.get("category", "")) or "*"
    tags_str = str(form.get("tags", ""))
    paused = str(form.get("paused", "")).lower() in ("true", "1")
    savepath = str(form.get("savepath", ""))

    # Override category from savepath if provided
    if savepath and not category:
        category = savepath.rsplit("/", 1)[-1] if "/" in savepath else savepath

    errors = []

    # Process URLs (magnet links and HTTP URLs, newline-separated)
    if urls_str and urls_str.strip():
        for url in urls_str.strip().split("\n"):
            url = url.strip()
            if not url:
                continue
            try:
                nzo_id = generate_nzo_id()
                client = TorboxClient(api_key=torbox_api_key, base_url=base_url)

                if url.lower().startswith("magnet:?"):
                    result = await client.create_torrent(magnet=url)
                else:
                    result = await client.create_usenet_download(link=url)

                await client.close()

                if not result.success:
                    errors.append(url)
                    continue

                # Extract torbox_id
                torbox_id = _extract_torbox_id(result.data)

                # Insert into database
                await _insert_job(
                    db, nzo_id, url, category, tags_str, paused,
                    torbox_id, "torrent" if url.lower().startswith("magnet:?") else "usenet",
                    result,
                )
            except Exception:
                logger.exception("Failed to add torrent from URL: %s", url[:100])
                errors.append(url)

    # Process uploaded .torrent files
    for key, value in form.multi_items():
        if isinstance(value, UploadFile):
            file_content = await value.read()
            await value.close()
            file_name = value.filename or "upload.torrent"

            try:
                nzo_id = generate_nzo_id()
                client = TorboxClient(api_key=torbox_api_key, base_url=base_url)

                if file_name.lower().endswith(".torrent"):
                    result = await client.create_torrent(
                        file_data=file_content, file_name=file_name,
                    )
                    url_type = "torrent"
                else:
                    result = await client.create_usenet_download(
                        file_data=file_content, file_name=file_name,
                    )
                    url_type = "usenet"

                await client.close()

                if not result.success:
                    errors.append(file_name)
                    continue

                torbox_id = _extract_torbox_id(result.data)

                await _insert_job(
                    db, nzo_id, "", category, tags_str, paused,
                    torbox_id, url_type, result,
                    filename=file_name,
                )
            except Exception:
                logger.exception("Failed to add torrent from file: %s", file_name)
                errors.append(file_name)

    if errors:
        logger.warning("Some torrents failed to add: %s", errors)

    return Response(content="Ok.", media_type="text/plain")


def _extract_torbox_id(data: Any) -> int | None:
    """Extract a Torbox download ID from the API response data."""
    if isinstance(data, int) and not isinstance(data, bool):
        return data
    if isinstance(data, str) and data.strip().isdigit():
        return int(data.strip())
    if isinstance(data, dict):
        raw_id = data.get("torrent_id") or data.get("usenet_id") or data.get("web_id") or data.get("id")
        if raw_id is not None:
            try:
                return int(raw_id) if isinstance(raw_id, str) else int(raw_id)
            except (ValueError, TypeError):
                pass
    return None


async def _insert_job(
    db: Database, nzo_id: str, url: str, category: str, tags: str,
    paused: bool, torbox_id: int | None, url_type: str,
    result: Any, filename: str = "",
) -> None:
    """Insert a new job into the database after torrent creation."""
    if not db or not db.conn:
        return

    if not filename:
        if url.lower().startswith("magnet:?"):
            from urllib.parse import parse_qs, unquote
            fragment = url[8:]
            params = parse_qs(fragment)
            dn_values = params.get("dn", [])
            filename = unquote(dn_values[0]) if dn_values else "magnet_download"
        else:
            filename = url.rsplit("/", 1)[-1] if "/" in url else "download"

    status = "Paused" if paused else "Queued"
    now = time.time()
    torbox_hash = ""
    if isinstance(result.data, dict):
        torbox_hash = result.data.get("hash", "")

    try:
        cursor = await db.conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM jobs")
        row = await cursor.fetchone()
        position = row[0] if row else 0
    except Exception:
        position = 0

    try:
        await db.conn.execute(
            """INSERT INTO jobs (
                nzo_id, filename, password, nzo_url, category, script, priority, pp,
                status, size, sizeleft, percentage, time_added, avg_age,
                torbox_id, torbox_type, torbox_hash, position, torbox_state, tags
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                nzo_id, filename, "", url, category, "Default", 0, -1,
                status, 0, 0, 0, now, "",
                str(torbox_id) if torbox_id else None, url_type, torbox_hash,
                position, "queued" if not paused else "paused", tags,
            ),
        )
        await db.conn.commit()
        logger.info("qbit: created job nzo_id=%s type=%s torbox_id=%s", nzo_id, url_type, torbox_id)
    except Exception:
        logger.exception("qbit: failed to insert job nzo_id=%s", nzo_id)


# ------------------------------------------------------------------ #
#  Pause / Resume / Delete                                              #
# ------------------------------------------------------------------ #


@router.post("/stop")
async def torrents_stop(
    request: Request,
    sid: str = Depends(require_sid),
    db: Database = Depends(get_db),
):
    """Pause (stop) torrents by hash. Local-only — Torbox doesn't support torrent pause."""
    form = await request.form()
    hashes = _parse_hashes(str(form.get("hashes", "")))

    if not db or not db.conn:
        return Response(content="Ok.", media_type="text/plain")

    if hashes:
        placeholders = ",".join("?" for _ in hashes)
        await db.conn.execute(
            f"UPDATE jobs SET status = 'Paused' WHERE LOWER(torbox_hash) IN ({placeholders}) AND status NOT IN ('Complete', 'Failed', 'Fetching')",
            [h.lower() for h in hashes],
        )
    else:
        # 'all' — pause all active torrent-type jobs (what the qBittorrent client sees)
        await db.conn.execute(
            "UPDATE jobs SET status = 'Paused' WHERE status IN ('Queued', 'Downloading') AND torbox_type = 'torrent'"
        )

    await db.conn.commit()
    return Response(content="Ok.", media_type="text/plain")


@router.post("/start")
async def torrents_start(
    request: Request,
    sid: str = Depends(require_sid),
    db: Database = Depends(get_db),
):
    """Resume (start) paused torrents."""
    form = await request.form()
    hashes = _parse_hashes(str(form.get("hashes", "")))

    if not db or not db.conn:
        return Response(content="Ok.", media_type="text/plain")

    if hashes:
        placeholders = ",".join("?" for _ in hashes)
        await db.conn.execute(
            f"UPDATE jobs SET status = 'Queued' WHERE LOWER(torbox_hash) IN ({placeholders}) AND status = 'Paused'",
            [h.lower() for h in hashes],
        )
    else:
        # 'all' — resume all paused torrent-type jobs (what the qBittorrent client sees)
        await db.conn.execute(
            "UPDATE jobs SET status = 'Queued' WHERE status = 'Paused' AND torbox_type = 'torrent'"
        )

    await db.conn.commit()
    return Response(content="Ok.", media_type="text/plain")


@router.post("/delete")
async def torrents_delete(
    request: Request,
    sid: str = Depends(require_sid),
    db: Database = Depends(get_db),
    config: ConfigStore = Depends(get_config),
):
    """Delete torrents by hash.

    Only cancels ACTIVE downloads on Torbox (still downloading/queued).
    Completed/failed downloads are kept on Torbox so the user retains
    access to their files.

    If ``deleteFiles=true``, also removes the local downloaded file
    from disk (matching real qBittorrent behavior).
    """
    form = await request.form()
    hashes = _parse_hashes(str(form.get("hashes", "")))
    delete_files = str(form.get("deleteFiles", "")).lower() in ("true", "1")

    if not db or not db.conn:
        return Response(content="Ok.", media_type="text/plain")

    torbox_api_key = await config.get("torbox", "api_key")
    base_url = await config.get("torbox", "base_url", "https://api.torbox.app/v1")

    # Find matching jobs
    if hashes:
        rows = await _find_jobs_by_hashes(db, hashes)
    else:
        # 'all' — delete all torrent-type jobs (what the qBittorrent client sees)
        cursor = await db.conn.execute(
            """SELECT nzo_id, filename, nzo_url, category, priority, status,
                      size, sizeleft, percentage, time_added, time_completed,
                      torbox_id, torbox_type, torbox_hash, speed, tags, position
               FROM jobs WHERE torbox_type = 'torrent'"""
        )
        rows = await cursor.fetchall()

    # Cancel ACTIVE downloads on Torbox and collect nzo_ids for deletion.
    # Only cancel downloads that are still in progress — completed/failed
    # downloads are kept on Torbox so the user retains access.
    nzo_ids = []
    for row in rows:
        nzo_id = row[0]
        status = row[5]
        torbox_id = row[11]
        torbox_type = row[12]
        nzo_ids.append(nzo_id)

        if torbox_id and torbox_api_key and status not in ("Complete", "Failed"):
            try:
                client = TorboxClient(api_key=torbox_api_key, base_url=base_url)
                if torbox_type == "torrent":
                    await client.control_torrent(int(torbox_id), "Delete")
                elif torbox_type == "usenet":
                    await client.control_usenet_download(int(torbox_id), "Delete")
                elif torbox_type == "webdl":
                    await client.control_web_download(int(torbox_id), "Delete")
                await client.close()
                logger.info("qbit delete: cancelled active Torbox %s id=%s", torbox_type, torbox_id)
            except Exception:
                logger.warning("Failed to cancel torbox_id=%s (type=%s)", torbox_id, torbox_type, exc_info=True)

        # If deleteFiles=true, remove the local downloaded file from disk
        if delete_files and status in ("Complete", "Fetching"):
            local_path = row[17] if len(row) > 17 else ""
            if local_path and not local_path.startswith(("http://", "https://")):
                try:
                    from pathlib import Path as PathLib
                    p = PathLib(local_path)
                    if p.exists():
                        p.unlink()
                        logger.info("qbit delete: removed local file %s", local_path)
                except Exception:
                    logger.warning("qbit delete: failed to remove %s", local_path, exc_info=True)

    # Delete from database
    if nzo_ids:
        placeholders = ",".join("?" for _ in nzo_ids)
        await db.conn.execute(
            f"DELETE FROM jobs WHERE nzo_id IN ({placeholders})", nzo_ids
        )
        await db.conn.commit()

    return Response(content="Ok.", media_type="text/plain")


# ------------------------------------------------------------------ #
#  Properties / Files / Trackers                                        #
# ------------------------------------------------------------------ #


@router.get("/properties")
async def torrents_properties(
    request: Request,
    hash: str = Query(""),
    sid: str = Depends(require_sid),
    db: Database = Depends(get_db),
    config: ConfigStore = Depends(get_config),
):
    """Get detailed properties for a single torrent."""
    if not hash or not db or not db.conn:
        return JSONResponse(content={})

    row = await _find_job_by_hash(db, hash)
    if not row:
        return JSONResponse(content={})

    nzo_id, filename, nzo_url, category, priority, status = row[:6]
    size, sizeleft, percentage, time_added, time_completed = row[6:11]
    torbox_id, torbox_type, torbox_hash, speed, tags = row[11:16]
    local_path = row[17] if len(row) > 17 else ""

    # Safety net: never expose CDN URLs as file paths.
    if local_path.startswith(("http://", "https://")):
        local_path = ""

    save_path = str(Path(
        await config.get("folders", "complete_dir", "downloads/complete")
    ).resolve())
    if category and category != "*":
        cursor = await db.conn.execute(
            "SELECT dir FROM categories WHERE name = ?", (category,)
        )
        cat_row = await cursor.fetchone()
        if cat_row and cat_row[0]:
            save_path = str(Path(cat_row[0]).resolve())
        else:
            save_path = str(Path(f"{save_path}/{category}").resolve())

    dloaded = size - sizeleft if size and sizeleft else 0
    ratio = 0.0
    elapsed = (time.time() - time_added) if time_added else 0

    # Use local_path for save_path if the file has been downloaded,
    # so *arr clients can find the actual file location.
    if local_path:
        resolved_save_path = str(Path(local_path).parent.resolve()) if local_path else save_path
    else:
        resolved_save_path = save_path

    props = {
        "save_path": resolved_save_path,
        "creation_date": int(time_added) if time_added else 0,
        "piece_size": 0,
        "comment": "",
        "total_wasted": 0,
        "total_uploaded": 0,
        "total_downloaded": int(dloaded),
        "up_limit": -1,
        "dl_limit": -1,
        "time_elapsed": int(elapsed),
        "seeding_time": 0,
        "nb_connections": 0,
        "share_ratio": ratio,
        "addition_date": int(time_added) if time_added else 0,
        "completion_date": int(time_completed) if time_completed and time_completed > 0 else -1,
        "created_by": "DebridNZBd",
        "dl_speed_avg": int(speed) if speed else 0,
        "dl_speed": int(speed) if speed else 0,
        "up_speed_avg": 0,
        "up_speed": 0,
        "eta": 8640000,
        "last_seen": int(time.time()) if status == "Complete" else 0,
        "peers": 0,
        "peers_total": 0,
        "pieces_have": 0,
        "pieces_num": 0,
        "reannounce": 0,
        "seeds": 0,
        "seeds_total": 0,
        "total_size": int(size) if size else 0,
        "isPrivate": False,
    }

    return JSONResponse(content=props)


@router.get("/files")
async def torrents_files(
    request: Request,
    hash: str = Query(""),
    sid: str = Depends(require_sid),
    db: Database = Depends(get_db),
    config: ConfigStore = Depends(get_config),
):
    """Get the file list for a torrent. Queries Torbox for real file data."""
    if not hash or not db or not db.conn:
        return JSONResponse(content=[])

    row = await _find_job_by_hash(db, hash)
    if not row:
        return JSONResponse(content=[])

    torbox_id = row[11]
    torbox_type = row[12]

    if not torbox_id:
        return JSONResponse(content=[])

    # Query Torbox for file list
    torbox_api_key = await config.get("torbox", "api_key")
    base_url = await config.get("torbox", "base_url", "https://api.torbox.app/v1")

    if not torbox_api_key:
        return JSONResponse(content=[])

    try:
        client = TorboxClient(api_key=torbox_api_key, base_url=base_url)
        if torbox_type == "torrent":
            downloads = await client.get_torrent_list(torrent_id=int(torbox_id))
        elif torbox_type == "usenet":
            downloads = await client.get_usenet_list(usenet_id=int(torbox_id))
        else:
            downloads = []
        await client.close()

        files = []
        if downloads:
            dl = downloads[0]
            raw_files = getattr(dl, "files", []) or []
            for idx, f in enumerate(raw_files):
                if isinstance(f, dict):
                    files.append({
                        "index": idx,
                        "name": f.get("name", f"file_{idx}"),
                        "size": f.get("size", 0),
                        "progress": 1.0 if status == "Complete" else 0.0,
                        "priority": 1,
                        "is_seed": False,
                        "piece_range": [0, 0],
                        "availability": 0.0,
                    })

        return JSONResponse(content=files)
    except Exception:
        logger.exception("Failed to get files for torbox_id=%s", torbox_id)
        return JSONResponse(content=[])


@router.get("/trackers")
async def torrents_trackers(
    request: Request,
    hash: str = Query(""),
    sid: str = Depends(require_sid),
    db: Database = Depends(get_db),
):
    """Get tracker list for a torrent. Returns minimal stub data."""
    # qBittorrent expects at least DHT/LSD/PeX entries
    return JSONResponse(content=[
        {"url": "** [DHT] **", "status": 2, "tier": 0, "num_peers": 0, "num_seeds": 0, "num_leechers": 0, "num_downloaded": 0, "msg": ""},
        {"url": "** [PeX] **", "status": 2, "tier": 0, "num_peers": 0, "num_seeds": 0, "num_leechers": 0, "num_downloaded": 0, "msg": ""},
        {"url": "** [LSD] **", "status": 2, "tier": 0, "num_peers": 0, "num_seeds": 0, "num_leechers": 0, "num_downloaded": 0, "msg": ""},
    ])


@router.post("/reannounce")
async def torrents_reannounce(
    request: Request,
    sid: str = Depends(require_sid),
    db: Database = Depends(get_db),
    config: ConfigStore = Depends(get_config),
):
    """Reannounce torrents. Calls Torbox control API."""
    form = await request.form()
    hashes = _parse_hashes(str(form.get("hashes", "")))

    if not db or not db.conn or not hashes:
        return Response(content="Ok.", media_type="text/plain")

    torbox_api_key = await config.get("torbox", "api_key")
    base_url = await config.get("torbox", "base_url", "https://api.torbox.app/v1")

    if not torbox_api_key:
        return Response(content="Ok.", media_type="text/plain")

    rows = await _find_jobs_by_hashes(db, hashes)
    for row in rows:
        torbox_id = row[11]
        torbox_type = row[12]
        if torbox_id and torbox_type == "torrent":
            try:
                client = TorboxClient(api_key=torbox_api_key, base_url=base_url)
                await client.control_torrent(int(torbox_id), "Reannounce")
                await client.close()
            except Exception:
                logger.warning("Failed to reannounce torbox_id=%s", torbox_id, exc_info=True)

    return Response(content="Ok.", media_type="text/plain")


# ------------------------------------------------------------------ #
#  Category management                                                  #
# ------------------------------------------------------------------ #


@router.get("/categories")
async def torrents_categories(
    request: Request,
    sid: str = Depends(require_sid),
    db: Database = Depends(get_db),
    config: ConfigStore = Depends(get_config),
):
    """List all categories with their save paths."""
    if not db or not db.conn:
        return JSONResponse(content={})

    complete_dir = str(Path(
        await config.get("folders", "complete_dir", "downloads/complete")
    ).resolve())

    cursor = await db.conn.execute("SELECT name, dir FROM categories ORDER BY name")
    rows = await cursor.fetchall()

    categories = {}
    for name, cat_dir in rows:
        if cat_dir:
            save_path = str(Path(cat_dir).resolve())
        else:
            save_path = str(Path(f"{complete_dir}/{name}").resolve())
        categories[name] = {"name": name, "savePath": save_path}

    return JSONResponse(content=categories)


@router.post("/setCategory")
async def torrents_set_category(
    request: Request,
    sid: str = Depends(require_sid),
    db: Database = Depends(get_db),
):
    """Set the category for one or more torrents."""
    form = await request.form()
    hashes = _parse_hashes(str(form.get("hashes", "")))
    category = str(form.get("category", ""))

    if not db or not db.conn:
        return Response(content="Ok.", media_type="text/plain")

    if hashes:
        placeholders = ",".join("?" for _ in hashes)
        await db.conn.execute(
            f"UPDATE jobs SET category = ? WHERE LOWER(torbox_hash) IN ({placeholders})",
            [category] + [h.lower() for h in hashes],
        )
    else:
        # 'all' — set category on all torrent-type jobs
        await db.conn.execute("UPDATE jobs SET category = ? WHERE torbox_type = 'torrent'", (category,))

    await db.conn.commit()
    return Response(content="Ok.", media_type="text/plain")


@router.post("/createCategory")
async def torrents_create_category(
    request: Request,
    sid: str = Depends(require_sid),
    db: Database = Depends(get_db),
):
    """Create a new category."""
    form = await request.form()
    category = str(form.get("category", ""))
    save_path = str(form.get("savePath", ""))

    if not category or not db or not db.conn:
        return Response(content="Ok.", media_type="text/plain")

    try:
        await db.conn.execute(
            "INSERT OR IGNORE INTO categories (name, dir, script, priority) VALUES (?, ?, 'Default', 0)",
            (category, save_path),
        )
        await db.conn.commit()
    except Exception:
        logger.exception("Failed to create category %s", category)

    return Response(content="Ok.", media_type="text/plain")


@router.post("/removeCategories")
async def torrents_remove_categories(
    request: Request,
    sid: str = Depends(require_sid),
    db: Database = Depends(get_db),
):
    """Remove one or more categories (newline-separated)."""
    form = await request.form()
    categories_str = str(form.get("categories", ""))

    if not categories_str or not db or not db.conn:
        return Response(content="Ok.", media_type="text/plain")

    for cat_name in categories_str.split("\n"):
        cat_name = cat_name.strip()
        if cat_name and cat_name != "*":
            try:
                await db.conn.execute("DELETE FROM categories WHERE name = ?", (cat_name,))
            except Exception:
                logger.warning("Failed to remove category %s", cat_name)

    await db.conn.commit()
    return Response(content="Ok.", media_type="text/plain")


# ------------------------------------------------------------------ #
#  Tag management                                                       #
# ------------------------------------------------------------------ #


@router.get("/tags")
async def torrents_tags(
    request: Request,
    sid: str = Depends(require_sid),
    db: Database = Depends(get_db),
):
    """List all unique tags across all jobs."""
    if not db or not db.conn:
        return JSONResponse(content=[])

    # Only show tags from torrent-type jobs (what the qBittorrent client sees)
    cursor = await db.conn.execute("SELECT tags FROM jobs WHERE tags IS NOT NULL AND tags != '' AND torbox_type = 'torrent'")
    rows = await cursor.fetchall()

    all_tags: set[str] = set()
    for row in rows:
        if row[0]:
            for tag in row[0].split(","):
                tag = tag.strip()
                if tag:
                    all_tags.add(tag)

    return JSONResponse(content=sorted(all_tags))


@router.post("/addTags")
async def torrents_add_tags(
    request: Request,
    sid: str = Depends(require_sid),
    db: Database = Depends(get_db),
):
    """Add tags to matching torrents."""
    form = await request.form()
    hashes = _parse_hashes(str(form.get("hashes", "")))
    tags_str = str(form.get("tags", ""))

    if not tags_str or not db or not db.conn:
        return Response(content="Ok.", media_type="text/plain")

    new_tags = [t.strip() for t in tags_str.split(",") if t.strip()]

    if hashes:
        rows = await _find_jobs_by_hashes(db, hashes)
    else:
        # 'all' — add tags to all torrent-type jobs
        cursor = await db.conn.execute("SELECT nzo_id, tags FROM jobs WHERE torbox_type = 'torrent'")
        rows = await cursor.fetchall()

    for row in rows:
        nzo_id = row[0]
        existing_tags = [t.strip() for t in (row[1] or "").split(",") if t.strip()]
        merged = list(dict.fromkeys(existing_tags + new_tags))  # dedupe, preserve order
        await db.conn.execute(
            "UPDATE jobs SET tags = ? WHERE nzo_id = ?",
            (",".join(merged), nzo_id),
        )

    await db.conn.commit()
    return Response(content="Ok.", media_type="text/plain")


@router.post("/removeTags")
async def torrents_remove_tags(
    request: Request,
    sid: str = Depends(require_sid),
    db: Database = Depends(get_db),
):
    """Remove tags from matching torrents."""
    form = await request.form()
    hashes = _parse_hashes(str(form.get("hashes", "")))
    tags_str = str(form.get("tags", ""))

    if not tags_str or not db or not db.conn:
        return Response(content="Ok.", media_type="text/plain")

    remove_tags = set(t.strip() for t in tags_str.split(",") if t.strip())

    if hashes:
        rows = await _find_jobs_by_hashes(db, hashes)
    else:
        # 'all' — remove tags from all torrent-type jobs
        cursor = await db.conn.execute("SELECT nzo_id, tags FROM jobs WHERE torbox_type = 'torrent'")
        rows = await cursor.fetchall()

    for row in rows:
        nzo_id = row[0]
        existing_tags = [t.strip() for t in (row[1] or "").split(",") if t.strip()]
        filtered = [t for t in existing_tags if t not in remove_tags]
        await db.conn.execute(
            "UPDATE jobs SET tags = ? WHERE nzo_id = ?",
            (",".join(filtered), nzo_id),
        )

    await db.conn.commit()
    return Response(content="Ok.", media_type="text/plain")


@router.post("/createTags")
async def torrents_create_tags(
    request: Request,
    sid: str = Depends(require_sid),
):
    """Create tags (no-op — tags exist when assigned to torrents)."""
    return Response(content="Ok.", media_type="text/plain")


@router.post("/deleteTags")
async def torrents_delete_tags(
    request: Request,
    sid: str = Depends(require_sid),
    db: Database = Depends(get_db),
):
    """Delete tags from all torrents that have them."""
    form = await request.form()
    tags_str = str(form.get("tags", ""))

    if not tags_str or not db or not db.conn:
        return Response(content="Ok.", media_type="text/plain")

    remove_tags = set(t.strip() for t in tags_str.split(",") if t.strip())

    cursor = await db.conn.execute("SELECT nzo_id, tags FROM jobs WHERE tags IS NOT NULL AND tags != ''")
    rows = await cursor.fetchall()

    for row in rows:
        nzo_id = row[0]
        existing_tags = [t.strip() for t in (row[1] or "").split(",") if t.strip()]
        filtered = [t for t in existing_tags if t not in remove_tags]
        await db.conn.execute(
            "UPDATE jobs SET tags = ? WHERE nzo_id = ?",
            (",".join(filtered), nzo_id),
        )

    await db.conn.commit()
    return Response(content="Ok.", media_type="text/plain")


# ------------------------------------------------------------------ #
#  Stubbed endpoints (accept and ignore)                               #
# ------------------------------------------------------------------ #


@router.post("/filePrio")
async def torrents_file_prio(sid: str = Depends(require_sid)):
    """Set file priority — stubbed (Torbox manages file selection)."""
    return Response(content="Ok.", media_type="text/plain")


@router.post("/downloadLimit")
async def torrents_download_limit(
    request: Request,
    sid: str = Depends(require_sid),
    db: Database = Depends(get_db),
):
    """Get per-torrent download limits."""
    return JSONResponse(content={})


@router.post("/uploadLimit")
async def torrents_upload_limit(
    request: Request,
    sid: str = Depends(require_sid),
):
    """Get per-torrent upload limits."""
    return JSONResponse(content={})


@router.post("/setDownloadLimit")
async def torrents_set_download_limit(sid: str = Depends(require_sid)):
    """Set per-torrent download limit — stubbed."""
    return Response(content="Ok.", media_type="text/plain")


@router.post("/setUploadLimit")
async def torrents_set_upload_limit(sid: str = Depends(require_sid)):
    """Set per-torrent upload limit — stubbed."""
    return Response(content="Ok.", media_type="text/plain")


@router.post("/setShareLimits")
async def torrents_set_share_limits(sid: str = Depends(require_sid)):
    """Set share limits — stubbed."""
    return Response(content="Ok.", media_type="text/plain")


@router.post("/setLocation")
async def torrents_set_location(sid: str = Depends(require_sid)):
    """Set save location — stubbed."""
    return Response(content="Ok.", media_type="text/plain")


@router.post("/rename")
async def torrents_rename(sid: str = Depends(require_sid)):
    """Rename torrent — stubbed."""
    return Response(content="Ok.", media_type="text/plain")


@router.post("/setForceStart")
async def torrents_set_force_start(sid: str = Depends(require_sid)):
    """Set force start — stubbed."""
    return Response(content="Ok.", media_type="text/plain")


@router.post("/setSuperSeeding")
async def torrents_set_super_seeding(sid: str = Depends(require_sid)):
    """Set super seeding — stubbed."""
    return Response(content="Ok.", media_type="text/plain")


@router.post("/toggleSequentialDownload")
async def torrents_toggle_sequential(sid: str = Depends(require_sid)):
    """Toggle sequential download — stubbed."""
    return Response(content="Ok.", media_type="text/plain")


@router.post("/toggleFirstLastPiecePrio")
async def torrents_toggle_first_last_prio(sid: str = Depends(require_sid)):
    """Toggle first/last piece priority — stubbed."""
    return Response(content="Ok.", media_type="text/plain")


@router.post("/setAutoManagement")
async def torrents_set_auto_management(sid: str = Depends(require_sid)):
    """Set automatic torrent management — stubbed."""
    return Response(content="Ok.", media_type="text/plain")


@router.post("/increasePrio")
async def torrents_increase_prio(sid: str = Depends(require_sid)):
    """Increase priority — stubbed."""
    return Response(content="Ok.", media_type="text/plain")


@router.post("/decreasePrio")
async def torrents_decrease_prio(sid: str = Depends(require_sid)):
    """Decrease priority — stubbed."""
    return Response(content="Ok.", media_type="text/plain")


@router.post("/topPrio")
async def torrents_top_prio(sid: str = Depends(require_sid)):
    """Move to top priority — stubbed."""
    return Response(content="Ok.", media_type="text/plain")


@router.post("/bottomPrio")
async def torrents_bottom_prio(sid: str = Depends(require_sid)):
    """Move to bottom priority — stubbed."""
    return Response(content="Ok.", media_type="text/plain")


@router.post("/addTrackers")
async def torrents_add_trackers(sid: str = Depends(require_sid)):
    """Add trackers — stubbed."""
    return Response(content="Ok.", media_type="text/plain")


@router.post("/editTracker")
async def torrents_edit_tracker(sid: str = Depends(require_sid)):
    """Edit tracker — stubbed."""
    return Response(content="Ok.", media_type="text/plain")


@router.post("/removeTrackers")
async def torrents_remove_trackers(sid: str = Depends(require_sid)):
    """Remove trackers — stubbed."""
    return Response(content="Ok.", media_type="text/plain")


@router.post("/addPeers")
async def torrents_add_peers(sid: str = Depends(require_sid)):
    """Add peers — stubbed."""
    return Response(content="Ok.", media_type="text/plain")


@router.get("/webseeds")
async def torrents_webseeds(
    hash: str = Query(""),
    sid: str = Depends(require_sid),
):
    """Get web seeds — stubbed."""
    return JSONResponse(content=[])


@router.get("/pieceStates")
async def torrents_piece_states(
    hash: str = Query(""),
    sid: str = Depends(require_sid),
):
    """Get piece states — stubbed."""
    return JSONResponse(content=[])


@router.get("/pieceHashes")
async def torrents_piece_hashes(
    hash: str = Query(""),
    sid: str = Depends(require_sid),
):
    """Get piece hashes — stubbed."""
    return JSONResponse(content=[])