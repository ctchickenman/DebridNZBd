# DebridNZBd Architecture Documentation

## Overview

DebridNZBd is a web service that implements the SABnzbd HTTP API, routing all download
requests through the Torbox debrid service instead of NNTP Usenet servers. This allows
existing SABnzbd-compatible clients (Sonarr, Radarr, Lidarr, Readarr, etc.) to use
Torbox without any client-side modifications.

## System Architecture

```
┌──────────────────────────────────────────────────────┐
│                     DebridNZBd                        │
│                                                       │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐ │
│  │  SABnzbd API │  │   Web UI     │  │  Static      │ │
│  │  Endpoints   │  │  (Jinja2)    │  │  Files       │ │
│  └──────┬───────┘  └──────┬───────┘  └──────────────┘ │
│         │                 │                           │
│  ┌──────▼─────────────────▼──────────────────────┐    │
│  │              Core Services                     │    │
│  │  Queue Mgr │ History Mgr │ Config Store │ Auth│    │
│  └─────────────┬────────────────────────────────┘    │
│                │                                      │
│  ┌─────────────▼────────────────────────────────┐    │
│  │         Torbox Client Adapter                 │    │
│  └─────────────┬────────────────────────────────┘    │
│                │                                      │
│  ┌─────────────▼────────────────────────────────┐    │
│  │    Background Workers                        │    │
│  │  State Sync Poller │ CDN Download Workers    │    │
│  └──────────────────────────────────────────────┘    │
└──────────────────────┬───────────────────────────────┘
                       │ HTTPS
                       ▼
              ┌─────────────────┐
              │  Torbox API     │
              │  api.torbox.app │
              └─────────────────┘
```

## Request Flow

### Adding a Download

1. Client sends `GET /api?mode=addurl&name=<URL>&apikey=<KEY>`
2. `api/auth.py` validates the API key against config
3. `api/router.py` dispatches to `api/queue.py:addurl_handler()`
4. `core/download_router.py` detects the URL type:
   - `.nzb` extension → usenet
   - `magnet:?` prefix → torrent
   - Other URL → webdl (or configured default)
5. `core/queue_manager.py` generates an nzo_id, inserts a job row in SQLite
6. `torbox/client.py` submits to the appropriate Torbox endpoint
7. Response returned in SABnzbd format: `{"status": true, "nzo_ids": ["SABnzbd_nzo_abc123"]}`

### State Synchronization

1. `core/state_sync.py` runs every N seconds (configurable, default 5)
2. Calls `torbox/client.py` to fetch usenet/torrents/webdl lists
3. Matches Torbox IDs to local jobs by `torbox_id` column
4. Updates job status, size, percentage from Torbox data
5. If a job reaches "completed" or "cached" state:
   - Requests CDN download link via `torbox/client.py:request_dl()`
   - Enqueues the job to the CDN download worker pool
6. `core/cdn_downloader.py` streams the CDN file to local disk
7. On success: moves job from `jobs` table to `history` table, triggers notifications

### SABnzbd API Mode Dispatch

The `api/router.py` handles all incoming `?mode=XXX` requests:

| Mode | Handler Module | Auth Required |
|------|---------------|---------------|
| `version` | `api/status.py` | No |
| `auth` | `api/status.py` | No |
| `queue` | `api/queue.py` | Yes (API or NZB key) |
| `addurl` | `api/queue.py` | Yes (API or NZB key) |
| `addfile` | `api/queue.py` | Yes (API or NZB key) |
| `history` | `api/history.py` | Yes (API key only) |
| `status`/`fullstatus` | `api/status.py` | Yes (API key only) |
| `get_config`/`set_config` | `api/config.py` | Yes (API key only) |
| ... | ... | ... |

## Database Schema

See `debridnzbd/migrations/001_initial.py` for the full SQL schema. Key tables:

### `config` — Key-value configuration store

```sql
CREATE TABLE config (
    section  TEXT NOT NULL,
    keyword  TEXT NOT NULL,
    value    TEXT,
    PRIMARY KEY (section, keyword)
);
```

Sections: `misc`, `folders`, `torbox`, `switches`, `notifications`, `sorting`, `special`

### `jobs` — Active download queue

Maps SABnzbd queue slots to Torbox downloads. Key fields:
- `nzo_id`: SABnzbd-compatible job ID (e.g., `SABnzbd_nzo_a1b2c3d4e5`)
- `torbox_id`: Corresponding Torbox download ID
- `torbox_type`: `usenet`, `torrent`, or `webdl`
- `status`: SABnzbd status string (Queued, Downloading, Paused, etc.)
- `position`: Integer ordering within the queue

### `history` — Completed/failed jobs

Archived jobs with final status, local file paths, and timing data.

### `categories`, `sorters`, `schedules`, `warnings`

Support tables for category management, sorting rules, scheduled tasks, and warning messages.

## Torbox Client (`debridnzbd/torbox/`)

The Torbox client module provides a fully async HTTP interface to the Torbox debrid API.
It is organized into three files:

### Module Structure

```
debridnzbd/torbox/
├── __init__.py      — Public API exports
├── client.py        — Async HTTP client (TorboxClient)
├── exceptions.py    — Exception hierarchy
└── models.py        — Pydantic response models
```

### Authentication

All Torbox API calls use Bearer token authentication:
```
Authorization: Bearer <TORBOX_API_KEY>
```

The API key is stored in the `config` table under section `torbox`, keyword `api_key`,
and is set in the `Authorization` header by the `TorboxClient` constructor.

### TorboxClient

The `TorboxClient` class in `client.py` is the main entry point. It:

- Uses **httpx** for async HTTP with connection pooling (10 max connections, 5 keepalive)
- Automatically retries on **429** (rate limit), **5xx** (server error), connection errors, and timeouts
- Uses **exponential backoff** (base 1s) for retries, up to `max_retries` (default 3)
- Respects **Retry-After** header on 429 responses (default 60s)
- Supports **async context manager** (`async with TorboxClient(...) as client:`)
- Provides a **test_connection()** convenience method for config UI validation
- Accepts a configurable **base_url** (default: `https://api.torbox.app/v1`) for testing

#### Usenet Endpoints

| Method | Torbox Endpoint | Description |
|--------|----------------|-------------|
| `create_usenet_download(link=, file_data=)` | POST `/usenet/createusenetdownload` | Submit NZB link or file |
| `control_usenet_download(id, operation)` | POST `/usenet/controlusenetdownload` | Pause/Resume/Delete |
| `request_usenet_dl(usenet_id, file_id=, zip_link=)` | GET `/usenet/requestdl` | Get CDN download link |
| `get_usenet_list(bypass_cache=, usenet_id=, offset=, limit=)` | GET `/usenet/mylist` | List usenet downloads |
| `check_usenet_cached(hashes)` | GET `/usenet/checkcached` | Check cache availability |

#### Torrent Endpoints

| Method | Torbox Endpoint | Description |
|--------|----------------|-------------|
| `create_torrent(magnet=, file_data=, ...)` | POST `/torrents/createtorrent` | Submit magnet or .torrent file |
| `control_torrent(id, operation)` | POST `/torrents/controltorrent` | Delete/Reannounce/Resume |
| `request_torrent_dl(torrent_id, file_id=, zip_link=)` | GET `/torrents/requestdl` | Get CDN download link |
| `get_torrent_list(bypass_cache=, torrent_id=, offset=, limit=)` | GET `/torrents/mylist` | List torrent downloads |
| `check_torrent_cached(hashes, list_files=)` | GET `/torrents/checkcached` | Check cache availability |

#### Web Download Endpoints

| Method | Torbox Endpoint | Description |
|--------|----------------|-------------|
| `create_web_download(link)` | POST `/webdl/createwebdownload` | Submit direct URL |
| `control_web_download(id, operation)` | POST `/webdl/controlwebdownload` | Delete |
| `request_web_dl(web_id, file_id=, zip_link=)` | GET `/webdl/requestdl` | Get CDN download link |
| `get_web_download_list(bypass_cache=, web_id=, offset=, limit=)` | GET `/webdl/mylist` | List web downloads |
| `check_web_cached(hashes)` | GET `/webdl/checkcached` | Check cache availability |
| `get_hosters_list()` | GET `/webdl/hosters` | List supported hosters |

#### Queued Download Endpoints

| Method | Torbox Endpoint | Description |
|--------|----------------|-------------|
| `get_queued_downloads(download_type=, bypass_cache=, ...)` | GET `/queued/getqueued` | List queued downloads |
| `control_queued_download(id, operation)` | POST `/queued/controlqueued` | Delete/Start |

#### User Endpoints

| Method | Torbox Endpoint | Description |
|--------|----------------|-------------|
| `get_user_me(settings=)` | GET `/user/me` | Get account info, plan status |
| `test_connection()` | (calls get_user_me) | Returns (bool, message) tuple |

### Exception Hierarchy

```
TorboxError (base) — message, status_code
├── TorboxAuthError       — 401/403: Invalid or missing API key
├── TorboxRateLimitError — 429: Rate limit exceeded (includes retry_after)
├── TorboxNotFoundError  — 404: Resource not found
├── TorboxServerError    — 5xx: Server-side error (includes status_code)
└── TorboxConnectionError — Network/timeout: Cannot reach Torbox API
```

Retry behavior:
- **401/403**: No retry — raises `TorboxAuthError` immediately
- **404**: No retry — raises `TorboxNotFoundError` immediately
- **429**: Retry up to `max_retries` times, respecting `Retry-After` header
- **5xx**: Retry up to `max_retries` times with exponential backoff
- **Connection/Timeout**: Retry up to `max_retries` times with exponential backoff

### Response Models (Pydantic)

All API responses are parsed into typed Pydantic models:

- `TorboxResponse` — Base response wrapper (success, detail, data)
- `TorboxUserData` — User account info (id, email, plan, subscription)
- `TorboxUsenetDownload` — Usenet download item (id, hash, status, progress, size, files)
- `TorboxTorrentDownload` — Torrent download item (id, hash, name, seeders, progress, files)
- `TorboxWebDownload` — Web download item (id, hash, name, progress, size)
- `TorboxDownloadLink` — CDN download link (url)
- `TorboxCachedItem` — Cache check result (hash, cached)
- `TorboxQueuedDownload` — Queued download item (id, type, hash)
- `TorboxHoster` — Supported hoster info (name, domains, status, limits)

### CDN Download Links

CDN links are requested on-demand via the `request_*_dl()` methods. Links expire
after 3 hours. The client extracts the URL from multiple response formats:
- Direct string: `{"data": "https://cdn.torbox.app/..."}`
- Dict with `url` key: `{"data": {"url": "https://..."}}`
- Dict with `download_link` key: `{"data": {"download_link": "https://..."}}`

### Testing

The Torbox client tests (`tests/test_torbox_client.py`) use **respx** to mock httpx
requests. 57 tests cover:

- **Authentication**: Bearer token in header, User-Agent header
- **User endpoints**: get_user_me, settings parameter, failure handling
- **Usenet endpoints**: create (link/file), control, requestdl, list, cached check
- **Torrent endpoints**: create (magnet/file), control, requestdl, list, cached check
- **Web download endpoints**: create, control, requestdl, list, cached check, hosters
- **Queued endpoints**: list with type filter, control
- **Error handling**: 401/403 auth, 404 not found, 429 retry + exhaust, 5xx retry + exhaust, connection retry + exhaust, timeout retry + exhaust
- **Convenience methods**: test_connection success/failure
- **Context manager**: async with protocol
- **Custom base URL**: for testing
- **Edge cases**: empty lists, failed responses, CDN link format variants, pagination, file_id/zip_link parameters

## Configuration Defaults

All defaults are seeded into the `config` table on first run. Key sections:

### `torbox` section (replaces SABnzbd's `servers` section)

| Keyword | Default | Description |
|---------|---------|-------------|
| `api_key` | `` (empty) | Torbox API key — must be configured |
| `base_url` | `https://api.torbox.app/v1` | Torbox API endpoint |
| `default_type` | `usenet` | Default download type for unrecognized URLs |
| `auto_check_cached` | `1` | Check cached availability before submitting |
| `download_on_complete` | `1` | Auto-download CDN files to local disk |
| `cdn_download_concurrency` | `2` | Max simultaneous CDN downloads |
| `poll_interval` | `5` | Seconds between Torbox state polls |

### `misc` section

Standard SABnzbd-compatible settings: host, port, HTTPS, API keys, auth, etc.

### `folders` section

Standard download directory paths, all relative to the working directory by default.

## Authentication

Two levels of authentication, matching SABnzbd:

1. **API Key** — Full access to all API modes and configuration
2. **NZB Key** — Restricted to adding NZBs and checking queue status only

Both are auto-generated UUIDs on first run and stored in the `config` table.

Web UI access can optionally require username/password (stored in `misc` section).

## Error Handling

- **Torbox API errors:** Logged as warnings, job status set to "Failed" with the error message
- **CDN link expiration:** If a CDN link expires (3-hour window), it's re-requested
- **Rate limiting:** Poll interval is configurable; exponential backoff on 429 responses
- **Disk full:** Auto-pause when free space drops below configured thresholds

## Testing Strategy

- **Unit tests:** Each module tested independently with mocked dependencies
- **API tests:** FastAPI TestClient with mocked Torbox client
- **Torbox client tests:** 57 tests using respx to mock httpx HTTP calls, covering all endpoints, error handling, retries, and edge cases
- **Integration tests:** Full addurl → queue → sync → download → history flow
- **Client compatibility:** Verified against Sonarr/Radarr SABnzbd connection settings

Test framework: pytest + pytest-asyncio + respx (for httpx mocking)

### Current Test Coverage

| Test Module | Tests | Coverage Area |
|-------------|-------|---------------|
| `test_database.py` | 24 | SQLite schema, migrations, CRUD, categories, config, singleton |
| `test_config_store.py` | 27 | Seeding, type-safe reads/writes, security (redaction, protected sections) |
| `test_auth.py` | 24 | Auth middleware, API key validation, NZB key restrictions, router dispatch, security |
| `test_torbox_client.py` | 57 | All Torbox API endpoints, error handling, retries, auth, context manager, edge cases |
| **Total** | **132** | |