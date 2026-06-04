# DebridNZBd

SABnzbd-compatible API server that routes downloads through the Torbox debrid service.

## Architecture

See [docs/architecture.md](docs/architecture.md) for full design documentation.

## Tech Stack

- **Language:** Python 3.11+
- **Framework:** FastAPI (async)
- **Database:** SQLite via aiosqlite
- **Templates:** Jinja2 + HTMX + Pico CSS
- **HTTP Client:** httpx (async)
- **Scheduling:** APScheduler

## Project Structure

```
debridnzbd/
  app.py              # FastAPI app factory, lifespan handler
  __main__.py          # Entry point: python -m debridnzbd
  api/
    router.py          # Main ?mode=XXX dispatcher
    queue.py           # Queue API modes
    history.py         # History API modes
    status.py          # Status API modes
    config.py          # Config API modes
    auth.py            # Auth middleware
  core/
    config_store.py    # Config read/write with defaults
    queue_manager.py   # Queue CRUD, ordering, priority
    history_manager.py # History CRUD, retry logic
    download_router.py # URL type detection → Torbox dispatch
    state_sync.py      # Background Torbox state poller
    cdn_downloader.py  # CDN file downloader
    scheduler.py       # APScheduler for scheduled tasks
    notifications.py   # Email + Apprise dispatcher
  torbox/
    client.py          # Async httpx client for Torbox API
    models.py          # Pydantic response models for Torbox
    exceptions.py      # Torbox-specific errors
  db/
    database.py        # SQLite connection management, migrations
    models.py          # Pydantic models for local tables
  web/
    routes.py          # Web UI page routes
    templates/          # Jinja2 HTML templates
    static/             # CSS, JS, images
  utils/
    nzo_id.py          # SABnzbd-compatible nzo_id generation
    diskspace.py       # Disk space checking
    version.py          # Version constant
  migrations/
    001_initial.py      # Initial schema
```

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run in development mode
uvicorn debridnzbd.app:create_app --factory --reload
```

## SABnzbd API Compatibility

DebridNZBd implements the SABnzbd HTTP API (`/api?mode=...`) so existing *arr clients
can connect without modification. The `Servers` configuration section is replaced
with `Torbox` configuration for the debrid service.

### Key API Modes

- **Queue:** `addurl`, `addfile`, `queue`, `pause`, `resume`, `delete`, `switch`, `change_cat`, `priority`
- **History:** `history`, `retry`, `retry_all`, `mark_as_completed`
- **Status:** `status`, `fullstatus`, `warnings`, `server_stats`
- **Config:** `get_config`, `set_config`, `del_config`, `get_cats`, `get_scripts`, `speedlimit`
- **Other:** `version`, `auth`, `shutdown`, `restart`

### Stubbed Modes

Modes with no Torbox equivalent return valid empty/default SABnzbd responses:
`pause_pp`, `resume_pp`, `restart_repair`, `unblock_server`, `delete_orphan`, `rss_now`, etc.

## Download Flow

1. Client sends `?mode=addurl&name=<NZB_URL>&apikey=<KEY>`
2. DebridNZBd detects type (usenet/torrent/webdl) from URL
3. Creates download via Torbox API
4. Local job stored in SQLite queue
5. Background poller syncs Torbox state every 5s
6. On completion, CDN link requested and file downloaded to local disk
7. Job moved to history, *arr client sees completion

## Conventions

- **nzo_id format:** `SABnzbd_nzo_<10 hex chars>` — matches SABnzbd pattern
- **Config sections:** `misc`, `folders`, `torbox`, `switches`, `notifications`, `sorting`, `special`
- **All API responses** follow SABnzbd JSON format: `{"status": true, ...}` or `{"error": "message"}`
- **Database:** Single SQLite file at `<admin_dir>/debridnzbd.db`
- **Tests:** Use pytest-asyncio with mocked Torbox responses (httpx respx)