"""State translation and response builders for the qBittorrent API.

Maps DebridNZBd's internal data model to qBittorrent-compatible
response formats. Handles state translation, hash lookups, and
torrent info dict construction.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
#  State translation                                                    #
# ------------------------------------------------------------------ #

# Map DebridNZBd job status to qBittorrent torrent state.
# qBittorrent uses compound state names like "pausedDL", "stalledDL", etc.
_STATUS_TO_QBIT: dict[str, str] = {
    "Queued": "queuedDL",
    "Downloading": "downloading",
    "Paused": "pausedDL",
    "Fetching": "moving",
    "Complete": "uploading",
    "Failed": "error",
}


def debrid_status_to_qbit(status: str, speed: float = 0.0, stalled_since: float = 0) -> str:
    """Convert DebridNZBd job status to qBittorrent torrent state.

    For "Downloading" or "Fetching" status, uses stalled_since and speed
    to distinguish between "downloading" (active) and "stalledDL" (no progress).
    Jobs with stalled_since > 0 are considered stalled regardless of speed.
    """
    if stalled_since > 0 and status in ("Downloading", "Fetching"):
        return "stalledDL"
    if status == "Downloading" and speed <= 0:
        return "stalledDL"
    return _STATUS_TO_QBIT.get(status, "queuedDL")


# ------------------------------------------------------------------ #
#  Hash helpers                                                         #
# ------------------------------------------------------------------ #


def get_torrent_hash(torbox_hash: str, nzo_id: str, torbox_type: str) -> str:
    """Return the info hash for a job.

    For torrent-type jobs with a real hash, returns it directly.
    For non-torrent jobs, synthesizes a hash from the nzo_id so
    each job has a unique identifier for the qBittorrent API.
    """
    if torbox_hash:
        return torbox_hash.lower()
    # Synthesize a stable 40-char hex hash from the nzo_id
    return hashlib.sha1(nzo_id.encode()).hexdigest()


# ------------------------------------------------------------------ #
#  Torrent info dict builder                                            #
# ------------------------------------------------------------------ #


def build_torrent_info(
    row: tuple,
    save_path: str = "",
    torbox_type: str = "torrent",
) -> dict[str, Any]:
    """Build a qBittorrent torrent info dict from a database row.

    The row tuple should follow the column order from the jobs table
    query in the torrents/info endpoint. Expected columns:
    nzo_id, filename, nzo_url, category, priority, status,
    size, sizeleft, percentage, time_added, time_completed,
    torbox_id, torbox_type, torbox_hash, speed, tags, position,
    stalled_since

    Optionally, an 18th column ``local_path`` may be included. When
    present and non-empty, it is used as the ``content_path`` so that
    *arr clients can find the actual downloaded file.

    Args:
        row: Database row tuple.
        save_path: Absolute path to the download directory. *arr clients
            require an absolute path so they can apply their own remote
            path mappings. When empty, ``save_path`` and ``content_path``
            are set to empty strings for backward compatibility.
        torbox_type: The download type for hash synthesis.
    """
    # Unpack row — local_path is an optional 18th column
    if len(row) >= 18:
        (
            nzo_id, filename, nzo_url, category, priority, status,
            size, sizeleft, percentage, time_added, time_completed,
            torbox_id, torbox_type_val, torbox_hash, speed, tags,
            position, stalled_since, local_path,
        ) = row
        local_path = local_path or ""
    else:
        (
            nzo_id, filename, nzo_url, category, priority, status,
            size, sizeleft, percentage, time_added, time_completed,
            torbox_id, torbox_type_val, torbox_hash, speed, tags,
            position, stalled_since,
        ) = row
        local_path = ""

    # Safety net: never expose CDN URLs as file paths.
    if local_path.startswith(("http://", "https://")):
        local_path = ""

    info_hash = get_torrent_hash(torbox_hash or "", nzo_id, torbox_type_val)
    qbit_state = debrid_status_to_qbit(status, speed, stalled_since or 0)
    dloaded = size - sizeleft
    progress = percentage / 100.0 if percentage else 0.0

    # Estimate ETA from speed and remaining bytes
    eta = 8640000  # Default: ~100 days (unknown)
    if speed and speed > 0 and sizeleft > 0:
        eta = int(sizeleft / speed)

    return {
        "hash": info_hash,
        "name": filename or "unknown",
        "size": int(size) if size else 0,
        "progress": round(progress, 6),
        "dloaded": int(dloaded) if dloaded else 0,
        "uploaded": 0,  # Debrid: no upload
        "ratio": 0.0,
        "upspeed": 0,  # Debrid: no upload
        "dspeed": int(speed) if speed else 0,
        "state": qbit_state,
        "category": category or "",
        "tags": tags or "",
        "added_on": int(time_added) if time_added else 0,
        "completion_on": int(time_completed) if time_completed and time_completed > 0 else -1,
        "save_path": save_path,
        "content_path": local_path if local_path else (f"{save_path}/{filename}" if save_path and filename else save_path),
        "num_seeds": 0,
        "num_leechs": 0,
        "num_complete": 0,
        "num_incomplete": 0,
        "eta": eta,
        "dl_limit": -1,
        "up_limit": -1,
        "isPrivate": False,
        "force_start": False,
        "auto_tmm": False,
        "seq_dl": False,
        "f_l_piece_prio": False,
        "magnet_uri": nzo_url if (nzo_url or "").startswith("magnet:") else "",
        "tracker": "",
        "priority": position or 0,
    }


# ------------------------------------------------------------------ #
#  Filter helpers                                                       #
# ------------------------------------------------------------------ #

# Map qBittorrent filter values to sets of qBittorrent states
FILTER_STATES: dict[str, set[str]] = {
    "all": set(),
    "downloading": {"downloading", "stalledDL", "metaDL", "forcedDL"},
    "seeding": {"uploading", "stalledUP", "forcedUP"},
    "completed": {"uploading", "stalledUP", "forcedUP"},
    "stopped": {"pausedDL", "pausedUP"},
    "active": {"downloading", "uploading", "forcedDL", "forcedUP", "moving"},
    "inactive": {"pausedDL", "pausedUP", "stalledDL", "stalledUP", "queuedDL", "queuedUP"},
    "resumed": {"downloading", "uploading", "stalledDL", "stalledUP",
                "forcedDL", "forcedUP", "moving", "metaDL", "queuedDL", "queuedUP"},
    "stalled": {"stalledDL", "stalledUP"},
    "stalled_uploading": {"stalledUP"},
    "stalled_downloading": {"stalledDL"},
    "errored": {"error", "missingFiles", "unknown"},
    "running": {"downloading", "uploading", "forcedDL", "forcedUP", "moving"},
}


def matches_filter(qbit_state: str, filter_name: str) -> bool:
    """Check if a qBittorrent state matches a filter category."""
    if filter_name == "all" or not filter_name:
        return True
    allowed = FILTER_STATES.get(filter_name)
    if allowed is None:
        return True  # Unknown filter, show everything
    return qbit_state in allowed