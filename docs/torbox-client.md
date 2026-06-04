# Torbox Client — Implementation Documentation

## Overview

The Torbox client module (`debridnzbd/torbox/`) provides a fully async HTTP interface
to the Torbox debrid service API. It handles authentication, error mapping, automatic
retries with exponential backoff, and response parsing into typed Pydantic models.

## Module Structure

```
debridnzbd/torbox/
├── __init__.py      — Public API exports (all classes and exceptions)
├── client.py        — TorboxClient async HTTP client
├── exceptions.py   — Exception hierarchy for error handling
└── models.py        — Pydantic response models
```

## TorboxClient

### Constructor

```python
client = TorboxClient(
    api_key="tb_xxxx",           # Required: Torbox API key
    base_url=DEFAULT_BASE_URL,   # Optional: Override API base URL
    timeout=30,                  # Optional: Request timeout in seconds
    max_retries=3,               # Optional: Max retry attempts for transient errors
)
```

### Connection Pooling

The client uses httpx with connection pooling:
- Maximum connections: 10
- Maximum keepalive connections: 5
- Connect timeout: 10 seconds
- Read/write timeout: configurable (default 30s)
- Follows redirects automatically

### Authentication

All requests include the `Authorization: Bearer <api_key>` header and a
`User-Agent: DebridNZBd/1.0.0` header. The API key is set once in the
constructor and included automatically on every request.

### Retry Behavior

The client implements automatic retry with exponential backoff for
transient errors:

| Error Type | Status Code | Retry? | Backoff |
|-----------|-------------|--------|---------|
| Auth error | 401, 403 | No | — |
| Not found | 404 | No | — |
| Rate limit | 429 | Yes | Retry-After header (default 60s) |
| Server error | 5xx | Yes | Exponential (1s, 2s, 4s) |
| Connection error | N/A | Yes | Exponential (1s, 2s, 4s) |
| Timeout | N/A | Yes | Exponential (1s, 2s, 4s) |

After exhausting all retries, the appropriate exception is raised.

### Context Manager

```python
async with TorboxClient(api_key="tb_xxxx") as client:
    user = await client.get_user_me()
# Client is automatically closed on exit
```

### Usenet Methods

#### `create_usenet_download(link="", post_processing=-1, file_data=None, file_name="")`

Submit an NZB to Torbox for download via Usenet. Either `link` (URL) or
`file_data` (raw bytes) must be provided.

- `post_processing`: -1=default, 0=none, 1=repair, 2=repair+unpack, 3=repair+unpack+delete
- When `file_data` is provided, the request uses multipart/form-data encoding
- When `link` is provided, the request uses JSON encoding

#### `control_usenet_download(usenet_id, operation)`

Control a usenet download. Operations: "Delete", "Pause", "Resume".

#### `request_usenet_dl(usenet_id, file_id=None, zip_link=False, user_ip=None, redirect=False)`

Request a CDN download link for a completed usenet download. Links expire
after 3 hours. Returns the CDN URL as a string.

- `file_id`: Specific file to download (omit for all files)
- `zip_link`: Get a zip archive of all files
- `user_ip`: IP for CDN edge selection
- `redirect`: If True, the API redirects directly to the CDN link

#### `get_usenet_list(bypass_cache=False, usenet_id=None, offset=0, limit=1000)`

List the user's usenet downloads. Updated every 5 seconds for live downloads.

#### `check_usenet_cached(hashes, format="object")`

Check if usenet downloads are already cached on Torbox's servers.
Cached items are available for immediate download without waiting.

### Torrent Methods

#### `create_torrent(magnet="", file_data=None, file_name="", allow_zip=False, as_queued=False, seed=0)`

Submit a torrent to Torbox. Either `magnet` (magnet link) or `file_data`
(.torrent file bytes) must be provided.

- `allow_zip`: Allow zip download
- `as_queued`: Add to queue instead of starting immediately
- `seed`: Seeding time in seconds (0 = default)

#### `control_torrent(torrent_id, operation)`

Control a torrent. Operations: "Reannounce", "Delete", "Resume".

#### `request_torrent_dl(torrent_id, file_id=None, zip_link=False, user_ip=None, redirect=False)`

Request a CDN download link for a completed torrent.

#### `get_torrent_list(bypass_cache=False, torrent_id=None, offset=0, limit=1000)`

List the user's torrents. Updated every 600 seconds for cached data,
or live with `bypass_cache=True`.

#### `check_torrent_cached(hashes, format="object", list_files=False)`

Check if torrents are cached. Set `list_files=True` to include file lists
for cached items.

### Web Download Methods

#### `create_web_download(link)`

Submit a direct URL for download. The URL must be from a supported hoster
(see `get_hosters_list()`).

#### `control_web_download(web_id, operation="Delete")`

Control a web download. Currently only "Delete" is supported.

#### `request_web_dl(web_id, file_id=None, zip_link=False, user_ip=None, redirect=False)`

Request a CDN download link for a completed web download.

#### `get_web_download_list(bypass_cache=False, web_id=None, offset=0, limit=1000)`

List the user's web downloads. Updated every 5 seconds.

#### `check_web_cached(hashes, format="object")`

Check if web downloads are cached.

#### `get_hosters_list()`

List all supported hosters for web downloads. Returns hoster info including
name, domains, status, and daily limits.

### Queued Methods

#### `get_queued_downloads(download_type="", bypass_cache=False, queued_id=None, offset=0, limit=1000)`

List queued downloads. Filter by `download_type`: "torrent", "usenet", or "webdl".

#### `control_queued_download(queued_id, operation="Delete")`

Control a queued download. Operations: "Delete", "Start".

### User Methods

#### `get_user_me(settings=False)`

Get the current user's account information. Returns a `TorboxUserData` with
plan type, email, subscription status, etc.

#### `test_connection()`

Test the API key and connectivity. Returns a `(bool, str)` tuple:
- `(True, "Connected — Pro plan, expires 2025-12-31")` on success
- `(False, "Authentication failed: ...")` on auth failure
- `(False, "Cannot reach Torbox API: ...")` on connection failure

## Exception Hierarchy

```
TorboxError (base)
├── TorboxAuthError       — 401/403: Invalid or missing API key
│   └── .message: str     — Human-readable error description
├── TorboxRateLimitError  — 429: Too many requests
│   ├── .message: str
│   └── .retry_after: int | None  — Seconds until retry is allowed
├── TorboxNotFoundError   — 404: Resource not found
│   └── .message: str
├── TorboxServerError     — 5xx: Server-side error
│   ├── .message: str
│   └── .status_code: int  — The HTTP status code (500, 502, etc.)
└── TorboxConnectionError — Network/timeout: Cannot reach Torbox API
    └── .message: str
```

All exceptions inherit from `TorboxError`, so callers can catch all
Torbox-related errors with a single `except TorboxError` clause, or
handle specific error types individually.

## Response Models

All API responses are parsed into Pydantic models for type safety:

| Model | Fields | Used By |
|-------|--------|---------|
| `TorboxResponse` | success, detail, data | All endpoints (base wrapper) |
| `TorboxUserData` | id, email, plan, is_subscribed, premium_expires_at, total_downloaded, customer | `get_user_me()` |
| `TorboxUsenetDownload` | id, hash, status, created_at, files, progress, size | `get_usenet_list()` |
| `TorboxTorrentDownload` | id, hash, status, created_at, name, progress, size, seeders, files | `get_torrent_list()` |
| `TorboxWebDownload` | id, hash, status, created_at, name, progress, size, files | `get_web_download_list()` |
| `TorboxDownloadLink` | url | CDN link responses |
| `TorboxCachedItem` | hash, cached | `check_*_cached()` methods |
| `TorboxQueuedDownload` | id, type, hash, created_at | `get_queued_downloads()` |
| `TorboxHoster` | name, domains, url, icon, status, type, note, daily_link_limit, daily_link_used, daily_bandwidth_limit, daily_bandwidth_used | `get_hosters_list()` |

## CDN Download Links

CDN links are requested on-demand via the `request_*_dl()` methods and are
valid for 3 hours. The client handles multiple response formats:

1. **Direct string**: `{"success": true, "data": "https://cdn.torbox.app/..."}`
2. **Dict with `url` key**: `{"success": true, "data": {"url": "https://..."}}`
3. **Dict with `download_link` key**: `{"success": true, "data": {"download_link": "https://..."}}`

If the response data is null or unrecognized, an empty string is returned.

## Security Considerations

- The API key is stored in the `config` table under `torbox.api_key` and is
  marked as a sensitive keyword (redacted in logs and `get_all()` responses)
- The API key is sent via the `Authorization: Bearer` header, not query parameters
- Connection pooling limits prevent resource exhaustion (10 max connections)
- Request timeouts prevent hanging connections (30s default, 10s connect)
- No API key is ever logged or included in error messages