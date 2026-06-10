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
    queue.py           # Queue API modes (addurl, addfile, queue, pause, etc.)
    history.py         # History API modes
    status.py          # Status API modes
    config.py          # Config API modes
    auth.py            # Auth middleware
    qbittorrent/
      __init__.py      # Aggregated router with /api/v2 prefix
      auth.py          # Session-based login/logout (SID cookies)
      app_info.py      # App version, preferences, defaultSavePath
      dependencies.py  # Shared deps: get_db, get_config, require_sid, require_csrf
      mappers.py        # State translation (DebridNZBd → qBittorrent)
      sync.py          # maindata/torrentPeers polling endpoints
      torrents.py      # Torrent CRUD, categories, tags, file/property stubs
      transfer.py      # Speed limits, global transfer stats
  core/
    config_store.py    # Config read/write with defaults
    state_sync.py      # Background Torbox state poller + orphan reconciliation
    cdn_downloader.py  # CDN file downloader with concurrency semaphore
  torbox/
    client.py          # Async httpx client for Torbox API
    models.py          # Pydantic response models for Torbox
    exceptions.py      # Torbox-specific errors
  db/
    database.py        # SQLite connection management, migrations
    models.py          # Pydantic models for local tables
  web/
    routes.py          # Web UI page routes
    auth.py            # Web UI session auth (login/logout/cookies)
    templates/          # Jinja2 HTML templates
    static/             # CSS, JS, images
  utils/
    nzo_id.py          # SABnzbd-compatible nzo_id generation
    diskspace.py       # Disk space checking
    format.py          # Size/speed/time formatting utilities
    version.py          # Version constant
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

- **Queue:** `addurl`, `addfile`, `queue`, `pause`, `resume`, `delete`, `purge`, `switch`, `change_cat`, `priority`
- **History:** `history`, `retry`, `retry_all`
- **Status:** `status`, `fullstatus`, `warnings`, `server_stats`
- **Config:** `get_config`, `set_config`, `del_config`, `get_cats`, `get_scripts`, `speedlimit`
- **Other:** `version`, `auth`

### `addfile` Mode

The `addfile` mode accepts multipart form uploads of `.torrent` and `.nzb` files
via the `nzbfile` parameter. The file type is detected from the extension
(`.torrent` → torrent, `.nzb` → usenet), and the file bytes are forwarded to
the appropriate Torbox API endpoint. The web UI provides a file upload tab
alongside the URL input.

### Stubbed Modes

Modes with no Torbox equivalent return valid empty/default SABnzbd responses:
`pause_pp`, `resume_pp`, `restart_repair`, `unblock_server`, `delete_orphan`, `rss_now`, etc.

## qBittorrent WebUI API Compatibility

DebridNZBd also implements the qBittorrent WebUI API (v2) at `/api/v2/`, allowing
3rd-party torrent management clients (Transdroid, qBittorrent Remote, etc.) to
connect and manage downloads through Torbox.

### Authentication

Uses cookie-based SID sessions (not API keys). Login validates against
`misc.username`/`misc.password`, returns a `SID` cookie. All `/api/v2/` endpoints
(except `/auth/login`) require a valid SID cookie. CSRF protection enforces
Referer/Origin header matching on mutating requests.

### Key Endpoints

- **Auth:** `POST /auth/login`, `GET/POST /auth/logout`
- **App:** `GET /app/version`, `GET /app/webapiVersion`, `GET /app/preferences`, `POST /app/setPreferences`
- **Torrents:** `GET /torrents/info`, `POST /torrents/add`, `POST /torrents/stop`, `POST /torrents/start`, `POST /torrents/delete`
- **Categories:** `GET /torrents/categories`, `POST /torrents/createCategory`, `POST /torrents/removeCategories`
- **Tags:** `GET /torrents/tags`, `POST /torrents/addTags`, `POST /torrents/removeTags`
- **Transfer:** `GET /transfer/info`, `GET/POST /transfer/downloadLimit|setDownloadLimit`
- **Sync:** `GET /sync/maindata`, `GET /sync/torrentPeers`

### State Mapping

| DebridNZBd Status | qBittorrent State | Notes |
|---|---|---|
| Queued | `queuedDL` | |
| Downloading (speed > 0) | `downloading` | |
| Downloading (speed = 0) | `stalledDL` | |
| Paused | `pausedDL` | Local-only; Torbox doesn't support pause |
| Fetching | `moving` | CDN download in progress |
| Complete | `uploading` | qBittorrent convention for seeding |
| Failed | `error` | |

### Configuration

| Section | Key | Default | Description |
|---|---|---|---|
| `torbox` | `qbit_show_all_types` | `0` | Show usenet/webdl jobs in qBittorrent API (0 = torrent only) |
| `torbox` | `qbit_dl_limit` | `0` | Download speed limit in bytes/s (0 = unlimited) |
| `torbox` | `qbit_version` | `4.6.3` | Emulated qBittorrent version string |
| `torbox` | `qbit_webapi_version` | `2.11.2` | Emulated WebUI API version string |

## Web UI Authentication

DebridNZBd implements session-based authentication for the web interface, separate
from the SABnzbd API key auth and qBittorrent SID auth.

### First-Run Setup

When no `misc.username`/`misc.password` are configured on first launch:
1. Temporary credentials generated (`admin` + random 16-char password)
2. Credentials displayed in log; `temp_credentials=1`, `setup_complete=0` set
3. After login, user is force-redirected to `/setup` wizard
4. Must set permanent username (≥3 chars), password (≥6 chars), optional trusted networks
5. `set_web_credentials()` stores new creds, clears temp flag, sets `setup_complete=1`

### Authentication Flow

1. When `misc.username` and `misc.password` are configured, all web UI pages require authentication
2. Trusted network bypass: IPs matching `misc.trusted_networks` CIDRs skip auth (disabled during temp creds)
3. Unauthenticated GET requests are redirected to `/login`
4. Unauthenticated non-GET requests return 403
5. When no credentials are configured (both empty), the web UI is accessible without authentication
6. Login creates a session cookie (`web_session`) with 8-hour inactivity timeout
7. If `setup_complete=0`, authenticated users are redirected to `/setup`
8. Rate limited: 10 failed login attempts per IP per 5-minute window

### Session Cookie

- Name: `web_session`
- Value: 48 hex characters (192-bit random token)
- Flags: `HttpOnly`, `SameSite=Lax`, `Secure` (when HTTPS enabled)
- Timeout: 28800 seconds (8 hours) of inactivity

### Exempt Paths (have their own auth)

- `/api` — SABnzbd API key authentication
- `/api/v2/*` — qBittorrent SID authentication
- `/static/*` — static assets (no auth needed)
- `/login`, `/logout` — must be accessible without auth

### Key Endpoints

- `GET /login` — Login page
- `POST /login` — Authenticate and create session (redirects to `/setup` if setup incomplete)
- `GET /setup` — Setup wizard page (requires valid session)
- `POST /setup` — Complete setup wizard, set permanent credentials
- `GET/POST /logout` — Destroy session

## Download Flow

1. Client sends `?mode=addurl&name=<URL>&apikey=<KEY>` or `?mode=addfile` with file upload
2. DebridNZBd detects type (usenet/torrent/webdl) from URL or file extension
3. Creates download via Torbox API
4. Local job stored in SQLite queue
5. Background poller syncs Torbox state every 5s
6. On completion, CDN link requested and file downloaded to local disk
7. Job moved to history, *arr client sees completion

## Conventions

- **nzo_id format:** `SABnzbd_nzo_<10 hex chars>` — matches SABnzbd pattern
- **Config sections:** `misc`, `folders`, `torbox`, `switches`, `notifications`, `sorting`, `special`
- **All SABnzbd API responses** follow SABnzbd JSON format: `{"status": true, ...}` or `{"error": "message"}`
- **qBittorrent API responses** follow qBittorrent WebUI API format (plain text for success/failure, JSON for data)
- **Database:** Single SQLite file at `<admin_dir>/debridnzbd.db`
- **Tests:** Use pytest-asyncio with mocked Torbox responses (httpx respx)

## CLI Subcommands

DebridNZBd supports the following subcommands:

- `debridnzbd run [--host HOST] [--port PORT]` — Start the server (default when no subcommand given)
- `debridnzbd reset-password [-u USER] [-p PASS] [--temp] [--db-path PATH]` — Reset web UI credentials

The `reset-password` command is useful for password recovery. Use `--temp` to generate temporary credentials (like first launch), or provide `--username` and `--password` to set permanent credentials directly. If `--password` is omitted, it will be prompted interactively.