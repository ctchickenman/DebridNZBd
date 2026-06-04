"""SABnzbd-compatible API handlers for queue operations.

Implements the addurl mode for submitting NZB URLs, magnet links,
and web download URLs to DebridNZBd via the Torbox debrid service,
plus the queue mode for listing active downloads and various control
modes (pause, resume, delete, etc.).

URL type detection routes submissions to the appropriate Torbox API:
- magnet:? links → Torbox torrent endpoint
- .nzb URLs → Torbox usenet endpoint
- Other URLs → Torbox web download endpoint (or configured default)

The response format matches SABnzbd's API so *arr clients can connect
without modification.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import urlparse, parse_qs, unquote

from fastapi.responses import JSONResponse

from debridnzbd.db.models import QueueResponse, QueueSlot
from debridnzbd.torbox.client import TorboxClient
from debridnzbd.torbox.exceptions import (
    TorboxAuthError,
    TorboxConnectionError,
    TorboxError,
    TorboxRateLimitError,
)
from debridnzbd.utils.format import format_size, format_speed, format_timeleft
from debridnzbd.utils.nzo_id import generate_nzo_id

logger = logging.getLogger(__name__)

# Valid download types that can be configured as default
VALID_TYPES = {"usenet", "torrent", "webdl"}

# Bytes per MB — used to convert between SABnzbd's MB and our bytes
BYTES_PER_MB = 1048576


def detect_url_type(url: str, default_type: str = "usenet") -> str:
    """Detect the download type from the URL pattern.

    Classification rules (in order of precedence):
    1. URLs starting with ``magnet:?`` → "torrent"
    2. URLs with ``.nzb`` extension or ``/nzb/`` in the path → "usenet"
    3. All other URLs → the configured default type

    Args:
        url: The download URL to classify.
        default_type: Fallback type when the URL doesn't match known
            patterns. Should be one of "usenet", "torrent", or "webdl".

    Returns:
        One of "usenet", "torrent", or "webdl".
    """
    url = url.strip()

    # Magnet links are always torrents
    if url.lower().startswith("magnet:?"):
        return "torrent"

    # Parse the URL to check the path
    parsed = urlparse(url)
    path_lower = parsed.path.lower()

    # .nzb extension → usenet
    if path_lower.endswith(".nzb"):
        return "usenet"

    # /nzb/ in the path → usenet
    if "/nzb/" in path_lower:
        return "usenet"

    # Fall back to configured default type
    if default_type not in VALID_TYPES:
        default_type = "usenet"
    return default_type


def _derive_filename(url: str, nzbname: str | None = None) -> str:
    """Derive a display filename from the URL and optional nzbname.

    If nzbname is provided, it's used directly. Otherwise:
    - Magnet links: extract the ``dn=`` parameter, or fall back to
      "magnet_download"
    - HTTP URLs: use the last path component, or fall back to "download"

    Note: This function produces a URL-derived fallback name that may not
    be meaningful for indexer API URLs (e.g. ``?mode=addurl&name=<NZB_URL>``).
    The caller should prefer the actual download name from Torbox when
    available — see :func:`_fetch_download_name`.

    Args:
        url: The source URL.
        nzbname: Optional custom name from the client.

    Returns:
        A human-readable filename string.
    """
    if nzbname and nzbname.strip():
        return nzbname.strip()

    url = url.strip()

    # Magnet link — try to extract display name
    if url.lower().startswith("magnet:?"):
        # Parse magnet URI parameters: magnet:?xt=...&dn=DisplayName&...
        fragment = url[8:]  # Remove "magnet:?"
        params = parse_qs(fragment)
        dn_values = params.get("dn", [])
        if dn_values:
            return unquote(dn_values[0])
        return "magnet_download"

    # HTTP/HTTPS URL — use last path component
    parsed = urlparse(url)
    filename = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    if filename:
        return unquote(filename)

    return "download"


def _extract_download_name(dl, url_type: str) -> str | None:
    """Extract a display name from a Torbox download object.

    For torrents and web downloads, the ``name`` field is used directly.
    For usenet downloads (which lack a top-level ``name`` field), the
    directory component of the first file's path is used as the release
    name — e.g. ``"Release.Name.Group/file.mp4"`` → ``"Release.Name.Group"``.

    Args:
        dl: A TorboxUsenetDownload, TorboxTorrentDownload, or
            TorboxWebDownload model instance.
        url_type: The download type (``"usenet"``, ``"torrent"``, or
            ``"webdl"``).

    Returns:
        A display name string, or None if no name could be extracted.
    """
    # Torrents and web downloads have a direct name field
    if url_type in ("torrent", "webdl"):
        name = getattr(dl, "name", None)
        if name and name.strip():
            return name.strip()

    # Usenet downloads have a files list with path-like names
    if url_type == "usenet":
        files = getattr(dl, "files", None) or []
        if files and isinstance(files[0], dict):
            file_path = files[0].get("name", "")
            if file_path:
                # File names look like "Release.Name.Group/file.ext"
                # Extract the directory (release) name
                if "/" in file_path:
                    dir_name = file_path.split("/")[0]
                    if dir_name.strip():
                        return dir_name.strip()
                # Fallback: use the filename without extension
                base = file_path.rsplit(".", 1)[0] if "." in file_path else file_path
                if base.strip():
                    return base.strip()

    return None


async def _fetch_download_name(
    api_key: str, base_url: str, torbox_id: int, url_type: str,
) -> str | None:
    """Fetch the actual download name from Torbox after creation.

    Queries the Torbox mylist endpoint for the specific download to
    extract its display name. Returns None if the name cannot be
    determined (e.g. download not yet processed by Torbox).

    Args:
        api_key: Torbox API key.
        base_url: Torbox API base URL.
        torbox_id: The Torbox download ID to look up.
        url_type: The download type (``"usenet"``, ``"torrent"``, or
            ``"webdl"``).

    Returns:
        The display name string, or None if unavailable.
    """
    try:
        client = TorboxClient(api_key=api_key, base_url=base_url)
        try:
            if url_type == "usenet":
                downloads = await client.get_usenet_list(
                    bypass_cache=True, usenet_id=torbox_id,
                )
            elif url_type == "torrent":
                downloads = await client.get_torrent_list(
                    bypass_cache=True, torrent_id=torbox_id,
                )
            else:
                downloads = await client.get_web_download_list(
                    bypass_cache=True, web_id=torbox_id,
                )

            if downloads:
                name = _extract_download_name(downloads[0], url_type)
                if name:
                    logger.info(
                        "addurl: fetched name from Torbox for id=%s type=%s: %r",
                        torbox_id, url_type, name,
                    )
                    return name
        finally:
            await client.close()
    except Exception:
        logger.debug(
            "addurl: could not fetch download name from Torbox for id=%s type=%s",
            torbox_id, url_type, exc_info=True,
        )
    return None


async def handle_addurl(params: dict) -> JSONResponse:
    """Handle ?mode=addurl — submit a download URL to Torbox.

    Accepts an NZB URL, magnet link, or web download URL and submits it
    to the appropriate Torbox API endpoint. Creates a local job entry in
    the database for tracking.

    SABnzbd-compatible parameters:
        name: The URL to download (required)
        cat / category: Download category (default: "*")
        priority: Priority (-100=paused, 0=normal, 1=low, 2=high)
        nzbname: Custom display name for the job
        pp: Post-processing option (-1=default, 0=none, 1=repair, 2=unpack, 3=unpack+delete)
        password: NZB password
        script: Post-processing script

    Returns:
        JSONResponse with SABnzbd-compatible format:
        Success: {"status": true, "nzo_ids": ["SABnzbd_nzo_..."]}
        Failure: {"status": false, "error": "error message"}
    """
    request = params.get("request")

    # --- Extract URL from params (already merged from query string and form body by router) ---
    url = params.get("name") or ""
    url = url.strip() if url else ""

    if not url:
        return JSONResponse(
            status_code=400,
            content={"status": False, "error": "No URL provided"},
        )

    # --- Get config and database from app state ---
    config = getattr(request.app.state, "config", None) if request else None
    db = getattr(request.app.state, "db", None) if request else None

    if config is None:
        return JSONResponse(
            status_code=500,
            content={"status": False, "error": "Server not initialized"},
        )

    # --- Read Torbox configuration ---
    torbox_api_key = await config.get("torbox", "api_key")
    if not torbox_api_key:
        return JSONResponse(
            status_code=500,
            content={"status": False, "error": "Torbox API key not configured. Set it in Config → Torbox."},
        )

    base_url = await config.get("torbox", "base_url", "https://api.torbox.app/v1")
    default_type = await config.get("torbox", "default_type", "usenet")
    post_processing = int(params.get("pp") or -1)

    # --- Detect URL type and generate job ID ---
    url_type = detect_url_type(url, default_type)
    nzo_id = generate_nzo_id()
    filename = _derive_filename(url, params.get("nzbname"))

    logger.info("addurl: nzo_id=%s type=%s url=%s", nzo_id, url_type, url[:100])

    # --- Submit to Torbox ---
    client = TorboxClient(api_key=torbox_api_key, base_url=base_url)
    torbox_id = None
    torbox_hash = ""

    try:
        if url_type == "torrent":
            result = await client.create_torrent(magnet=url)
        elif url_type == "webdl":
            result = await client.create_web_download(link=url)
        else:  # usenet
            result = await client.create_usenet_download(
                link=url,
                post_processing=post_processing,
            )

        if not result.success:
            error_msg = result.detail or "Unknown error from Torbox"
            logger.warning("addurl: Torbox rejected nzo_id=%s: %s", nzo_id, error_msg)
            return JSONResponse(
                status_code=502,
                content={"status": False, "error": f"Torbox error: {error_msg}"},
            )

        # Extract Torbox download ID from response data.
        # The response shape varies by endpoint: int, dict with "id", or dict with
        # type-specific keys like "usenet_id" or "torrent_id".  The Torbox API
        # may also return the ID as a numeric string (e.g. "12345") which we
        # convert to int.
        data = result.data
        logger.info(
            "addurl: Torbox create response for nzo_id=%s type=%s: "
            "data_type=%s data=%s detail=%s",
            nzo_id, url_type, type(data).__name__,
            repr(data)[:300] if data is not None else "None",
            result.detail[:200] if result.detail else "",
        )

        if isinstance(data, int) and not isinstance(data, bool):
            torbox_id = data
        elif isinstance(data, str) and data.strip().isdigit():
            torbox_id = int(data.strip())
        elif isinstance(data, dict):
            raw_id = (
                data.get("usenet_id")
                or data.get("torrent_id")
                or data.get("web_id")
                or data.get("id")
            )
            if raw_id is not None:
                try:
                    torbox_id = int(raw_id) if isinstance(raw_id, str) else int(raw_id)
                except (ValueError, TypeError):
                    logger.warning("addurl: could not parse torbox_id from %r", raw_id)
            torbox_hash = data.get("hash", "")
        elif isinstance(data, list) and data:
            # Some responses return a list with the created item
            first = data[0] if isinstance(data[0], dict) else {}
            raw_id = (
                first.get("usenet_id")
                or first.get("torrent_id")
                or first.get("web_id")
                or first.get("id")
            )
            if raw_id is not None:
                try:
                    torbox_id = int(raw_id) if isinstance(raw_id, str) else int(raw_id)
                except (ValueError, TypeError):
                    pass
            torbox_hash = first.get("hash", "")

    except TorboxAuthError:
        logger.warning("addurl: Torbox auth failed for nzo_id=%s", nzo_id)
        return JSONResponse(
            status_code=502,
            content={"status": False, "error": "Torbox authentication failed — check your API key"},
        )
    except TorboxConnectionError:
        logger.warning("addurl: Cannot reach Torbox for nzo_id=%s", nzo_id)
        return JSONResponse(
            status_code=502,
            content={"status": False, "error": "Cannot connect to Torbox API"},
        )
    except TorboxRateLimitError:
        logger.warning("addurl: Torbox rate limited for nzo_id=%s", nzo_id)
        return JSONResponse(
            status_code=429,
            content={"status": False, "error": "Torbox rate limit exceeded, please retry later"},
        )
    except TorboxError as e:
        logger.error("addurl: Torbox error for nzo_id=%s: %s", nzo_id, e)
        return JSONResponse(
            status_code=502,
            content={"status": False, "error": f"Torbox error: {e}"},
        )
    except Exception as e:
        logger.exception("addurl: Unexpected error for nzo_id=%s", nzo_id)
        return JSONResponse(
            status_code=500,
            content={"status": False, "error": "Internal server error"},
        )
    finally:
        await client.close()

    # --- Fallback: query mylist to find torbox_id if creation response didn't include one ---
    if torbox_id is None:
        logger.info(
            "addurl: no torbox_id from creation response for nzo_id=%s type=%s, "
            "querying mylist to find it",
            nzo_id, url_type,
        )
        try:
            fallback_client = TorboxClient(api_key=torbox_api_key, base_url=base_url)
            try:
                # Get existing torbox_ids from the database so we don't claim
                # a download that's already matched to another local job.
                existing_ids: set[str] = set()
                if db and db.conn:
                    cursor = await db.conn.execute(
                        "SELECT torbox_id FROM jobs WHERE torbox_id IS NOT NULL AND torbox_id != ''"
                    )
                    for row in await cursor.fetchall():
                        existing_ids.add(str(row[0]))

                if url_type == "usenet":
                    recent_list = await fallback_client.get_usenet_list(bypass_cache=True)
                elif url_type == "torrent":
                    recent_list = await fallback_client.get_torrent_list(bypass_cache=True)
                else:
                    recent_list = await fallback_client.get_web_download_list(bypass_cache=True)

                if recent_list:
                    # Sort by ID descending — highest ID is most recently created.
                    # Skip downloads already claimed by other local jobs.
                    for dl in sorted(recent_list, key=lambda d: d.id, reverse=True):
                        if str(dl.id) in existing_ids:
                            continue
                        torbox_id = dl.id
                        if hasattr(dl, "hash") and dl.hash:
                            torbox_hash = dl.hash
                        logger.info(
                            "addurl: matched torbox_id=%s from mylist for nzo_id=%s type=%s",
                            torbox_id, nzo_id, url_type,
                        )
                        break
                    else:
                        logger.warning(
                            "addurl: all downloads in mylist already claimed for nzo_id=%s type=%s",
                            nzo_id, url_type,
                        )
                else:
                    logger.warning(
                        "addurl: mylist returned empty for nzo_id=%s type=%s",
                        nzo_id, url_type,
                    )
            finally:
                await fallback_client.close()
        except Exception:
            logger.warning(
                "addurl: failed to query mylist fallback for nzo_id=%s",
                nzo_id, exc_info=True,
            )

    # --- Resolve the display name from Torbox when nzbname was not provided ---
    # If the caller didn't supply an explicit nzbname, try to get the real
    # download name from Torbox.  This produces much better names than
    # extracting from an indexer URL (which yields meaningless path components
    # like "get" or "api").  If Torbox hasn't processed the download yet (e.g.
    # the files list is empty for usenet), the state sync poller will update
    # the filename on the next cycle.
    nzbname_param = params.get("nzbname")
    if not nzbname_param and torbox_id:
        real_name = await _fetch_download_name(
            torbox_api_key, base_url, torbox_id, url_type,
        )
        if real_name:
            filename = real_name
            logger.info(
                "addurl: using Torbox name %r instead of URL-derived %r for nzo_id=%s",
                real_name, _derive_filename(url), nzo_id,
            )

    # --- Insert job into database ---
    if db and db.conn:
        category = params.get("cat") or params.get("category") or "*"
        priority = int(params.get("priority") or 0)
        script = params.get("script") or "Default"
        password = params.get("password") or ""
        now = time.time()

        # Calculate next position in queue
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
                    torbox_id, torbox_type, torbox_hash, position, torbox_state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    nzo_id,
                    filename,
                    password,
                    url,
                    category,
                    script,
                    priority,
                    post_processing,
                    "Queued",
                    0,  # size — unknown until Torbox reports
                    0,  # sizeleft
                    0,  # percentage
                    now,
                    "",  # avg_age
                    str(torbox_id) if torbox_id else None,
                    url_type,
                    torbox_hash,
                    position,
                    "queued",  # torbox_state — initial status before first poll
                ),
            )
            await db.conn.commit()
            logger.info("addurl: created job nzo_id=%s type=%s torbox_id=%s", nzo_id, url_type, torbox_id)
        except Exception:
            logger.exception("addurl: failed to insert job nzo_id=%s into database", nzo_id)
            # Don't fail the request — the Torbox download was created successfully
            # and the state sync poller will reconcile it later.
    else:
        logger.warning("addurl: database not available, job nzo_id=%s not persisted", nzo_id)

    return JSONResponse(content={"status": True, "nzo_ids": [nzo_id]})


# ------------------------------------------------------------------ #
#  Queue listing                                                       #
# ------------------------------------------------------------------ #


async def handle_queue(params: dict) -> JSONResponse:
    """Handle ?mode=queue — return the current download queue.

    Returns a SABnzbd-compatible queue response with slot details,
    speed, size, and time estimates. *arr clients poll this endpoint
    to track download progress.

    Parameters:
        start: Start index for pagination (default 0)
        limit: Maximum number of slots to return (default 0 = all)
        sort: Sort field (ignored, always returns by position)
        dir: Sort direction (ignored)
        search: Filter by filename (not yet implemented)

    Returns:
        JSONResponse with nested queue structure matching SABnzbd format.
    """
    request = params.get("request")
    db = getattr(request.app.state, "db", None) if request else None
    config = getattr(request.app.state, "config", None) if request else None

    if db is None or db.conn is None:
        return JSONResponse(
            content={"status": True, "queue": QueueResponse().model_dump()},
        )

    start = int(params.get("start") or 0)
    limit = int(params.get("limit") or 0)

    # Read all active jobs ordered by position
    cursor = await db.conn.execute(
        "SELECT nzo_id, filename, password, nzo_url, category, script, priority, pp, "
        "status, size, sizeleft, percentage, time_added, avg_age, "
        "torbox_id, torbox_type, torbox_hash, torbox_state, cdn_link, "
        "local_path, position, labels, stage_log, fail_message, speed, download_time "
        "FROM jobs ORDER BY position"
    )
    rows = await cursor.fetchall()

    # Build queue slots
    slots = []
    total_speed = 0.0
    total_size = 0.0
    total_sizeleft = 0.0

    for row in rows:
        nzo_id = row[0]
        filename = row[1]
        category = row[4] or "*"
        script = row[5] or "Default"
        priority = row[6] or 0
        status = row[8] or "Queued"
        size = row[9] or 0
        sizeleft = row[10] or 0
        percentage = row[11] or 0
        time_added = row[12] or 0
        avg_age = row[13] or ""
        speed = row[24] or 0

        total_speed += speed
        total_size += size
        total_sizeleft += sizeleft

        mb = size / BYTES_PER_MB
        mbleft = sizeleft / BYTES_PER_MB

        # Estimate time left from speed and remaining size
        if speed > 0 and sizeleft > 0:
            timeleft = format_timeleft(sizeleft / speed)
        else:
            timeleft = "0:00:00"

        slots.append(QueueSlot(
            status=status,
            index=0,  # Will be set after filtering
            password="***",
            avg_age=avg_age,
            time_added=time_added,
            script=script,
            mb=round(mb, 2),
            mbleft=round(mbleft, 2),
            mbmissing=0,
            size=format_size(size),
            sizeleft=format_size(sizeleft),
            filename=filename,
            labels=[],
            priority=priority,
            cat=category,
            timeleft=timeleft,
            percentage=str(int(percentage)),
            nzo_id=nzo_id,
            unpackopts="",
        ))

    # Apply pagination
    if start > 0:
        slots = slots[start:]
    if limit > 0:
        slots = slots[:limit]

    # Re-index slots after pagination
    for i, slot in enumerate(slots):
        slot.index = start + i

    # Compute total time left
    if total_speed > 0 and total_sizeleft > 0:
        total_timeleft = format_timeleft(total_sizeleft / total_speed)
    else:
        total_timeleft = "0:00:00"

    # Get disk space info
    diskspace1 = "0"
    diskspace2 = "0"
    diskspacex1 = "0"
    diskspacex2 = "0"
    if config:
        try:
            from debridnzbd.utils.diskspace import get_disk_usage
            download_dir = await config.get("folders", "download_dir", "downloads/incomplete")
            complete_dir = await config.get("folders", "complete_dir", "downloads/complete")
            try:
                usage = get_disk_usage(download_dir)
                diskspace1 = format_size(usage["free"])
                diskspacex1 = format_size(usage["free"])
            except (FileNotFoundError, ValueError, OSError):
                pass
            try:
                usage = get_disk_usage(complete_dir)
                diskspace2 = format_size(usage["free"])
                diskspacex2 = format_size(usage["free"])
            except (FileNotFoundError, ValueError, OSError):
                pass
        except Exception:
            pass

    # Build response — noofslots is always the total count, not the paginated slice
    total_count = len(rows)
    queue_response = QueueResponse(
        paused=False,
        noofslots=total_count,
        timeleft=total_timeleft,
        speed=format_speed(total_speed),
        kbpersec=str(int(total_speed / 1024)),
        size=format_size(total_size),
        sizeleft=format_size(total_sizeleft),
        mb=round(total_size / BYTES_PER_MB, 2),
        mbleft=round(total_sizeleft / BYTES_PER_MB, 2),
        slots=slots,
        diskspace1=diskspace1,
        diskspace2=diskspace2,
        diskspacex1=diskspacex1,
        diskspacex2=diskspacex2,
    )

    # Get speed limit from config
    if config:
        speedlimit = await config.get("misc", "speedlimit", "100")
        queue_response.speedlimit = speedlimit
        queue_response.speedlimit_abs = speedlimit

    return JSONResponse(
        content={"status": True, "queue": queue_response.model_dump()},
    )


# ------------------------------------------------------------------ #
#  Queue control modes (stubs)                                        #
# ------------------------------------------------------------------ #


async def handle_pause(params: dict) -> JSONResponse:
    """Handle ?mode=pause — pause the entire download queue.

    In DebridNZBd, the queue is managed by Torbox so this only
    updates the local paused state. Returns success immediately.
    """
    request = params.get("request")
    db = getattr(request.app.state, "db", None) if request else None

    # Mark all active jobs as Paused locally
    if db and db.conn:
        try:
            cursor = await db.conn.execute(
                "UPDATE jobs SET status = 'Paused' WHERE status IN ('Queued', 'Downloading')"
            )
            await db.conn.commit()
            if cursor.rowcount > 0:
                logger.info("pause: paused %d job(s)", cursor.rowcount)
        except Exception:
            logger.exception("pause: failed to update jobs")

    return JSONResponse(content={"status": True})


async def handle_resume(params: dict) -> JSONResponse:
    """Handle ?mode=resume — resume the download queue.

    In DebridNZBd, the queue is managed by Torbox so this only
    updates the local paused state. Returns success immediately.
    """
    request = params.get("request")
    db = getattr(request.app.state, "db", None) if request else None

    # Mark all paused jobs as Queued
    if db and db.conn:
        try:
            cursor = await db.conn.execute(
                "UPDATE jobs SET status = 'Queued' WHERE status = 'Paused'"
            )
            await db.conn.commit()
            if cursor.rowcount > 0:
                logger.info("resume: resumed %d job(s)", cursor.rowcount)
        except Exception:
            logger.exception("resume: failed to update jobs")

    return JSONResponse(content={"status": True})


async def handle_delete(params: dict) -> JSONResponse:
    """Handle ?mode=delete — remove jobs from the queue or history.

    Accepts nzo_ids from either the active queue or the history table.
    Deletes matching entries from both tables and also cancels the
    corresponding download on the Torbox side so it doesn't continue
    consuming resources.

    Parameters:
        nzo_ids: Comma-separated list of nzo_ids to delete
        del_files: Whether to also delete downloaded files (ignored in DebridNZBd)

    Returns:
        JSONResponse with status True.
    """
    request = params.get("request")
    db = getattr(request.app.state, "db", None) if request else None
    config = getattr(request.app.state, "config", None) if request else None

    nzo_ids_str = params.get("nzo_ids") or ""
    if not nzo_ids_str or not db or not db.conn:
        return JSONResponse(content={"status": True})

    nzo_ids = [nid.strip() for nid in nzo_ids_str.split(",") if nid.strip()]
    if not nzo_ids:
        return JSONResponse(content={"status": True})

    # Look up torbox_id and torbox_type for each entry so we can cancel
    # the download on the Torbox side before deleting the local record.
    # We check both the jobs and history tables since delete can come from
    # either the queue page or the history page.
    torbox_entries: list[tuple[str, str]] = []  # (torbox_id, torbox_type)
    placeholders = ",".join(["?"] * len(nzo_ids))

    for table in ("jobs", "history"):
        cursor = await db.conn.execute(
            f"SELECT torbox_id, torbox_type FROM {table} "
            f"WHERE nzo_id IN ({placeholders}) AND torbox_id IS NOT NULL AND torbox_id != ''",
            nzo_ids,
        )
        for row in await cursor.fetchall():
            torbox_entries.append((str(row[0]), row[1]))

    # Cancel downloads on the Torbox side.
    # We do this before deleting local records so we still have the IDs.
    # Failures are logged but don't block the local deletion — the download
    # may have already been removed on the Torbox side, or the API may be
    # temporarily unavailable.
    if config and torbox_entries:
        api_key = await config.get("torbox", "api_key")
        base_url = await config.get("torbox", "base_url", "https://api.torbox.app/v1")
        if api_key:
            client = TorboxClient(api_key=api_key, base_url=base_url)
            try:
                for torbox_id, torbox_type in torbox_entries:
                    try:
                        dl_id = int(torbox_id)
                        if torbox_type == "usenet":
                            await client.control_usenet_download(dl_id, "Delete")
                        elif torbox_type == "torrent":
                            await client.control_torrent(dl_id, "Delete")
                        elif torbox_type == "webdl":
                            await client.control_web_download(dl_id, "Delete")
                        logger.info("delete: cancelled Torbox %s id=%s", torbox_type, torbox_id)
                    except (TorboxError, TorboxAuthError, TorboxConnectionError) as e:
                        logger.warning("delete: failed to cancel Torbox %s id=%s: %s", torbox_type, torbox_id, e)
                    except Exception:
                        logger.warning("delete: unexpected error cancelling Torbox %s id=%s", torbox_type, torbox_id, exc_info=True)
            finally:
                await client.close()

    # Delete from local database
    await db.conn.execute(
        f"DELETE FROM jobs WHERE nzo_id IN ({placeholders})",
        nzo_ids,
    )
    await db.conn.execute(
        f"DELETE FROM history WHERE nzo_id IN ({placeholders})",
        nzo_ids,
    )
    await db.conn.commit()
    logger.info("delete: removed %d entries", len(nzo_ids))

    return JSONResponse(content={"status": True})


async def handle_purge(params: dict) -> JSONResponse:
    """Handle ?mode=purge — remove completed/failed jobs and/or history entries.

    If failed_only=1 is passed, only deletes failed entries from history.
    Otherwise, deletes completed/failed jobs from the queue and all
    entries from history (full purge).

    Also cancels the corresponding downloads on the Torbox side for
    any purged entries that have a torbox_id.

    Parameters:
        failed_only: If "1", only delete failed history entries

    Returns:
        JSONResponse with status True.
    """
    request = params.get("request")
    db = getattr(request.app.state, "db", None) if request else None
    config = getattr(request.app.state, "config", None) if request else None

    failed_only = int(params.get("failed_only") or 0)

    if not db or not db.conn:
        return JSONResponse(content={"status": True})

    # Collect torbox_ids for entries we're about to purge so we can cancel
    # them on the Torbox side.  We only cancel active (non-Complete/Failed)
    # queue items since completed/failed ones are already done on Torbox.
    torbox_entries: list[tuple[str, str]] = []
    cursor = await db.conn.execute(
        "SELECT torbox_id, torbox_type FROM jobs "
        "WHERE status NOT IN ('Complete', 'Failed') AND torbox_id IS NOT NULL AND torbox_id != ''"
    )
    for row in await cursor.fetchall():
        torbox_entries.append((str(row[0]), row[1]))

    # Cancel active downloads on the Torbox side before purging locally.
    if config and torbox_entries:
        api_key = await config.get("torbox", "api_key")
        base_url = await config.get("torbox", "base_url", "https://api.torbox.app/v1")
        if api_key:
            client = TorboxClient(api_key=api_key, base_url=base_url)
            try:
                for torbox_id, torbox_type in torbox_entries:
                    try:
                        dl_id = int(torbox_id)
                        if torbox_type == "usenet":
                            await client.control_usenet_download(dl_id, "Delete")
                        elif torbox_type == "torrent":
                            await client.control_torrent(dl_id, "Delete")
                        elif torbox_type == "webdl":
                            await client.control_web_download(dl_id, "Delete")
                        logger.info("purge: cancelled Torbox %s id=%s", torbox_type, torbox_id)
                    except (TorboxError, TorboxAuthError, TorboxConnectionError) as e:
                        logger.warning("purge: failed to cancel Torbox %s id=%s: %s", torbox_type, torbox_id, e)
                    except Exception:
                        logger.warning("purge: unexpected error cancelling Torbox %s id=%s", torbox_type, torbox_id, exc_info=True)
            finally:
                await client.close()

    # Delete from local database
    await db.conn.execute(
        "DELETE FROM jobs WHERE status IN ('Complete', 'Failed')"
    )

    if failed_only:
        # Only delete failed history entries
        await db.conn.execute(
            "DELETE FROM history WHERE status = 'Failed'"
        )
    else:
        # Full purge — delete all history
        await db.conn.execute("DELETE FROM history")

    await db.conn.commit()
    logger.info("purge: cleared queue completed/failed, history %s",
                 "failed only" if failed_only else "all")

    return JSONResponse(content={"status": True})


async def handle_switch(params: dict) -> JSONResponse:
    """Handle ?mode=switch — reorder jobs in the queue.

    Parameters:
        value: The nzo_id of the job to move
        value2: The new position (0-indexed)

    SABnzbd returns the new position after the move.
    """
    request = params.get("request")
    db = getattr(request.app.state, "db", None) if request else None

    nzo_id = params.get("value") or ""
    new_pos = int(params.get("value2") or 0)

    if nzo_id and db and db.conn:
        try:
            await db.conn.execute(
                "UPDATE jobs SET position = ? WHERE nzo_id = ?",
                (new_pos, nzo_id),
            )
            await db.conn.commit()
            logger.info("switch: moved job %s to position %d", nzo_id, new_pos)
        except Exception:
            logger.exception("switch: failed to reorder job %s", nzo_id)

    return JSONResponse(content={"status": True, "position": new_pos})


async def handle_change_cat(params: dict) -> JSONResponse:
    """Handle ?mode=change_cat — change the category of a job.

    Parameters:
        nzo_ids: Comma-separated nzo_ids (or single nzo_id)
        cat / category: New category name
    """
    request = params.get("request")
    db = getattr(request.app.state, "db", None) if request else None

    nzo_ids_str = params.get("nzo_ids") or ""
    category = params.get("cat") or params.get("category") or "*"

    if nzo_ids_str and db and db.conn:
        nzo_ids = [nid.strip() for nid in nzo_ids_str.split(",") if nid.strip()]
        if nzo_ids:
            placeholders = ",".join(["?"] * len(nzo_ids))
            await db.conn.execute(
                f"UPDATE jobs SET category = ? WHERE nzo_id IN ({placeholders})",
                [category] + nzo_ids,
            )
            await db.conn.commit()
            logger.info("change_cat: set category=%s for %d job(s)", category, len(nzo_ids))

    return JSONResponse(content={"status": True})


async def handle_priority(params: dict) -> JSONResponse:
    """Handle ?mode=priority — change the priority of a job.

    Parameters:
        nzo_ids: Comma-separated nzo_ids (or single nzo_id)
        priority: New priority (-100=paused, 0=normal, 1=low, 2=high)
    """
    request = params.get("request")
    db = getattr(request.app.state, "db", None) if request else None

    nzo_ids_str = params.get("nzo_ids") or ""
    priority = int(params.get("value") or params.get("priority") or 0)

    if nzo_ids_str and db and db.conn:
        nzo_ids = [nid.strip() for nid in nzo_ids_str.split(",") if nid.strip()]
        if nzo_ids:
            placeholders = ",".join(["?"] * len(nzo_ids))
            await db.conn.execute(
                f"UPDATE jobs SET priority = ? WHERE nzo_id IN ({placeholders})",
                [priority] + nzo_ids,
            )
            await db.conn.commit()
            logger.info("priority: set priority=%d for %d job(s)", priority, len(nzo_ids))

    return JSONResponse(content={"status": True})


async def handle_speedlimit(params: dict) -> JSONResponse:
    """Handle ?mode=speedlimit — get or set the speed limit.

    Parameters:
        value: New speed limit as a percentage of max line speed (SABnzbd convention).
               If not provided, returns the current limit.

    In DebridNZBd, speed limiting is handled by Torbox, not locally.
    This endpoint stores the value for compatibility but does not
    enforce it.
    """
    request = params.get("request")
    config = getattr(request.app.state, "config", None) if request else None

    if config is None:
        return JSONResponse(content={"status": True, "speedlimit": "100"})

    current_limit = await config.get("misc", "speedlimit", "100")

    value = params.get("value")
    if value is not None:
        try:
            new_limit = str(int(value))
            await config.set("misc", "speedlimit", new_limit)
            current_limit = new_limit
        except (ValueError, TypeError):
            pass

    return JSONResponse(content={"status": True, "speedlimit": current_limit})