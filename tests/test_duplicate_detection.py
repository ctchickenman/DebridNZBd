"""Tests for duplicate detection and cache-aware re-download.

Validates:
- URL normalization for duplicate matching
- DuplicateCheckResult dataclass defaults
- handle_duplicate_check logic for all action types
- addurl duplicate detection: reuse_local, redownload_cdn, resubmit, new
- Config gating: duplicate_detection switch enables/disables the feature
- Database migration: torbox_hash column added to history table
"""

import pytest
import pytest_asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from io import BytesIO

from fastapi.testclient import TestClient

from debridnzbd.api.queue import (
    DuplicateCheckResult,
    normalize_url,
    handle_duplicate_check,
)
from debridnzbd.db.database import Database
from debridnzbd.core.config_store import ConfigStore
from debridnzbd.app import create_app


# ------------------------------------------------------------------ #
#  Unit tests for normalize_url                                        #
# ------------------------------------------------------------------ #


class TestNormalizeUrl:
    """Test URL normalization for duplicate comparison."""

    def test_lowercase_scheme_and_host(self) -> None:
        assert normalize_url("HTTP://EXAMPLE.COM/path") == "http://example.com/path"

    def test_strip_trailing_slash(self) -> None:
        assert normalize_url("http://example.com/path/") == "http://example.com/path"

    def test_preserve_path_without_trailing_slash(self) -> None:
        assert normalize_url("http://example.com/path") == "http://example.com/path"

    def test_sort_query_parameters(self) -> None:
        result = normalize_url("http://example.com/path?b=2&a=1")
        assert result == "http://example.com/path?a=1&b=2"

    def test_query_params_with_trailing_slash(self) -> None:
        result = normalize_url("http://example.com/path/?z=3&a=1")
        assert result == "http://example.com/path?a=1&z=3"

    def test_empty_url_returns_empty(self) -> None:
        assert normalize_url("") == ""

    def test_whitespace_stripped(self) -> None:
        assert normalize_url("  http://example.com/path  ") == "http://example.com/path"

    def test_no_query_params(self) -> None:
        assert normalize_url("https://api.torbox.app/v1/torrents") == "https://api.torbox.app/v1/torrents"

    def test_magnet_link_normalization(self) -> None:
        # Magnet links have no host to lowercase, but path is preserved
        result = normalize_url("magnet:?xt=urn:btih:ABC123&dn=Test")
        assert "xt=urn:btih:ABC123" in result or "dn=Test" in result

    def test_different_query_order_normalizes_same(self) -> None:
        url1 = normalize_url("http://example.com/path?b=2&a=1")
        url2 = normalize_url("http://example.com/path?a=1&b=2")
        assert url1 == url2


class TestDuplicateCheckResult:
    """Test the DuplicateCheckResult dataclass."""

    def test_defaults(self) -> None:
        result = DuplicateCheckResult(action="new")
        assert result.action == "new"
        assert result.history_row is None
        assert result.local_path is None
        assert result.size == 0.0

    def test_reuse_local_result(self) -> None:
        result = DuplicateCheckResult(
            action="reuse_local",
            history_row=("SABnzbd_nzo_abc123",),
            local_path="/data/downloads/complete/movie.mkv",
            size=1073741824.0,
        )
        assert result.action == "reuse_local"
        assert result.history_row == ("SABnzbd_nzo_abc123",)
        assert result.local_path == "/data/downloads/complete/movie.mkv"
        assert result.size == 1073741824.0

    def test_redownload_cdn_result(self) -> None:
        result = DuplicateCheckResult(
            action="redownload_cdn",
            history_row=("SABnzbd_nzo_def456",),
            size=536870912.0,
        )
        assert result.action == "redownload_cdn"
        assert result.local_path is None

    def test_resubmit_result(self) -> None:
        result = DuplicateCheckResult(
            action="resubmit",
            history_row=("SABnzbd_nzo_789",),
        )
        assert result.action == "resubmit"


# ------------------------------------------------------------------ #
#  Unit tests for handle_duplicate_check                               #
# ------------------------------------------------------------------ #


@pytest_asyncio.fixture
async def dup_db(tmp_path: Path) -> Database:
    """Create a database with schema for duplicate detection tests."""
    db_path = tmp_path / "admin" / "debridnzbd.db"
    database = Database(db_path)
    await database.initialize()
    config = ConfigStore(database)
    await config.seed_defaults()
    # Enable duplicate detection by default
    await config.set("switches", "duplicate_detection", "1")
    # Set a Torbox API key (needed for CDN availability checks)
    await config.set("torbox", "api_key", "test_api_key")
    yield database
    await database.close()


@pytest_asyncio.fixture
async def dup_config(dup_db: Database) -> ConfigStore:
    """Create a ConfigStore with duplicate detection enabled."""
    config = ConfigStore(dup_db)
    # seed_defaults was already called in dup_db fixture
    return config


class TestHandleDuplicateCheck:
    """Test the handle_duplicate_check async function."""

    @pytest.mark.asyncio
    async def test_no_db_returns_new(self, dup_config: ConfigStore) -> None:
        """When db is None, should return 'new' action."""
        result = await handle_duplicate_check(None, dup_config, "http://example.com/file.nzb", "usenet")
        assert result.action == "new"

    @pytest.mark.asyncio
    async def test_no_connection_returns_new(self, dup_config: ConfigStore) -> None:
        """When db.conn is None, should return 'new' action."""
        db = Database(Path("/nonexistent/db.db"))
        db.conn = None
        result = await handle_duplicate_check(db, dup_config, "http://example.com/file.nzb", "usenet")
        assert result.action == "new"

    @pytest.mark.asyncio
    async def test_disabled_returns_new(self, dup_db: Database, dup_config: ConfigStore) -> None:
        """When duplicate_detection is disabled, should return 'new' action."""
        await dup_config.set("switches", "duplicate_detection", "0")
        result = await handle_duplicate_check(dup_db, dup_config, "http://example.com/file.nzb", "usenet")
        assert result.action == "new"

    @pytest.mark.asyncio
    async def test_no_url_no_hash_returns_new(self, dup_db: Database, dup_config: ConfigStore) -> None:
        """When both url and torbox_hash are empty, should return 'new'."""
        result = await handle_duplicate_check(dup_db, dup_config, url="", url_type="torrent", torbox_hash="")
        assert result.action == "new"

    @pytest.mark.asyncio
    async def test_url_not_in_history_returns_new(self, dup_db: Database, dup_config: ConfigStore) -> None:
        """When URL is not in history, should return 'new'."""
        result = await handle_duplicate_check(
            dup_db, dup_config, "http://example.com/nonexistent.nzb", "usenet"
        )
        assert result.action == "new"

    @pytest.mark.asyncio
    async def test_hash_not_in_history_returns_new(self, dup_db: Database, dup_config: ConfigStore) -> None:
        """When hash is not in history, should return 'new'."""
        result = await handle_duplicate_check(
            dup_db, dup_config, url="", url_type="torrent", torbox_hash="abc123def456"
        )
        assert result.action == "new"

    @pytest.mark.asyncio
    async def test_url_in_history_file_on_disk_returns_reuse_local(
        self, dup_db: Database, dup_config: ConfigStore, tmp_path: Path
    ) -> None:
        """When URL is in history and file exists on disk, should return 'reuse_local'."""
        # Create a file on disk
        complete_dir = tmp_path / "downloads" / "complete"
        complete_dir.mkdir(parents=True, exist_ok=True)
        test_file = complete_dir / "movie.mkv"
        test_file.write_bytes(b"fake movie data")

        # Insert a history entry with the file path — URL must be in normalized form
        normalized_url = normalize_url("http://example.com/movie.nzb")
        await dup_db.conn.execute(
            """INSERT INTO history
            (nzo_id, name, status, size, category, completed, time_added, torbox_id, torbox_type, nzo_url, path, torbox_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "SABnzbd_nzo_test001",
                "movie.mkv",
                "Completed",
                1073741824.0,
                "movies",
                1700000000.0,
                1699999000.0,
                "12345",
                "usenet",
                normalized_url,
                str(test_file),
                "",
            ),
        )
        await dup_db.conn.commit()

        result = await handle_duplicate_check(
            dup_db, dup_config, "http://example.com/movie.nzb", "usenet"
        )
        assert result.action == "reuse_local"
        assert result.local_path == str(test_file)
        assert result.size == 1073741824.0

    @pytest.mark.asyncio
    async def test_url_in_history_file_not_on_disk_cdn_available_returns_redownload(
        self, dup_db: Database, dup_config: ConfigStore, tmp_path: Path
    ) -> None:
        """When URL is in history, file not on disk, but CDN available, should return 'redownload_cdn'."""
        # Insert a history entry with a non-existent file path
        normalized_url = normalize_url("magnet:?xt=urn:btih:deadbeef&dn=show")
        await dup_db.conn.execute(
            """INSERT INTO history
            (nzo_id, name, status, size, category, completed, time_added, torbox_id, torbox_type, nzo_url, path, torbox_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "SABnzbd_nzo_test002",
                "show.mkv",
                "Completed",
                536870912.0,
                "tv",
                1700000000.0,
                1699999000.0,
                "67890",
                "torrent",
                normalized_url,
                "/nonexistent/path/show.mkv",
                "",
            ),
        )
        await dup_db.conn.commit()

        # Mock check_torbox_availability at its definition site
        with patch("debridnzbd.core.state_sync.check_torbox_availability") as mock_check:
            mock_check.return_value = ("completed", True, 100, "torrent")

            # Also mock TorboxClient since handle_duplicate_check creates one
            with patch("debridnzbd.api.queue.TorboxClient") as MockClient:
                mock_client_instance = AsyncMock()
                mock_client_instance.close = AsyncMock()
                MockClient.return_value = mock_client_instance

                result = await handle_duplicate_check(
                    dup_db, dup_config, "magnet:?xt=urn:btih:deadbeef&dn=show", "torrent"
                )
                assert result.action == "redownload_cdn"
                assert result.size == 536870912.0

    @pytest.mark.asyncio
    async def test_url_in_history_file_not_on_disk_cdn_not_available_returns_resubmit(
        self, dup_db: Database, dup_config: ConfigStore, tmp_path: Path
    ) -> None:
        """When URL is in history, file not on disk, CDN not available, should return 'resubmit'."""
        # Insert a history entry with a non-existent file path
        normalized_url = normalize_url("http://example.com/file.nzb")
        await dup_db.conn.execute(
            """INSERT INTO history
            (nzo_id, name, status, size, category, completed, time_added, torbox_id, torbox_type, nzo_url, path, torbox_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "SABnzbd_nzo_test003",
                "file.nzb",
                "Completed",
                1024.0,
                "*",
                1700000000.0,
                1699999000.0,
                "11111",
                "usenet",
                normalized_url,
                "/nonexistent/path/file.nzb",
                "",
            ),
        )
        await dup_db.conn.commit()

        # Mock check_torbox_availability to return CDN NOT available
        with patch("debridnzbd.core.state_sync.check_torbox_availability") as mock_check:
            mock_check.return_value = ("failed", False, 0, "usenet")

            with patch("debridnzbd.api.queue.TorboxClient") as MockClient:
                mock_client_instance = AsyncMock()
                mock_client_instance.close = AsyncMock()
                MockClient.return_value = mock_client_instance

                result = await handle_duplicate_check(
                    dup_db, dup_config, "http://example.com/file.nzb", "usenet"
                )
                assert result.action == "resubmit"

    @pytest.mark.asyncio
    async def test_url_in_history_no_torbox_id_returns_resubmit(
        self, dup_db: Database, dup_config: ConfigStore, tmp_path: Path
    ) -> None:
        """When history entry has no torbox_id, should return 'resubmit'."""
        # Insert a history entry with no torbox_id (NULL)
        normalized_url = normalize_url("http://example.com/old_file.nzb")
        await dup_db.conn.execute(
            """INSERT INTO history
            (nzo_id, name, status, size, category, completed, time_added, torbox_id, torbox_type, nzo_url, path, torbox_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "SABnzbd_nzo_test004",
                "old_file.nzb",
                "Failed",
                2048.0,
                "*",
                1700000000.0,
                1699999000.0,
                None,  # No torbox_id
                "usenet",
                normalized_url,
                "/nonexistent/old_file.nzb",
                "",
            ),
        )
        await dup_db.conn.commit()

        result = await handle_duplicate_check(
            dup_db, dup_config, "http://example.com/old_file.nzb", "usenet"
        )
        assert result.action == "resubmit"

    @pytest.mark.asyncio
    async def test_hash_in_history_file_on_disk_returns_reuse_local(
        self, dup_db: Database, dup_config: ConfigStore, tmp_path: Path
    ) -> None:
        """When torbox_hash is in history and file exists on disk, should return 'reuse_local'."""
        # Create a file on disk
        complete_dir = tmp_path / "downloads" / "complete"
        complete_dir.mkdir(parents=True, exist_ok=True)
        test_file = complete_dir / "hashed_movie.mkv"
        test_file.write_bytes(b"fake movie data 2")

        # Insert a history entry matched by hash
        await dup_db.conn.execute(
            """INSERT INTO history
            (nzo_id, name, status, size, category, completed, time_added, torbox_id, torbox_type, nzo_url, path, torbox_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "SABnzbd_nzo_test005",
                "hashed_movie.mkv",
                "Completed",
                2048.0,
                "movies",
                1700000000.0,
                1699999000.0,
                "99999",
                "torrent",
                "",
                str(test_file),
                "abc123def456",  # stored lowercase in DB
            ),
        )
        await dup_db.conn.commit()

        result = await handle_duplicate_check(
            dup_db, dup_config, url="", url_type="torrent", torbox_hash="ABC123DEF456"
        )
        assert result.action == "reuse_local"
        assert result.local_path == str(test_file)

    @pytest.mark.asyncio
    async def test_hash_case_insensitive_matching(
        self, dup_db: Database, dup_config: ConfigStore, tmp_path: Path
    ) -> None:
        """Hash matching should be case-insensitive (stored lowercase)."""
        # Create a file on disk
        complete_dir = tmp_path / "downloads" / "complete"
        complete_dir.mkdir(parents=True, exist_ok=True)
        test_file = complete_dir / "case_test.mkv"
        test_file.write_bytes(b"case test data")

        # Insert history with lowercase hash
        await dup_db.conn.execute(
            """INSERT INTO history
            (nzo_id, name, status, size, category, completed, time_added, torbox_id, torbox_type, nzo_url, path, torbox_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "SABnzbd_nzo_test006",
                "case_test.mkv",
                "Completed",
                1024.0,
                "*",
                1700000000.0,
                1699999000.0,
                "55555",
                "torrent",
                "",
                str(test_file),
                "abc123def456",
            ),
        )
        await dup_db.conn.commit()

        # Search with uppercase hash should still match
        result = await handle_duplicate_check(
            dup_db, dup_config, url="", url_type="torrent", torbox_hash="ABC123DEF456"
        )
        assert result.action == "reuse_local"

    @pytest.mark.asyncio
    async def test_cdn_check_exception_falls_back_to_resubmit(
        self, dup_db: Database, dup_config: ConfigStore, tmp_path: Path
    ) -> None:
        """When Torbox availability check throws, should fall back to 'resubmit'."""
        # Insert a history entry with a non-existent file path but valid torbox_id
        normalized_url = normalize_url("http://example.com/error_file.nzb")
        await dup_db.conn.execute(
            """INSERT INTO history
            (nzo_id, name, status, size, category, completed, time_added, torbox_id, torbox_type, nzo_url, path, torbox_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "SABnzbd_nzo_test007",
                "error_file.nzb",
                "Completed",
                4096.0,
                "*",
                1700000000.0,
                1699999000.0,
                "77777",
                "usenet",
                normalized_url,
                "/nonexistent/error_file.nzb",
                "",
            ),
        )
        await dup_db.conn.commit()

        # Mock TorboxClient to raise an exception
        with patch("debridnzbd.api.queue.TorboxClient") as MockClient:
            mock_client_instance = AsyncMock()
            mock_client_instance.close = AsyncMock()
            MockClient.return_value = mock_client_instance

            # Mock check_torbox_availability to raise — imported dynamically,
            # so patch at the definition site
            with patch("debridnzbd.core.state_sync.check_torbox_availability") as mock_check:
                mock_check.side_effect = Exception("Torbox API error")

                result = await handle_duplicate_check(
                    dup_db, dup_config, "http://example.com/error_file.nzb", "usenet"
                )
                assert result.action == "resubmit"

    @pytest.mark.asyncio
    async def test_url_normalization_matches_different_forms(
        self, dup_db: Database, dup_config: ConfigStore, tmp_path: Path
    ) -> None:
        """Different URL forms that normalize to the same thing should match."""
        # Create a file on disk
        complete_dir = tmp_path / "downloads" / "complete"
        complete_dir.mkdir(parents=True, exist_ok=True)
        test_file = complete_dir / "norm_test.mkv"
        test_file.write_bytes(b"normalized test")

        # Store URL in its normalized form (what handle_addurl would do)
        normalized_url = normalize_url("http://example.com/path?b=2&a=1")
        await dup_db.conn.execute(
            """INSERT INTO history
            (nzo_id, name, status, size, category, completed, time_added, torbox_id, torbox_type, nzo_url, path, torbox_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "SABnzbd_nzo_test008",
                "norm_test.mkv",
                "Completed",
                8192.0,
                "*",
                1700000000.0,
                1699999000.0,
                "88888",
                "usenet",
                normalized_url,
                str(test_file),
                "",
            ),
        )
        await dup_db.conn.commit()

        # Search with a different form of the same URL (different query param order)
        # Both normalize to the same thing
        result = await handle_duplicate_check(
            dup_db, dup_config, "http://example.com/path?a=1&b=2", "usenet"
        )
        assert result.action == "reuse_local"


# ------------------------------------------------------------------ #
#  Integration tests for addurl duplicate detection                   #
# ------------------------------------------------------------------ #


@pytest_asyncio.fixture
async def addurl_client(tmp_path: Path):
    """Create a test application for addurl duplicate detection tests."""
    for dir_name in ["admin", "downloads/incomplete", "downloads/complete", "logs", "scripts"]:
        (tmp_path / dir_name).mkdir(parents=True, exist_ok=True)

    db_path = tmp_path / "admin" / "debridnzbd.db"
    database = Database(db_path)
    await database.initialize()
    config = ConfigStore(database)
    await config.seed_defaults()

    # Enable duplicate detection
    await config.set("switches", "duplicate_detection", "1")
    # Set Torbox API key
    await config.set("torbox", "api_key", "test_torbox_api_key_12345")

    app = create_app()
    app.state.db = database
    app.state.config = config

    client = TestClient(app)
    api_key = await config.get("misc", "api_key")

    yield client, api_key, database, config, tmp_path

    await database.close()


class TestAddurlDuplicateDetection:
    """Test the addurl endpoint with duplicate detection enabled."""

    def test_addurl_reuses_local_file(self, addurl_client) -> None:
        """When a URL matches history and file is on disk, should reuse it."""
        client, api_key, db, config, tmp_path = addurl_client

        # Create a file on disk
        complete_dir = tmp_path / "downloads" / "complete"
        test_file = complete_dir / "reused_file.mkv"
        test_file.write_bytes(b"reused content")

        # Insert history entry
        import asyncio

        async def setup_history():
            from debridnzbd.api.queue import normalize_url
            url = normalize_url("http://example.com/reused_file.nzb")
            await db.conn.execute(
                """INSERT INTO history
                (nzo_id, name, status, size, category, completed, time_added, torbox_id, torbox_type, nzo_url, path, torbox_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "SABnzbd_nzo_hist001",
                    "reused_file.mkv",
                    "Completed",
                    1024.0,
                    "*",
                    1700000000.0,
                    1699999000.0,
                    "44444",
                    "usenet",
                    url,
                    str(test_file),
                    "",
                ),
            )
            await db.conn.commit()

        asyncio.get_event_loop().run_until_complete(setup_history())

        # Submit the same URL — should detect the duplicate
        response = client.get(
            "/api",
            params={
                "mode": "addurl",
                "name": "http://example.com/reused_file.nzb",
                "apikey": api_key,
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] is True
        assert len(data["nzo_ids"]) == 1

        # Verify a Complete job was created in the queue
        async def check_job():
            cursor = await db.conn.execute("SELECT status FROM jobs WHERE nzo_id = ?", (data["nzo_ids"][0],))
            row = await cursor.fetchone()
            return row[0] if row else None

        job_status = asyncio.get_event_loop().run_until_complete(check_job())
        assert job_status == "Complete"

    def test_addurl_new_url_proceeds_normally(self, addurl_client) -> None:
        """When URL is not in history, should proceed with normal Torbox submission."""
        client, api_key, db, config, tmp_path = addurl_client

        with patch("debridnzbd.api.queue.TorboxClient") as MockClient:
            mock_instance = AsyncMock()
            mock_result = MagicMock(success=True, data=12345, detail="")
            mock_instance.create_usenet_download = AsyncMock(return_value=mock_result)
            mock_instance.close = AsyncMock()
            MockClient.return_value = mock_instance

            response = client.get(
                "/api",
                params={
                    "mode": "addurl",
                    "name": "http://example.com/new_file.nzb",
                    "apikey": api_key,
                },
            )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] is True
            # Should have called Torbox for a new download
            mock_instance.create_usenet_download.assert_called_once()


# ------------------------------------------------------------------ #
#  Database migration tests                                            #
# ------------------------------------------------------------------ #


class TestHistoryTorboxHashMigration:
    """Test that the torbox_hash column is added to the history table."""

    @pytest.mark.asyncio
    async def test_history_has_torbox_hash_column(self, tmp_path: Path) -> None:
        """After migration, the history table should have a torbox_hash column."""
        db_path = tmp_path / "admin" / "test.db"
        database = Database(db_path)
        await database.initialize()
        config = ConfigStore(database)
        await config.seed_defaults()

        # Check that torbox_hash column exists
        cursor = await database.conn.execute("PRAGMA table_info(history)")
        columns = [row[1] for row in await cursor.fetchall()]
        assert "torbox_hash" in columns, f"torbox_hash not found in columns: {columns}"

        await database.close()

    @pytest.mark.asyncio
    async def test_history_torbox_hash_index_exists(self, tmp_path: Path) -> None:
        """After migration, there should be an index on history.torbox_hash."""
        db_path = tmp_path / "admin" / "test.db"
        database = Database(db_path)
        await database.initialize()
        config = ConfigStore(database)
        await config.seed_defaults()

        # Check index exists
        cursor = await database.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_history_torbox_hash'"
        )
        row = await cursor.fetchone()
        assert row is not None, "idx_history_torbox_hash index not found"

        await database.close()

    @pytest.mark.asyncio
    async def test_insert_and_query_torbox_hash(self, tmp_path: Path) -> None:
        """Should be able to insert and query by torbox_hash in history."""
        db_path = tmp_path / "admin" / "test.db"
        database = Database(db_path)
        await database.initialize()
        config = ConfigStore(database)
        await config.seed_defaults()

        # Insert a history entry with torbox_hash
        await database.conn.execute(
            """INSERT INTO history
            (nzo_id, name, status, size, category, completed, time_added, torbox_id, torbox_type, nzo_url, path, torbox_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "SABnzbd_nzo_migrate_test",
                "test.mkv",
                "Completed",
                1024.0,
                "*",
                1700000000.0,
                1699999000.0,
                "12345",
                "torrent",
                "",
                "/path/to/test.mkv",
                "abc123def456",
            ),
        )
        await database.conn.commit()

        # Query by torbox_hash
        cursor = await database.conn.execute(
            "SELECT nzo_id, name FROM history WHERE torbox_hash = ?",
            ("abc123def456",),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "SABnzbd_nzo_migrate_test"
        assert row[1] == "test.mkv"

        await database.close()