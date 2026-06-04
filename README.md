# DebridNZBd

SABnzbd-compatible API server that routes downloads through the Torbox debrid service.

## Overview

DebridNZBd implements the SABnzbd API so that existing clients (Sonarr, Radarr, Lidarr, Readarr, etc.) can connect to it as if it were a real SABnzbd instance. Instead of downloading from NNTP servers, all downloads are routed through the Torbox API.

## Features

- **SABnzbd API compatible** — Drop-in replacement for *arr clients
- **Torbox integration** — Usenet, torrent, and web download support
- **Web management UI** — Full configuration interface mirroring SABnzbd
- **Auto-download** — CDN files downloaded to local disk automatically
- **Queue management** — Pause, resume, reorder, categorize downloads
- **History tracking** — Complete job history with retry support
- **Notifications** — Email and Apprise notifications
- **Scheduling** — Time-based pause/resume/speedlimit

## Quick Start

```bash
# Install
pip install -e .

# Run
debridnzd

# Or with Python directly
python -m debridnzd
```

Open http://127.0.0.1:8080 in your browser and configure your Torbox API key.

## Configuration

Configure DebridNZBd through the web interface or directly via the SABnzbd API:

1. **General** — Host, port, HTTPS, authentication
2. **Folders** — Download directories, watched folder
3. **Torbox** — API key, download type, connection settings
4. **Categories** — Priority, post-processing, destination folders
5. **Switches** — Queue behavior, duplicate detection, naming rules
6. **Sorting** — TV/Movie/Date sort patterns
7. **Notifications** — Email and Apprise
8. **Scheduling** — Time-based actions
9. **RSS** — (Planned)
10. **Special** — Advanced settings

## Client Setup

In your *arr application:

1. Settings → Download Clients → Add → SABnzbd
2. Host: `127.0.0.1`
3. Port: `8080`
4. API Key: (shown in DebridNZBd General settings)

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run in development mode
uvicorn debridnzd.app:create_app --factory --reload
```

## License

MIT