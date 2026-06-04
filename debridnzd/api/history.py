"""SABnzbd-compatible API handlers for history operations.

Implements the history mode for listing completed/failed downloads,
and retry/retry_all modes for re-submitting failed downloads.
"""

from __future__ import annotations

import logging
import time

from fastapi.responses import JSONResponse

from debridnzd.db.models import HistoryResponse, HistorySlot
from debridnzd.utils.format import format_size

logger = logging.getLogger(__name__)

# Bytes per MB — used to convert between SABnzbd's MB and our bytes
BYTES_PER_MB = 1048576


async def handle_history(params: dict) -> JSONResponse:
    """Handle ?mode=history — return completed/failed download history.

    Returns a SABnzbd-compatible history response with slot details.
    *arr clients poll this endpoint to detect download completion.

    Parameters:
        start: Start index for pagination (default 0)
        limit: Maximum number of slots to return (0 = all)
        search: Filter by name (not yet implemented)
        failed_only: Show only failed downloads (0 or 1)
        cat / category: Filter by category
        last_history_update: Only return entries newer than this timestamp

    Returns:
        JSONResponse with nested history structure matching SABnzbd format.
    """
    request = params.get("request")
    db = getattr(request.app.state, "db", None) if request else None

    if db is None or db.conn is None:
        return JSONResponse(
            content={"status": True, "history": HistoryResponse().model_dump()},
        )

    start = int(params.get("start") or 0)
    limit = int(params.get("limit") or 0)
    failed_only = int(params.get("failed_only") or 0)

    # Build query with optional filters
    query = (
        "SELECT nzo_id, name, status, size, category, pp, storage, path, "
        "download_time, postproc_time, completed, time_added, duplicate_key, "
        "fail_message, stage_log, archive, torbox_id, torbox_type, nzo_url "
        "FROM history"
    )
    conditions = []
    query_params = []

    if failed_only:
        conditions.append("status = 'Failed'")

    if conditions:
        query += " WHERE " + " AND ".join(conditions)

    query += " ORDER BY completed DESC"

    cursor = await db.conn.execute(query, query_params)
    rows = await cursor.fetchall()

    # Build history slots
    slots = []
    for row in rows:
        nzo_id = row[0]
        name = row[1]
        status = row[2]
        size = row[3] or 0
        category = row[4] or "*"
        pp = str(row[5]) if row[5] else ""
        storage = row[6] or ""
        path = row[7] or ""
        download_time = int(row[8] or 0)
        postproc_time = int(row[9] or 0)
        completed = int(row[11] or 0)
        time_added = row[12] or 0
        fail_message = row[14] or ""
        url = row[18] or ""

        # Map status to SABnzbd conventions
        sab_status = "Completed" if status == "Completed" else "Failed"

        slots.append(HistorySlot(
            fail_message=fail_message,
            size=format_size(size),
            category=category,
            pp=pp,
            script="",
            nzb_name=name,
            download_time=download_time,
            storage=storage,
            status=sab_status,
            script_line="",
            completed=completed,
            time_added=time_added,
            nzo_id=nzo_id,
            downloaded=int(size / BYTES_PER_MB) if size > 0 else 0,
            password="***",
            path=path,
            postproc_time=postproc_time,
            name=name,
            url=url,
            bytes=int(size),
            archive=False,
        ))

    # Apply pagination
    total_count = len(slots)
    if start > 0:
        slots = slots[start:]
    if limit > 0:
        slots = slots[:limit]

    # Compute size stats
    total_bytes = sum(s.bytes for s in slots) if slots else 0
    # Use all rows (not just paginated) for totals
    all_total_bytes = 0
    cursor = await db.conn.execute("SELECT COALESCE(SUM(size), 0) FROM history")
    row = await cursor.fetchone()
    if row:
        all_total_bytes = row[0]

    history_response = HistoryResponse(
        noofslots=total_count,
        slots=slots,
        total_size=format_size(all_total_bytes),
    )

    # Get last history update timestamp
    try:
        cursor = await db.conn.execute(
            "SELECT COALESCE(MAX(completed), 0) FROM history"
        )
        row = await cursor.fetchone()
        if row:
            history_response.last_history_update = float(row[0]) if row[0] else 0
    except Exception:
        pass

    return JSONResponse(
        content={"status": True, "history": history_response.model_dump()},
    )


async def handle_retry(params: dict) -> JSONResponse:
    """Handle ?mode=retry — retry a failed download from history.

    Removes the history entry and re-submits the original URL to Torbox,
    creating a new job in the queue.

    Parameters:
        nzo_ids: The nzo_id of the history entry to retry (single ID)
        value: Alternative parameter name for nzo_id (SABnzbd compatibility)

    Returns:
        JSONResponse with status True on success.
    """
    request = params.get("request")
    db = getattr(request.app.state, "db", None) if request else None

    nzo_id = params.get("nzo_ids") or params.get("value") or ""
    if not nzo_id:
        return JSONResponse(
            status_code=400,
            content={"status": False, "error": "No nzo_id provided"},
        )

    # Clean up comma-separated lists — take the first one
    nzo_id = nzo_id.split(",")[0].strip()

    if db is None or db.conn is None:
        return JSONResponse(
            content={"status": False, "error": "Database not available"},
        )

    # Look up the history entry
    cursor = await db.conn.execute(
        "SELECT nzo_id, name, status, size, category, nzo_url, torbox_id, torbox_type "
        "FROM history WHERE nzo_id = ?",
        (nzo_id,),
    )
    row = await cursor.fetchone()

    if row is None:
        return JSONResponse(
            status_code=404,
            content={"status": False, "error": f"History entry not found: {nzo_id}"},
        )

    url = row[5]  # nzo_url
    category = row[4]  # category

    # Delete the history entry
    await db.conn.execute("DELETE FROM history WHERE nzo_id = ?", (nzo_id,))
    await db.conn.commit()
    logger.info("retry: deleted history entry %s to re-submit", nzo_id)

    # If we have the original URL, re-submit it via addurl logic
    if url:
        # Import here to avoid circular imports
        from debridnzd.api.queue import handle_addurl

        addurl_params = dict(params)
        addurl_params["name"] = url
        addurl_params["cat"] = category or "*"
        return await handle_addurl(addurl_params)

    # If no URL is available, just remove from history
    logger.info("retry: removed history entry %s (no URL to re-submit)", nzo_id)
    return JSONResponse(content={"status": True})


async def handle_retry_all(params: dict) -> JSONResponse:
    """Handle ?mode=retry_all — retry all failed downloads.

    Removes all failed entries from history. Note: this does not
    re-submit them to Torbox because we may not have the original URLs.
    SABnzbd's retry_all also just removes from history and re-queues.

    Returns:
        JSONResponse with status True.
    """
    request = params.get("request")
    db = getattr(request.app.state, "db", None) if request else None

    if db and db.conn:
        cursor = await db.conn.execute(
            "DELETE FROM history WHERE status = 'Failed'"
        )
        await db.conn.commit()
        logger.info("retry_all: removed %d failed entries from history", cursor.rowcount)

    return JSONResponse(content={"status": True})