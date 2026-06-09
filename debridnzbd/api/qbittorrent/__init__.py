"""qBittorrent WebUI API compatibility layer for DebridNZBd.

Implements the qBittorrent WebUI API (v5.0) so that 3rd-party torrent
management clients (Transdroid, qBittorrent Remote, etc.) can connect
to DebridNZBd and manage downloads through the Torbox debrid service.

The API is mounted at /api/v2/ and uses cookie-based SID authentication,
completely separate from the SABnzbd API at /api (which uses API keys).
The SABnzbd auth middleware only intercepts exact /api paths, so all
/api/v2/... paths naturally bypass it.
"""

from __future__ import annotations

from fastapi import APIRouter

from debridnzbd.api.qbittorrent.auth import router as auth_router
from debridnzbd.api.qbittorrent.app_info import router as app_router
from debridnzbd.api.qbittorrent.torrents import router as torrents_router
from debridnzbd.api.qbittorrent.transfer import router as transfer_router
from debridnzbd.api.qbittorrent.sync import router as sync_router

qbittorrent_router = APIRouter(prefix="/api/v2")
qbittorrent_router.include_router(auth_router)
qbittorrent_router.include_router(app_router)
qbittorrent_router.include_router(torrents_router)
qbittorrent_router.include_router(transfer_router)
qbittorrent_router.include_router(sync_router)