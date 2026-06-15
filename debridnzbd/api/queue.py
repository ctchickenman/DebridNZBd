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
from dataclasses import dataclass
from pathlib import Path
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


# ------------------------------------------------------------------ #
#  Duplicate detection                                                   #
# ------------------------------------------------------------------ #

# Statuses that indicate a download is available on Torbox CDN
# (completed, cached, or seeding — meaning CDN download is possible)
_CDN_AVAILABLE_STATUSES = {"completed", "cached", "seeding"}


@dataclass
class DuplicateCheckResult:
    """Result of checking for a duplicate download in history or active queue.

    Attributes:
        action: One of "duplicate_active" (already in the active queue),
            "reuse_local" (file on disk), "redownload_cdn" (cached on
            Torbox CDN but not on disk), "resubmit" (in history but not
            on disk or CDN), or "new" (not found).
        history_row: The matching history row tuple, or None.
        local_path: Absolute path to the existing file on disk, or None.
        size: File size from the history entry, or 0.
        nzo_id: The nzo_id of the existing active job (for duplicate_active).
    """
    action: str
    history_row: tuple | None = None
    local_path: str | None = None
    size: float = 0.0
    nzo_id: str | None = None


def normalize_url(url: str) -> str:
    """Normalize a URL for duplicate comparison.

    Strips trailing slashes, lowercases the scheme and host, and sorts
    query parameters so that ``?a=1&b=2`` matches ``?b=2&a=1``.
    """
    url = url.strip()
    if not url:
        return ""
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    # Sort query parameters for consistent matching
    if parsed.query:
        params = sorted(parsed.query.split("&"))
        normalized_query = "&".join(params)
        return f"{scheme}://{netloc}{path}?{normalized_query}"
    return f"{scheme}://{netloc}{path}"


# Extensions to strip when normalizing download names for duplicate comparison.
# These are file-type suffixes that don't affect the content identity.
_ARCHIVE_EXTENSIONS = (".nzb", ".torrent", ".nzb.gz", ".par2", ".rar", ".zip", ".7z")


def normalize_name(name: str) -> str:
    """Normalize a download name for duplicate comparison.

    Lowercases the name and strips common archive/file-type extensions
    so that ``Movie.2024.nzb`` and ``Movie.2024`` are treated as the same
    download.  This mirrors SABnzbd's approach of matching on the
    "final name" after stripping file-specific suffixes.

    Args:
        name: The download filename or display name.

    Returns:
        The normalized name (lowercase, extensions stripped).
    """
    name = name.strip().lower()
    # Strip extensions from longest to shortest to handle compound
    # suffixes like .nzb.gz before single suffixes like .nzb.
    for ext in sorted(_ARCHIVE_EXTENSIONS, key=len, reverse=True):
        if name.endswith(ext):
            name = name[: -len(ext)]
            break
    return name


def _build_job_queries(
    normalized_name: str,
    normalized_url: str,
    normalized_hash: str,
    url_type: str,
    original_name_lower: str = "",
) -> list[tuple[str, str, tuple]]:
    """Build ordered SQL queries for checking the jobs table.

    Returns a list of (match_method, query, params) tuples to try in
    order: name → URL → hash.  Each query returns (nzo_id, status,
    local_path) from the most recent matching row.

    Name matching tries multiple forms to handle extension differences:
    the stored filename may have ``.nzb`` or ``.torrent`` extensions that
    the search name doesn't (or vice versa).  We match against the
    normalized name (extension stripped) plus common extension variants.
    """
    queries: list[tuple[str, str, tuple]] = []

    # Primary: name-based match (case-insensitive via LOWER())
    # Try the normalized name (stripped of extension) plus common
    # extension variants, and the original name as provided.  This
    # catches the case where "Movie.2024.Group" (no extension) matches
    # a stored "Movie.2024.Group.nzb" (with extension).
    if normalized_name:
        name_forms = {normalized_name}
        for ext in (".nzb", ".torrent", ".nzb.gz"):
            name_forms.add(normalized_name + ext)
        if original_name_lower and original_name_lower != normalized_name:
            name_forms.add(original_name_lower)
        # Deduplicate and sort for deterministic query generation
        names_list = sorted(name_forms)
        placeholders = ",".join("?" * len(names_list))
        queries.append((
            "name",
            f"SELECT nzo_id, status, local_path FROM jobs "
            f"WHERE LOWER(filename) IN ({placeholders}) "
            f"ORDER BY time_added DESC LIMIT 1",
            tuple(names_list),
        ))

    # Secondary: URL match
    if normalized_url:
        queries.append((
            "url",
            "SELECT nzo_id, status, local_path FROM jobs "
            "WHERE nzo_url = ? AND torbox_type = ? "
            "ORDER BY time_added DESC LIMIT 1",
            (normalized_url, url_type),
        ))

    # Tertiary: hash match
    if normalized_hash:
        queries.append((
            "hash",
            "SELECT nzo_id, status, local_path FROM jobs "
            "WHERE torbox_hash = ? "
            "ORDER BY time_added DESC LIMIT 1",
            (normalized_hash,),
        ))

    return queries


def _build_history_queries(
    normalized_name: str,
    normalized_url: str,
    normalized_hash: str,
    url_type: str,
    original_name_lower: str = "",
) -> list[tuple[str, str, tuple]]:
    """Build ordered SQL queries for checking the history table.

    Returns a list of (match_method, query, params) tuples to try in
    order: name → URL → hash.  Each query returns the full history row.
    Failed downloads are excluded from matching so they don't block retries.
    """
    history_columns = (
        "nzo_id, name, status, size, category, nzo_url, "
        "torbox_id, torbox_type, path, storage, torbox_hash"
    )
    queries: list[tuple[str, str, tuple]] = []

    # Primary: name-based match (case-insensitive via LOWER())
    if normalized_name:
        name_forms = {normalized_name}
        for ext in (".nzb", ".torrent", ".nzb.gz"):
            name_forms.add(normalized_name + ext)
        if original_name_lower and original_name_lower != normalized_name:
            name_forms.add(original_name_lower)
        names_list = sorted(name_forms)
        placeholders = ",".join("?" * len(names_list))
        queries.append((
            "name",
            f"SELECT {history_columns} FROM history "
            f"WHERE LOWER(name) IN ({placeholders}) AND status != 'Failed' "
            f"ORDER BY completed DESC LIMIT 1",
            tuple(names_list),
        ))

    # Secondary: URL match
    if normalized_url:
        queries.append((
            "url",
            f"SELECT {history_columns} FROM history "
            "WHERE nzo_url = ? AND torbox_type = ? AND status != 'Failed' "
            "ORDER BY completed DESC LIMIT 1",
            (normalized_url, url_type),
        ))

    # Tertiary: hash match
    if normalized_hash:
        queries.append((
            "hash",
            f"SELECT {history_columns} FROM history "
            "WHERE torbox_hash = ? AND status != 'Failed' "
            "ORDER BY completed DESC LIMIT 1",
            (normalized_hash,),
        ))

    return queries


async def handle_duplicate_check(
    db: object,
    config: object,
    url: str,
    url_type: str,
    torbox_hash: str = "",
    name: str = "",
) -> DuplicateCheckResult:
    """Check if a download already exists in the active queue or history.

    Detection order (each step short-circuits on a match):

    1. **Name match** — Case-insensitive comparison of the normalized
       download name against ``jobs.filename`` and ``history.name``.
       This is the primary mechanism, matching SABnzbd's approach.
       Catches duplicates regardless of source URL or upload method.

    2. **URL match** — Exact match on the normalized URL in
       ``jobs.nzo_url`` and ``history.nzo_url`` (for URL submissions).

    3. **Hash match** — Exact match on ``torbox_hash`` in ``jobs``
       and ``history`` (for .torrent file uploads).

    For each table (jobs first, then history), the checks are applied
    in order: name → URL → hash. The first match wins.

    When a match is found in the active queue (``jobs`` table), returns
    ``duplicate_active`` (or ``reuse_local`` if Complete with file on disk).
    When found only in history, checks local disk and CDN availability.

    Args:
        db: Database instance.
        config: ConfigStore instance.
        url: The original submission URL (empty string for file uploads).
        url_type: Download type ("usenet", "torrent", or "webdl").
        torbox_hash: Torrent info hash for file upload dedup (optional).
        name: The download display name for name-based matching (optional).

    Returns:
        DuplicateCheckResult with the recommended action.
    """
    if db is None or db.conn is None:
        return DuplicateCheckResult(action="new")

    # Check if duplicate detection is enabled (values: "0"=off, "1"=basic, "2"=smart)
    # Both "1" and "2" enable detection. Name-based matching is always on when
    # detection is enabled (basic and smart), matching SABnzbd's behavior.
    dup_val = await config.get("switches", "duplicate_detection", "0")
    if dup_val not in ("1", "2"):
        return DuplicateCheckResult(action="new")

    normalized_name = normalize_name(str(name)) if name else ""
    original_name_lower = str(name).strip().lower() if name else ""
    normalized_url = normalize_url(url) if url else ""
    normalized_hash = torbox_hash.lower() if torbox_hash else ""

    # --- Step 1: Check the active jobs table ---
    # Try name match first (primary), then URL, then hash.
    # Each query returns at most one row (most recent).
    for match_method, query, params in _build_job_queries(
        normalized_name, normalized_url, normalized_hash, url_type,
        original_name_lower=original_name_lower,
    ):
        cursor = await db.conn.execute(query, params)
        job_row = await cursor.fetchone()
        if job_row:
            job_nzo_id, job_status, job_local_path = job_row
            logger.info(
                "duplicate_check: found active job nzo_id=%s status=%s "
                "via %s match — returning duplicate_active",
                job_nzo_id, job_status, match_method,
            )
            if job_status == "Complete" and job_local_path and Path(job_local_path).exists():
                return DuplicateCheckResult(
                    action="reuse_local",
                    local_path=job_local_path,
                    size=0.0,
                    nzo_id=job_nzo_id,
                )
            return DuplicateCheckResult(action="duplicate_active", nzo_id=job_nzo_id)

    # --- Step 2: Check the history table ---
    # Same order: name → URL → hash.
    for match_method, query, params in _build_history_queries(
        normalized_name, normalized_url, normalized_hash, url_type,
        original_name_lower=original_name_lower,
    ):
        cursor = await db.conn.execute(query, params)
        row = await cursor.fetchone()
        if row:
            (hist_nzo_id, hist_name, hist_status, hist_size, hist_category,
             hist_nzo_url, hist_torbox_id, hist_torbox_type, hist_path,
             hist_storage, hist_torbox_hash) = row

            logger.info(
                "duplicate_check: found history entry nzo_id=%s name=%r "
                "status=%s path=%r torbox_id=%s via %s match",
                hist_nzo_id, hist_name, hist_status, hist_path,
                hist_torbox_id, match_method,
            )

            # Check if the local file still exists on disk
            if hist_path:
                file_path = Path(hist_path)
                if file_path.exists():
                    logger.info(
                        "duplicate_check: file exists on disk at %r — reusing local file",
                        hist_path,
                    )
                    return DuplicateCheckResult(
                        action="reuse_local",
                        history_row=row,
                        local_path=hist_path,
                        size=hist_size or 0.0,
                    )

            # File not on disk — check if the content is cached on Torbox CDN
            # so we can re-download without re-submitting to Torbox.
            if hist_torbox_id:
                try:
                    from debridnzbd.core.state_sync import check_torbox_availability

                    torbox_api_key = await config.get("torbox", "api_key")
                    base_url = await config.get("torbox", "base_url", "https://api.torbox.app/v1")
                    client = TorboxClient(api_key=torbox_api_key, base_url=base_url)
                    try:
                        torbox_status, is_cdn_available, progress, actual_type = (
                            await check_torbox_availability(
                                client, str(hist_torbox_id), hist_torbox_type
                            )
                        )
                        if is_cdn_available:
                            logger.info(
                                "duplicate_check: content cached on Torbox CDN "
                                "(status=%s) — will re-download",
                                torbox_status,
                            )
                            return DuplicateCheckResult(
                                action="redownload_cdn",
                                history_row=row,
                                local_path=None,
                                size=hist_size or 0.0,
                            )
                    finally:
                        await client.close()
                except Exception:
                    logger.debug(
                        "duplicate_check: Torbox availability check failed for "
                        "torbox_id=%s, falling back to resubmit",
                        hist_torbox_id,
                        exc_info=True,
                    )

            # In history but not on disk and not on CDN — resubmit normally
            logger.info(
                "duplicate_check: found in history but not on disk or CDN — resubmitting"
            )
            return DuplicateCheckResult(
                action="resubmit",
                history_row=row,
                local_path=None,
                size=hist_size or 0.0,
            )

    # No match found in either table
    return DuplicateCheckResult(action="new")

    job_row = await cursor.fetchone()
    if job_row:
        job_nzo_id, job_status, job_local_path = job_row
        logger.info(
            "duplicate_check: found active job nzo_id=%s status=%s — "
            "returning duplicate_active",
            job_nzo_id, job_status,
        )
        # If the job is Complete and the file exists on disk, return reuse_local
        # so the caller can create a fresh Completed job pointing to the file.
        if job_status == "Complete" and job_local_path and Path(job_local_path).exists():
            return DuplicateCheckResult(
                action="reuse_local",
                local_path=job_local_path,
                size=0.0,
                nzo_id=job_nzo_id,
            )
        return DuplicateCheckResult(action="duplicate_active", nzo_id=job_nzo_id)

    # --- Step 2: Check the history table ---
    # Build the query — match by URL (for addurl) or by hash (for addfile)
    if url:
        normalized = normalize_url(url)
        cursor = await db.conn.execute(
            "SELECT nzo_id, name, status, size, category, nzo_url, "
            "torbox_id, torbox_type, path, storage, torbox_hash "
            "FROM history WHERE nzo_url = ? AND torbox_type = ? "
            "ORDER BY completed DESC LIMIT 1",
            (normalized, url_type),
        )
    elif torbox_hash:
        cursor = await db.conn.execute(
            "SELECT nzo_id, name, status, size, category, nzo_url, "
            "torbox_id, torbox_type, path, storage, torbox_hash "
            "FROM history WHERE torbox_hash = ? "
            "ORDER BY completed DESC LIMIT 1",
            (torbox_hash.lower(),),
        )
    else:
        return DuplicateCheckResult(action="new")

    row = await cursor.fetchone()
    if not row:
        return DuplicateCheckResult(action="new")

    (hist_nzo_id, hist_name, hist_status, hist_size, hist_category,
     hist_nzo_url, hist_torbox_id, hist_torbox_type, hist_path,
     hist_storage, hist_torbox_hash) = row

    logger.info(
        "duplicate_check: found history entry nzo_id=%s name=%r "
        "status=%s path=%r torbox_id=%s",
        hist_nzo_id, hist_name, hist_status, hist_path, hist_torbox_id,
    )

    # Check if the local file still exists on disk
    if hist_path:
        file_path = Path(hist_path)
        if file_path.exists():
            logger.info(
                "duplicate_check: file exists on disk at %r — reusing local file",
                hist_path,
            )
            return DuplicateCheckResult(
                action="reuse_local",
                history_row=row,
                local_path=hist_path,
                size=hist_size or 0.0,
            )

    # File not on disk — check if the content is cached on Torbox CDN
    # so we can re-download without re-submitting to Torbox.
    if hist_torbox_id:
        try:
            from debridnzbd.core.state_sync import check_torbox_availability

            torbox_api_key = await config.get("torbox", "api_key")
            base_url = await config.get("torbox", "base_url", "https://api.torbox.app/v1")
            client = TorboxClient(api_key=torbox_api_key, base_url=base_url)
            try:
                torbox_status, is_cdn_available, progress, actual_type = (
                    await check_torbox_availability(
                        client, str(hist_torbox_id), hist_torbox_type
                    )
                )
                if is_cdn_available:
                    logger.info(
                        "duplicate_check: content cached on Torbox CDN "
                        "(status=%s) — will re-download",
                        torbox_status,
                    )
                    return DuplicateCheckResult(
                        action="redownload_cdn",
                        history_row=row,
                        local_path=None,
                        size=hist_size or 0.0,
                    )
            finally:
                await client.close()
        except Exception:
            logger.debug(
                "duplicate_check: Torbox availability check failed for "
                "torbox_id=%s, falling back to resubmit",
                hist_torbox_id,
                exc_info=True,
            )

    # In history but not on disk and not on CDN — resubmit normally
    logger.info(
        "duplicate_check: found in history but not on disk or CDN — resubmitting"
    )
    return DuplicateCheckResult(
        action="resubmit",
        history_row=row,
        local_path=None,
        size=hist_size or 0.0,
    )


def detect_file_type(filename: str, default_type: str = "usenet") -> str:
    """Detect the download type from the uploaded filename extension.

    Classification rules (in order of precedence):
    1. ``.torrent`` extension → "torrent"
    2. ``.nzb`` extension → "usenet"
    3. Unknown extension → the configured default type

    Args:
        filename: The uploaded file's original name.
        default_type: Fallback type when the extension doesn't match known
            patterns. Should be one of "usenet", "torrent", or "webdl".

    Returns:
        One of "usenet", "torrent", or "webdl".
    """
    name_lower = filename.lower()
    if name_lower.endswith(".torrent"):
        return "torrent"
    if name_lower.endswith(".nzb"):
        return "usenet"
    if default_type not in VALID_TYPES:
        default_type = "usenet"
    return default_type


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

    # Normalize the URL for consistent storage and duplicate detection.
    # The original URL is preserved for Torbox submission, but the database
    # stores the normalized form so that future duplicate checks can match
    # regardless of query parameter order, case differences, etc.
    url = normalize_url(url) or url  # fall back to raw URL if normalization returns empty

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
    filename = _derive_filename(url, params.get("nzbname"))

    logger.info("addurl: type=%s url=%s", url_type, url[:100])

    # --- Duplicate detection ---
    # Check if this URL has already been downloaded or is in the active queue.
    # If active in the queue, return the existing nzo_id. If on disk, reuse it.
    # If cached on Torbox CDN, re-download. Otherwise, proceed normally.
    dup_result = await handle_duplicate_check(db, config, url, url_type, name=filename)

    if dup_result.action == "duplicate_active":
        # URL is already in the active queue — return the existing nzo_id
        logger.info(
            "addurl: duplicate active job found nzo_id=%s — returning existing ID",
            dup_result.nzo_id,
        )
        return JSONResponse(content={"status": True, "nzo_ids": [dup_result.nzo_id]})

    if dup_result.action == "reuse_local":
        # File already exists on disk. If this match came from the active jobs
        # table (nzo_id is set), return the existing job ID. Otherwise (from
        # history table), create a new Completed job.
        if dup_result.nzo_id:
            logger.info(
                "addurl: duplicate Complete job on disk nzo_id=%s path=%r — "
                "returning existing ID",
                dup_result.nzo_id, dup_result.local_path,
            )
            return JSONResponse(content={"status": True, "nzo_ids": [dup_result.nzo_id]})

        nzo_id = generate_nzo_id()
        logger.info(
            "addurl: duplicate found on disk nzo_id=%s path=%r — reusing local file",
            nzo_id, dup_result.local_path,
        )
        category = params.get("cat") or params.get("category") or "*"
        priority = int(params.get("priority") or 0)
        script = params.get("script") or "Default"
        password = params.get("password") or ""
        post_processing = int(params.get("pp") or -1)
        now = time.time()
        hist = dup_result.history_row

        try:
            cursor = await db.conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM jobs"
            )
            row = await cursor.fetchone()
            position = row[0] if row else 0
        except Exception:
            position = 0

        try:
            await db.conn.execute(
                """INSERT INTO jobs (
                    nzo_id, filename, password, nzo_url, category, script, priority, pp,
                    status, size, sizeleft, percentage, time_added, time_completed,
                    torbox_id, torbox_type, torbox_hash, position, torbox_state,
                    cdn_link, local_path, speed, download_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    nzo_id,
                    filename or (hist[1] if hist else "unknown"),
                    password,
                    url,
                    category,
                    script,
                    priority,
                    post_processing,
                    "Complete",
                    dup_result.size or 0,
                    0,  # sizeleft
                    100,  # percentage
                    now,
                    now,  # time_completed
                    hist[6] if hist else None,  # torbox_id
                    url_type,
                    hist[10] if hist else "",  # torbox_hash
                    position,
                    "completed",
                    hist[9] if hist else "",  # cdn_link (storage)
                    dup_result.local_path,
                    0,  # speed
                    0,  # download_time
                ),
            )
            await db.conn.commit()
        except Exception:
            logger.exception("addurl: failed to insert reused job nzo_id=%s", nzo_id)

        return JSONResponse(content={"status": True, "nzo_ids": [nzo_id]})

    elif dup_result.action == "redownload_cdn":
        # Content is cached on Torbox CDN — create a Fetching job to re-download
        nzo_id = generate_nzo_id()
        logger.info(
            "addurl: duplicate found on CDN nzo_id=%s torbox_id=%s — re-downloading",
            nzo_id, hist[6] if hist else "?",
        )
        category = params.get("cat") or params.get("category") or "*"
        priority = int(params.get("priority") or 0)
        script = params.get("script") or "Default"
        password = params.get("password") or ""
        post_processing = int(params.get("pp") or -1)
        now = time.time()
        hist = dup_result.history_row

        try:
            cursor = await db.conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM jobs"
            )
            row = await cursor.fetchone()
            position = row[0] if row else 0
        except Exception:
            position = 0

        try:
            await db.conn.execute(
                """INSERT INTO jobs (
                    nzo_id, filename, password, nzo_url, category, script, priority, pp,
                    status, size, sizeleft, percentage, time_added,
                    torbox_id, torbox_type, torbox_hash, position, torbox_state,
                    cdn_link, local_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    nzo_id,
                    filename or (hist[1] if hist else "unknown"),
                    password,
                    url,
                    category,
                    script,
                    priority,
                    post_processing,
                    "Fetching",
                    dup_result.size or 0,
                    0,  # sizeleft
                    100,  # percentage — CDN content is complete
                    now,
                    hist[6] if hist else None,  # torbox_id
                    url_type,
                    hist[10] if hist else "",  # torbox_hash
                    position,
                    "completed",  # torbox_state — content is available on CDN
                    "",  # cdn_link — will be requested by CDN processor
                    "",  # local_path — will be set by CDN processor
                ),
            )
            await db.conn.commit()
        except Exception:
            logger.exception("addurl: failed to insert CDN re-download job nzo_id=%s", nzo_id)

        return JSONResponse(content={"status": True, "nzo_ids": [nzo_id]})

    # --- No duplicate found or duplicate detection disabled — proceed normally ---
    nzo_id = generate_nzo_id()

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


async def handle_addfile(params: dict) -> JSONResponse:
    """Handle ?mode=addfile — upload a torrent or NZB file to Torbox.

    Accepts a ``.torrent`` or ``.nzb`` file upload and submits it to the
    appropriate Torbox API endpoint. Creates a local job entry in the
    database for tracking.

    SABnzbd-compatible parameters:
        nzbfile: The uploaded file (passed via _upload_file_data /
            _upload_file_name keys set by the router)
        cat / category: Download category (default: "*")
        priority: Priority (-100=paused, 0=normal, 1=low, 2=high)
        nzbname: Custom display name for the job
        pp: Post-processing option (-1=default, 0=none, 1=repair,
            2=unpack, 3=unpack+delete)

    Returns:
        JSONResponse with SABnzbd-compatible format:
        Success: {"status": true, "nzo_ids": ["SABnzbd_nzo_..."]}
        Failure: {"status": false, "error": "error message"}
    """
    request = params.get("request")

    # --- Extract file data from params (set by router from multipart upload) ---
    file_data: bytes | None = params.get("_upload_file_data")
    file_name: str = params.get("_upload_file_name", "")

    if not file_data:
        return JSONResponse(
            status_code=400,
            content={"status": False, "error": "No file uploaded"},
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

    # --- Detect file type from extension ---
    url_type = detect_file_type(file_name, default_type)
    nzo_id = generate_nzo_id()

    # Derive display name: prefer nzbname param, then strip extension from filename
    nzbname_param = params.get("nzbname")
    if nzbname_param and nzbname_param.strip():
        filename = nzbname_param.strip()
    else:
        # Strip extension from uploaded filename for a cleaner display name
        base = file_name.rsplit(".", 1)[0] if "." in file_name else file_name
        filename = base or "file_upload"

    logger.info("addfile: nzo_id=%s type=%s filename=%s size=%d", nzo_id, url_type, file_name, len(file_data))

    # --- Submit to Torbox ---
    client = TorboxClient(api_key=torbox_api_key, base_url=base_url)
    torbox_id = None
    torbox_hash = ""

    try:
        if url_type == "torrent":
            result = await client.create_torrent(
                file_data=file_data,
                file_name=file_name or "upload.torrent",
            )
        elif url_type == "usenet":
            result = await client.create_usenet_download(
                file_data=file_data,
                file_name=file_name or "upload.nzb",
                post_processing=post_processing,
            )
        else:
            return JSONResponse(
                status_code=400,
                content={"status": False, "error": f"Unsupported file type for addfile: {url_type}"},
            )

        if not result.success:
            error_msg = result.detail or "Unknown error from Torbox"
            logger.warning("addfile: Torbox rejected nzo_id=%s: %s", nzo_id, error_msg)
            return JSONResponse(
                status_code=502,
                content={"status": False, "error": f"Torbox error: {error_msg}"},
            )

        # Extract Torbox download ID from response data (same logic as addurl)
        data = result.data
        logger.info(
            "addfile: Torbox create response for nzo_id=%s type=%s: "
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
                    logger.warning("addfile: could not parse torbox_id from %r", raw_id)
            torbox_hash = data.get("hash", "")
        elif isinstance(data, list) and data:
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
        logger.warning("addfile: Torbox auth failed for nzo_id=%s", nzo_id)
        return JSONResponse(
            status_code=502,
            content={"status": False, "error": "Torbox authentication failed — check your API key"},
        )
    except TorboxConnectionError:
        logger.warning("addfile: Cannot reach Torbox for nzo_id=%s", nzo_id)
        return JSONResponse(
            status_code=502,
            content={"status": False, "error": "Cannot connect to Torbox API"},
        )
    except TorboxRateLimitError:
        logger.warning("addfile: Torbox rate limited for nzo_id=%s", nzo_id)
        return JSONResponse(
            status_code=429,
            content={"status": False, "error": "Torbox rate limit exceeded, please retry later"},
        )
    except TorboxError as e:
        logger.error("addfile: Torbox error for nzo_id=%s: %s", nzo_id, e)
        return JSONResponse(
            status_code=502,
            content={"status": False, "error": f"Torbox error: {e}"},
        )
    except Exception as e:
        logger.exception("addfile: Unexpected error for nzo_id=%s", nzo_id)
        return JSONResponse(
            status_code=500,
            content={"status": False, "error": "Internal server error"},
        )
    finally:
        await client.close()

    # --- Fallback: query mylist to find torbox_id ---
    if torbox_id is None:
        logger.info(
            "addfile: no torbox_id from creation response for nzo_id=%s type=%s, "
            "querying mylist to find it",
            nzo_id, url_type,
        )
        try:
            fallback_client = TorboxClient(api_key=torbox_api_key, base_url=base_url)
            try:
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
                    for dl in sorted(recent_list, key=lambda d: d.id, reverse=True):
                        if str(dl.id) in existing_ids:
                            continue
                        torbox_id = dl.id
                        if hasattr(dl, "hash") and dl.hash:
                            torbox_hash = dl.hash
                        logger.info(
                            "addfile: matched torbox_id=%s from mylist for nzo_id=%s type=%s",
                            torbox_id, nzo_id, url_type,
                        )
                        break
                    else:
                        logger.warning(
                            "addfile: all downloads in mylist already claimed for nzo_id=%s type=%s",
                            nzo_id, url_type,
                        )
                else:
                    logger.warning(
                        "addfile: mylist returned empty for nzo_id=%s type=%s",
                        nzo_id, url_type,
                    )
            finally:
                await fallback_client.close()
        except Exception:
            logger.warning(
                "addfile: failed to query mylist fallback for nzo_id=%s",
                nzo_id, exc_info=True,
            )

    # --- Resolve the display name from Torbox ---
    if not nzbname_param and torbox_id:
        real_name = await _fetch_download_name(
            torbox_api_key, base_url, torbox_id, url_type,
        )
        if real_name:
            filename = real_name
            logger.info(
                "addfile: using Torbox name %r instead of file-derived name for nzo_id=%s",
                real_name, nzo_id,
            )

    # --- Duplicate detection for file uploads ---
    # Check for duplicates using name-based matching (all file types) and
    # hash-based matching (torrents only). Name-based matching catches
    # re-uploads of the same NZB or torrent regardless of the source URL.
    # URL-based matching is not applicable for file uploads (nzo_url is empty).
    dup_result = await handle_duplicate_check(
        db, config, url="", url_type=url_type,
        torbox_hash=torbox_hash if url_type == "torrent" else "",
        name=filename,
    )

    if dup_result.action == "duplicate_active":
        # Already in the active queue — delete the duplicate from Torbox
        # (if one was submitted) and return the existing nzo_id
        logger.info(
            "addfile: duplicate active download found nzo_id=%s — "
            "deleting Torbox download and returning existing ID",
            dup_result.nzo_id,
        )
        if torbox_id:
            try:
                cleanup_client = TorboxClient(api_key=torbox_api_key, base_url=base_url)
                await cleanup_client.control_torrent(int(torbox_id), "Delete")
                await cleanup_client.close()
            except Exception:
                logger.debug("addfile: failed to delete duplicate Torbox download %s", torbox_id)
        return JSONResponse(content={"status": True, "nzo_ids": [dup_result.nzo_id]})

    if dup_result.action == "reuse_local":
        # File already on disk. If this match came from the active jobs
        # table (nzo_id is set), delete the duplicate from Torbox and
        # return the existing job ID. Otherwise (from history), create a
        # new Completed job.
        if dup_result.nzo_id:
            logger.info(
                "addfile: duplicate Complete download on disk nzo_id=%s path=%r — "
                "deleting Torbox download and returning existing ID",
                dup_result.nzo_id, dup_result.local_path,
            )
            if torbox_id:
                try:
                    cleanup_client = TorboxClient(api_key=torbox_api_key, base_url=base_url)
                    await cleanup_client.control_torrent(int(torbox_id), "Delete")
                    await cleanup_client.close()
                except Exception:
                    logger.debug("addfile: failed to delete duplicate Torbox download %s", torbox_id)
            return JSONResponse(content={"status": True, "nzo_ids": [dup_result.nzo_id]})

        logger.info(
            "addfile: duplicate download found on disk nzo_id=%s path=%r — "
            "deleting Torbox download and reusing local file",
            nzo_id, dup_result.local_path,
        )
        if torbox_id:
            try:
                cleanup_client = TorboxClient(api_key=torbox_api_key, base_url=base_url)
                await cleanup_client.control_torrent(int(torbox_id), "Delete")
                await cleanup_client.close()
            except Exception:
                logger.debug("addfile: failed to delete duplicate Torbox download %s", torbox_id)

        category = params.get("cat") or params.get("category") or "*"
        priority = int(params.get("priority") or 0)
        script = params.get("script") or "Default"
        password = params.get("password") or ""
        post_processing = int(params.get("pp") or -1)
        now = time.time()
        hist = dup_result.history_row

        try:
            cursor = await db.conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM jobs"
            )
            row = await cursor.fetchone()
            position = row[0] if row else 0
        except Exception:
            position = 0

        try:
            await db.conn.execute(
                """INSERT INTO jobs (
                    nzo_id, filename, password, nzo_url, category, script, priority, pp,
                    status, size, sizeleft, percentage, time_added, time_completed,
                    torbox_id, torbox_type, torbox_hash, position, torbox_state,
                    cdn_link, local_path, speed, download_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    nzo_id,
                    filename or (hist[1] if hist else "unknown"),
                    password,
                    "",  # no URL for file uploads
                    category,
                    script,
                    priority,
                    post_processing,
                    "Complete",
                    dup_result.size or 0,
                    0,  # sizeleft
                    100,  # percentage
                    now,
                    now,  # time_completed
                    hist[6] if hist else None,  # torbox_id from history
                    url_type,
                    torbox_hash,
                    position,
                    "completed",
                    hist[9] if hist else "",  # cdn_link (storage)
                    dup_result.local_path,
                    0,  # speed
                    0,  # download_time
                ),
            )
            await db.conn.commit()
        except Exception:
            logger.exception("addfile: failed to insert reused job nzo_id=%s", nzo_id)

        return JSONResponse(content={"status": True, "nzo_ids": [nzo_id]})

    elif dup_result.action == "redownload_cdn":
        # Cached on CDN — delete the duplicate from Torbox and re-download
        logger.info(
            "addfile: duplicate download found on CDN nzo_id=%s — "
            "deleting Torbox download and re-downloading from CDN",
            nzo_id,
        )
        if torbox_id:
            try:
                cleanup_client = TorboxClient(api_key=torbox_api_key, base_url=base_url)
                await cleanup_client.control_torrent(int(torbox_id), "Delete")
                await cleanup_client.close()
            except Exception:
                logger.debug("addfile: failed to delete duplicate Torbox download %s", torbox_id)

        category = params.get("cat") or params.get("category") or "*"
        priority = int(params.get("priority") or 0)
        script = params.get("script") or "Default"
        password = params.get("password") or ""
        post_processing = int(params.get("pp") or -1)
        now = time.time()
        hist = dup_result.history_row

        try:
            cursor = await db.conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM jobs"
            )
            row = await cursor.fetchone()
            position = row[0] if row else 0
        except Exception:
            position = 0

        try:
            await db.conn.execute(
                """INSERT INTO jobs (
                    nzo_id, filename, password, nzo_url, category, script, priority, pp,
                    status, size, sizeleft, percentage, time_added,
                    torbox_id, torbox_type, torbox_hash, position, torbox_state,
                    cdn_link, local_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    nzo_id,
                    filename or (hist[1] if hist else "unknown"),
                    password,
                    "",  # no URL for file uploads
                    category,
                    script,
                    priority,
                    post_processing,
                    "Fetching",
                    dup_result.size or 0,
                    0,  # sizeleft
                    100,  # percentage — CDN content is complete
                    now,
                    hist[6] if hist else None,  # torbox_id from history
                    url_type,
                    torbox_hash,
                    position,
                    "completed",  # torbox_state — content available on CDN
                    "",  # cdn_link — will be requested by CDN processor
                    "",  # local_path — will be set by CDN processor
                ),
            )
            await db.conn.commit()
        except Exception:
            logger.exception("addfile: failed to insert CDN re-download job nzo_id=%s", nzo_id)

        return JSONResponse(content={"status": True, "nzo_ids": [nzo_id]})

    # --- No duplicate found or duplicate detection disabled — proceed normally ---

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
                    "",  # no URL for file uploads
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
                    "queued",  # torbox_state
                ),
            )
            await db.conn.commit()
            logger.info("addfile: created job nzo_id=%s type=%s torbox_id=%s", nzo_id, url_type, torbox_id)
        except Exception:
            logger.exception("addfile: failed to insert job nzo_id=%s into database", nzo_id)
    else:
        logger.warning("addfile: database not available, job nzo_id=%s not persisted", nzo_id)

    return JSONResponse(content={"status": True, "nzo_ids": [nzo_id]})


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

    # Read usenet-only jobs ordered by position.
    # The SABnzbd API shows only usenet jobs — torrent and webdl jobs are
    # managed through the qBittorrent API or processed in the background.
    cursor = await db.conn.execute(
        "SELECT nzo_id, filename, password, nzo_url, category, script, priority, pp, "
        "status, size, sizeleft, percentage, time_added, avg_age, "
        "torbox_id, torbox_type, torbox_hash, torbox_state, cdn_link, "
        "local_path, position, labels, stage_log, fail_message, speed, download_time, "
        "stalled_since "
        "FROM jobs WHERE torbox_type = 'usenet' ORDER BY position"
    )
    rows = await cursor.fetchall()

    # Build queue slots
    slots = []
    total_speed = 0.0
    total_size = 0.0
    total_sizeleft = 0.0
    now = time.time()

    # Resolve the complete directory to an absolute path for *arr clients.
    # This is used as the fallback path when a job's local_path is empty
    # (download not yet complete or CDN download failed).
    complete_dir_resolved = str(Path(
        await config.get("folders", "complete_dir", "downloads/complete")
    ).resolve()) if config else ""

    for row in rows:
        nzo_id = row[0]
        filename = row[1]
        category = row[4] or "*"
        script = row[5] or "Default"
        priority = row[6] or 0
        status = row[8] or "Queued"
        # Map internal status to SABnzbd queue status names.
        # *arr clients parse status via SabnzbdDownloadStatus enum which
        # expects "Completed" (with "d"), not "Complete".
        if status == "Complete":
            status = "Completed"
        elif status == "Failed":
            status = "Failed"
        size = row[9] or 0
        sizeleft = row[10] or 0
        percentage = row[11] or 0
        time_added = row[12] or 0
        avg_age = row[13] or ""
        speed = row[24] or 0
        stalled_since = row[26] or 0
        local_path = row[20] or ""

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

        # Compute stall state
        stalled = stalled_since > 0
        if stalled and stalled_since > 0:
            elapsed = now - stalled_since
            minutes = int(elapsed) // 60
            seconds = int(elapsed) % 60
            stall_duration = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"
        else:
            stall_duration = ""

        # Compute the output path for *arr clients.
        # storage = the file's full local path (on disk), never a CDN URL.
        # path    = the parent directory of the file.
        # When local_path is empty (download not yet complete or CDN download
        # failed), fall back to the resolved complete_dir so *arr clients
        # always get an absolute path they can use for remote path mapping.
        # Safety net: strip any CDN URL that should not be exposed to clients.
        if local_path.startswith(("http://", "https://")):
            local_path = ""
        if local_path:
            # Resolve to absolute path in case local_path is relative
            if not Path(local_path).is_absolute():
                local_path = str(Path(local_path).resolve())
            storage = local_path
            path = str(Path(local_path).parent)
        else:
            storage = complete_dir_resolved
            path = complete_dir_resolved

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
            stalled=stalled,
            stall_duration=stall_duration,
            storage=storage,
            path=path,
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
        # my_home (DefaultRootFolder): absolute path to the complete directory.
        # *arr clients (Sonarr, Radarr) read this as the base directory for
        # resolving relative complete_dir values when the SABnzbd version
        # is < 2.0. Since we report version 1.x, they use this field.
        my_home=complete_dir_resolved,
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


async def handle_retry_stalled(params: dict) -> JSONResponse:
    """Handle ?mode=retry_stalled — manually retry a stalled or stuck download.

    Checks Torbox to determine the best recovery action:
    - If the download is available on CDN (completed/cached/seeding),
      transitions the job to Fetching so the CDN processor re-downloads it.
    - If the download is still in progress on Torbox, sends Reannounce/Resume.
    - Always resets local stall tracking counters.

    Parameters:
        nzo_id: The nzo_id of the download to retry

    Returns:
        JSONResponse with status True and an "action" field indicating
        what was done: "cdn_retry", "reannounce", "reannounce_fallback",
        or "stall_counters_reset".
    """
    from debridnzbd.core.state_sync import check_torbox_availability, COMPLETED_STATUSES

    request = params.get("request")
    db = getattr(request.app.state, "db", None) if request else None
    config = getattr(request.app.state, "config", None) if request else None

    nzo_id = params.get("nzo_id") or ""
    if not nzo_id:
        return JSONResponse(
            status_code=400,
            content={"status": False, "error": "No nzo_id provided"},
        )

    if db is None or db.conn is None:
        return JSONResponse(
            status_code=500,
            content={"status": False, "error": "Database not available"},
        )

    # Look up the job — fetch additional columns for CDN retry
    cursor = await db.conn.execute(
        "SELECT torbox_id, torbox_type, stalled_since, status, "
        "cdn_link, local_path, filename, category FROM jobs WHERE nzo_id = ?",
        (nzo_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return JSONResponse(
            status_code=404,
            content={"status": False, "error": f"Job {nzo_id} not found"},
        )

    (torbox_id, torbox_type, stalled_since, current_status,
     cdn_link, local_path, filename, category) = row

    logger.warning(
        "retry_stalled: %s torbox_id=%s torbox_type=%s status=%s "
        "stalled_since=%s cdn_link=%s local_path=%s",
        nzo_id, torbox_id, torbox_type, current_status,
        stalled_since, "set" if cdn_link else "empty", "set" if local_path else "empty",
    )

    # Reset stall tracking counters always
    now = time.time()
    try:
        await db.conn.execute(
            "UPDATE jobs SET stalled_since = 0, last_progress_change = ?, "
            "stall_retries = 0 WHERE nzo_id = ?",
            (now, nzo_id),
        )
        await db.conn.commit()
    except Exception:
        logger.exception("retry_stalled: failed to reset stall counters for %s", nzo_id)
        return JSONResponse(
            status_code=500,
            content={"status": False, "error": "Failed to reset stall counters"},
        )

    # If no Torbox ID or no config, we can only reset counters
    if not torbox_id or not torbox_type or not config:
        logger.info("retry_stalled: %s has no torbox_id/type — stall counters reset only", nzo_id)
        return JSONResponse(content={"status": True, "action": "stall_counters_reset"})

    torbox_api_key = await config.get("torbox", "api_key")
    base_url = await config.get("torbox", "base_url", "https://api.torbox.app/v1")
    if not torbox_api_key:
        logger.info("retry_stalled: no Torbox API key — stall counters reset only")
        return JSONResponse(content={"status": True, "action": "stall_counters_reset"})

    client = TorboxClient(api_key=torbox_api_key, base_url=base_url)
    effective_type = torbox_type  # fallback if API call fails
    try:
        # Check if the download is available on Torbox CDN
        # Searches all download types as a fallback if the stored type doesn't match
        torbox_status, is_cdn_available, progress, actual_type = await check_torbox_availability(
            client, str(torbox_id), torbox_type,
        )

        # Fix torbox_type if the download was found in a different type list
        effective_type = actual_type or torbox_type
        if actual_type and actual_type != torbox_type:
            logger.info(
                "retry_stalled: correcting torbox_type for %s from %s to %s",
                nzo_id, torbox_type, actual_type,
            )
            try:
                await db.conn.execute(
                    "UPDATE jobs SET torbox_type = ? WHERE nzo_id = ?",
                    (actual_type, nzo_id),
                )
                await db.conn.commit()
            except Exception:
                logger.debug("retry_stalled: failed to correct torbox_type for %s", nzo_id)

        if is_cdn_available:
            # Download is complete/cached/seeding on Torbox — retry CDN download
            # Clean up any previous partial download
            if local_path:
                from pathlib import Path
                try:
                    p = Path(local_path)
                    if p.exists():
                        p.unlink()
                        logger.info("retry_stalled: removed previous local file %s", local_path)
                except Exception:
                    logger.exception("retry_stalled: failed to remove %s", local_path)

            # Also clean up .tmp_ partial files in the download directory
            if filename:
                download_dir = await config.get("folders", "download_dir", "downloads/incomplete")
                from pathlib import Path as PathLib
                tmp_pattern = f".tmp_{filename}.part"
                for tmp_file in PathLib(download_dir).glob(f".tmp_{filename}*.part"):
                    try:
                        tmp_file.unlink()
                        logger.info("retry_stalled: removed temp file %s", tmp_file)
                    except Exception:
                        logger.exception("retry_stalled: failed to remove temp file %s", tmp_file)

            # Set job to Fetching so CDN processor picks it up and re-downloads
            try:
                await db.conn.execute(
                    "UPDATE jobs SET status = 'Fetching', cdn_link = '', local_path = '', "
                    "percentage = 100, sizeleft = 0 WHERE nzo_id = ?",
                    (nzo_id,),
                )
                await db.conn.commit()
                logger.info(
                    "retry_stalled: %s is CDN-available (torbox=%s, progress=%.0f%%), "
                    "transitioning to Fetching for CDN re-download",
                    nzo_id, torbox_status, progress * 100,
                )
            except Exception:
                logger.exception("retry_stalled: failed to set %s to Fetching", nzo_id)
                return JSONResponse(
                    status_code=500,
                    content={"status": False, "error": "Failed to update job status"},
                )

            return JSONResponse(content={
                "status": True,
                "action": "cdn_retry",
                "torbox_status": torbox_status,
            })

        elif torbox_status and torbox_status.lower() not in ("", "error", "failed"):
            # Still in progress on Torbox — fall back to Reannounce/Resume
            # Use effective_type (corrected if needed) for the control command
            try:
                dl_id = int(torbox_id)
            except (ValueError, TypeError):
                logger.warning("retry_stalled: invalid torbox_id %r for %s", torbox_id, nzo_id)
                return JSONResponse(content={"status": True, "action": "reannounce", "torbox_status": torbox_status})

            try:
                if effective_type == "torrent":
                    await client.control_torrent(dl_id, "Reannounce")
                    logger.info("retry_stalled: sent Reannounce for %s (torbox_id=%s)", nzo_id, torbox_id)
                elif effective_type == "usenet":
                    await client.control_usenet_download(dl_id, "Resume")
                    logger.info("retry_stalled: sent Resume for %s (torbox_id=%s)", nzo_id, torbox_id)
                else:
                    logger.info("retry_stalled: WebDL %s has no resume command — stall counters reset", nzo_id)
            except (TorboxAuthError, TorboxConnectionError, TorboxRateLimitError, TorboxError) as e:
                logger.warning("retry_stalled: Torbox error for %s: %s", nzo_id, e)
            except Exception:
                logger.exception("retry_stalled: unexpected error for %s", nzo_id)

            return JSONResponse(content={
                "status": True,
                "action": "reannounce",
                "torbox_status": torbox_status,
            })

        else:
            # Download not found or in error state on Torbox
            logger.warning("retry_stalled: %s not found or errored on Torbox (status=%s)", nzo_id, torbox_status or "unknown")
            return JSONResponse(content={
                "status": True,
                "action": "not_found",
                "torbox_status": torbox_status or "unknown",
            })

    except (TorboxAuthError, TorboxConnectionError, TorboxRateLimitError, TorboxError) as e:
        logger.warning("retry_stalled: Torbox API error checking availability for %s: %s", nzo_id, e)
        # Fall back to Reannounce/Resume as best effort
        try:
            dl_id = int(torbox_id)
            if effective_type == "torrent":
                await client.control_torrent(dl_id, "Reannounce")
            elif effective_type == "usenet":
                await client.control_usenet_download(dl_id, "Resume")
        except Exception:
            logger.debug("retry_stalled: fallback reannounce also failed for %s", nzo_id)
        return JSONResponse(content={"status": True, "action": "reannounce_fallback"})
    except Exception:
        logger.exception("retry_stalled: unexpected error for %s", nzo_id)
        return JSONResponse(
            status_code=500,
            content={"status": False, "error": "Unexpected error"},
        )
    finally:
        await client.close()


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

    # Look up torbox_id, torbox_type, and status for each entry so we can
    # cancel ACTIVE downloads on the Torbox side. Completed/failed downloads
    # should NOT be deleted from Torbox — the user's files must remain
    # accessible. Only cancel downloads that are still in progress.
    # We check both the jobs and history tables since delete can come from
    # either the queue page or the history page.
    torbox_entries: list[tuple[str, str]] = []  # (torbox_id, torbox_type)
    placeholders = ",".join(["?"] * len(nzo_ids))

    for table in ("jobs", "history"):
        if table == "jobs":
            cursor = await db.conn.execute(
                f"SELECT torbox_id, torbox_type FROM {table} "
                f"WHERE nzo_id IN ({placeholders}) AND torbox_id IS NOT NULL AND torbox_id != '' "
                f"AND status NOT IN ('Complete', 'Failed')",
                nzo_ids,
            )
        else:
            # History entries are by definition completed/failed — never
            # cancel them on Torbox.
            continue
        for row in await cursor.fetchall():
            torbox_entries.append((str(row[0]), row[1]))

    # Cancel ACTIVE downloads on the Torbox side.
    # We only cancel downloads that are still in progress — completed/failed
    # downloads are kept on Torbox so the user retains access to their files.
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
                        logger.info("delete: cancelled active Torbox %s id=%s", torbox_type, torbox_id)
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