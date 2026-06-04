"""SABnzbd-compatible API handlers for configuration operations.

Implements get_config, set_config, del_config, get_cats, and get_scripts
modes for reading and modifying DebridNZBd settings.
"""

from __future__ import annotations

import logging

from fastapi.responses import JSONResponse

from debridnzbd.db.models import CategoriesResponse, ConfigResponse, ScriptsResponse

logger = logging.getLogger(__name__)


async def _get_categories_config(db) -> list[dict[str, str]]:
    """Fetch categories from the categories table as a list of dicts.

    *arr clients (Sonarr, Radarr, etc.) expect categories as a JSON array
    of objects, each with a "name" field plus pp, script, dir, etc.
    This differs from SABnzbd's dict-keyed format but is what the *arr
    deserializer requires.
    """
    categories: list[dict[str, str]] = []
    if db and db.conn:
        cursor = await db.conn.execute(
            "SELECT name, priority, pp, script, dir, newzbin FROM categories ORDER BY order_index"
        )
        for name, priority, pp, script, dir_val, newzbin in await cursor.fetchall():
            categories.append({
                "name": name,
                "priority": str(priority),
                "pp": str(pp),
                "script": script or "Default",
                "dir": dir_val or "",
                "newzbin": newzbin or "",
            })
    return categories


async def handle_get_config(params: dict) -> JSONResponse:
    """Handle ?mode=get_config — return configuration settings.

    SABnzbd returns sections including 'categories' which contains detailed
    category data (pp, script, dir) keyed by name. *arr clients like Sonarr
    and Radarr use this to validate configured categories during connection
    testing. Categories are stored in a separate database table rather than
    the flat config key-value store, so they must be fetched and merged in.

    Parameters:
        section: Optional section name to filter by (e.g., 'misc', 'torbox').
                 If omitted, returns all sections.

    Returns:
        JSONResponse with nested config structure.
    """
    request = params.get("request")
    config = getattr(request.app.state, "config", None) if request else None
    db = getattr(request.app.state, "db", None) if request else None

    if config is None:
        return JSONResponse(
            content={"status": True, "config": {}},
        )

    section = params.get("section") or params.get("keyword")

    if section:
        # Return a single section
        if section == "categories":
            # Categories live in a separate table, not the config key-value store
            section_data = await _get_categories_config(db)
        else:
            try:
                section_data = await config.get_section(section, redact_secrets=True)
            except Exception:
                section_data = {}
        # SABnzbd wraps single sections in the same nested format
        all_config = {section: section_data}
    else:
        # Return all sections — must include categories from its own table
        all_config = await config.get_all(redact_secrets=True)
        all_config["categories"] = await _get_categories_config(db)

    return JSONResponse(content={"status": True, "config": all_config})


async def handle_set_config(params: dict) -> JSONResponse:
    """Handle ?mode=set_config — update configuration settings.

    Accepts key=value pairs in SABnzbd's format. Configuration keys
    are specified as section.keyword (e.g., 'misc.speedlimit').

    The following keys are protected and cannot be modified:
    - misc.api_key, misc.nzb_key, misc.password (auth credentials)
    - special.disable_api_key (security setting)
    - misc.host, misc.port, misc.https_* (network binding)
    - torbox.base_url (SSRF protection)

    Parameters:
        section: Config section to update
        keyword: Config keyword to update
        value: New value for the setting

    Returns:
        JSONResponse with status True on success.
    """
    request = params.get("request")
    config = getattr(request.app.state, "config", None) if request else None

    if config is None:
        return JSONResponse(
            content={"status": False, "error": "Configuration not available"},
        )

    section = params.get("section") or ""
    keyword = params.get("keyword") or ""
    value = params.get("value") or ""

    if not section or not keyword:
        # Try parsing from SABnzbd format: section.keyword=value
        # Some clients send settings as separate parameters
        return JSONResponse(
            status_code=400,
            content={"status": False, "error": "section and keyword are required"},
        )

    try:
        await config.set(section, keyword, str(value))
        logger.info("Config updated: %s.%s", section, keyword)
    except ValueError as e:
        logger.warning("Config update rejected: %s.%s — %s", section, keyword, e)
        return JSONResponse(
            content={"status": False, "error": str(e)},
        )

    return JSONResponse(content={"status": True})


async def handle_del_config(params: dict) -> JSONResponse:
    """Handle ?mode=del_config — delete a configuration setting.

    Parameters:
        section: Config section
        keyword: Config keyword to delete

    Returns:
        JSONResponse with status True on success.
    """
    request = params.get("request")
    config = getattr(request.app.state, "config", None) if request else None

    if config is None:
        return JSONResponse(
            content={"status": False, "error": "Configuration not available"},
        )

    section = params.get("section") or ""
    keyword = params.get("keyword") or ""

    if not section or not keyword:
        return JSONResponse(
            status_code=400,
            content={"status": False, "error": "section and keyword are required"},
        )

    try:
        deleted = await config.delete(section, keyword)
        if deleted:
            logger.info("Config deleted: %s.%s", section, keyword)
        else:
            logger.debug("Config key not found: %s.%s", section, keyword)
    except ValueError as e:
        logger.warning("Config delete rejected: %s.%s — %s", section, keyword, e)
        return JSONResponse(
            content={"status": False, "error": str(e)},
        )

    return JSONResponse(content={"status": True})


async def handle_get_cats(params: dict) -> JSONResponse:
    """Handle ?mode=get_cats — return available download categories.

    *arr clients use this to populate category dropdown menus.

    Returns:
        JSONResponse with list of category names.
    """
    request = params.get("request")
    db = getattr(request.app.state, "db", None) if request else None

    categories = []

    if db and db.conn:
        cursor = await db.conn.execute(
            "SELECT name FROM categories ORDER BY order_index"
        )
        categories = [row[0] for row in await cursor.fetchall()]

    response = CategoriesResponse(categories=categories)
    return JSONResponse(content=response.model_dump())


async def handle_get_scripts(params: dict) -> JSONResponse:
    """Handle ?mode=get_scripts — return available post-processing scripts.

    DebridNZBd does not support custom post-processing scripts (Torbox
    handles post-processing), so this always returns an empty list.

    Returns:
        JSONResponse with empty scripts list.
    """
    response = ScriptsResponse(scripts=[])
    return JSONResponse(content=response.model_dump())