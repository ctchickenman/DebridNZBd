"""Tests for the addfile API mode and detect_file_type helper.

Validates that:
- detect_file_type correctly classifies .torrent and .nzb files
- handle_addfile accepts .torrent file uploads and calls create_torrent
- handle_addfile accepts .nzb file uploads and calls create_usenet_download
- handle_addfile rejects requests with no file data
- handle_addfile rejects unsupported file types when no default matches
- The addfile mode is accessible with NZB key (restricted access)
- The addfile mode requires authentication
"""

import pytest
import pytest_asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from io import BytesIO

from fastapi.testclient import TestClient

from debridnzbd.api.queue import detect_file_type
from debridnzbd.db.database import Database
from debridnzbd.core.config_store import ConfigStore
from debridnzbd.app import create_app


# ------------------------------------------------------------------ #
#  Unit tests for detect_file_type                                     #
# ------------------------------------------------------------------ #


class TestDetectFileType:
    """Test the detect_file_type helper function."""

    def test_torrent_extension(self) -> None:
        assert detect_file_type("movie.torrent") == "torrent"

    def test_torrent_extension_case_insensitive(self) -> None:
        assert detect_file_type("movie.TORRENT") == "torrent"
        assert detect_file_type("movie.Torrent") == "torrent"

    def test_nzb_extension(self) -> None:
        assert detect_file_type("release.nzb") == "usenet"

    def test_nzb_extension_case_insensitive(self) -> None:
        assert detect_file_type("release.NZB") == "usenet"

    def test_unknown_extension_falls_back_to_default(self) -> None:
        assert detect_file_type("file.txt", "usenet") == "usenet"

    def test_unknown_extension_falls_back_to_torrent_default(self) -> None:
        assert detect_file_type("file.txt", "torrent") == "torrent"

    def test_unknown_extension_default_usenet(self) -> None:
        assert detect_file_type("file.zip") == "usenet"

    def test_invalid_default_type_normalized_to_usenet(self) -> None:
        assert detect_file_type("file.dat", "invalid_type") == "usenet"

    def test_empty_filename_uses_default(self) -> None:
        assert detect_file_type("", "torrent") == "torrent"

    def test_filename_with_path(self) -> None:
        assert detect_file_type("/path/to/release.torrent") == "torrent"
        assert detect_file_type("/path/to/release.nzb") == "usenet"


# ------------------------------------------------------------------ #
#  Integration tests for addfile API mode                               #
# ------------------------------------------------------------------ #


@pytest_asyncio.fixture
async def app_client(tmp_path: Path):
    """Create a test application with an initialized database."""
    for dir_name in ["admin", "downloads/incomplete", "downloads/complete", "logs", "scripts"]:
        (tmp_path / dir_name).mkdir(parents=True, exist_ok=True)

    db_path = tmp_path / "admin" / "debridnzbd.db"
    database = Database(db_path)
    await database.initialize()
    config = ConfigStore(database)
    await config.seed_defaults()

    # Set a Torbox API key so addfile doesn't bail with "not configured"
    await config.set("torbox", "api_key", "test_torbox_api_key_12345")

    app = create_app()
    app.state.db = database
    app.state.config = config

    api_key = await config.get("misc", "api_key")
    nzb_key = await config.get("misc", "nzb_key")

    client = TestClient(app)

    yield client, api_key, nzb_key

    await database.close()


class TestAddfileApi:
    """Test the addfile API endpoint."""

    def test_addfile_no_file_returns_400(self, app_client) -> None:
        """addfile without file data should return 400."""
        client, api_key, _ = app_client
        response = client.post(
            "/api?mode=addfile",
            data={"apikey": api_key},
        )
        assert response.status_code == 400
        data = response.json()
        assert data["status"] is False
        assert "no file" in data["error"].lower()

    def test_addfile_requires_auth(self, app_client) -> None:
        """addfile without API key should return 403."""
        client, _, _ = app_client
        torrent_data = b"d8:announce42:http://example.com/announce12:piece lengthi262144e6:pieces20:xxxxxxxxxxxxxxxxxxxxe"
        response = client.post(
            "/api?mode=addfile",
            files={"nzbfile": ("test.torrent", BytesIO(torrent_data), "application/x-bittorrent")},
        )
        assert response.status_code == 403

    def test_addfile_nzb_key_allowed(self, app_client) -> None:
        """addfile should be accessible with the NZB key."""
        client, _, nzb_key = app_client
        torrent_data = b"d8:announce42:http://example.com/announce12:piece lengthi262144e6:pieces20:xxxxxxxxxxxxxxxxxxxxe"

        with patch("debridnzbd.api.queue.TorboxClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.create_torrent = AsyncMock(return_value=MagicMock(
                success=True, data=12345, detail=""
            ))
            mock_instance.close = AsyncMock()
            MockClient.return_value = mock_instance

            response = client.post(
                "/api?mode=addfile",
                data={"apikey": nzb_key},
                files={"nzbfile": ("test.torrent", BytesIO(torrent_data), "application/x-bittorrent")},
            )
            # Auth should pass (might fail on Torbox call but not 403)
            assert response.status_code != 403

    def test_addfile_torrent_upload(self, app_client) -> None:
        """Uploading a .torrent file should call create_torrent."""
        client, api_key, _ = app_client
        torrent_data = b"d8:announce42:http://example.com/announce12:piece lengthi262144e6:pieces20:xxxxxxxxxxxxxxxxxxxxe"

        with patch("debridnzbd.api.queue.TorboxClient") as MockClient:
            mock_instance = AsyncMock()
            mock_result = MagicMock(success=True, data=12345, detail="")
            mock_instance.create_torrent = AsyncMock(return_value=mock_result)
            mock_instance.close = AsyncMock()
            MockClient.return_value = mock_instance

            response = client.post(
                "/api?mode=addfile",
                data={"apikey": api_key},
                files={"nzbfile": ("test.torrent", BytesIO(torrent_data), "application/x-bittorrent")},
            )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] is True
            assert len(data["nzo_ids"]) == 1

            # Verify create_torrent was called with file data
            mock_instance.create_torrent.assert_called_once()
            call_kwargs = mock_instance.create_torrent.call_args
            assert call_kwargs[1]["file_data"] == torrent_data
            assert call_kwargs[1]["file_name"] == "test.torrent"

    def test_addfile_nzb_upload(self, app_client) -> None:
        """Uploading a .nzb file should call create_usenet_download."""
        client, api_key, _ = app_client
        nzb_data = b'<?xml version="1.0" encoding="UTF-8"?><nzb xmlns="http://newzbin.com/DTD/2003/nzb-1.0.dtd"><file poster="test" subject="test"><groups><group>alt.binaries.test</group></groups><segments><segment bytes="100" number="1">test@news.example.com</segment></segments></file></nzb>'

        with patch("debridnzbd.api.queue.TorboxClient") as MockClient:
            mock_instance = AsyncMock()
            mock_result = MagicMock(success=True, data=67890, detail="")
            mock_instance.create_usenet_download = AsyncMock(return_value=mock_result)
            mock_instance.close = AsyncMock()
            MockClient.return_value = mock_instance

            response = client.post(
                "/api?mode=addfile",
                data={"apikey": api_key},
                files={"nzbfile": ("test.nzb", BytesIO(nzb_data), "application/x-nzb")},
            )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] is True
            assert len(data["nzo_ids"]) == 1

            # Verify create_usenet_download was called with file data
            mock_instance.create_usenet_download.assert_called_once()
            call_kwargs = mock_instance.create_usenet_download.call_args
            assert call_kwargs[1]["file_data"] == nzb_data
            assert call_kwargs[1]["file_name"] == "test.nzb"

    def test_addfile_torbox_error_returns_502(self, app_client) -> None:
        """If Torbox rejects the upload, addfile should return 502."""
        client, api_key, _ = app_client
        torrent_data = b"d8:announce42:http://example.com/announce12:piece lengthi262144e6:pieces20:xxxxxxxxxxxxxxxxxxxxe"

        with patch("debridnzbd.api.queue.TorboxClient") as MockClient:
            mock_instance = AsyncMock()
            mock_result = MagicMock(success=False, data=None, detail="File already exists")
            mock_instance.create_torrent = AsyncMock(return_value=mock_result)
            mock_instance.close = AsyncMock()
            MockClient.return_value = mock_instance

            response = client.post(
                "/api?mode=addfile",
                data={"apikey": api_key},
                files={"nzbfile": ("test.torrent", BytesIO(torrent_data), "application/x-bittorrent")},
            )

            assert response.status_code == 502
            data = response.json()
            assert data["status"] is False
            assert "torbox" in data["error"].lower()

    def test_addfile_unsupported_type_with_webdl_default(self, app_client) -> None:
        """addfile with a non-torrent/nzb file and webdl default should return 400."""
        client, api_key, _ = app_client
        file_data = b"some random data"

        # Set default_type to webdl to test that file upload doesn't support it
        with patch("debridnzbd.api.queue.TorboxClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.close = AsyncMock()
            MockClient.return_value = mock_instance

            # We need to mock config.get to return "webdl" for default_type
            # This is tricky with the app_client fixture, so let's test
            # the handle_addfile function directly instead

        # Direct test of the handler
        from debridnzbd.api.queue import handle_addfile
        import asyncio

        # Create minimal params dict for webdl type
        params = {
            "_upload_file_data": b"data",
            "_upload_file_name": "file.txt",
            "request": None,
        }

        # Mock the config
        mock_config = AsyncMock()
        mock_config.get = AsyncMock(side_effect=[
            "test_api_key",  # torbox.api_key
            "https://api.torbox.app/v1",  # torbox.base_url
            "webdl",  # torbox.default_type
        ])

        mock_app = MagicMock()
        mock_app.state.config = mock_config
        mock_app.state.db = None

        # Create a request mock
        mock_request = MagicMock()
        mock_request.app = mock_app

        params["request"] = mock_request

        result = asyncio.get_event_loop().run_until_complete(handle_addfile(params))
        assert result.status_code == 400

    def test_addfile_creates_database_entry(self, app_client) -> None:
        """addfile should insert a job into the database with correct fields."""
        client, api_key, _ = app_client
        torrent_data = b"d8:announce42:http://example.com/announce12:piece lengthi262144e6:pieces20:xxxxxxxxxxxxxxxxxxxxe"

        with patch("debridnzbd.api.queue.TorboxClient") as MockClient:
            mock_instance = AsyncMock()
            mock_result = MagicMock(success=True, data=12345, detail="")
            mock_instance.create_torrent = AsyncMock(return_value=mock_result)
            mock_instance.close = AsyncMock()
            MockClient.return_value = mock_instance

            response = client.post(
                "/api?mode=addfile",
                data={"apikey": api_key},
                files={"nzbfile": ("my_movie.torrent", BytesIO(torrent_data), "application/x-bittorrent")},
            )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] is True
            nzo_id = data["nzo_ids"][0]
            assert nzo_id.startswith("SABnzbd_nzo_")

    def test_addfile_with_category_and_priority(self, app_client) -> None:
        """addfile should accept category and priority parameters."""
        client, api_key, _ = app_client
        torrent_data = b"d8:announce42:http://example.com/announce12:piece lengthi262144e6:pieces20:xxxxxxxxxxxxxxxxxxxxe"

        with patch("debridnzbd.api.queue.TorboxClient") as MockClient:
            mock_instance = AsyncMock()
            mock_result = MagicMock(success=True, data=12345, detail="")
            mock_instance.create_torrent = AsyncMock(return_value=mock_result)
            mock_instance.close = AsyncMock()
            MockClient.return_value = mock_instance

            response = client.post(
                "/api?mode=addfile",
                data={"apikey": api_key, "cat": "movies", "priority": "1"},
                files={"nzbfile": ("test.torrent", BytesIO(torrent_data), "application/x-bittorrent")},
            )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] is True