"""Background state synchronization poller for DebridNZBd.

Periodically queries the Torbox API for download status updates and
synchronizes them with the local jobs database. When downloads complete,
the poller requests CDN download links and moves finished jobs to history.

The poller runs as an asyncio task created during app startup and is
gracefully stopped during shutdown via a cancellation event.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from debridnzbd.core.cdn_downloader import download_file, move_to_category_dir
from debridnzbd.torbox.client import TorboxClient
from debridnzbd.torbox.exceptions import (
    TorboxAuthError,
    TorboxConnectionError,
    TorboxError,
    TorboxRateLimitError,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

# Mapping from Torbox status strings to SABnzbd-compatible local status values.
# Torbox returns lowercase strings; we normalize to our PascalCase conventions.
TORBOX_STATUS_MAP: dict[str, str] = {
    "queued": "Queued",
    "queued_caching": "Queued",
    "downloading": "Downloading",
    "meta_downloading": "Downloading",
    "completed": "Complete",
    "cached": "Complete",
    "paused": "Paused",
    "paused_caching": "Paused",
    "seeding": "Downloading",
    "error": "Failed",
    "stalled": "Failed",
    "failed": "Failed",
}

# Torbox statuses that indicate the download is available on CDN
COMPLETED_STATUSES = {"completed", "cached"}

# Torbox statuses that indicate a permanent failure
# "stalled" is included because Torbox reports it for downloads that have
# permanently stalled on their end — we move them to history like other failures.
# Local stall detection (no progress for 60+ seconds) is tracked separately
# via the stalled_since column and does NOT use this set.
FAILED_STATUSES = {"error", "failed", "stalled"}

# Stall detection threshold: seconds without progress before considering a download stalled
STALL_THRESHOLD_SECONDS = 60


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


def _map_torbox_status(torbox_status: str, progress: float = 0.0) -> str:
    """Map a Torbox status string to a local SABnzbd-compatible status.

    Falls back to "Queued" for unknown statuses so we never crash
    on new Torbox status values.

    The Torbox API sometimes returns an empty status string for completed
    usenet downloads (with progress=1.0).  In that case, treat the
    download as "cached" since it's clearly done.
    """
    status_lower = torbox_status.lower()
    if status_lower in TORBOX_STATUS_MAP:
        return TORBOX_STATUS_MAP[status_lower]
    # Empty or unknown status with full progress → treat as cached/completed.
    # This handles the Torbox API quirk where completed usenet downloads
    # return status="" and progress=1.0.
    if progress >= 1.0:
        return "Complete"
    return "Queued"


async def run_state_sync(app: FastAPI, cancelled: asyncio.Event) -> None:
    """Background task that periodically syncs Torbox download state.

    Polls the Torbox API for usenet, torrent, and web download lists,
    matches them to local jobs by torbox_id, and updates status/progress.
    When a download completes, requests CDN link and moves to history.

    Args:
        app: The FastAPI application instance (for accessing config and db).
        cancelled: An asyncio.Event that is set on shutdown to signal
            the poller to stop.
    """
    logger.info("State sync poller starting")

    while not cancelled.is_set():
        config = getattr(app.state, "config", None)
        db = getattr(app.state, "db", None)

        if config is None or db is None:
            # Not initialized yet — wait and retry
            await asyncio.sleep(2)
            continue

        api_key = await config.get("torbox", "api_key")
        if not api_key:
            # No API key configured — wait longer
            await asyncio.sleep(15)
            continue

        base_url = await config.get("torbox", "base_url", "https://api.torbox.app/v1")
        poll_interval = int(await config.get("torbox", "poll_interval", "5"))
        download_on_complete = await config.get_bool("torbox", "download_on_complete", True)

        client = TorboxClient(api_key=api_key, base_url=base_url)

        # Create a semaphore to limit concurrent CDN downloads
        concurrency = int(await config.get("torbox", "cdn_download_concurrency", "2"))
        semaphore = asyncio.Semaphore(max(1, concurrency))

        try:
            await _sync_all_downloads(client, db, config, download_on_complete, semaphore)
        except Exception:
            logger.exception("State sync: unexpected error during sync cycle")
        finally:
            await client.close()

        # Wait for the next poll interval, but check cancelled every second
        for _ in range(max(poll_interval, 1)):
            if cancelled.is_set():
                break
            await asyncio.sleep(1)

    logger.info("State sync poller stopped")


async def _sync_all_downloads(
    client: TorboxClient,
    db: object,
    config: object,
    download_on_complete: bool,
    semaphore: asyncio.Semaphore,
) -> None:
    """Sync status for all download types from Torbox.

    Fetches usenet, torrent, and web download lists from Torbox and
    updates matching local jobs. Each type is synced independently —
    an error in one type does not block the others.

    Matching happens in two passes:
    1. By torbox_id — fast, exact match for jobs where we recorded the ID.
    2. By URL — fallback for orphaned jobs that have no torbox_id yet.
    """
    if not db or not db.conn:
        return

    # Fetch ALL local jobs (not just those with torbox_id) so we can
    # match orphaned jobs by URL in the second pass. Also fetch stall
    # tracking columns for stall detection logic.
    cursor = await db.conn.execute(
        "SELECT nzo_id, torbox_id, torbox_type, status, nzo_url, filename, "
        "percentage, sizeleft, last_progress_change, stalled_since, stall_retries, "
        "category, priority FROM jobs"
    )
    all_jobs = await cursor.fetchall()

    if not all_jobs:
        return  # No jobs to sync

    # Build a lookup: (torbox_id, torbox_type) → list of job tuples
    # for jobs that have a torbox_id. Each tuple now includes stall data.
    # Format: (nzo_id, status, percentage, sizeleft,
    #          last_progress_change, stalled_since, stall_retries,
    #          nzo_url, category, priority, torbox_type)
    jobs_by_key: dict[tuple[str, str], list[tuple]] = {}
    # Collect orphaned jobs (no torbox_id) for URL-based fallback matching.
    orphaned_jobs: list[tuple[str, str, str]] = []  # (nzo_id, nzo_url, torbox_type)

    for row in all_jobs:
        (nzo_id, torbox_id, torbox_type, status, nzo_url, filename,
         percentage, sizeleft, last_progress_change, stalled_since,
         stall_retries, category, priority) = row
        if torbox_id and torbox_type:
            key = (str(torbox_id), torbox_type)
            jobs_by_key.setdefault(key, []).append((
                nzo_id, status, percentage or 0, sizeleft or 0,
                last_progress_change or 0, stalled_since or 0,
                stall_retries or 0, nzo_url or "", category or "*",
                priority or 0, torbox_type,
            ))
        else:
            # Orphaned — no torbox_id yet.  Try to reconcile by URL later.
            url = nzo_url or ""
            if url or filename:
                orphaned_jobs.append((nzo_id, url, torbox_type or "usenet"))

    # Get poll interval for speed calculation
    poll_interval = int(await config.get("torbox", "poll_interval", "5"))
    now = time.time()

    # Fetch each download type independently
    usenet_downloads: list = []
    torrent_downloads: list = []
    web_downloads: list = []

    try:
        usenet_downloads = await client.get_usenet_list(bypass_cache=True)
    except (TorboxAuthError, TorboxConnectionError, TorboxRateLimitError, TorboxError):
        logger.debug("State sync: failed to fetch usenet list", exc_info=True)
    except Exception:
        logger.warning("State sync: unexpected error fetching usenet list", exc_info=True)

    try:
        torrent_downloads = await client.get_torrent_list(bypass_cache=True)
    except (TorboxAuthError, TorboxConnectionError, TorboxRateLimitError, TorboxError):
        logger.debug("State sync: failed to fetch torrent list", exc_info=True)
    except Exception:
        logger.warning("State sync: unexpected error fetching torrent list", exc_info=True)

    try:
        web_downloads = await client.get_web_download_list(bypass_cache=True)
    except (TorboxAuthError, TorboxConnectionError, TorboxRateLimitError, TorboxError):
        logger.debug("State sync: failed to fetch web download list", exc_info=True)
    except Exception:
        logger.warning("State sync: unexpected error fetching web download list", exc_info=True)

    # Process updates for each download type — pass 1: match by torbox_id
    for dl in usenet_downloads:
        dl_name = _extract_download_name(dl, "usenet")
        await _update_job_from_torbox(
            db, client, str(dl.id), "usenet", dl.status, dl.progress, dl.size,
            jobs_by_key, config, download_on_complete, semaphore, now,
            poll_interval, download_name=dl_name,
        )

    for dl in torrent_downloads:
        dl_name = _extract_download_name(dl, "torrent")
        await _update_job_from_torbox(
            db, client, str(dl.id), "torrent", dl.status, dl.progress, dl.size,
            jobs_by_key, config, download_on_complete, semaphore, now,
            poll_interval, download_name=dl_name,
        )

    for dl in web_downloads:
        dl_name = _extract_download_name(dl, "webdl")
        await _update_job_from_torbox(
            db, client, str(dl.id), "webdl", dl.status, dl.progress, dl.size,
            jobs_by_key, config, download_on_complete, semaphore, now,
            poll_interval, download_name=dl_name,
        )

    # Pass 2: reconcile orphaned jobs by matching their URL against
    # Torbox downloads.  This handles the case where the initial
    # addurl response didn't include a torbox_id (e.g. Torbox returned
    # the ID in an unexpected format).
    if orphaned_jobs:
        await _reconcile_orphaned_jobs(
            db, client, orphaned_jobs,
            usenet_downloads, torrent_downloads, web_downloads,
            jobs_by_key, config, download_on_complete, semaphore, now,
        )


async def _update_job_from_torbox(
    db: object,
    client: TorboxClient,
    torbox_id: str,
    torbox_type: str,
    torbox_status: str,
    progress: float,
    size: float,
    jobs_by_key: dict[tuple[str, str], list[tuple]],
    config: object,
    download_on_complete: bool,
    semaphore: asyncio.Semaphore,
    now: float,
    poll_interval: int = 5,
    download_name: str | None = None,
) -> None:
    """Update local jobs matching a Torbox download.

    Finds all local jobs that match the given torbox_id/type and updates
    their status, progress, size, and optionally filename. For completed
    downloads, requests the CDN link and moves the job to history.

    Also tracks download speed from sizeleft changes and detects stalled
    downloads (no progress for 60+ seconds), triggering automatic retries.

    Args:
        jobs_by_key: Maps (torbox_id, torbox_type) to list of job tuples:
            (nzo_id, status, percentage, sizeleft,
             last_progress_change, stalled_since, stall_retries,
             nzo_url, category, priority, torbox_type)
        poll_interval: Seconds between poll cycles (used for speed calc).
        download_name: If provided, update the job's filename to this
            value. Used to replace URL-derived placeholder names with
            the real NZB/torrent name from Torbox.
    """
    key = (torbox_id, torbox_type)
    matching_jobs = jobs_by_key.get(key, [])

    if not matching_jobs:
        return

    local_status = _map_torbox_status(torbox_status, progress)
    percentage = min(100, max(0, int(progress * 100)))  # progress is 0.0-1.0
    # Normalise the display status: if Torbox returns an empty string but
    # the download is complete (progress=100%), show "cached" instead.
    display_torbox_state = torbox_status if torbox_status else ("cached" if progress >= 1.0 else "queued")

    # Determine effective status — mark completed downloads as "Fetching"
    # so the CDN processor can handle them without blocking the poller.
    is_completed = (
        torbox_status.lower() in COMPLETED_STATUSES
        or (not torbox_status and progress >= 1.0)
        or (local_status == "Complete")
    )
    effective_status = "Fetching" if (is_completed and download_on_complete) else local_status

    new_sizeleft = size * (1.0 - progress) if progress < 1.0 else 0

    for job_tuple in matching_jobs:
        (nzo_id, current_status, old_percentage, old_sizeleft,
         last_progress_change, stalled_since, stall_retries,
         nzo_url, category, priority, job_torbox_type) = job_tuple

        # Skip if job is already in a final state, being fetched from CDN,
        # or locally paused (the qBittorrent API sets local Paused status
        # that the poller should not overwrite with Torbox's reported status).
        if current_status in ("Complete", "Failed", "Fetching", "Paused"):
            continue

        # Compute download speed from sizeleft delta
        # Speed = bytes downloaded per second = (old_sizeleft - new_sizeleft) / poll_interval
        speed = 0.0
        if old_sizeleft > 0 and new_sizeleft < old_sizeleft and poll_interval > 0:
            speed = max(0.0, (old_sizeleft - new_sizeleft) / poll_interval)

        # Stall detection: check if progress has changed since last poll
        progress_changed = percentage != old_percentage

        # Initialize last_progress_change for new jobs (default is 0).
        # Without this, stall detection would never trigger because the
        # elif condition requires last_progress_change > 0.
        if last_progress_change == 0 and effective_status in ("Downloading", "Fetching"):
            last_progress_change = now

        if progress_changed:
            last_progress_change = now
            # If progress changed, clear stall state
            stalled_since = 0.0
            stall_retries = 0
        elif effective_status in ("Downloading", "Fetching") and last_progress_change > 0:
            # No progress — check if we've been stalled long enough
            stall_duration = now - last_progress_change
            if stall_duration >= STALL_THRESHOLD_SECONDS:
                # Stall detected — determine retry action
                if stall_retries == 0:
                    # First stall detection: try resume/reannounce
                    stalled_since = now if stalled_since == 0 else stalled_since
                    stall_retries = 1
                    last_progress_change = now  # Reset to give another window
                    logger.info(
                        "State sync: stall detected for %s (no progress for %ds), "
                        "attempting resume (attempt 1)",
                        nzo_id, int(stall_duration),
                    )
                    await _retry_stalled_job(
                        db, client, nzo_id, torbox_id, torbox_type,
                        stall_retries, nzo_url, category, priority, config,
                    )
                elif stall_retries == 1:
                    # Second stall: try restart (delete + re-add)
                    stall_retries = 2
                    last_progress_change = now  # Reset to give another window
                    logger.info(
                        "State sync: stall persists for %s after resume attempt, "
                        "attempting restart (attempt 2)",
                        nzo_id,
                    )
                    await _retry_stalled_job(
                        db, client, nzo_id, torbox_id, torbox_type,
                        stall_retries, nzo_url, category, priority, config,
                    )
                elif stall_retries >= 2:
                    # Third stall: give up, mark as failed
                    logger.warning(
                        "State sync: giving up on %s after %d retry attempts — "
                        "marking as failed",
                        nzo_id, stall_retries,
                    )
                    await _fail_job(db, nzo_id, "stalled", now)
                    continue

        try:
            # Update filename if we have a real name from Torbox
            if download_name:
                await db.conn.execute(
                    """UPDATE jobs SET
                        status = ?, percentage = ?, size = ?,
                        sizeleft = ?, torbox_state = ?, filename = ?,
                        speed = ?, last_progress_change = ?,
                        stalled_since = ?, stall_retries = ?
                    WHERE nzo_id = ?""",
                    (
                        effective_status,
                        percentage,
                        size,
                        new_sizeleft,
                        display_torbox_state,
                        download_name,
                        speed,
                        last_progress_change,
                        stalled_since,
                        stall_retries,
                        nzo_id,
                    ),
                )
            else:
                await db.conn.execute(
                    """UPDATE jobs SET
                        status = ?, percentage = ?, size = ?,
                        sizeleft = ?, torbox_state = ?,
                        speed = ?, last_progress_change = ?,
                        stalled_since = ?, stall_retries = ?
                    WHERE nzo_id = ?""",
                    (
                        effective_status,
                        percentage,
                        size,
                        new_sizeleft,
                        display_torbox_state,
                        speed,
                        last_progress_change,
                        stalled_since,
                        stall_retries,
                        nzo_id,
                    ),
                )
            await db.conn.commit()
        except Exception:
            logger.exception("State sync: failed to update job %s", nzo_id)
            continue

        logger.debug(
            "State sync: updated %s → %s (%s%%, torbox=%s, speed=%s)",
            nzo_id, effective_status, percentage, torbox_status, format_speed_value(speed),
        )

        # Handle failure — move to history
        # Completion is handled by the CDN processor, which picks up
        # jobs with status "Fetching" and downloads from CDN asynchronously.
        if torbox_status.lower() in FAILED_STATUSES:
            await _fail_job(db, nzo_id, torbox_status, now)


def format_speed_value(speed: float) -> str:
    """Format a speed value in bytes/s to a human-readable string."""
    if speed <= 0:
        return "0 B/s"
    for unit, threshold in [("GB/s", 1_000_000_000), ("MB/s", 1_000_000), ("KB/s", 1_000)]:
        if speed >= threshold:
            return f"{speed / threshold:.1f} {unit}"
    return f"{speed:.0f} B/s"


async def _retry_stalled_job(
    db: object,
    client: TorboxClient,
    nzo_id: str,
    torbox_id: str,
    torbox_type: str,
    stall_retries: int,
    nzo_url: str,
    category: str,
    priority: int,
    config: object,
) -> None:
    """Attempt to recover a stalled download.

    For stall_retries == 1 (first attempt): try resume/reannounce on Torbox.
    For stall_retries >= 2 (second attempt): delete from Torbox and re-add.
    """
    try:
        dl_id = int(torbox_id)
    except (ValueError, TypeError):
        logger.warning("State sync: cannot retry %s — invalid torbox_id %r", nzo_id, torbox_id)
        return

    if stall_retries == 1:
        # First retry: try resume/reannounce
        try:
            if torbox_type == "torrent":
                await client.control_torrent(dl_id, "Reannounce")
                logger.info("State sync: sent Reannounce for stalled torrent %s (torbox_id=%s)", nzo_id, torbox_id)
            elif torbox_type == "usenet":
                await client.control_usenet_download(dl_id, "Resume")
                logger.info("State sync: sent Resume for stalled usenet %s (torbox_id=%s)", nzo_id, torbox_id)
            else:
                # WebDL has no resume — skip directly to restart on next cycle
                logger.info("State sync: WebDL %s cannot be resumed — will retry as restart", nzo_id)
                # Mark stall_retries as 1 so the next cycle will try restart
                return
        except (TorboxAuthError, TorboxConnectionError, TorboxRateLimitError, TorboxError) as e:
            logger.warning("State sync: Torbox resume/reannounce failed for %s: %s", nzo_id, e)
        except Exception:
            logger.exception("State sync: unexpected error retrying %s", nzo_id)

    elif stall_retries >= 2:
        # Second retry: delete and re-add
        if not nzo_url:
            logger.warning("State sync: cannot restart %s — no nzo_url stored", nzo_id)
            return

        # Delete from Torbox
        try:
            if torbox_type == "torrent":
                await client.control_torrent(dl_id, "Delete")
            elif torbox_type == "usenet":
                await client.control_usenet_download(dl_id, "Delete")
            elif torbox_type == "webdl":
                await client.control_web_download(dl_id, "Delete")
            logger.info("State sync: deleted stalled %s from Torbox (torbox_id=%s)", nzo_id, torbox_id)
        except (TorboxAuthError, TorboxConnectionError, TorboxRateLimitError, TorboxError) as e:
            logger.warning("State sync: failed to delete %s from Torbox: %s", nzo_id, e)
            return
        except Exception:
            logger.exception("State sync: unexpected error deleting %s from Torbox", nzo_id)
            return

        # Delete from local database
        try:
            await db.conn.execute("DELETE FROM jobs WHERE nzo_id = ?", (nzo_id,))
            await db.conn.commit()
            logger.info("State sync: deleted stalled job %s from local DB", nzo_id)
        except Exception:
            logger.exception("State sync: failed to delete job %s from local DB", nzo_id)
            return

        # Re-submit via resubmit helper
        await _resubmit_job(nzo_url, torbox_type, category, priority, config, db)


async def _resubmit_job(
    url: str,
    url_type: str,
    category: str,
    priority: int,
    config: object,
    db: object,
) -> None:
    """Re-submit a download URL to Torbox and create a new local job.

    This is used by the stall retry system to restart downloads that couldn't
    be recovered by resume/reannounce. It mirrors the core logic of handle_addurl
    but without the HTTP request/response handling.
    """
    from debridnzbd.utils.nzo_id import generate_nzo_id
    from debridnzbd.api.queue import detect_url_type

    nzo_id = generate_nzo_id()
    logger.info("State sync: resubmitting %s download as new job %s (url=%s)", url_type, nzo_id, url[:100])

    # Get Torbox config
    torbox_api_key = await config.get("torbox", "api_key")
    if not torbox_api_key:
        logger.error("State sync: no Torbox API key configured, cannot resubmit %s", nzo_id)
        return

    base_url = await config.get("torbox", "base_url", "https://api.torbox.app/v1")
    resubmit_client = TorboxClient(api_key=torbox_api_key, base_url=base_url)

    try:
        # Detect URL type if not specified
        if url_type not in ("usenet", "torrent", "webdl"):
            url_type = detect_url_type(url, await config.get("torbox", "default_type", "usenet"))

        # Submit to Torbox
        torbox_id = None
        torbox_hash = ""

        try:
            if url_type == "torrent":
                result = await resubmit_client.create_torrent(magnet=url)
            elif url_type == "webdl":
                result = await resubmit_client.create_web_download(link=url)
            else:
                result = await resubmit_client.create_usenet_download(link=url)

            if result.success:
                data = result.data
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
                            logger.warning("State sync: could not parse torbox_id from %r", raw_id)
                    torbox_hash = data.get("hash", "")
            else:
                logger.warning(
                    "State sync: Torbox rejected resubmit for %s: %s",
                    nzo_id, result.detail or "Unknown error",
                )
                return
        except (TorboxAuthError, TorboxConnectionError, TorboxRateLimitError, TorboxError) as e:
            logger.warning("State sync: Torbox error resubmitting %s: %s", nzo_id, e)
            return

        # Insert new job into database
        now = time.time()
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
                    url.split("/")[-1].split("?")[0] or url[:50],  # Filename from URL
                    "",  # password
                    url,
                    category or "*",
                    "Default",  # script
                    priority or 0,
                    -1,  # pp
                    "Queued",
                    0, 0, 0,  # size, sizeleft, percentage
                    now,  # time_added
                    "",  # avg_age
                    str(torbox_id) if torbox_id else None,
                    url_type,
                    torbox_hash,
                    position,
                    "queued",  # initial torbox_state
                ),
            )
            await db.conn.commit()
            logger.info(
                "State sync: resubmitted job %s (torbox_id=%s, type=%s)",
                nzo_id, torbox_id, url_type,
            )
        except Exception:
            logger.exception("State sync: failed to insert resubmitted job %s", nzo_id)

    finally:
        await resubmit_client.close()


async def _complete_job(
    db: object,
    client: TorboxClient,
    nzo_id: str,
    torbox_id: str,
    torbox_type: str,
    config: object,
    semaphore: asyncio.Semaphore,
    now: float,
    category: str = "*",
) -> None:
    """Mark a job as complete and move it to history.

    Requests the CDN download link from Torbox, downloads the file to
    the incomplete directory, then moves it to the category-specific
    complete directory. Finally moves the job from the jobs table to
    the history table.
    """
    # Check if the job still exists — it may have been moved to history
    # already by a provider_download call or another sync cycle.
    cursor = await db.conn.execute("SELECT 1 FROM jobs WHERE nzo_id = ?", (nzo_id,))
    if await cursor.fetchone() is None:
        logger.debug("State sync: job %s already removed from queue, skipping", nzo_id)
        return

    # Request CDN link — if this fails, continue without it so the job
    # is still moved to history rather than stuck in "Fetching" forever.
    cdn_link = ""
    try:
        dl_id = int(torbox_id)
        logger.info("State sync: requesting CDN link for %s (type=%s, torbox_id=%s)", nzo_id, torbox_type, torbox_id)
        if torbox_type == "usenet":
            cdn_link = await client.request_usenet_dl(usenet_id=dl_id)
        elif torbox_type == "torrent":
            cdn_link = await client.request_torrent_dl(torrent_id=dl_id)
        elif torbox_type == "webdl":
            cdn_link = await client.request_web_dl(web_id=dl_id)
    except (TorboxError, TorboxConnectionError, TorboxAuthError):
        logger.warning("State sync: failed to get CDN link for %s (type=%s) — moving to history without CDN link", nzo_id, torbox_type)
    except Exception:
        logger.warning("State sync: unexpected error getting CDN link for %s — moving to history without CDN link", nzo_id, exc_info=True)

    # Download the file from CDN to the incomplete directory
    local_path: str | None = None
    if cdn_link:
        download_dir = await config.get("folders", "download_dir", "downloads/incomplete")
        try:
            local_path = await download_file(
                url=cdn_link,
                dest_dir=download_dir,
                semaphore=semaphore,
            )
            if local_path:
                # Move from incomplete to category-specific complete directory
                final_path = await move_to_category_dir(local_path, category, config)
                if final_path:
                    local_path = final_path
                    logger.info("State sync: moved file for %s to %s", nzo_id, local_path)
                else:
                    logger.warning("State sync: failed to move file for %s — keeping at %s", nzo_id, local_path)
            else:
                logger.warning("State sync: CDN download returned no path for %s — CDN link stored as fallback", nzo_id)
        except Exception:
            logger.exception("State sync: CDN download failed for %s — CDN link stored as fallback", nzo_id)
            local_path = None

    # Update the job with CDN link, local path, and completed status
    try:
        await db.conn.execute(
            "UPDATE jobs SET status = ?, cdn_link = ?, local_path = ?, percentage = 100, "
            "sizeleft = 0, time_completed = ? WHERE nzo_id = ?",
            ("Complete", cdn_link, local_path or "", now, nzo_id),
        )
        await db.conn.commit()
        logger.info("State sync: completed %s — CDN link stored, local_path=%s", nzo_id, local_path or "none")
    except Exception:
        logger.exception("State sync: failed to mark %s complete", nzo_id)
        return

    # Move to history
    await _move_to_history(db, nzo_id, now)


async def _fail_job(
    db: object,
    nzo_id: str,
    torbox_status: str,
    now: float,
) -> None:
    """Mark a job as failed and move it to history."""
    try:
        await db.conn.execute(
            "UPDATE jobs SET status = ?, fail_message = ?, time_completed = ? "
            "WHERE nzo_id = ?",
            ("Failed", f"Torbox: {torbox_status}", now, nzo_id),
        )
        await db.conn.commit()
        logger.info("State sync: failed %s — %s", nzo_id, torbox_status)
    except Exception:
        logger.exception("State sync: failed to mark %s as failed", nzo_id)
        return

    await _move_to_history(db, nzo_id, now)


async def _move_to_history(db: object, nzo_id: str, now: float) -> None:
    """Move a completed/failed job from jobs to history table."""
    try:
        # Read the job row
        cursor = await db.conn.execute(
            "SELECT nzo_id, filename, status, size, category, "
            "time_added, download_time, cdn_link, torbox_id, torbox_type, "
            "fail_message, nzo_url, local_path FROM jobs WHERE nzo_id = ?",
            (nzo_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return

        # Insert into history
        await db.conn.execute(
            """INSERT OR IGNORE INTO history
            (nzo_id, name, status, size, category, download_time,
             completed, time_added, storage, torbox_id, torbox_type, fail_message, nzo_url, path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row[0],   # nzo_id
                row[1],   # filename → name
                row[2],   # status
                row[3],   # size
                row[4],   # category
                row[6] or 0,  # download_time
                now,      # completed
                row[5],   # time_added
                row[7] or row[11] or "",  # storage: cdn_link or nzo_url
                row[8],   # torbox_id
                row[9],   # torbox_type
                row[10] or "",  # fail_message
                row[11] or "",  # nzo_url
                row[12] or "",  # path: local_path
            ),
        )

        # Delete from jobs
        await db.conn.execute("DELETE FROM jobs WHERE nzo_id = ?", (nzo_id,))
        await db.conn.commit()
        logger.info("State sync: moved %s to history", nzo_id)
    except Exception:
        logger.exception("State sync: failed to move %s to history", nzo_id)


async def _reconcile_orphaned_jobs(
    db: object,
    client: TorboxClient,
    orphaned_jobs: list[tuple[str, str, str]],
    usenet_downloads: list,
    torrent_downloads: list,
    web_downloads: list,
    jobs_by_key: dict[tuple[str, str], list[tuple]],
    config: object,
    download_on_complete: bool,
    semaphore: asyncio.Semaphore,
    now: float,
) -> None:
    """Match orphaned jobs (no torbox_id) against Torbox downloads by URL.

    When addurl can't extract a torbox_id from the Torbox response (e.g.
    unexpected format), the local job is left with torbox_id=None.  This
    function attempts to reconcile those orphaned jobs by matching their
    submission URL against the URLs stored in Torbox download objects.

    On a successful match, the job's torbox_id is updated in the database
    and the status is synced like a normal (non-orphaned) job.
    """
    if not orphaned_jobs:
        return

    # Build a URL → (torbox_id, type, status, progress, size) lookup from
    # all Torbox downloads.  For usenet we use the raw link if available;
    # for torrents we use the magnet hash; for web downloads we use the
    # name field as a last resort.
    from debridnzbd.torbox.models import (
        TorboxUsenetDownload,
        TorboxTorrentDownload,
        TorboxWebDownload,
    )

    torbox_by_url: dict[str, tuple[str, str, str, float, float, str | None]] = {}
    # Collect all downloads by type for type-based matching fallback
    unclaimed_usenet: list[TorboxUsenetDownload] = []
    unclaimed_torrent: list[TorboxTorrentDownload] = []
    unclaimed_web: list[TorboxWebDownload] = []

    # Track which torbox_ids are already claimed by non-orphaned jobs
    claimed_torbox_ids: set[str] = set()
    for key, jobs in jobs_by_key.items():
        claimed_torbox_ids.add(key[0])

    for dl in usenet_downloads:
        if isinstance(dl, TorboxUsenetDownload):
            dl_name = _extract_download_name(dl, "usenet")
            # Usenet downloads don't have a name field, so we can't
            # match by URL or filename.  Instead, we collect them for
            # type-based matching (unclaimed usenet downloads → orphaned
            # usenet jobs) as a fallback.
            if str(dl.id) not in claimed_torbox_ids:
                unclaimed_usenet.append(dl)

    for dl in torrent_downloads:
        if isinstance(dl, TorboxTorrentDownload):
            dl_name = _extract_download_name(dl, "torrent")
            # Torrents have a magnet hash which we stored in torbox_hash
            if str(dl.id) not in claimed_torbox_ids:
                unclaimed_torrent.append(dl)
            if dl.hash:
                torbox_by_url[dl.hash.lower()] = (str(dl.id), "torrent", dl.status, dl.progress, dl.size, dl_name)
            # Also index by name for filename-based matching
            if dl.name:
                torbox_by_url[f"name:{dl.name.lower()}"] = (str(dl.id), "torrent", dl.status, dl.progress, dl.size, dl_name)

    for dl in web_downloads:
        if isinstance(dl, TorboxWebDownload):
            dl_name = _extract_download_name(dl, "webdl")
            if str(dl.id) not in claimed_torbox_ids:
                unclaimed_web.append(dl)
            if dl.name:
                torbox_by_url[f"name:{dl.name.lower()}"] = (str(dl.id), "webdl", dl.status, dl.progress, dl.size, dl_name)

    for nzo_id, nzo_url, job_type in orphaned_jobs:
        matched: tuple[str, str, str, float, float, str | None] | None = None

        # Strategy 1: match by URL substring (usenet NZB URLs)
        if nzo_url:
            url_lower = nzo_url.lower()
            for key, info in torbox_by_url.items():
                if key.startswith("name:"):
                    continue  # Skip name-only entries for URL matching
                if url_lower in key or key in url_lower:
                    matched = info
                    break

        # Strategy 2: for torrents, match magnet hash
        if not matched and nzo_url and nzo_url.startswith("magnet:"):
            # Extract btih hash from magnet URL
            import re
            hash_match = re.search(r"btih:([a-fA-F0-9]{40})", nzo_url, re.IGNORECASE)
            if hash_match:
                magnet_hash = hash_match.group(1).lower()
                lookup = torbox_by_url.get(magnet_hash)
                if lookup:
                    matched = lookup

        # Strategy 3: match by filename against Torbox download names
        if not matched:
            # Get the local filename for this job
            cursor = await db.conn.execute(
                "SELECT filename FROM jobs WHERE nzo_id = ?", (nzo_id,)
            )
            row = await cursor.fetchone()
            if row and row[0]:
                filename_lower = row[0].lower()
                name_key = f"name:{filename_lower}"
                lookup = torbox_by_url.get(name_key)
                if lookup:
                    matched = lookup
                else:
                    # Try partial match — local filename may be a substring
                    # of the Torbox name (e.g. without extension or quality tags)
                    for key, info in torbox_by_url.items():
                        if not key.startswith("name:"):
                            continue
                        torbox_name = key[5:]  # Strip "name:" prefix
                        if torbox_name in filename_lower or filename_lower in torbox_name:
                            matched = info
                            break

        # Strategy 4: type-based fallback — match orphaned jobs to unclaimed
        # Torbox downloads of the same type by picking the most recently
        # created one.  This handles usenet downloads which don't expose a
        # source URL or name for matching.
        if not matched:
            if job_type == "usenet" and unclaimed_usenet:
                # Pick the most recently created unclaimed usenet download
                dl = max(unclaimed_usenet, key=lambda d: d.id)
                dl_name = _extract_download_name(dl, "usenet")
                matched = (str(dl.id), "usenet", dl.status, dl.progress, dl.size, dl_name)
                unclaimed_usenet.remove(dl)
                logger.debug(
                    "State sync: matched orphaned usenet job %s → torbox_id=%s by type fallback",
                    nzo_id, dl.id,
                )
            elif job_type == "torrent" and unclaimed_torrent:
                dl = max(unclaimed_torrent, key=lambda d: d.id)
                dl_name = _extract_download_name(dl, "torrent")
                matched = (str(dl.id), "torrent", dl.status, dl.progress, dl.size, dl_name)
                unclaimed_torrent.remove(dl)
                logger.debug(
                    "State sync: matched orphaned torrent job %s → torbox_id=%s by type fallback",
                    nzo_id, dl.id,
                )
            elif job_type == "webdl" and unclaimed_web:
                dl = max(unclaimed_web, key=lambda d: d.id)
                dl_name = _extract_download_name(dl, "webdl")
                matched = (str(dl.id), "webdl", dl.status, dl.progress, dl.size, dl_name)
                unclaimed_web.remove(dl)
                logger.debug(
                    "State sync: matched orphaned webdl job %s → torbox_id=%s by type fallback",
                    nzo_id, dl.id,
                )

        if matched:
            torbox_id, torbox_type, torbox_status, progress, size, dl_name = matched
            logger.info(
                "State sync: reconciled orphaned job %s → torbox_id=%s (type=%s) via URL matching",
                nzo_id, torbox_id, torbox_type,
            )

            # Update the job with the torbox_id so future polls can match directly
            try:
                await db.conn.execute(
                    "UPDATE jobs SET torbox_id = ?, torbox_type = ? WHERE nzo_id = ?",
                    (torbox_id, torbox_type, nzo_id),
                )
                await db.conn.commit()
            except Exception:
                logger.exception("State sync: failed to update torbox_id for %s", nzo_id)
                continue

            # Now update status/progress like a normal matched job
            local_status = _map_torbox_status(torbox_status, progress)
            percentage = min(100, max(0, int(progress * 100)))
            # Normalise the display status: if Torbox returns an empty string
            # but the download is complete (progress=100%), show "cached".
            display_torbox_state = torbox_status if torbox_status else ("cached" if progress >= 1.0 else "queued")

            # Determine effective status — mark completed downloads as "Fetching"
            # so the CDN processor can handle them without blocking the poller.
            is_completed = (
                torbox_status.lower() in COMPLETED_STATUSES
                or (not torbox_status and progress >= 1.0)
                or (local_status == "Complete")
            )
            effective_status = "Fetching" if (is_completed and download_on_complete) else local_status

            try:
                # Update filename if we have a name from Torbox
                if dl_name:
                    await db.conn.execute(
                        """UPDATE jobs SET
                            status = ?, percentage = ?, size = ?,
                            sizeleft = ?, torbox_state = ?, filename = ?,
                            speed = ?, last_progress_change = ?
                        WHERE nzo_id = ?""",
                        (
                            effective_status,
                            percentage,
                            size,
                            size * (1.0 - progress) if progress < 1.0 else 0,
                            display_torbox_state,
                            dl_name,
                            0.0,  # speed — not computed for orphaned jobs
                            now,   # last_progress_change — initialize to now
                            nzo_id,
                        ),
                    )
                else:
                    await db.conn.execute(
                        """UPDATE jobs SET
                            status = ?, percentage = ?, size = ?,
                            sizeleft = ?, torbox_state = ?,
                            speed = ?, last_progress_change = ?
                        WHERE nzo_id = ?""",
                        (
                            effective_status,
                            percentage,
                            size,
                            size * (1.0 - progress) if progress < 1.0 else 0,
                            display_torbox_state,
                            0.0,  # speed — not computed for orphaned jobs
                            now,   # last_progress_change — initialize to now
                            nzo_id,
                        ),
                    )
                await db.conn.commit()
            except Exception:
                logger.exception("State sync: failed to update status for %s", nzo_id)
                continue

            # Handle failure — completion is handled by the CDN processor
            if torbox_status.lower() in FAILED_STATUSES:
                await _fail_job(db, nzo_id, torbox_status, now)
        else:
            logger.debug(
                "State sync: no Torbox match found for orphaned job %s (url=%s, type=%s)",
                nzo_id, nzo_url[:80] if nzo_url else "", job_type,
            )


async def run_cdn_processor(app: FastAPI, cancelled: asyncio.Event) -> None:
    """Background task that processes jobs waiting for CDN download.

    Finds jobs with status 'Fetching' (completed on Torbox but not yet
    downloaded locally), requests CDN links, downloads files, and moves
    completed jobs to history. Downloads run concurrently, limited by
    the cdn_download_concurrency config setting.

    This runs alongside the state sync poller so that CDN downloads
    don't block status updates for other jobs in the queue.

    Args:
        app: The FastAPI application instance.
        cancelled: An asyncio.Event set on shutdown to signal exit.
    """
    logger.info("CDN processor starting")

    while not cancelled.is_set():
        config = getattr(app.state, "config", None)
        db = getattr(app.state, "db", None)

        if config is None or db is None:
            await asyncio.sleep(2)
            continue

        api_key = await config.get("torbox", "api_key")
        if not api_key:
            await asyncio.sleep(15)
            continue

        # Find jobs that are ready for CDN download
        try:
            cursor = await db.conn.execute(
                "SELECT nzo_id, torbox_id, torbox_type, category FROM jobs "
                "WHERE status = 'Fetching' AND torbox_id IS NOT NULL "
                "ORDER BY position"
            )
            fetching_jobs = await cursor.fetchall()
        except Exception:
            logger.exception("CDN processor: error querying jobs")
            await asyncio.sleep(5)
            continue

        if not fetching_jobs:
            # No fetching jobs — pause before checking again
            for _ in range(3):
                if cancelled.is_set():
                    break
                await asyncio.sleep(1)
            continue

        base_url = await config.get("torbox", "base_url", "https://api.torbox.app/v1")
        concurrency = int(await config.get("torbox", "cdn_download_concurrency", "2"))
        semaphore = asyncio.Semaphore(max(1, concurrency))

        client = TorboxClient(api_key=api_key, base_url=base_url)
        now = time.time()

        try:
            # Process all fetching jobs concurrently (limited by semaphore)
            async def _process_job(nzo_id: str, tb_id, tb_type: str, category: str):
                try:
                    await _complete_job(
                        db, client, nzo_id, str(tb_id), tb_type,
                        config, semaphore, now, category=category,
                    )
                except Exception:
                    logger.exception("CDN processor: error processing job %s", nzo_id)

            tasks = [
                asyncio.create_task(_process_job(row[0], row[1], row[2], row[3] or "*"))
                for row in fetching_jobs
            ]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        except Exception:
            logger.exception("CDN processor: unexpected error")
        finally:
            await client.close()

        # Brief pause before next check
        for _ in range(2):
            if cancelled.is_set():
                break
            await asyncio.sleep(1)

    logger.info("CDN processor stopped")