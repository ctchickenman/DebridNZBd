"""DebridNZBd application factory and lifespan handler.

Creates and configures the FastAPI application with:
- SQLite database initialization and migrations
- Config store seeding with defaults
- Auth middleware for API key validation (SABnzbd API)
- Web UI session authentication
- SABnzbd API router at /api
- qBittorrent API router at /api/v2
- Web UI routes
- Static file serving for CSS/JS/images

The lifespan handler starts and stops background services:
- State sync poller (polls Torbox for download status)
- Scheduler (for user-defined scheduled tasks)
"""

from __future__ import annotations

import logging
import logging.handlers
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from debridnzbd.api.auth import auth_middleware
from debridnzbd.api.router import router as api_router
from debridnzbd.core.config_store import ConfigStore
from debridnzbd.db.database import Database, init_database, close_database
from debridnzbd.utils.diskspace import set_allowed_dirs
from debridnzbd.web.auth import web_auth_middleware

logger = logging.getLogger(__name__)

# Default paths relative to the application directory
DEFAULT_ADMIN_DIR = "admin"

# Maximum request body size (10 MB) to prevent memory exhaustion attacks.
# SABnzbd API requests are typically small (URLs, config values, NZB files).
# NZB files are rarely over 1 MB; 10 MB provides generous headroom.
MAX_REQUEST_BODY_SIZE = 10 * 1024 * 1024  # 10 MB
DEFAULT_COMPLETE_DIR = "downloads/complete"
DEFAULT_INCOMPLETE_DIR = "downloads/incomplete"


def setup_logging(log_dir: str = "logs", debug: bool = False) -> None:
    """Configure application-level logging with file and console output.

    Creates a RotatingFileHandler that writes to ``{log_dir}/debridnzbd.log``
    (10 MB max per file, 5 backups) and a StreamHandler for console output.
    The log level is set to DEBUG when ``debug`` is True, otherwise INFO.

    Args:
        log_dir: Directory for log files. Created if it doesn't exist.
        debug: If True, set log level to DEBUG for verbose output.
    """
    log_level = logging.DEBUG if debug else logging.INFO

    # Ensure log directory exists
    log_path = Path(log_dir)
    if not log_path.exists():
        logger.info("Creating log directory: %s", log_path)
    Path(log_dir).mkdir(parents=True, exist_ok=True, mode=0o755)

    # Root logger configuration
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Remove existing handlers to avoid duplicates on reload
    root_logger.handlers.clear()

    # Log format
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Rotating file handler — 10 MB per file, keep 5 backups
    log_file = Path(log_dir) / "debridnzbd.log"
    file_handler = logging.handlers.RotatingFileHandler(
        str(log_file),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(fmt)
    root_logger.addHandler(file_handler)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(fmt)
    root_logger.addHandler(console_handler)

    logging.getLogger(__name__).info(
        "Logging configured: level=%s, file=%s",
        logging.getLevelName(log_level),
        log_file,
    )

    # Suppress overly chatty third-party loggers even in debug mode.
    # These produce noise (every SQL operation) that obscures application-level debug output.
    for noisy in ("aiosqlite", "httpcore", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database, config, and background tasks on startup.

    Startup sequence:
    1. Create download directories if they don't exist
    2. Initialize the SQLite database (runs migrations if needed)
    3. Seed configuration defaults
    4. Configure allowed directories for disk space checks
    5. Store the ConfigStore and Database in app.state for access in routes
    6. Log startup warnings for security-sensitive settings
    7. TODO: Start state sync poller
    8. TODO: Start scheduler

    Shutdown sequence:
    1. TODO: Stop state sync poller
    2. TODO: Stop scheduler
    3. Close the database connection
    """
    logger.info("DebridNZBd starting up...")

    # --- Configure logging early (before database/config init) ---
    # Use defaults first; once config is loaded we may reconfigure if debug_mode is set.
    setup_logging(log_dir="logs", debug=False)

    # --- Create download directories if they don't exist ---
    import os
    # Use mode=0o755 for download/log/script directories — owner-only write,
    # but group and others can read and traverse. More restrictive than default
    # umask (typically 0o755) but still allows directory listing.
    for dir_path in [DEFAULT_INCOMPLETE_DIR, DEFAULT_COMPLETE_DIR, "logs", "scripts"]:
        p = Path(dir_path)
        try:
            if not p.exists():
                logger.info("Creating directory: %s", dir_path)
            p.mkdir(parents=True, exist_ok=True, mode=0o755)
        except PermissionError:
            logger.error(
                "Cannot create directory '%s' — permission denied. "
                "If running in Docker, ensure the /data volume is writable by UID 1000. "
                "Try: docker exec -u root <container> chown -R 1000:1000 /data",
                dir_path,
            )
            raise

    # Admin directory gets restrictive permissions from creation to protect the
    # database with API keys and passwords. Using mode= in mkdir() avoids the
    # TOCTOU window between creation and chmod.
    admin_path = Path(DEFAULT_ADMIN_DIR)
    try:
        if not admin_path.exists():
            logger.info("Creating admin directory: %s", admin_path)
        admin_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    except PermissionError:
        logger.error(
            "Cannot create admin directory '%s' — permission denied. "
            "If running in Docker, ensure the /data volume is writable by UID 1000. "
            "Try: docker exec -u root <container> chown -R 1000:1000 /data",
            admin_path,
        )
        raise
    # Also chmod in case the directory already existed with wrong permissions
    try:
        os.chmod(str(admin_path), 0o700)
        logger.info("Set admin directory permissions to 0700 (owner-only): %s", admin_path)
    except OSError:
        logger.warning(
            "Could not set admin directory permissions to 0700: %s. "
            "This is expected on some filesystems (e.g., NFS, FAT32). "
            "The database will still function but credentials may be readable by other users.",
            admin_path,
        )

    # --- Initialize database ---
    db_path = Path(DEFAULT_ADMIN_DIR) / "debridnzbd.db"
    database = await init_database(db_path)
    logger.info("Database initialized at %s", db_path)

    # --- Seed config defaults ---
    config = ConfigStore(database)
    await config.seed_defaults()
    logger.info("Configuration defaults seeded")

    # --- Set allowed directories for disk space checks ---
    download_dir = await config.get("folders", "download_dir", DEFAULT_INCOMPLETE_DIR)
    complete_dir = await config.get("folders", "complete_dir", DEFAULT_COMPLETE_DIR)
    admin_dir = await config.get("folders", "admin_dir", DEFAULT_ADMIN_DIR)
    set_allowed_dirs([download_dir, complete_dir, admin_dir])

    # --- Clean up stale temp files from interrupted downloads ---
    from debridnzbd.core.cdn_downloader import cleanup_stale_temp_files
    removed = cleanup_stale_temp_files(complete_dir)
    if removed:
        logger.info("Cleaned up %d stale temp file(s) from %s", removed, complete_dir)
    removed = cleanup_stale_temp_files(download_dir)
    if removed:
        logger.info("Cleaned up %d stale temp file(s) from %s", removed, download_dir)

    # --- Store in app state for route access ---
    app.state.db = database
    app.state.config = config
    app.state.start_time = __import__("time").time()  # For uptime calculation

    # --- Reconfigure logging with config values ---
    debug_mode = await config.get_bool("special", "debug_mode", False)
    log_dir = await config.get("folders", "log_dir", "logs")
    if debug_mode or log_dir != "logs":
        setup_logging(log_dir=log_dir, debug=debug_mode)
        if debug_mode:
            logger.info("Debug mode enabled — verbose logging active")

    # --- Security warnings ---
    disable_api_key = await config.get_bool("special", "disable_api_key", False)
    if disable_api_key:
        logger.warning(
            "⚠️  API authentication is DISABLED (special.disable_api_key=1). "
            "Anyone can access all API endpoints without a key. "
            "This should only be used for development."
        )

    username = await config.get("misc", "username")
    password = await config.get("misc", "password")
    temp_creds = await config.get_bool("misc", "temp_credentials", False)
    setup_done = await config.get_bool("misc", "setup_complete", False)

    if not username and not password:
        # First launch with no credentials — generate temporary ones
        temp_user, temp_pass = await config.generate_temp_credentials()
        logger.warning("=" * 60)
        logger.warning("TEMPORARY CREDENTIALS GENERATED FOR FIRST LAUNCH")
        logger.warning("Username: %s", temp_user)
        logger.warning("Password: %s", temp_pass)
        logger.warning("Log in to complete the setup wizard.")
        logger.warning("=" * 60)
    elif temp_creds and not setup_done:
        logger.warning(
            "⚠️  Temporary credentials active — log in to complete setup."
        )

    # --- Set restrictive permissions on database file ---
    import os
    try:
        os.chmod(str(db_path), 0o600)
        logger.info("Set database file permissions to 0600 (owner-only): %s", db_path)
    except OSError:
        logger.warning("Could not set database file permissions (may not be supported): %s", db_path)

    # TODO: Start scheduler

    # --- Start state sync poller ---
    import asyncio
    from debridnzbd.core.state_sync import run_cdn_processor, run_state_sync

    sync_cancelled = asyncio.Event()
    sync_task = asyncio.create_task(run_state_sync(app, sync_cancelled))
    app.state.sync_task = sync_task
    app.state.sync_cancelled = sync_cancelled

    # --- Start CDN download processor ---
    cdn_cancelled = asyncio.Event()
    cdn_task = asyncio.create_task(run_cdn_processor(app, cdn_cancelled))
    app.state.cdn_task = cdn_task
    app.state.cdn_cancelled = cdn_cancelled

    from debridnzbd import __version__
    logger.info("DebridNZBd v%s ready", __version__)

    yield  # Application is running

    # --- Shutdown ---
    logger.info("DebridNZBd shutting down...")

    # Stop state sync poller
    sync_cancelled.set()
    if sync_task and not sync_task.done():
        try:
            await asyncio.wait_for(sync_task, timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("State sync poller did not stop in time — cancelling")
            sync_task.cancel()

    # Stop CDN processor
    cdn_cancelled.set()
    if cdn_task and not cdn_task.done():
        try:
            await asyncio.wait_for(cdn_task, timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("CDN processor did not stop in time — cancelling")
            cdn_task.cancel()

    await close_database()
    logger.info("Database connection closed")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application.

    This is the application factory called by uvicorn. It sets up:
    - The FastAPI app with metadata
    - Auth middleware for API key validation
    - The SABnzbd API router at /api
    - Static file serving for the web UI

    Returns:
        A configured FastAPI application instance.
    """
    from debridnzbd import __version__

    app = FastAPI(
        title="DebridNZBd",
        description="SABnzbd-compatible API server powered by Torbox",
        version=__version__,
        lifespan=lifespan,
        # Disable auto-generated API documentation endpoints to prevent
        # information disclosure. These expose every endpoint, parameter type,
        # and response schema to unauthenticated users. Re-enable only in
        # development with proper auth if needed.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # Add request body size limiting middleware to prevent memory exhaustion.
    # Checks both Content-Length header and actual bytes received (for chunked
    # transfer encoding). Rejects requests exceeding MAX_REQUEST_BODY_SIZE.
    @app.middleware("http")
    async def request_size_limit_middleware(request, call_next):
        # Only check body size for methods that send a body
        if request.method in ("POST", "PUT", "PATCH"):
            # First, check Content-Length header (fast path)
            content_length = request.headers.get("content-length")
            if content_length is not None:
                try:
                    if int(content_length) > MAX_REQUEST_BODY_SIZE:
                        return JSONResponse(
                            status_code=413,
                            content={"status": False, "error": "Request body too large"},
                        )
                except (ValueError, TypeError):
                    pass  # Invalid Content-Length, proceed to streaming check

            # For chunked transfer encoding (no Content-Length), wrap the
            # receive callable to track bytes and reject oversized bodies.
            if content_length is None:
                original_receive = request.receive
                bytes_read = 0

                async def size_limited_receive():
                    nonlocal bytes_read
                    message = await original_receive()
                    if message.get("type") == "http.request.body":
                        body = message.get("body", b"")
                        bytes_read += len(body)
                        if bytes_read > MAX_REQUEST_BODY_SIZE:
                            raise HTTPException(
                                status_code=413,
                                detail="Request body too large",
                            )
                    return message

                request._receive = size_limited_receive

        return await call_next(request)

    # Add auth middleware for /api endpoint authentication.
    # Uses the centralized auth_middleware from api/auth.py which:
    # - Validates API keys using hmac.compare_digest (constant-time)
    # - Supports both full API key and restricted NZB key
    # - Rejects empty keys to prevent "" == "" bypass
    # - Returns 503 during startup before config is initialized
    # - Logs security warnings for auth-disabled mode
    app.middleware("http")(auth_middleware)

    # Add web UI session authentication middleware.
    # Uses cookie-based sessions (web_auth_middleware from web/auth.py) which:
    # - Redirects unauthenticated GET requests to /login
    # - Returns 403 for unauthenticated non-GET requests
    # - Skips auth for /api (has its own key auth), /api/v2/* (SID auth),
    #   /static/* (public assets), and /login, /logout
    # - Allows access without auth when no credentials are configured
    app.middleware("http")(web_auth_middleware)

    # Add security headers middleware to all responses.
    # These headers protect against clickjacking, MIME-type sniffing,
    # cross-site scripting, and other client-side attacks.
    @app.middleware("http")
    async def security_headers_middleware(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # Content-Security-Policy restricts script/style sources to same origin,
        # preventing inline script injection (XSS) and unauthorized resource loading.
        # 'unsafe-inline' for styles is needed for Pico CSS; unsafe-eval is not allowed.
        # TODO(Phase 7): Remove 'unsafe-inline' from script-src once inline handlers
        # are refactored to use data attributes + addEventListener.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self'; "
            "connect-src 'self'"
        )
        # Add HSTS header when HTTPS is enabled
        try:
            config = request.app.state.config
            if config and await config.get_bool("misc", "https_enabled", False):
                response.headers["Strict-Transport-Security"] = (
                    "max-age=31536000; includeSubDomains"
                )
        except Exception:
            pass  # Config not ready during startup
        return response

    # Include the SABnzbd API router
    app.include_router(api_router)

    # Include the qBittorrent WebUI API router
    from debridnzbd.api.qbittorrent import qbittorrent_router
    app.include_router(qbittorrent_router)

    # Include web UI routes (root /, /history, /config, /status, etc.)
    # Must be included before the static mount so route matching works.
    from debridnzbd.web.routes import router as web_router
    app.include_router(web_router)

    # Mount static files for the web UI
    static_dir = Path(__file__).parent / "web" / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    return app