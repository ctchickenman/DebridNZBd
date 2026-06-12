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
    queue.py           # Queue API modes + duplicate detection (addurl, addfile, queue, pause, etc.)
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
    state_sync.py      # Background Torbox state poller + orphan reconciliation + CDN availability check
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

## Docker Deployment

The Docker image uses a multi-stage build (builder + runtime) and an entrypoint script that handles volume ownership:

- **Entry point**: `docker-entrypoint.sh` runs as root, fixes `/data` ownership via `chown -R debridnzbd:debridnzbd /data`, then **always** runs `chmod -R a+rwX /data` as a safety net (since `chown` silently fails on restricted filesystems like NFS, SMB/CIFS). Drops privileges to UID 1000 via `gosu` (with `setpriv`/`su` fallbacks).
- **User**: The app runs as `debridnzbd` (UID 1000). Do NOT set `--user` or `user:` in Docker/Docker Compose — it bypasses the entrypoint's privilege drop
- **Volume**: `/data` holds all persistent data (database, config, downloads, logs)
- **Ports**: 8080 (configurable via `--port`)
- **Directory permissions**: `admin/` is created with `0o755` then tightened to `0o700` when the filesystem supports it, ensuring accessibility on restricted filesystems

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

- **Queue:** `addurl`, `addfile`, `queue`, `pause`, `resume`, `delete`, `purge`, `switch`, `change_cat`, `priority`, `retry_stalled`
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
| Stalled (locally detected) | `stalledDL` | No progress for 60+ seconds |
| Paused | `pausedDL` | Local-only; Torbox doesn't support pause |
| Fetching | `moving` | CDN download in progress |
| Complete | `uploading` | qBittorrent convention for seeding |
| Failed | `error` | |

### Configuration

| Section | Key | Default | Description |
|---|---|---|---|
| `torbox` | `qbit_dl_limit` | `0` | Download speed limit in bytes/s (0 = unlimited) |
| `torbox` | `qbit_version` | `4.6.3` | Emulated qBittorrent version string |
| `torbox` | `qbit_webapi_version` | `2.11.2` | Emulated WebUI API version string |

### Type Filtering

Each API surface only shows jobs of its corresponding type:
- **SABnzbd API** (`?mode=queue`, `?mode=history`): Only shows `usenet` jobs. Torrent and webdl jobs are accepted but hidden from queue/history listings.
- **qBittorrent API** (`/api/v2/torrents/*`, `/api/v2/sync/*`): Only shows `torrent` jobs. Usenet and webdl jobs are hidden.
- **Web UI**: Shows all job types (no filtering).
- **Actions** (delete, pause, resume, retry): Work across all types by nzo_id/hash, regardless of which API surface is used.

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

## Stalled Download Retry

Downloads can stall when no progress is made for an extended period while Torbox reports the download as active. DebridNZBd detects and automatically recovers from stalls.

### Stall Detection

The background state sync poller tracks when each job's `percentage` last changed:
- If `percentage` is unchanged for 60+ seconds while status is "Downloading" or "Fetching", the download is considered stalled
- Stall state is tracked via `stalled_since` and `stall_retries` columns in the `jobs` table
- The UI shows a "Stalled" badge with duration (e.g., "Stalled 2m 15s")
- qBittorrent API shows stalled downloads with `stalledDL` state

### Automatic Retry

When a stall is detected, the poller automatically attempts recovery:
1. **First retry** (after 60s stall): Checks CDN availability via `check_torbox_availability()`. If CDN-available (completed/cached/seeding), transitions the job to Fetching for CDN re-download. Otherwise, sends Reannounce (torrents) or Resume (usenet) to Torbox. WebDL skips directly to step 2.
2. **Second retry** (after another 60s stall): Deletes the download from Torbox and re-submits the original URL, creating a new job.
3. **Give up** (after another 60s stall): Marks the job as Failed and moves it to history.

Each retry resets the stall timer, giving the download a fresh 60-second window.

### Manual Retry

The `?mode=retry_stalled&nzo_id=XXX` API mode checks Torbox availability and takes the best recovery action:

1. **CDN-available** (completed/cached/seeding on Torbox): Transitions the job to `Fetching` so the CDN processor re-downloads the file. Cleans up any previous partial download.
2. **Still in progress on Torbox** (downloading/queued): Sends Reannounce/Resume to Torbox as a fallback.
3. **Not found on Torbox**: Returns a warning. Stall counters are still reset.

In the web UI, a Retry button (↻) is available on all non-terminal downloads:
- **Stalled** downloads: filled blue button with "Retry stalled download" tooltip
- **Fetching** downloads: filled blue button with "Retry CDN download" tooltip
- **Other active** downloads (Downloading, Queued, Paused): subtle outline button with "Retry download" tooltip

The automatic stall retry (first attempt at 60s) also checks CDN availability before sending Reannounce.

## Queue Complete Grace Period

When a download completes or fails, DebridNZBd keeps the job in the active queue for a configurable grace period before moving it to history. This gives download clients (*arr, qBittorrent) time to observe the completed state and grab the file path before the job disappears.

### Configuration

| Section | Key | Default | Description |
|---|---|---|---|
| `switches` | `queue_complete` | `300` | Seconds to keep completed/failed jobs in the queue before moving to history. Set to `0` for immediate move (old behavior). |

### Behavior

- When `queue_complete > 0` (default 300 = 5 minutes): Completed jobs stay in the queue with `status = "Complete"` (or `"uploading"` in qBittorrent API) until the grace period expires, then are moved to history by the state sync poller.
- When `queue_complete = 0`: Jobs are moved to history immediately after completion, same as the old behavior.
- The state sync poller checks for expired jobs every cycle and moves them to history.
- For SABnzbd API: *arr clients see the download complete in the queue (with `storage` and `path` fields pointing to the local file), then see it in history after the grace period.
- For qBittorrent API: Torrents show as `"uploading"` (complete) with the correct `content_path` pointing to the downloaded file, then vanish after the grace period.
- `content_path` in the qBittorrent API uses the actual `local_path` from the database when available, falling back to `{save_path}/{filename}` for jobs still downloading.

### Output Path Handling

All API responses that expose file paths (`storage`, `path`, `content_path`, `save_path`) use the local disk path — **never** the Torbox CDN URL. CDN links are an internal implementation detail used only to download files to disk; they must not be exposed to download clients.

The queue and history responses both include `storage` (full file path) and `path` (parent directory). The `storage` field in particular is what *arr clients read to locate the downloaded file. These fields are derived from `local_path` in the jobs table, which is set by the CDN downloader after a successful download to disk.

Safety nets at multiple layers ensure CDN URLs never leak:
1. **Database migration 006**: Clears any CDN URLs (`http://` or `https://` prefixes) from the `storage` and `path` columns in the history table.
2. **`_move_to_history()`**: Strips CDN URLs from `local_path` before writing to `storage` and `path`.
3. **API responses**: All endpoints strip CDN URLs from path fields before returning them.
4. **Web UI**: Templates display `storage` as "Output" (not "CDN Link").

When `local_path` is empty (download not yet complete or CDN download failed), `storage` and `path` are empty strings — *arr clients interpret this as "file not yet available on disk."

## Duplicate Detection and Cache-Aware Re-Download

When a download request is received, DebridNZBd checks the history table for a matching entry before submitting to Torbox. This avoids creating duplicate downloads and leverages cached content.

### Configuration

| Section | Key | Default | Description |
|---|---|---|---|
| `switches` | `duplicate_detection` | `0` | Enable duplicate detection (`1` = enabled, `0` = disabled) |

When disabled (default), all requests proceed to Torbox without checking history.

### Detection Logic

For **URL submissions** (`?mode=addurl`), the check happens *before* Torbox submission:
1. Normalize the URL (lowercase scheme/host, sort query params, strip trailing `/`)
2. Query `history` table for exact URL match (by type: usenet/torrent/webdl)
3. If found → check local disk, then CDN availability

For **file uploads** (`?mode=addfile` with `.torrent` files), the check happens *after* Torbox submission:
1. Torbox returns the torrent info hash from the upload response
2. Query `history` table for matching `torbox_hash` (case-insensitive)
3. If found → check local disk, then CDN availability

NZB file uploads skip duplicate detection (no reliable hash for dedup).

### Actions

| Condition | Action | Job Created |
|---|---|---|
| File on local disk | `reuse_local` | Job with status `Complete`, `local_path` set |
| Not on disk, cached on CDN | `redownload_cdn` | Job with status `Fetching`, CDN processor re-downloads |
| Not on disk, not on CDN | `resubmit` | Normal Torbox submission proceeds |
| Not in history | `new` | Normal Torbox submission proceeds |

For `reuse_local` and `redownload_cdn` with file uploads, the duplicate Torbox download is deleted before creating the local job.

### URL Normalization

`normalize_url()` lowercases the scheme and host, strips trailing slashes, and sorts query parameters. This ensures `?a=1&b=2` matches `?b=2&a=1`.

### Speed Tracking

The state sync poller computes download speed from `sizeleft` changes between poll cycles. This fixes the issue where speed was always 0 in the database, and enables the qBittorrent API to correctly distinguish between `downloading` (active, speed > 0) and `stalledDL` (no progress) states.

### CDN Availability Check

`check_torbox_availability()` in `state_sync.py` queries the Torbox API by specific download ID to determine the current status and CDN availability of a download. It:

1. Queries all three download types (torrent, usenet, webdl) by ID, starting with the expected type
2. Handles the Torbox API quirk where ID-based queries return `data` as a single object (dict) instead of a list
3. Verifies the result ID matches the requested ID (to prevent false matches if Torbox ignores the ID filter on error paths)
4. Corrects the job's `torbox_type` if the download was found in a different type list than expected
5. Returns `(status, is_cdn_available, progress, actual_type)` for retry decision-making

### Torbox API Response Formats

The Torbox API returns `data` in different formats depending on whether an ID filter is used:

- **Without ID** (`GET /api/torrents/mylist`): `data` is a **list** of download objects: `{"success": true, "data": [{...}, {...}]}`
- **With ID** (`GET /api/torrents/mylist?id=123`): `data` is a **single object** (dict): `{"success": true, "data": {"id": 123, ...}}`

All `get_*_list` methods in `torbox/client.py` handle both formats transparently, always returning a list. When `data` is a single object, it's wrapped in a list of one element.

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