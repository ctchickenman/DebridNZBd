# Download Workflow

This document traces the complete lifecycle of a download in DebridNZBd, covering
both the web UI submission flow and the API-based flow used by *arr clients
(Sonarr, Radarr, Lidarr, etc.).

## Overview — Web UI Submission

![Web UI download flow](download-flow.svg)

## Overview — API Submission (*arr Client)

![API download flow](download-flow-api.svg)

The two flows share the same backend — the only difference is the entry point
and how progress is monitored. Steps 2–9 (Torbox submission, local job creation,
background polling, status mapping, CDN link retrieval, and history archival)
are identical.

## Entry Points

### Web UI Submission

The "Add NZB" form on the home page (`/`) sends a POST request to
`/api?mode=addurl` with these fields:

| Field       | Source                  | Example                                  |
|-------------|-------------------------|------------------------------------------|
| `apikey`    | Hidden field (template) | `apikey_5821170d...`                      |
| `mode`      | Hidden field            | `addurl`                                 |
| `name`      | URL input               | `https://nzbindex.com/download/...nzb`   |
| `cat`       | Category dropdown       | `tv`                                     |
| `priority`  | Priority dropdown        | `0`                                      |
| `nzbname`   | Optional name input     | `My.Show.S01E01`                          |

The JavaScript handler in `index.html` intercepts the form submit, sends it
via `fetch()` as AJAX, and on success reloads the page to show the new queue
entry. On error, it displays the error message inline.

Progress is shown through the web UI, which auto-refreshes the queue table
every 10 seconds by replacing the `#queue-refresh` div content.

### API Submission (*arr Client)

*arr clients (Sonarr, Radarr, etc.) connect to DebridNZBd using the SABnzbd
HTTP API protocol. They send requests directly to `/api?mode=...` without any
browser interaction.

**Submitting a download:**

```
GET /api?mode=addurl&name=https://nzbindex.com/download/...nzb&apikey=<KEY>&cat=tv
```

Or as a POST with form-encoded body:

```
POST /api?mode=addurl
Content-Type: application/x-www-form-urlencoded

apikey=<KEY>&name=https://nzbindex.com/download/...nzb&cat=tv&priority=0
```

**All parameters** accepted by the `addurl` mode:

| Parameter  | Required | Description                                      |
|------------|----------|--------------------------------------------------|
| `apikey`   | Yes      | Full API key or NZB key (see Auth below)        |
| `name`     | Yes      | NZB URL, magnet link, or web download URL       |
| `cat`      | No       | Category (defaults to `*`)                       |
| `priority` | No       | `-100` (paused), `0` (normal), `1` (low), `2` (high) |
| `nzbname`  | No       | Custom display name for the job                  |
| `pp`       | No       | Post-processing: `-1` default, `0` none, `1` repair, `2` unpack, `3` unpack+delete |
| `password` | No       | NZB password                                     |
| `script`   | No       | Post-processing script name                      |

**Monitoring progress:**

*arr clients poll the API at regular intervals:

| Mode       | Purpose                                           | Poll Frequency |
|------------|---------------------------------------------------|-----------------|
| `queue`    | Active downloads with progress, speed, size       | Every 5–10s     |
| `history`  | Completed/failed downloads with CDN links         | Every 30–60s    |
| `status`   | Server health, Torbox connection, disk space     | On startup      |
| `get_cats` | Available categories for configuration             | On startup      |

**Typical *arr polling loop:**

1. Submit URL via `?mode=addurl`
2. Poll `?mode=queue` until the download disappears from the queue
3. Poll `?mode=history` to find the completed download and retrieve the storage path
4. Import the file from the storage path

**Authentication:**

| Key Type | Key Name       | Access Level                                           |
|----------|----------------|--------------------------------------------------------|
| Full     | `misc.api_key` | All modes (addurl, queue, history, config, pause, etc.) |
| NZB      | `misc.nzb_key`  | `addurl`, `addfile`, `addlocalfile`, `queue`, `history`, `get_cats` only |

*arr clients typically use the **full API key** since they need access to
`queue`, `history`, and `get_cats`. The NZB key is intended for indexer
integration where only submission is needed.

## Authentication Middleware

`auth_middleware` (`api/auth.py`) intercepts every `/api` request:

- Public modes (`version`, `auth`) skip auth entirely.
- For all other modes, the `apikey` parameter is validated:
  - Checks query params first (`?apikey=...`)
  - Falls back to POST form body for web UI submissions
  - Also accepts `ma_username` + `ma_password` (SABnzbd compatibility)
  - Constant-time comparison via `hmac.compare_digest()`
- Full API key → all modes; NZB key → restricted set (`addurl`, `queue`,
  `history`, `get_cats`)
- Invalid/missing key → 403 response

## Shared Download Pipeline

After the request passes authentication, the following pipeline is the same
regardless of whether the request came from the web UI or an *arr client.

### URL Type Detection

`detect_url_type()` in `queue.py` classifies the URL:

| Pattern                       | Type      | Torbox endpoint              |
|-------------------------------|-----------|-------------------------------|
| `magnet:?xt=urn:btih:...`    | `torrent` | `/api/torrents/createtorrent` |
| `...nzb` or `/nzb/` in path  | `usenet`  | `/api/usenet/createusenetdownload` |
| Everything else               | `usenet`*  | `/api/usenet/createusenetdownload` |

\* The default type is configurable via `torbox.default_type` in config.

`_derive_filename()` generates the display name from the URL or the
optional `nzbname` parameter.

### Submission to Torbox

`handle_addurl` creates a `TorboxClient` and calls the appropriate method:

- **Usenet:** `client.create_usenet_download(link=url, post_processing=pp)`
- **Torrent:** `client.create_torrent(magnet=url)`
- **Web DL:** `client.create_web_download(link=url)`

Each method validates the input (URL scheme, magnet format, file size ≤ 50 MB),
sends the request with retries and SSRF protection, and returns a
`TorboxResponse` with `success`, `detail`, and `data` fields.

**Error responses:**

| Condition                     | HTTP Status | Message                              |
|-------------------------------|-------------|--------------------------------------|
| No URL provided               | 400         | `No URL provided`                    |
| No Torbox API key configured  | 500         | `Torbox API key not configured...`   |
| Torbox auth failure           | 502         | `Torbox authentication failed...`    |
| Torbox connection error       | 502         | `Cannot connect to Torbox API`       |
| Torbox rate limit             | 429         | `Torbox rate limit exceeded...`      |
| Torbox rejects the download   | 502         | `Torbox error: <detail>`             |

### Local Job Creation

On success, `handle_addurl` inserts a row into the `jobs` table:

```
nzo_id      = SABnzbd_nzo_<10 hex chars>  (locally generated)
filename    = derived from URL or nzbname
status      = 'Queued'
torbox_id   = <id from Torbox response>
torbox_type = 'usenet' | 'torrent' | 'webdl'
size        = 0  (unknown until Torbox reports)
percentage  = 0
time_added  = <current Unix timestamp>
```

If the database insert fails, the request still returns success because
the Torbox download was already created. The state-sync poller will
reconcile on the next cycle.

The response to the client is:
```json
{"status": true, "nzo_ids": ["SABnzbd_nzo_abc1234567"]}
```

### Background State-Sync Poller

`run_state_sync()` in `state_sync.py` runs as an asyncio background task,
polling Torbox every `poll_interval` seconds (default 5).

Each cycle:

1. **Fetches local jobs** with `torbox_id` from the `jobs` table.
2. **Fetches three Torbox lists** in parallel:
   - `GET /api/usenet/mylist?bypass_cache=true`
   - `GET /api/torrents/mylist?bypass_cache=true`
   - `GET /api/webdl/mylist?bypass_cache=true`
3. **For each matching download**, calls `_update_job_from_torbox()`.

### Status Mapping

Torbox status strings are mapped to local SABnzbd-compatible values:

| Torbox Status         | Local Status      | Action                              |
|-----------------------|-------------------|--------------------------------------|
| `queued`              | `Queued`          | Update percentage, size              |
| `queued_caching`      | `Queued`          | Update percentage, size              |
| `downloading`         | `Downloading`     | Update percentage, size              |
| `meta_downloading`    | `Downloading`     | Update percentage, size              |
| `paused`              | `Paused`          | No percentage change                 |
| `paused_caching`      | `Paused`           | No percentage change                 |
| `seeding`             | `Downloading`     | Update percentage, size              |
| `completed`           | `Complete`        | Request CDN link, move to history    |
| `cached`              | `Complete`         | Request CDN link, move to history    |
| `error`               | `Failed`          | Move to history with fail message    |
| `failed`              | `Failed`           | Move to history with fail message    |
| Unknown               | `Queued`           | No change (safe fallback)            |

Jobs already in `Complete` or `Failed` status are skipped.

### Completion: CDN Link and History

When a download reaches `completed` or `cached` status:

1. **Request CDN link** from Torbox:
   - Usenet: `GET /api/usenet/requestdl?usenet_id=<id>`
   - Torrent: `GET /api/torrents/requestdl?torrent_id=<id>`
   - Web DL: `GET /api/webdl/requestdl?web_id=<id>`
2. **If CDN request fails:** The job is **not** marked as failed. The poller
   retries on the next cycle.
3. **If CDN request succeeds:** Update the job row with `status='Complete'`,
   `cdn_link=<url>`, `percentage=100`, `sizeleft=0`, `time_completed=<now>`.
4. **Move to history:** Copy the job row into the `history` table and delete
   it from `jobs`. The `nzo_url` column preserves the original submission URL
   so that retries can re-submit it.

### Failure Handling

When a download reaches `error` or `failed` status:

1. Update the job row: `status='Failed'`, `fail_message='Torbox: <status>'`,
   `time_completed=<now>`.
2. Move to history (same as completion, but with `status='Failed'`).

## Progress Monitoring

### Web UI

The home page (`/`) auto-refreshes the queue every 10 seconds via
JavaScript. It fetches the current page with `X-Requested-With: XMLHttpRequest`
and replaces the `#queue-refresh` div content.

The queue table shows each job with columns: File, Status, Torbox, Category,
Priority, Progress, Size, Speed, Time Left, and action buttons (Pause/Resume,
Delete).

Action buttons (Delete, Pause, Resume) use `apiAction()` JavaScript calls
that submit POST requests to the API and show toast notifications for
success/failure, rather than navigating to a JSON response page.

Completed and failed downloads appear on the `/history` page. Each entry
shows the filename, status, category, size, download time, completion time,
and the CDN link (or error message for failures).

Failed downloads can be retried via the Retry button, which removes the
history entry and re-submits the original URL to Torbox via `?mode=retry`.

### *arr Client API Responses

*arr clients monitor progress through two API modes:

**`?mode=queue`** — Returns active downloads:

```json
{
  "status": true,
  "queue": {
    "paused": false,
    "noofslots": 1,
    "timeleft": "0:02:00",
    "speed": "2.5 MB/s",
    "kbpersec": "2560",
    "size": "1.0 GB",
    "sizeleft": "500 MB",
    "mb": 1024.0,
    "mbleft": 500.0,
    "slots": [
      {
        "status": "Downloading",
        "index": 0,
        "filename": "Some.Show.S01E01",
        "nzo_id": "SABnzbd_nzo_abc123",
        "cat": "tv",
        "percentage": "50",
        "size": "1.0 GB",
        "sizeleft": "500 MB",
        "timeleft": "0:02:00",
        "priority": 0
      }
    ]
  }
}
```

**`?mode=history`** — Returns completed/failed downloads:

```json
{
  "status": true,
  "history": {
    "noofslots": 1,
    "last_history_update": 1700000000.0,
    "slots": [
      {
        "status": "Completed",
        "nzo_id": "SABnzbd_nzo_abc123",
        "name": "Some.Show.S01E01",
        "nzb_name": "Some.Show.S01E01",
        "category": "tv",
        "size": "1.0 GB",
        "storage": "/path/to/file",
        "download_time": 120,
        "completed": 1700000000,
        "fail_message": ""
      }
    ]
  }
}
```

The `storage` field contains the CDN link (or local file path) that *arr uses
to import the completed download.

## Orphaned Jobs

If a download is deleted on the Torbox side (e.g., by the user in the
Torbox web UI), the state-sync poller will no longer see it in the Torbox
download lists. Currently, such local jobs remain stuck in `Queued` or
`Downloading` status because the poller only updates jobs it finds in the
Torbox responses — it does not detect missing jobs. This is a known gap
that may be addressed in a future release.

## Configuration

Key configuration values that affect the download workflow:

| Section   | Key                    | Default                       | Purpose                                         |
|-----------|------------------------|-------------------------------|-------------------------------------------------|
| `torbox`  | `api_key`              | *(empty, required)*           | Torbox API authentication key                   |
| `torbox`  | `base_url`             | `https://api.torbox.app/v1`  | Torbox API base URL                              |
| `torbox`  | `default_type`         | `usenet`                      | Default type for unrecognized URLs               |
| `torbox`  | `poll_interval`        | `5`                           | Seconds between state-sync polls                 |
| `torbox`  | `download_on_complete` | `1` (true)                    | Whether to request CDN links on completion      |
| `misc`    | `api_key`              | *(auto-generated)*            | Full API key for admin access                    |
| `misc`    | `nzb_key`              | *(auto-generated)*            | Restricted NZB key for addurl/queue/history     |