"""Torbox API client module.

Provides the async HTTP client for interacting with the Torbox debrid
service API. All API endpoints used by DebridNZBd are encapsulated
in the `TorboxClient` class.

Usage::

    from debridnzbd.torbox import TorboxClient

    async with TorboxClient(api_key="tb_xxxx") as client:
        user = await client.get_user_me()
        result = await client.create_usenet_download(link="https://...")

Exception hierarchy::

    TorboxError (base)
    ├── TorboxAuthError      — 401/403 responses
    ├── TorboxRateLimitError — 429 responses
    ├── TorboxNotFoundError  — 404 responses
    ├── TorboxServerError    — 5xx responses
    └── TorboxConnectionError — network/timeout failures
"""

from debridnzbd.torbox.client import TorboxClient
from debridnzbd.torbox.exceptions import (
    TorboxAuthError,
    TorboxConnectionError,
    TorboxError,
    TorboxNotFoundError,
    TorboxRateLimitError,
    TorboxServerError,
)
from debridnzbd.torbox.models import (
    TorboxCachedItem,
    TorboxControlOperation,
    TorboxCreateTorrentRequest,
    TorboxCreateUsenetRequest,
    TorboxCreateWebDownloadRequest,
    TorboxDownloadLink,
    TorboxHoster,
    TorboxQueuedDownload,
    TorboxResponse,
    TorboxTorrentDownload,
    TorboxUsenetDownload,
    TorboxUserData,
    TorboxWebDownload,
)

__all__ = [
    "TorboxClient",
    "TorboxError",
    "TorboxAuthError",
    "TorboxRateLimitError",
    "TorboxNotFoundError",
    "TorboxServerError",
    "TorboxConnectionError",
    "TorboxResponse",
    "TorboxUserData",
    "TorboxUsenetDownload",
    "TorboxTorrentDownload",
    "TorboxWebDownload",
    "TorboxDownloadLink",
    "TorboxCachedItem",
    "TorboxQueuedDownload",
    "TorboxHoster",
    "TorboxControlOperation",
    "TorboxCreateUsenetRequest",
    "TorboxCreateTorrentRequest",
    "TorboxCreateWebDownloadRequest",
]