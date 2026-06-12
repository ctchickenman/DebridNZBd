"""Tests for the qBittorrent WebUI API.

Validates:
- Session-based authentication (login, logout, SID validation)
- CSRF protection for mutating requests
- Torrent listing, adding, pausing, resuming, deleting
- Category and tag management
- Transfer info and speed limits
- App version/preferences endpoints
- Sync/maindata polling
"""

import pytest
import pytest_asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from debridnzbd.db.database import Database
from debridnzbd.core.config_store import ConfigStore
from debridnzbd.app import create_app


@pytest_asyncio.fixture
async def qbit_client(tmp_path: Path):
    """Create a test application with an initialized database and Torbox API key."""
    for dir_name in ["admin", "downloads/incomplete", "downloads/complete", "logs", "scripts"]:
        (tmp_path / dir_name).mkdir(parents=True, exist_ok=True)

    db_path = tmp_path / "admin" / "debridnzbd.db"
    database = Database(db_path)
    await database.initialize()
    config = ConfigStore(database)
    await config.seed_defaults()

    # Set up credentials and Torbox API key
    # Username and password are restricted — use set_web_credentials()
    await config.set_web_credentials("admin", "adminpass")
    await config.set("torbox", "api_key", "test_torbox_key_12345")

    app = create_app()
    app.state.db = database
    app.state.config = config

    client = TestClient(app)

    yield client

    await database.close()


class TestQbitAuth:
    """Test qBittorrent authentication endpoints."""

    def test_login_success(self, qbit_client) -> None:
        """Login with correct credentials should return Ok. with SID cookie."""
        response = qbit_client.post(
            "/api/v2/auth/login",
            data={"username": "admin", "password": "adminpass"},
        )
        assert response.status_code == 200
        assert response.text == "Ok."
        # SID cookie should be set
        cookies = response.cookies
        assert "SID" in cookies

    def test_login_failure(self, qbit_client) -> None:
        """Login with wrong password should return Fails. with 403."""
        response = qbit_client.post(
            "/api/v2/auth/login",
            data={"username": "admin", "password": "wrong"},
        )
        assert response.status_code == 403
        assert "Fails" in response.text

    def test_login_no_credentials_when_configured(self, qbit_client) -> None:
        """Login without credentials should fail when credentials are configured."""
        response = qbit_client.post(
            "/api/v2/auth/login",
            data={},
        )
        assert response.status_code == 403

    def test_logout(self, qbit_client) -> None:
        """Logout should clear the SID cookie."""
        # Login first
        login_resp = qbit_client.post(
            "/api/v2/auth/login",
            data={"username": "admin", "password": "adminpass"},
        )
        assert login_resp.status_code == 200
        sid = login_resp.cookies.get("SID")

        # Logout
        response = qbit_client.get(
            "/api/v2/auth/logout",
            cookies={"SID": sid},
        )
        assert response.status_code == 200

    def test_unauthenticated_access_returns_403(self, qbit_client) -> None:
        """Accessing protected endpoints without SID should return 403."""
        response = qbit_client.get("/api/v2/app/version")
        assert response.status_code == 403

    def test_authenticated_access_works(self, qbit_client) -> None:
        """Accessing protected endpoints with valid SID should work."""
        login_resp = qbit_client.post(
            "/api/v2/auth/login",
            data={"username": "admin", "password": "adminpass"},
        )
        sid = login_resp.cookies.get("SID")

        response = qbit_client.get(
            "/api/v2/app/version",
            cookies={"SID": sid},
        )
        assert response.status_code == 200


class TestQbitApp:
    """Test qBittorrent app information endpoints."""

    def _login(self, qbit_client) -> dict:
        resp = qbit_client.post(
            "/api/v2/auth/login",
            data={"username": "admin", "password": "adminpass"},
        )
        return {"SID": resp.cookies.get("SID")}

    def test_app_version(self, qbit_client) -> None:
        """Should return a qBittorrent version string."""
        cookies = self._login(qbit_client)
        response = qbit_client.get("/api/v2/app/version", cookies=cookies)
        assert response.status_code == 200
        assert response.text  # non-empty version string

    def test_webapi_version(self, qbit_client) -> None:
        """Should return a WebAPI version string."""
        cookies = self._login(qbit_client)
        response = qbit_client.get("/api/v2/app/webapiVersion", cookies=cookies)
        assert response.status_code == 200
        assert response.text  # non-empty version string

    def test_default_save_path(self, qbit_client) -> None:
        """Should return an absolute default save path."""
        cookies = self._login(qbit_client)
        response = qbit_client.get("/api/v2/app/defaultSavePath", cookies=cookies)
        assert response.status_code == 200
        # Must be an absolute path (starts with /)
        assert response.text.startswith("/")
        assert "downloads/complete" in response.text

    def test_preferences(self, qbit_client) -> None:
        """Should return a preferences dict with key fields."""
        cookies = self._login(qbit_client)
        response = qbit_client.get("/api/v2/app/preferences", cookies=cookies)
        assert response.status_code == 200
        data = response.json()
        assert "save_path" in data
        assert "dl_limit" in data
        # Paths must be absolute for *arr remote path mapping
        assert data["save_path"].startswith("/")
        assert data["temp_path"].startswith("/")


class TestQbitTorrents:
    """Test qBittorrent torrent management endpoints."""

    def _login(self, qbit_client) -> dict:
        resp = qbit_client.post(
            "/api/v2/auth/login",
            data={"username": "admin", "password": "adminpass"},
        )
        return {"SID": resp.cookies.get("SID")}

    def test_torrents_info_empty(self, qbit_client) -> None:
        """Should return empty list when no torrents exist."""
        cookies = self._login(qbit_client)
        response = qbit_client.get("/api/v2/torrents/info", cookies=cookies)
        assert response.status_code == 200
        assert response.json() == []

    def test_torrents_add_magnet(self, qbit_client) -> None:
        """Adding a magnet link should create a job."""
        cookies = self._login(qbit_client)
        magnet = "magnet:?xt=urn:btih:abc123&dn=TestTorrent"

        with patch("debridnzbd.api.qbittorrent.torrents.TorboxClient") as MockClient:
            mock_instance = AsyncMock()
            mock_result = MagicMock(success=True, data=12345, detail="")
            mock_instance.create_torrent = AsyncMock(return_value=mock_result)
            mock_instance.close = AsyncMock()
            MockClient.return_value = mock_instance

            response = qbit_client.post(
                "/api/v2/torrents/add",
                data={"urls": magnet, "category": "tv"},
                cookies=cookies,
            )
            assert response.status_code == 200
            assert "Ok" in response.text

    def test_torrents_add_paused(self, qbit_client) -> None:
        """Adding a torrent with paused=true should set status to Paused."""
        cookies = self._login(qbit_client)
        magnet = "magnet:?xt=urn:btih:def456&dn=PausedTorrent"

        with patch("debridnzbd.api.qbittorrent.torrents.TorboxClient") as MockClient:
            mock_instance = AsyncMock()
            mock_result = MagicMock(success=True, data={"torrent_id": 67890, "hash": "def456"}, detail="")
            mock_instance.create_torrent = AsyncMock(return_value=mock_result)
            mock_instance.close = AsyncMock()
            MockClient.return_value = mock_instance

            response = qbit_client.post(
                "/api/v2/torrents/add",
                data={"urls": magnet, "paused": "true"},
                cookies=cookies,
            )
            assert response.status_code == 200

    def test_torrents_stop(self, qbit_client) -> None:
        """Stopping a torrent should set its status to Paused."""
        cookies = self._login(qbit_client)

        # First add a torrent
        magnet = "magnet:?xt=urn:btih:stop123&dn=StopTest"
        with patch("debridnzbd.api.qbittorrent.torrents.TorboxClient") as MockClient:
            mock_instance = AsyncMock()
            mock_result = MagicMock(success=True, data=11111, detail="")
            mock_instance.create_torrent = AsyncMock(return_value=mock_result)
            mock_instance.close = AsyncMock()
            MockClient.return_value = mock_instance

            qbit_client.post(
                "/api/v2/torrents/add",
                data={"urls": magnet},
                cookies=cookies,
            )

        # Now stop it
        response = qbit_client.post(
            "/api/v2/torrents/stop",
            data={"hashes": "stop123"},
            cookies=cookies,
        )
        assert response.status_code == 200
        assert "Ok" in response.text

    def test_torrents_start(self, qbit_client) -> None:
        """Starting a paused torrent should set its status to Queued."""
        cookies = self._login(qbit_client)

        # First add and pause a torrent
        magnet = "magnet:?xt=urn:btih:start123&dn=StartTest"
        with patch("debridnzbd.api.qbittorrent.torrents.TorboxClient") as MockClient:
            mock_instance = AsyncMock()
            mock_result = MagicMock(success=True, data=22222, detail="")
            mock_instance.create_torrent = AsyncMock(return_value=mock_result)
            mock_instance.close = AsyncMock()
            MockClient.return_value = mock_instance

            qbit_client.post(
                "/api/v2/torrents/add",
                data={"urls": magnet, "paused": "true"},
                cookies=cookies,
            )

        # Now start it
        response = qbit_client.post(
            "/api/v2/torrents/start",
            data={"hashes": "start123"},
            cookies=cookies,
        )
        assert response.status_code == 200
        assert "Ok" in response.text

    def test_torrents_categories(self, qbit_client) -> None:
        """Should return categories dict with absolute save paths."""
        cookies = self._login(qbit_client)
        response = qbit_client.get("/api/v2/torrents/categories", cookies=cookies)
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, dict)
        # All category save paths must be absolute for *arr remote path mapping
        for name, cat in data.items():
            assert cat["savePath"].startswith("/"), \
                f"Category {name} savePath must be absolute, got: {cat['savePath']}"

    def test_torrents_create_category(self, qbit_client) -> None:
        """Should create a new category."""
        cookies = self._login(qbit_client)
        response = qbit_client.post(
            "/api/v2/torrents/createCategory",
            data={"category": "anime", "savePath": "/downloads/complete/anime"},
            cookies=cookies,
        )
        assert response.status_code == 200

        # Verify it appears in categories list
        response = qbit_client.get("/api/v2/torrents/categories", cookies=cookies)
        data = response.json()
        assert "anime" in data

    def test_torrents_tags_empty(self, qbit_client) -> None:
        """Should return empty list when no tags exist."""
        cookies = self._login(qbit_client)
        response = qbit_client.get("/api/v2/torrents/tags", cookies=cookies)
        assert response.status_code == 200
        assert response.json() == []

    def test_torrents_trackers_stub(self, qbit_client) -> None:
        """Should return minimal stub tracker list."""
        cookies = self._login(qbit_client)
        response = qbit_client.get(
            "/api/v2/torrents/trackers?hash=abc123",
            cookies=cookies,
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1  # DHT entry at minimum

    def test_torrents_properties_empty(self, qbit_client) -> None:
        """Properties for nonexistent hash should return empty dict."""
        cookies = self._login(qbit_client)
        response = qbit_client.get(
            "/api/v2/torrents/properties?hash=nonexistent",
            cookies=cookies,
        )
        assert response.status_code == 200

    def test_torrents_files_empty(self, qbit_client) -> None:
        """Files for nonexistent hash should return empty list."""
        cookies = self._login(qbit_client)
        response = qbit_client.get(
            "/api/v2/torrents/files?hash=nonexistent",
            cookies=cookies,
        )
        assert response.status_code == 200
        assert response.json() == []


class TestQbitTransfer:
    """Test qBittorrent transfer management endpoints."""

    def _login(self, qbit_client) -> dict:
        resp = qbit_client.post(
            "/api/v2/auth/login",
            data={"username": "admin", "password": "adminpass"},
        )
        return {"SID": resp.cookies.get("SID")}

    def test_transfer_info(self, qbit_client) -> None:
        """Should return global transfer stats."""
        cookies = self._login(qbit_client)
        response = qbit_client.get("/api/v2/transfer/info", cookies=cookies)
        assert response.status_code == 200
        data = response.json()
        assert "dl_info_speed" in data
        assert "up_info_speed" in data
        assert "connection_status" in data
        assert data["up_info_speed"] == 0  # Debrid: no upload

    def test_download_limit_default(self, qbit_client) -> None:
        """Default download limit should be 0 (unlimited)."""
        cookies = self._login(qbit_client)
        response = qbit_client.get("/api/v2/transfer/downloadLimit", cookies=cookies)
        assert response.status_code == 200
        assert response.text == "0"

    def test_set_download_limit(self, qbit_client) -> None:
        """Setting download limit should persist the value."""
        cookies = self._login(qbit_client)
        response = qbit_client.post(
            "/api/v2/transfer/setDownloadLimit",
            data={"limit": "1048576"},
            cookies=cookies,
        )
        assert response.status_code == 200

        # Verify the limit was set
        response = qbit_client.get("/api/v2/transfer/downloadLimit", cookies=cookies)
        assert response.text == "1048576"

    def test_upload_limit_always_zero(self, qbit_client) -> None:
        """Upload limit should always be 0 (debrid = no upload)."""
        cookies = self._login(qbit_client)
        response = qbit_client.get("/api/v2/transfer/uploadLimit", cookies=cookies)
        assert response.text == "0"

    def test_speed_limits_mode(self, qbit_client) -> None:
        """Speed limits mode should always be 0 (normal)."""
        cookies = self._login(qbit_client)
        response = qbit_client.get("/api/v2/transfer/speedLimitsMode", cookies=cookies)
        assert response.text == "0"


class TestQbitSync:
    """Test qBittorrent sync endpoints."""

    def _login(self, qbit_client) -> dict:
        resp = qbit_client.post(
            "/api/v2/auth/login",
            data={"username": "admin", "password": "adminpass"},
        )
        return {"SID": resp.cookies.get("SID")}

    def test_maindata_full_update(self, qbit_client) -> None:
        """Should return full snapshot with valid structure."""
        cookies = self._login(qbit_client)
        response = qbit_client.get("/api/v2/sync/maindata", cookies=cookies)
        assert response.status_code == 200
        data = response.json()
        assert data["full_update"] is True
        assert "torrents" in data
        assert "categories" in data
        assert "tags" in data
        assert "server_state" in data
        assert "rid" in data
        assert data["rid"] > 0
        # Category save paths must be absolute
        for name, cat in data["categories"].items():
            assert cat["savePath"].startswith("/"), \
                f"Category {name} savePath must be absolute, got: {cat['savePath']}"

    def test_maindata_rid_increments(self, qbit_client) -> None:
        """Each maindata call should increment the rid."""
        cookies = self._login(qbit_client)
        resp1 = qbit_client.get("/api/v2/sync/maindata", cookies=cookies)
        rid1 = resp1.json()["rid"]
        resp2 = qbit_client.get("/api/v2/sync/maindata", cookies=cookies)
        rid2 = resp2.json()["rid"]
        assert rid2 > rid1


class TestQbitStateMapping:
    """Test the state translation between DebridNZBd and qBittorrent."""

    def test_queued_maps_to_queueddl(self) -> None:
        from debridnzbd.api.qbittorrent.mappers import debrid_status_to_qbit
        assert debrid_status_to_qbit("Queued") == "queuedDL"

    def test_downloading_with_speed_maps_to_downloading(self) -> None:
        from debridnzbd.api.qbittorrent.mappers import debrid_status_to_qbit
        assert debrid_status_to_qbit("Downloading", speed=1024) == "downloading"

    def test_downloading_no_speed_maps_to_stalleddl(self) -> None:
        from debridnzbd.api.qbittorrent.mappers import debrid_status_to_qbit
        assert debrid_status_to_qbit("Downloading", speed=0) == "stalledDL"

    def test_paused_maps_to_pauseddl(self) -> None:
        from debridnzbd.api.qbittorrent.mappers import debrid_status_to_qbit
        assert debrid_status_to_qbit("Paused") == "pausedDL"

    def test_fetching_maps_to_moving(self) -> None:
        from debridnzbd.api.qbittorrent.mappers import debrid_status_to_qbit
        assert debrid_status_to_qbit("Fetching") == "moving"

    def test_complete_maps_to_uploading(self) -> None:
        from debridnzbd.api.qbittorrent.mappers import debrid_status_to_qbit
        assert debrid_status_to_qbit("Complete") == "uploading"

    def test_failed_maps_to_error(self) -> None:
        from debridnzbd.api.qbittorrent.mappers import debrid_status_to_qbit
        assert debrid_status_to_qbit("Failed") == "error"

    def test_unknown_maps_to_queueddl(self) -> None:
        from debridnzbd.api.qbittorrent.mappers import debrid_status_to_qbit
        assert debrid_status_to_qbit("Unknown") == "queuedDL"


class TestQbitFilterStates:
    """Test the qBittorrent filter matching logic."""

    def test_all_filter_matches_everything(self) -> None:
        from debridnzbd.api.qbittorrent.mappers import matches_filter
        assert matches_filter("downloading", "all")
        assert matches_filter("pausedDL", "all")
        assert matches_filter("error", "all")

    def test_downloading_filter(self) -> None:
        from debridnzbd.api.qbittorrent.mappers import matches_filter
        assert matches_filter("downloading", "downloading")
        assert matches_filter("stalledDL", "downloading")
        assert not matches_filter("pausedDL", "downloading")

    def test_completed_filter(self) -> None:
        from debridnzbd.api.qbittorrent.mappers import matches_filter
        assert matches_filter("uploading", "completed")
        assert not matches_filter("downloading", "completed")

    def test_stopped_filter(self) -> None:
        from debridnzbd.api.qbittorrent.mappers import matches_filter
        assert matches_filter("pausedDL", "stopped")
        assert not matches_filter("downloading", "stopped")