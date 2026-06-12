# DebridNZBd

SABnzbd-compatible API server that routes downloads through the Torbox debrid service.

## Overview

DebridNZBd implements the SABnzbd API so that existing clients (Sonarr, Radarr, Lidarr, Readarr, etc.) can connect to it as if it were a real SABnzbd instance. Instead of downloading from NNTP servers, all downloads are routed through the Torbox API.

DebridNZBd also implements the qBittorrent WebUI API, allowing 3rd-party torrent management clients (Transdroid, qBittorrent Remote, etc.) to connect and manage downloads through Torbox.

## Features

- **SABnzbd API compatible** — Drop-in replacement for *arr clients
- **qBittorrent WebUI API compatible** — Works with Transdroid, qBittorrent Remote, and other torrent clients
- **Torbox integration** — Usenet, torrent, and web download support
- **File upload** — Upload `.torrent` and `.nzb` files directly via web UI or API
- **First-run setup wizard** — Temporary credentials on first launch, forced credential setup
- **Trusted networks** — CIDR-based IP bypass for local networks
- **Web management UI** — Full configuration interface mirroring SABnzbd
- **Auto-download** — CDN files downloaded to local disk automatically
- **Queue management** — Pause, resume, reorder, categorize downloads
- **History tracking** — Complete job history with retry support
- **Stalled download retry** — Automatic detection and recovery (resume → restart), manual retry button
- **Duplicate detection** — Checks history before re-downloading; reuses local files or re-downloads from CDN
- **Notifications** — Email and Apprise notifications
- **Scheduling** — Time-based pause/resume/speedlimit

## Installation

### Docker (recommended)

The easiest way to run DebridNZBd is with Docker using the pre-built image from GitHub Container Registry.

#### Using Docker Compose

Create a `docker-compose.yml`:

```yaml
services:
  debridnzbd:
    image: ghcr.io/ctchickenman/debridnzbd:latest
    container_name: debridnzbd
    ports:
      - "8080:8080"
    volumes:
      - debridnzbd-data:/data
      - /path/to/downloads:/data/downloads
    environment:
      - TZ=UTC
    restart: unless-stopped

volumes:
  debridnzbd-data:
```

Then run:

```bash
docker compose up -d
```

#### Using Docker CLI

```bash
docker run -d \
  --name debridnzbd \
  -p 8080:8080 \
  -v debridnzbd-data:/data \
  -v /path/to/downloads:/data/downloads \
  -e TZ=UTC \
  --restart unless-stopped \
  ghcr.io/ctchickenman/debridnzbd:latest
```

#### Building from source

If you prefer to build the image yourself:

```bash
git clone https://github.com/ctchickenman/DebridNZBd.git
cd DebridNZBd
docker compose up -d --build
```

#### Volumes

| Volume | Description |
|---|---|
| `/data` | Database, config, logs, and internal data |
| `/data/downloads` | Download output (incomplete + complete files) |

The container uses an entrypoint script that automatically fixes `/data` ownership on startup, so named Docker volumes work without manual configuration. The container starts as root to fix permissions (`chown -R`), then **always** runs `chmod -R a+rwX /data` as a safety net (since `chown` can silently fail on restricted filesystems like NFS, SMB/CIFS, returning exit code 0 without changing ownership). It then drops privileges to UID 1000 (`debridnzbd`) via `gosu` (with `setpriv`/`su` fallbacks).

**Do not set `--user` or `user:` in Docker/Docker Compose** — this would bypass the entrypoint's privilege drop and break volume ownership handling.

For host bind mounts, ensure the directory is writable by UID 1000:

```bash
chown -R 1000:1000 /path/to/downloads
```

### pip (for development)

```bash
# Install
pip install -e .

# Run
debridnzbd

# Or with Python directly
python -m debridnzbd
```

### Password Recovery

If you lose access to the web UI, reset credentials from the command line:

```bash
# Generate temporary credentials (like first launch)
python -m debridnzbd reset-password --temp --db-path /data/admin/debridnzbd.db

# Set permanent credentials directly
python -m debridnzbd reset-password -u myuser -p mypassword --db-path /data/admin/debridnzbd.db

# Interactive (prompts for password)
python -m debridnzbd reset-password -u myuser --db-path /data/admin/debridnzbd.db
```

For Docker, use `docker exec` or mount the volume and specify the path:

```bash
docker exec -it debridnzbd python -m debridnzbd reset-password --temp
```

## Configuration

### First Launch

On first launch with no credentials configured, DebridNZBd generates temporary credentials and displays them in the container logs:

```
============================================================
TEMPORARY CREDENTIALS GENERATED FOR FIRST LAUNCH
Username: admin
Password: aa26b08f64389d1f
Log in to complete the setup wizard.
============================================================
```

Log in at `http://<host>:8080` with these credentials. You'll be automatically redirected to the setup wizard, where you must set permanent credentials before using the application.

### Web UI Settings

Open http://127.0.0.1:8080 in your browser. All settings can be managed through the web interface or via the SABnzbd API:

1. **General** — Host, port, HTTPS, authentication, trusted networks
2. **Folders** — Download directories, watched folder
3. **Torbox** — API key, download type, connection settings
4. **Categories** — Priority, post-processing, destination folders
5. **Switches** — Queue behavior, duplicate detection, naming rules
6. **Sorting** — TV/Movie/Date sort patterns
7. **Notifications** — Email and Apprise
8. **Scheduling** — Time-based actions
9. **RSS** — (Planned)
10. **Special** — Advanced settings

### Authentication

DebridNZBd uses three independent authentication systems:

| System | Scope | Method |
|--------|-------|--------|
| **SABnzbd API** | `/api?mode=...` | API key (`apikey` parameter) |
| **qBittorrent** | `/api/v2/*` | Username/password login → SID cookie |
| **Web UI** | All other pages | Username/password login → session cookie |

**Trusted Networks:** You can configure CIDR ranges (e.g., `192.168.1.0/24`) that bypass web UI authentication. Requests from these networks won't require login. This is set during the setup wizard or in General settings. Trusted network bypass is disabled until the setup wizard is completed.

## Client Setup

### *arr Clients (SABnzbd protocol)

In your *arr application:

1. Settings → Download Clients → Add → SABnzbd
2. Host: `127.0.0.1` (or your Docker host IP)
3. Port: `8080`
4. API Key: (shown in DebridNZBd General settings)

### qBittorrent Clients (qBittorrent WebUI API)

In your torrent management client (Transdroid, qBittorrent Remote, etc.):

1. Add a new server with type qBittorrent
2. Host: `127.0.0.1` (or your Docker host IP)
3. Port: `8080`
4. Username: `admin` (or whatever you configured in `misc.username`)
5. Password: (as configured in `misc.password`)

The qBittorrent API is available at `/api/v2/` and uses cookie-based SID authentication. Each API surface only shows its corresponding job type: the SABnzbd API shows usenet jobs, and the qBittorrent API shows torrent jobs. Web downloads (webdl) are processed in the background but not shown in either API. The web UI shows all types.

## API Overview

### SABnzbd API (`/api?mode=...`)

Standard SABnzbd HTTP API for *arr client integration. Authentication uses API keys (`apikey` parameter).

Key modes: `addurl`, `addfile`, `queue`, `pause`, `resume`, `delete`, `retry_stalled`, `history`, `status`, `get_config`, `set_config`

### qBittorrent WebUI API (`/api/v2/...`)

RESTful API for torrent management client integration. Authentication uses username/password login with SID cookies.

Key endpoints: `auth/login`, `torrents/info`, `torrents/add`, `torrents/stop`, `torrents/start`, `torrents/delete`, `sync/maindata`, `transfer/info`

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run in development mode
uvicorn debridnzbd.app:create_app --factory --reload
```

## License

MIT