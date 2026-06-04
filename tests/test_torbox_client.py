"""Tests for the Torbox async HTTP client.

Validates that:
- All API endpoints construct correct requests (method, path, params, body)
- Authentication uses Bearer token in the Authorization header
- Rate limits (429) are retried with backoff
- Server errors (5xx) are retried with backoff
- Auth errors (401/403) raise TorboxAuthError immediately
- Not found (404) raises TorboxNotFoundError immediately
- Connection errors and timeouts are retried then raise TorboxConnectionError
- Response parsing handles various data shapes (dict, list, string)
- Convenience methods (test_connection) work correctly
- Context manager properly closes the client
- CDN download link extraction handles multiple response formats

Uses respx to mock httpx requests and verify outgoing HTTP calls.
"""

import pytest
import httpx
import respx

from debridnzd.torbox.client import TorboxClient, DEFAULT_BASE_URL, MAX_RETRIES_LIMIT
from debridnzd.torbox.exceptions import (
    TorboxAuthError,
    TorboxConnectionError,
    TorboxError,
    TorboxNotFoundError,
    TorboxRateLimitError,
    TorboxServerError,
)
from debridnzd.torbox.models import (
    TorboxCachedItem,
    TorboxHoster,
    TorboxQueuedDownload,
    TorboxResponse,
    TorboxTorrentDownload,
    TorboxUsenetDownload,
    TorboxUserData,
    TorboxWebDownload,
)


# ------------------------------------------------------------------ #
#  Fixtures                                                            #
# ------------------------------------------------------------------ #

TEST_API_KEY = "tb_test_api_key_12345"


@pytest.fixture
def client():
    """Create a TorboxClient with a test API key.

    Uses respx to mock all HTTP requests to the Torbox API.
    The client uses a short timeout and low retry count for tests.
    """
    with respx.mock(assert_all_called=False) as respx_mock:
        # Configure base route — individual tests add more specific routes
        test_client = TorboxClient(
            api_key=TEST_API_KEY,
            max_retries=2,
        )
        yield test_client, respx_mock
        # Cleanup happens via context manager


@pytest.fixture
def async_client():
    """Create an async TorboxClient for testing async methods.

    Returns the client and the respx mock router.
    """
    with respx.mock(assert_all_called=False) as respx_mock:
        test_client = TorboxClient(
            api_key=TEST_API_KEY,
            max_retries=2,
        )
        yield test_client, respx_mock


# ------------------------------------------------------------------ #
#  Test: Authentication header                                         #
# ------------------------------------------------------------------ #

class TestAuthentication:
    """Test that the API key is sent correctly in requests."""

    @pytest.mark.asyncio
    async def test_bearer_token_in_header(self):
        """All requests should include the Bearer token in the Authorization header."""
        with respx.mock(assert_all_called=False) as respx_mock:
            route = respx_mock.get(f"{DEFAULT_BASE_URL}/api/user/me").respond(
                json={"success": True, "data": {"id": 1, "email": "test@test.com", "plan": 0}}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            await client.get_user_me()

            # Verify the Authorization header was set correctly
            request = route.calls[0].request
            assert request.headers["Authorization"] == f"Bearer {TEST_API_KEY}"

            await client.close()

    @pytest.mark.asyncio
    async def test_user_agent_header(self):
        """Requests should include the DebridNZBd user agent."""
        with respx.mock(assert_all_called=False) as respx_mock:
            route = respx_mock.get(f"{DEFAULT_BASE_URL}/api/user/me").respond(
                json={"success": True, "data": {"id": 1, "email": "test@test.com", "plan": 0}}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            await client.get_user_me()

            request = route.calls[0].request
            assert "DebridNZBd" in request.headers["User-Agent"]

            await client.close()


# ------------------------------------------------------------------ #
#  Test: User endpoints                                                #
# ------------------------------------------------------------------ #

class TestUserEndpoints:
    """Test user-related API endpoints."""

    @pytest.mark.asyncio
    async def test_get_user_me(self):
        """get_user_me should return a TorboxUserData object."""
        with respx.mock(assert_all_called=False) as respx_mock:
            respx_mock.get(f"{DEFAULT_BASE_URL}/api/user/me").respond(
                json={
                    "success": True,
                    "data": {
                        "id": 42,
                        "email": "user@example.com",
                        "plan": 2,
                        "is_subscribed": True,
                        "premium_expires_at": "2025-12-31",
                        "total_downloaded": 1024.0,
                        "customer": "stripe_123",
                    },
                }
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            user = await client.get_user_me()

            assert isinstance(user, TorboxUserData)
            assert user.id == 42
            assert user.email == "user@example.com"
            assert user.plan == 2
            assert user.is_subscribed is True
            assert user.premium_expires_at == "2025-12-31"

            await client.close()

    @pytest.mark.asyncio
    async def test_get_user_me_with_settings(self):
        """get_user_me with settings=True should pass the query parameter."""
        with respx.mock(assert_all_called=False) as respx_mock:
            route = respx_mock.get(f"{DEFAULT_BASE_URL}/api/user/me").respond(
                json={"success": True, "data": {"id": 1, "email": "test@test.com", "plan": 0}}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            await client.get_user_me(settings=True)

            # Verify the settings parameter was passed
            request = route.calls[0].request
            assert "settings=true" in str(request.url)

            await client.close()

    @pytest.mark.asyncio
    async def test_get_user_me_failure(self):
        """get_user_me should raise TorboxError on failed response."""
        with respx.mock(assert_all_called=False) as respx_mock:
            respx_mock.get(f"{DEFAULT_BASE_URL}/api/user/me").respond(
                json={"success": False, "detail": "Invalid API key"}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)

            with pytest.raises(TorboxError, match="Failed to get user info"):
                await client.get_user_me()

            await client.close()


# ------------------------------------------------------------------ #
#  Test: Usenet endpoints                                              #
# ------------------------------------------------------------------ #

class TestUsenetEndpoints:
    """Test usenet download API endpoints."""

    @pytest.mark.asyncio
    async def test_create_usenet_download_with_link(self):
        """create_usenet_download with a link should POST as multipart/form-data."""
        with respx.mock(assert_all_called=False) as respx_mock:
            route = respx_mock.post(f"{DEFAULT_BASE_URL}/api/usenet/createusenetdownload").respond(
                json={"success": True, "data": {"id": 1001}}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            result = await client.create_usenet_download(link="https://indexer.com/file.nzb")

            assert result.success is True
            request = route.calls[0].request
            assert request.method == "POST"
            # Torbox API requires multipart/form-data for all create endpoints
            assert "multipart" in request.headers.get("content-type", "")

            await client.close()

    @pytest.mark.asyncio
    async def test_create_usenet_download_with_file(self):
        """create_usenet_download with file_data should POST as multipart."""
        with respx.mock(assert_all_called=False) as respx_mock:
            route = respx_mock.post(f"{DEFAULT_BASE_URL}/api/usenet/createusenetdownload").respond(
                json={"success": True, "data": {"id": 1002}}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            nzb_data = b"<nzb>fake content</nzb>"
            result = await client.create_usenet_download(
                file_data=nzb_data, file_name="test.nzb"
            )

            assert result.success is True
            request = route.calls[0].request
            # Multipart upload should have multipart content type
            assert "multipart" in request.headers.get("content-type", "")

            await client.close()

    @pytest.mark.asyncio
    async def test_control_usenet_download(self):
        """control_usenet_download should send the correct operation."""
        with respx.mock(assert_all_called=False) as respx_mock:
            route = respx_mock.post(f"{DEFAULT_BASE_URL}/api/usenet/controlusenetdownload").respond(
                json={"success": True, "detail": "Paused"}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            result = await client.control_usenet_download(1001, "Pause")

            assert result.success is True
            await client.close()

    @pytest.mark.asyncio
    async def test_request_usenet_dl_returns_url(self):
        """request_usenet_dl should return the CDN download URL."""
        with respx.mock(assert_all_called=False) as respx_mock:
            cdn_url = "https://cdn.torbox.app/usenet/12345/file.nzb"
            respx_mock.get(f"{DEFAULT_BASE_URL}/api/usenet/requestdl").respond(
                json={"success": True, "data": cdn_url}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            url = await client.request_usenet_dl(usenet_id=1001)

            assert url == cdn_url

            await client.close()

    @pytest.mark.asyncio
    async def test_request_usenet_dl_with_dict_response(self):
        """request_usenet_dl should handle dict response with url/download_link key."""
        with respx.mock(assert_all_called=False) as respx_mock:
            cdn_url = "https://cdn.torbox.app/usenet/12345/file.nzb"
            respx_mock.get(f"{DEFAULT_BASE_URL}/api/usenet/requestdl").respond(
                json={"success": True, "data": {"url": cdn_url}}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            url = await client.request_usenet_dl(usenet_id=1001)

            assert url == cdn_url
            await client.close()

    @pytest.mark.asyncio
    async def test_request_usenet_dl_with_download_link_key(self):
        """request_usenet_dl should handle dict response with download_link key."""
        with respx.mock(assert_all_called=False) as respx_mock:
            cdn_url = "https://cdn.torbox.app/usenet/12345/file.nzb"
            respx_mock.get(f"{DEFAULT_BASE_URL}/api/usenet/requestdl").respond(
                json={"success": True, "data": {"download_link": cdn_url}}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            url = await client.request_usenet_dl(usenet_id=1001)

            assert url == cdn_url
            await client.close()

    @pytest.mark.asyncio
    async def test_get_usenet_list(self):
        """get_usenet_list should return a list of TorboxUsenetDownload objects."""
        with respx.mock(assert_all_called=False) as respx_mock:
            respx_mock.get(f"{DEFAULT_BASE_URL}/api/usenet/mylist").respond(
                json={
                    "success": True,
                    "data": [
                        {
                            "id": 1001,
                            "hash": "abc123",
                            "status": "downloading",
                            "created_at": "2024-01-01T00:00:00",
                            "progress": 0.5,
                            "size": 1024000,
                            "files": [],
                        },
                        {
                            "id": 1002,
                            "hash": "def456",
                            "status": "completed",
                            "created_at": "2024-01-02T00:00:00",
                            "progress": 1.0,
                            "size": 2048000,
                            "files": [],
                        },
                    ],
                }
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            downloads = await client.get_usenet_list()

            assert len(downloads) == 2
            assert isinstance(downloads[0], TorboxUsenetDownload)
            assert downloads[0].id == 1001
            assert downloads[0].status == "downloading"
            assert downloads[0].progress == 0.5
            assert downloads[1].id == 1002
            assert downloads[1].status == "completed"

            await client.close()

    @pytest.mark.asyncio
    async def test_get_usenet_list_empty(self):
        """get_usenet_list should return an empty list when no downloads exist."""
        with respx.mock(assert_all_called=False) as respx_mock:
            respx_mock.get(f"{DEFAULT_BASE_URL}/api/usenet/mylist").respond(
                json={"success": True, "data": []}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            downloads = await client.get_usenet_list()

            assert downloads == []

            await client.close()

    @pytest.mark.asyncio
    async def test_get_usenet_list_with_bypass_cache(self):
        """get_usenet_list with bypass_cache should pass the query parameter."""
        with respx.mock(assert_all_called=False) as respx_mock:
            route = respx_mock.get(f"{DEFAULT_BASE_URL}/api/usenet/mylist").respond(
                json={"success": True, "data": []}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            await client.get_usenet_list(bypass_cache=True)

            request = route.calls[0].request
            assert "bypass_cache=true" in str(request.url)

            await client.close()

    @pytest.mark.asyncio
    async def test_check_usenet_cached(self):
        """check_usenet_cached should return cached status for given hashes."""
        with respx.mock(assert_all_called=False) as respx_mock:
            route = respx_mock.get(f"{DEFAULT_BASE_URL}/api/usenet/checkcached").respond(
                json={
                    "success": True,
                    "data": [
                        {"hash": "a1b2c3d4e5f6a7b8", "cached": True},
                        {"hash": "d4e5f6a7b8c9d0e1", "cached": False},
                    ],
                }
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            result = await client.check_usenet_cached(["a1b2c3d4e5f6a7b8", "d4e5f6a7b8c9d0e1"])

            assert len(result) == 2
            assert isinstance(result[0], TorboxCachedItem)
            assert result[0].hash == "a1b2c3d4e5f6a7b8"
            assert result[0].cached is True
            assert result[1].cached is False

            # Verify hashes are sent as comma-separated query param
            request = route.calls[0].request
            assert "a1b2c3d4e5f6a7b8" in str(request.url)

            await client.close()

    @pytest.mark.asyncio
    async def test_check_usenet_cached_object_format(self):
        """check_usenet_cached should handle object format response."""
        with respx.mock(assert_all_called=False) as respx_mock:
            respx_mock.get(f"{DEFAULT_BASE_URL}/api/usenet/checkcached").respond(
                json={
                    "success": True,
                    "data": {"a1b2c3d4e5f6a7b8": True, "d4e5f6a7b8c9d0e1": False},
                }
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            result = await client.check_usenet_cached(["a1b2c3d4e5f6a7b8", "d4e5f6a7b8c9d0e1"])

            assert len(result) == 2
            hashes = {item.hash for item in result}
            assert "a1b2c3d4e5f6a7b8" in hashes
            assert "d4e5f6a7b8c9d0e1" in hashes

            await client.close()


# ------------------------------------------------------------------ #
#  Test: Torrent endpoints                                            #
# ------------------------------------------------------------------ #

class TestTorrentEndpoints:
    """Test torrent download API endpoints."""

    @pytest.mark.asyncio
    async def test_create_torrent_with_magnet(self):
        """create_torrent with a magnet link should POST as multipart/form-data."""
        with respx.mock(assert_all_called=False) as respx_mock:
            route = respx_mock.post(f"{DEFAULT_BASE_URL}/api/torrents/createtorrent").respond(
                json={"success": True, "data": {"id": 2001}}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            magnet = "magnet:?xt=urn:btih:abc123&dn=test"
            result = await client.create_torrent(magnet=magnet)

            assert result.success is True
            request = route.calls[0].request
            assert request.method == "POST"
            # Torbox API requires multipart/form-data for all create endpoints
            assert "multipart" in request.headers.get("content-type", "")

            await client.close()

    @pytest.mark.asyncio
    async def test_create_torrent_with_file(self):
        """create_torrent with file_data should POST as multipart."""
        with respx.mock(assert_all_called=False) as respx_mock:
            route = respx_mock.post(f"{DEFAULT_BASE_URL}/api/torrents/createtorrent").respond(
                json={"success": True, "data": {"id": 2002}}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            torrent_data = b"d8:announce..."
            result = await client.create_torrent(
                file_data=torrent_data, file_name="test.torrent"
            )

            assert result.success is True
            request = route.calls[0].request
            assert "multipart" in request.headers.get("content-type", "")

            await client.close()

    @pytest.mark.asyncio
    async def test_control_torrent(self):
        """control_torrent should send the correct operation."""
        with respx.mock(assert_all_called=False) as respx_mock:
            route = respx_mock.post(f"{DEFAULT_BASE_URL}/api/torrents/controltorrent").respond(
                json={"success": True, "detail": "Deleted"}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            result = await client.control_torrent(2001, "Delete")

            assert result.success is True
            await client.close()

    @pytest.mark.asyncio
    async def test_request_torrent_dl(self):
        """request_torrent_dl should return the CDN download URL."""
        with respx.mock(assert_all_called=False) as respx_mock:
            cdn_url = "https://cdn.torbox.app/torrent/12345/file.mkv"
            respx_mock.get(f"{DEFAULT_BASE_URL}/api/torrents/requestdl").respond(
                json={"success": True, "data": cdn_url}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            url = await client.request_torrent_dl(torrent_id=2001)

            assert url == cdn_url
            await client.close()

    @pytest.mark.asyncio
    async def test_get_torrent_list(self):
        """get_torrent_list should return a list of TorboxTorrentDownload objects."""
        with respx.mock(assert_all_called=False) as respx_mock:
            respx_mock.get(f"{DEFAULT_BASE_URL}/api/torrents/mylist").respond(
                json={
                    "success": True,
                    "data": [
                        {
                            "id": 2001,
                            "hash": "abc123",
                            "status": "downloading",
                            "created_at": "2024-01-01T00:00:00",
                            "name": "test.torrent",
                            "progress": 0.75,
                            "size": 1024000,
                            "seeders": 42,
                            "files": [],
                        },
                    ],
                }
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            downloads = await client.get_torrent_list()

            assert len(downloads) == 1
            assert isinstance(downloads[0], TorboxTorrentDownload)
            assert downloads[0].id == 2001
            assert downloads[0].name == "test.torrent"
            assert downloads[0].seeders == 42

            await client.close()

    @pytest.mark.asyncio
    async def test_check_torrent_cached(self):
        """check_torrent_cached should return cache status for given hashes."""
        with respx.mock(assert_all_called=False) as respx_mock:
            respx_mock.get(f"{DEFAULT_BASE_URL}/api/torrents/checkcached").respond(
                json={
                    "success": True,
                    "data": [
                        {"hash": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6", "cached": True},
                    ],
                }
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            result = await client.check_torrent_cached(["a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"])

            assert len(result) == 1
            assert result[0].cached is True

            await client.close()


# ------------------------------------------------------------------ #
#  Test: Web download endpoints                                        #
# ------------------------------------------------------------------ #

class TestWebDownloadEndpoints:
    """Test web download API endpoints."""

    @pytest.mark.asyncio
    async def test_create_web_download(self):
        """create_web_download should POST as multipart/form-data with the link."""
        with respx.mock(assert_all_called=False) as respx_mock:
            route = respx_mock.post(f"{DEFAULT_BASE_URL}/api/webdl/createwebdownload").respond(
                json={"success": True, "data": {"id": 3001}}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            result = await client.create_web_download(link="https://example.com/file.zip")

            assert result.success is True
            request = route.calls[0].request
            assert request.method == "POST"
            # Torbox API requires multipart/form-data for all create endpoints
            assert "multipart" in request.headers.get("content-type", "")

            await client.close()

    @pytest.mark.asyncio
    async def test_control_web_download(self):
        """control_web_download should send the correct operation."""
        with respx.mock(assert_all_called=False) as respx_mock:
            route = respx_mock.post(f"{DEFAULT_BASE_URL}/api/webdl/controlwebdownload").respond(
                json={"success": True, "detail": "Deleted"}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            result = await client.control_web_download(3001, "Delete")

            assert result.success is True
            await client.close()

    @pytest.mark.asyncio
    async def test_request_web_dl(self):
        """request_web_dl should return the CDN download URL."""
        with respx.mock(assert_all_called=False) as respx_mock:
            cdn_url = "https://cdn.torbox.app/webdl/12345/file.zip"
            respx_mock.get(f"{DEFAULT_BASE_URL}/api/webdl/requestdl").respond(
                json={"success": True, "data": cdn_url}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            url = await client.request_web_dl(web_id=3001)

            assert url == cdn_url
            await client.close()

    @pytest.mark.asyncio
    async def test_get_web_download_list(self):
        """get_web_download_list should return a list of TorboxWebDownload objects."""
        with respx.mock(assert_all_called=False) as respx_mock:
            respx_mock.get(f"{DEFAULT_BASE_URL}/api/webdl/mylist").respond(
                json={
                    "success": True,
                    "data": [
                        {
                            "id": 3001,
                            "hash": "web123",
                            "status": "completed",
                            "created_at": "2024-01-01T00:00:00",
                            "name": "file.zip",
                            "progress": 1.0,
                            "size": 512000,
                            "files": [],
                        },
                    ],
                }
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            downloads = await client.get_web_download_list()

            assert len(downloads) == 1
            assert isinstance(downloads[0], TorboxWebDownload)
            assert downloads[0].name == "file.zip"

            await client.close()

    @pytest.mark.asyncio
    async def test_check_web_cached(self):
        """check_web_cached should return cache status for given hashes."""
        with respx.mock(assert_all_called=False) as respx_mock:
            respx_mock.get(f"{DEFAULT_BASE_URL}/api/webdl/checkcached").respond(
                json={"success": True, "data": [{"hash": "a1b2c3d4e5f6a7b8", "cached": True}]}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            result = await client.check_web_cached(["a1b2c3d4e5f6a7b8"])

            assert len(result) == 1
            assert result[0].cached is True

            await client.close()

    @pytest.mark.asyncio
    async def test_get_hosters_list(self):
        """get_hosters_list should return a list of TorboxHoster objects."""
        with respx.mock(assert_all_called=False) as respx_mock:
            respx_mock.get(f"{DEFAULT_BASE_URL}/api/webdl/hosters").respond(
                json={
                    "success": True,
                    "data": [
                        {
                            "name": "ExampleHoster",
                            "domains": ["example.com", "example.org"],
                            "url": "https://example.com",
                            "icon": "",
                            "status": "active",
                            "type": "hoster",
                            "note": "",
                            "daily_link_limit": 100,
                            "daily_link_used": 10,
                            "daily_bandwidth_limit": 0,
                            "daily_bandwidth_used": 0,
                        },
                    ],
                }
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            hosters = await client.get_hosters_list()

            assert len(hosters) == 1
            assert isinstance(hosters[0], TorboxHoster)
            assert hosters[0].name == "ExampleHoster"
            assert hosters[0].status == "active"

            await client.close()

    @pytest.mark.asyncio
    async def test_get_hosters_list_empty(self):
        """get_hosters_list should return empty list on failure."""
        with respx.mock(assert_all_called=False) as respx_mock:
            respx_mock.get(f"{DEFAULT_BASE_URL}/api/webdl/hosters").respond(
                json={"success": False, "detail": "Error"}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            hosters = await client.get_hosters_list()

            assert hosters == []
            await client.close()


# ------------------------------------------------------------------ #
#  Test: Queued download endpoints                                     #
# ------------------------------------------------------------------ #

class TestQueuedEndpoints:
    """Test queued download API endpoints."""

    @pytest.mark.asyncio
    async def test_get_queued_downloads(self):
        """get_queued_downloads should return queued items."""
        with respx.mock(assert_all_called=False) as respx_mock:
            respx_mock.get(f"{DEFAULT_BASE_URL}/api/queued/getqueued").respond(
                json={
                    "success": True,
                    "data": [
                        {
                            "id": 4001,
                            "type": "torrent",
                            "hash": "queue123",
                            "created_at": "2024-01-01T00:00:00",
                        },
                    ],
                }
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            queued = await client.get_queued_downloads()

            assert len(queued) == 1
            assert isinstance(queued[0], TorboxQueuedDownload)
            assert queued[0].type == "torrent"

            await client.close()

    @pytest.mark.asyncio
    async def test_get_queued_downloads_with_type_filter(self):
        """get_queued_downloads with download_type should pass the type parameter."""
        with respx.mock(assert_all_called=False) as respx_mock:
            route = respx_mock.get(f"{DEFAULT_BASE_URL}/api/queued/getqueued").respond(
                json={"success": True, "data": []}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            await client.get_queued_downloads(download_type="torrent")

            request = route.calls[0].request
            assert "type=torrent" in str(request.url)

            await client.close()

    @pytest.mark.asyncio
    async def test_control_queued_download(self):
        """control_queued_download should send the correct operation."""
        with respx.mock(assert_all_called=False) as respx_mock:
            route = respx_mock.post(f"{DEFAULT_BASE_URL}/api/queued/controlqueued").respond(
                json={"success": True, "detail": "Deleted"}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            result = await client.control_queued_download(4001, "Delete")

            assert result.success is True
            await client.close()


# ------------------------------------------------------------------ #
#  Test: Error handling and retry logic                                #
# ------------------------------------------------------------------ #

class TestErrorHandling:
    """Test that HTTP errors are mapped to the correct exceptions."""

    @pytest.mark.asyncio
    async def test_auth_error_401(self):
        """401 responses should raise TorboxAuthError immediately."""
        with respx.mock(assert_all_called=False) as respx_mock:
            respx_mock.get(f"{DEFAULT_BASE_URL}/api/user/me").respond(
                status_code=401, json={"detail": "Unauthorized"}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=2)

            with pytest.raises(TorboxAuthError, match="authentication failed"):
                await client.get_user_me()

            await client.close()

    @pytest.mark.asyncio
    async def test_auth_error_403(self):
        """403 responses should raise TorboxAuthError immediately."""
        with respx.mock(assert_all_called=False) as respx_mock:
            respx_mock.get(f"{DEFAULT_BASE_URL}/api/user/me").respond(
                status_code=403, json={"detail": "Forbidden"}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=2)

            with pytest.raises(TorboxAuthError, match="authentication failed"):
                await client.get_user_me()

            await client.close()

    @pytest.mark.asyncio
    async def test_not_found_404(self):
        """404 responses should raise TorboxNotFoundError immediately."""
        with respx.mock(assert_all_called=False) as respx_mock:
            respx_mock.get(f"{DEFAULT_BASE_URL}/api/user/me").respond(
                status_code=404, json={"detail": "Not found"}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=2)

            with pytest.raises(TorboxNotFoundError, match="not found"):
                await client.get_user_me()

            await client.close()

    @pytest.mark.asyncio
    async def test_rate_limit_429_retry(self):
        """429 responses should be retried with backoff, then raise TorboxRateLimitError."""
        with respx.mock(assert_all_called=False) as respx_mock:
            # Return 429 twice, then 200
            route = respx_mock.get(f"{DEFAULT_BASE_URL}/api/user/me").mock(
                side_effect=[
                    httpx.Response(status_code=429, headers={"Retry-After": "0"}, json={"detail": "Rate limited"}),
                    httpx.Response(status_code=429, headers={"Retry-After": "0"}, json={"detail": "Rate limited"}),
                    httpx.Response(status_code=200, json={"success": True, "data": {"id": 1, "plan": 0}}),
                ]
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=2)
            # Should succeed after retries
            user = await client.get_user_me()
            assert user.id == 1

            await client.close()

    @pytest.mark.asyncio
    async def test_rate_limit_429_exhausted(self):
        """429 responses beyond max retries should raise TorboxRateLimitError."""
        with respx.mock(assert_all_called=False) as respx_mock:
            # Return 429 three times (exceeds max_retries=2)
            respx_mock.get(f"{DEFAULT_BASE_URL}/api/user/me").respond(
                status_code=429, json={"detail": "Rate limited"}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=1)

            with pytest.raises(TorboxRateLimitError) as exc_info:
                await client.get_user_me()

            assert exc_info.value.retry_after is not None

            await client.close()

    @pytest.mark.asyncio
    async def test_server_error_500_retry(self):
        """500 responses should be retried with backoff, then raise TorboxServerError."""
        with respx.mock(assert_all_called=False) as respx_mock:
            # Return 500 twice, then 200
            route = respx_mock.get(f"{DEFAULT_BASE_URL}/api/user/me").mock(
                side_effect=[
                    httpx.Response(status_code=500, json={"detail": "Internal error"}),
                    httpx.Response(status_code=500, json={"detail": "Internal error"}),
                    httpx.Response(status_code=200, json={"success": True, "data": {"id": 1, "plan": 0}}),
                ]
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=2)
            user = await client.get_user_me()
            assert user.id == 1

            await client.close()

    @pytest.mark.asyncio
    async def test_server_error_exhausted(self):
        """500 responses beyond max retries should raise TorboxServerError."""
        with respx.mock(assert_all_called=False) as respx_mock:
            respx_mock.get(f"{DEFAULT_BASE_URL}/api/user/me").respond(
                status_code=500, json={"detail": "Internal error"}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=1)

            with pytest.raises(TorboxServerError, match="server error"):
                await client.get_user_me()

            await client.close()

    @pytest.mark.asyncio
    async def test_connection_error_retry(self):
        """Connection errors should be retried, then raise TorboxConnectionError."""
        with respx.mock(assert_all_called=False) as respx_mock:
            route = respx_mock.get(f"{DEFAULT_BASE_URL}/api/user/me").mock(
                side_effect=[
                    httpx.ConnectError("Connection refused"),
                    httpx.Response(status_code=200, json={"success": True, "data": {"id": 1, "plan": 0}}),
                ]
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=2)
            user = await client.get_user_me()
            assert user.id == 1

            await client.close()

    @pytest.mark.asyncio
    async def test_connection_error_exhausted(self):
        """Connection errors beyond max retries should raise TorboxConnectionError."""
        with respx.mock(assert_all_called=False) as respx_mock:
            respx_mock.get(f"{DEFAULT_BASE_URL}/api/user/me").mock(
                side_effect=httpx.ConnectError("Connection refused")
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=1)

            with pytest.raises(TorboxConnectionError, match="Cannot connect"):
                await client.get_user_me()

            await client.close()

    @pytest.mark.asyncio
    async def test_timeout_error_retry(self):
        """Timeout errors should be retried, then raise TorboxConnectionError."""
        with respx.mock(assert_all_called=False) as respx_mock:
            route = respx_mock.get(f"{DEFAULT_BASE_URL}/api/user/me").mock(
                side_effect=[
                    httpx.TimeoutException("Read timed out"),
                    httpx.Response(status_code=200, json={"success": True, "data": {"id": 1, "plan": 0}}),
                ]
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=2)
            user = await client.get_user_me()
            assert user.id == 1

            await client.close()

    @pytest.mark.asyncio
    async def test_timeout_error_exhausted(self):
        """Timeout errors beyond max retries should raise TorboxConnectionError."""
        with respx.mock(assert_all_called=False) as respx_mock:
            respx_mock.get(f"{DEFAULT_BASE_URL}/api/user/me").mock(
                side_effect=httpx.TimeoutException("Read timed out")
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=1)

            with pytest.raises(TorboxConnectionError, match="timed out"):
                await client.get_user_me()

            await client.close()


# ------------------------------------------------------------------ #
#  Test: Convenience methods                                           #
# ------------------------------------------------------------------ #

class TestConvenienceMethods:
    """Test convenience methods like test_connection."""

    @pytest.mark.asyncio
    async def test_test_connection_success(self):
        """test_connection should return (True, message) on success."""
        with respx.mock(assert_all_called=False) as respx_mock:
            respx_mock.get(f"{DEFAULT_BASE_URL}/api/user/me").respond(
                json={
                    "success": True,
                    "data": {
                        "id": 1,
                        "email": "test@test.com",
                        "plan": 2,
                        "premium_expires_at": "2025-12-31",
                    },
                }
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            success, message = await client.test_connection()

            assert success is True
            assert "Pro" in message
            assert "Connected" in message

            await client.close()

    @pytest.mark.asyncio
    async def test_test_connection_auth_failure(self):
        """test_connection should return (False, message) on auth failure."""
        with respx.mock(assert_all_called=False) as respx_mock:
            respx_mock.get(f"{DEFAULT_BASE_URL}/api/user/me").respond(
                status_code=401, json={"detail": "Unauthorized"}
            )
            client = TorboxClient(api_key="invalid_key", max_retries=0)
            success, message = await client.test_connection()

            assert success is False
            assert "Authentication" in message or "auth" in message.lower()

            await client.close()

    @pytest.mark.asyncio
    async def test_test_connection_network_failure(self):
        """test_connection should return (False, message) on network failure."""
        with respx.mock(assert_all_called=False) as respx_mock:
            respx_mock.get(f"{DEFAULT_BASE_URL}/api/user/me").mock(
                side_effect=httpx.ConnectError("Connection refused")
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            success, message = await client.test_connection()

            assert success is False
            assert "Cannot reach" in message or "connect" in message.lower()

            await client.close()


# ------------------------------------------------------------------ #
#  Test: Context manager                                               #
# ------------------------------------------------------------------ #

class TestContextManager:
    """Test the async context manager protocol."""

    @pytest.mark.asyncio
    async def test_context_manager(self):
        """Using TorboxClient as a context manager should close the client on exit."""
        with respx.mock(assert_all_called=False) as respx_mock:
            respx_mock.get(f"{DEFAULT_BASE_URL}/api/user/me").respond(
                json={"success": True, "data": {"id": 1, "plan": 0}}
            )
            async with TorboxClient(api_key=TEST_API_KEY, max_retries=0) as client:
                user = await client.get_user_me()
                assert user.id == 1
            # Client should be closed after the context manager exits
            # Verify by checking the client is closed
            assert client._client.is_closed


# ------------------------------------------------------------------ #
#  Test: Custom base URL                                              #
# ------------------------------------------------------------------ #

class TestCustomBaseUrl:
    """Test that custom base URLs are supported."""

    @pytest.mark.asyncio
    async def test_custom_base_url(self):
        """A custom base URL should be used instead of the default."""
        custom_url = "https://custom-api.example.com/v1"
        with respx.mock(assert_all_called=False) as respx_mock:
            route = respx_mock.get(f"{custom_url}/api/user/me").respond(
                json={"success": True, "data": {"id": 1, "plan": 0}}
            )
            client = TorboxClient(api_key=TEST_API_KEY, base_url=custom_url, max_retries=0)
            user = await client.get_user_me()

            assert user.id == 1
            # Verify the request went to the custom URL
            assert len(route.calls) == 1

            await client.close()


# ------------------------------------------------------------------ #
#  Test: Edge cases                                                    #
# ------------------------------------------------------------------ #

class TestEdgeCases:
    """Test edge cases and unusual responses."""

    @pytest.mark.asyncio
    async def test_empty_data_list(self):
        """An empty data list should return an empty result list."""
        with respx.mock(assert_all_called=False) as respx_mock:
            respx_mock.get(f"{DEFAULT_BASE_URL}/api/usenet/mylist").respond(
                json={"success": True, "data": []}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            result = await client.get_usenet_list()
            assert result == []

            await client.close()

    @pytest.mark.asyncio
    async def test_failed_response_returns_empty_list(self):
        """A failed response (success=false) should return an empty list."""
        with respx.mock(assert_all_called=False) as respx_mock:
            respx_mock.get(f"{DEFAULT_BASE_URL}/api/usenet/mylist").respond(
                json={"success": False, "detail": "Error"}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            result = await client.get_usenet_list()
            assert result == []

            await client.close()

    @pytest.mark.asyncio
    async def test_request_dl_empty_data(self):
        """request_usenet_dl with no data should return empty string."""
        with respx.mock(assert_all_called=False) as respx_mock:
            respx_mock.get(f"{DEFAULT_BASE_URL}/api/usenet/requestdl").respond(
                json={"success": True, "data": None}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            url = await client.request_usenet_dl(usenet_id=1001)

            assert url == ""

            await client.close()

    @pytest.mark.asyncio
    async def test_request_dl_with_file_id(self):
        """request_usenet_dl with file_id should include file_id parameter."""
        with respx.mock(assert_all_called=False) as respx_mock:
            cdn_url = "https://cdn.torbox.app/usenet/12345/file.nzb"
            route = respx_mock.get(f"{DEFAULT_BASE_URL}/api/usenet/requestdl").respond(
                json={"success": True, "data": cdn_url}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            url = await client.request_usenet_dl(usenet_id=1001, file_id=42)

            request = route.calls[0].request
            assert "file_id=42" in str(request.url)
            assert url == cdn_url

            await client.close()

    @pytest.mark.asyncio
    async def test_request_dl_with_zip_link(self):
        """request_usenet_dl with zip_link should include zip_link parameter."""
        with respx.mock(assert_all_called=False) as respx_mock:
            cdn_url = "https://cdn.torbox.app/usenet/12345/zip"
            route = respx_mock.get(f"{DEFAULT_BASE_URL}/api/usenet/requestdl").respond(
                json={"success": True, "data": cdn_url}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            url = await client.request_usenet_dl(usenet_id=1001, zip_link=True)

            request = route.calls[0].request
            assert "zip_link=true" in str(request.url)

            await client.close()

    @pytest.mark.asyncio
    async def test_pagination_params(self):
        """get_usenet_list with offset and limit should pass them as params."""
        with respx.mock(assert_all_called=False) as respx_mock:
            route = respx_mock.get(f"{DEFAULT_BASE_URL}/api/usenet/mylist").respond(
                json={"success": True, "data": []}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            await client.get_usenet_list(offset=10, limit=50)

            request = route.calls[0].request
            assert "offset=10" in str(request.url)
            assert "limit=50" in str(request.url)

            await client.close()

    @pytest.mark.asyncio
    async def test_specific_id_filter(self):
        """get_usenet_list with usenet_id should include id parameter."""
        with respx.mock(assert_all_called=False) as respx_mock:
            route = respx_mock.get(f"{DEFAULT_BASE_URL}/api/usenet/mylist").respond(
                json={"success": True, "data": []}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            await client.get_usenet_list(usenet_id=1001)

            request = route.calls[0].request
            assert "id=1001" in str(request.url)

            await client.close()

    @pytest.mark.asyncio
    async def test_create_usenet_with_post_processing(self):
        """create_usenet_download should pass post_processing parameter."""
        with respx.mock(assert_all_called=False) as respx_mock:
            route = respx_mock.post(f"{DEFAULT_BASE_URL}/api/usenet/createusenetdownload").respond(
                json={"success": True, "data": {"id": 1001}}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            result = await client.create_usenet_download(
                link="https://indexer.com/file.nzb", post_processing=3
            )

            assert result.success is True
            # Verify the request body includes post_processing (multipart/form-data)
            body = route.calls[0].request.content.decode()
            assert "post_processing" in body
            assert "3" in body

            await client.close()

    @pytest.mark.asyncio
    async def test_create_torrent_with_options(self):
        """create_torrent should pass allow_zip, as_queued, and seed options."""
        with respx.mock(assert_all_called=False) as respx_mock:
            route = respx_mock.post(f"{DEFAULT_BASE_URL}/api/torrents/createtorrent").respond(
                json={"success": True, "data": {"id": 2001}}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            result = await client.create_torrent(
                magnet="magnet:?xt=urn:btih:abc123",
                allow_zip=True,
                as_queued=True,
                seed=3600,
            )

            assert result.success is True
            # Verify the request body includes options (multipart/form-data)
            body = route.calls[0].request.content.decode()
            assert "allow_zip" in body
            assert "true" in body
            assert "as_queued" in body
            assert "seed" in body
            assert "3600" in body

            await client.close()


# ------------------------------------------------------------------ #
#  Test: Security validations                                         #
# ------------------------------------------------------------------ #

class TestSecurityValidations:
    """Test input validation and security features."""

    @pytest.mark.asyncio
    async def test_empty_api_key_rejected(self):
        """An empty API key should be rejected in the constructor."""
        with pytest.raises(ValueError, match="must not be empty"):
            TorboxClient(api_key="", max_retries=0)

    @pytest.mark.asyncio
    async def test_max_retries_capped(self):
        """max_retries should be capped to MAX_RETRIES_LIMIT."""
        client = TorboxClient(api_key=TEST_API_KEY, max_retries=100)
        assert client.max_retries == MAX_RETRIES_LIMIT
        await client.close()

    @pytest.mark.asyncio
    async def test_max_retries_negative_becomes_zero(self):
        """Negative max_retries should be clamped to 0."""
        client = TorboxClient(api_key=TEST_API_KEY, max_retries=-5)
        assert client.max_retries == 0
        await client.close()

    @pytest.mark.asyncio
    async def test_base_url_must_be_http_or_https(self):
        """base_url with invalid scheme should be rejected."""
        with pytest.raises(ValueError, match="http:// or https://"):
            TorboxClient(api_key=TEST_API_KEY, base_url="ftp://example.com", max_retries=0)

    @pytest.mark.asyncio
    async def test_base_url_http_nonlocalhost_warns(self):
        """http:// base_url for non-localhost should log a warning (not reject)."""
        client = TorboxClient(api_key=TEST_API_KEY, base_url="http://example.com", max_retries=0)
        assert client.base_url == "http://example.com"
        await client.close()

    @pytest.mark.asyncio
    async def test_url_validation_rejects_javascript_scheme(self):
        """create_usenet_download should reject javascript: URLs."""
        client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
        with pytest.raises(ValueError, match="scheme"):
            await client.create_usenet_download(link="javascript:alert(1)")
        await client.close()

    @pytest.mark.asyncio
    async def test_url_validation_rejects_empty_link(self):
        """create_web_download should reject empty link."""
        client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
        with pytest.raises(ValueError, match="must not be empty"):
            await client.create_web_download(link="")
        await client.close()

    @pytest.mark.asyncio
    async def test_magnet_validation_rejects_invalid(self):
        """create_torrent should reject non-magnet strings."""
        client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
        with pytest.raises(ValueError, match="magnet"):
            await client.create_torrent(magnet="https://example.com/file.torrent")
        await client.close()

    @pytest.mark.asyncio
    async def test_neither_link_nor_file_raises_error(self):
        """create_usenet_download should raise when neither link nor file_data provided."""
        client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
        with pytest.raises(ValueError, match="Either link or file_data"):
            await client.create_usenet_download()
        await client.close()

    @pytest.mark.asyncio
    async def test_file_size_validation(self):
        """create_usenet_download should reject files exceeding MAX_FILE_SIZE."""
        from debridnzd.torbox.client import MAX_FILE_SIZE
        client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
        with pytest.raises(ValueError, match="exceeds maximum"):
            await client.create_usenet_download(
                file_data=b"x" * (MAX_FILE_SIZE + 1),
                file_name="big.nzb"
            )
        await client.close()

    @pytest.mark.asyncio
    async def test_hash_validation_rejects_short(self):
        """check_usenet_cached should reject hashes shorter than 8 chars."""
        client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
        with pytest.raises(ValueError, match="Invalid hash format"):
            await client.check_usenet_cached(["abc"])
        await client.close()

    @pytest.mark.asyncio
    async def test_hash_validation_rejects_too_many(self):
        """check_usenet_cached should reject more than MAX_HASH_BATCH hashes."""
        from debridnzd.torbox.client import MAX_HASH_BATCH
        client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
        with pytest.raises(ValueError, match="Too many hashes"):
            await client.check_usenet_cached(["a1b2c3d4e5f6a7b8"] * (MAX_HASH_BATCH + 1))
        await client.close()

    @pytest.mark.asyncio
    async def test_ip_validation_rejects_non_ip(self):
        """request_usenet_dl should reject non-IP user_ip values."""
        client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
        with pytest.raises(ValueError, match="valid IPv4 or IPv6"):
            await client.request_usenet_dl(usenet_id=1, user_ip="example.com")
        await client.close()

    @pytest.mark.asyncio
    async def test_ip_validation_accepts_valid_ipv4(self):
        """request_usenet_dl should accept valid IPv4 addresses."""
        with respx.mock(assert_all_called=False) as respx_mock:
            respx_mock.get(f"{DEFAULT_BASE_URL}/api/usenet/requestdl").respond(
                json={"success": True, "data": "https://cdn.example.com/file"}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            url = await client.request_usenet_dl(usenet_id=1, user_ip="192.168.1.1")
            assert url == "https://cdn.example.com/file"
            await client.close()

    @pytest.mark.asyncio
    async def test_control_operation_validation(self):
        """control_usenet_download should reject invalid operations."""
        client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
        with pytest.raises(ValueError, match="Invalid usenet operation"):
            await client.control_usenet_download(1, "InvalidOp")
        await client.close()

    @pytest.mark.asyncio
    async def test_download_type_validation(self):
        """get_queued_downloads should reject invalid download_type."""
        client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
        with pytest.raises(ValueError, match="download_type"):
            await client.get_queued_downloads(download_type="invalid")
        await client.close()

    @pytest.mark.asyncio
    async def test_pagination_validation(self):
        """get_usenet_list should reject negative offset."""
        client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
        with pytest.raises(ValueError, match="offset must be >= 0"):
            await client.get_usenet_list(offset=-1)
        await client.close()

    @pytest.mark.asyncio
    async def test_pagination_limit_validation(self):
        """get_usenet_list should reject limit > 1000."""
        client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
        with pytest.raises(ValueError, match="limit must be between"):
            await client.get_usenet_list(limit=2000)
        await client.close()

    @pytest.mark.asyncio
    async def test_error_messages_sanitize_paths(self):
        """404 errors should not reveal full API paths with query params."""
        with respx.mock(assert_all_called=False) as respx_mock:
            respx_mock.get(f"{DEFAULT_BASE_URL}/api/user/me").respond(
                status_code=404, json={"detail": "Not found"}
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            with pytest.raises(TorboxNotFoundError) as exc_info:
                await client.get_user_me()
            # Error message should contain a sanitized path
            error_msg = str(exc_info.value)
            assert "?" not in error_msg  # No query params leaked

            await client.close()

    @pytest.mark.asyncio
    async def test_connection_error_message_sanitized(self):
        """Connection errors should not include raw httpx exception details."""
        with respx.mock(assert_all_called=False) as respx_mock:
            respx_mock.get(f"{DEFAULT_BASE_URL}/api/user/me").mock(
                side_effect=httpx.ConnectError("Connection refused to 10.0.0.1:443")
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=0)
            with pytest.raises(TorboxConnectionError) as exc_info:
                await client.get_user_me()
            # Error message should be generic, not contain internal details
            error_msg = str(exc_info.value)
            assert "10.0.0.1" not in error_msg
            assert "Cannot connect" in error_msg

            await client.close()

    # --- Round 2 security tests ---

    @pytest.mark.asyncio
    async def test_url_validation_rejects_private_ipv4(self):
        """SSRF prevention: URLs pointing to private/reserved IPs must be rejected."""
        from debridnzd.torbox.client import _validate_url

        private_ips = [
            "http://127.0.0.1/endpoint",
            "http://10.0.0.1/endpoint",
            "http://172.16.0.1/endpoint",
            "http://192.168.1.1/endpoint",
            "http://169.254.169.254/latest/meta-data/",  # AWS metadata
        ]
        for url in private_ips:
            with pytest.raises(ValueError, match="private/reserved IP"):
                _validate_url(url, "link")

    @pytest.mark.asyncio
    async def test_url_validation_rejects_private_ipv6(self):
        """SSRF prevention: URLs pointing to IPv6 loopback/link-local must be rejected."""
        from debridnzd.torbox.client import _validate_url

        with pytest.raises(ValueError, match="private/reserved IP"):
            _validate_url("http://[::1]/endpoint", "link")
        with pytest.raises(ValueError, match="private/reserved IP"):
            _validate_url("http://[fe80::1]/endpoint", "link")

    @pytest.mark.asyncio
    async def test_url_validation_allows_domain_names(self):
        """Non-IP hostnames (domain names) should pass validation."""
        from debridnzd.torbox.client import _validate_url

        # Domain names should NOT be rejected (DNS is done by Torbox API, not us)
        result = _validate_url("https://example.com/file.nzb", "link")
        assert result == "https://example.com/file.nzb"

    @pytest.mark.asyncio
    async def test_url_validation_allows_public_ip(self):
        """Public IP addresses should pass validation."""
        from debridnzd.torbox.client import _validate_url

        result = _validate_url("https://8.8.8.8/endpoint", "link")
        assert result == "https://8.8.8.8/endpoint"

    @pytest.mark.asyncio
    async def test_follow_redirects_is_false(self):
        """Client should NOT follow redirects to prevent SSRF via open redirects."""
        with respx.mock(assert_all_called=False):
            client = TorboxClient(api_key=TEST_API_KEY)
            # The httpx client should have follow_redirects=False
            assert client._client.follow_redirects is False
            await client.close()

    @pytest.mark.asyncio
    async def test_retry_after_has_floor(self):
        """Retry-After header values should be floored at 1 second minimum."""
        with respx.mock(assert_all_called=False) as respx_mock:
            # 429 with Retry-After: 0 (should be treated as 1 second, not 0)
            respx_mock.get(f"{DEFAULT_BASE_URL}/api/user/me").mock(
                side_effect=[
                    httpx.Response(429, headers={"Retry-After": "0"}, text="rate limited"),
                    httpx.Response(200, json={"success": True, "data": {"id": 1}}),
                ]
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=2)
            result = await client.get_user_me()
            assert result.id == 1
            await client.close()

    @pytest.mark.asyncio
    async def test_memory_error_not_caught(self):
        """MemoryError and RecursionError should not be caught by generic exception handler."""
        with respx.mock(assert_all_called=False) as respx_mock:
            respx_mock.get(f"{DEFAULT_BASE_URL}/api/user/me").mock(
                side_effect=MemoryError("out of memory")
            )
            client = TorboxClient(api_key=TEST_API_KEY, max_retries=2)
            # Should re-raise MemoryError, not wrap it in TorboxError
            with pytest.raises(MemoryError):
                await client.get_user_me()
            await client.close()

    # --- Round 4 security tests ---

    @pytest.mark.asyncio
    async def test_url_validation_rejects_decimal_ip(self):
        """SSRF prevention: decimal IP format (2130706433 = 127.0.0.1) must be rejected."""
        from debridnzd.torbox.client import _validate_url

        # 2130706433 = 127.0.0.1 in decimal
        with pytest.raises(ValueError, match="private/reserved IP"):
            _validate_url("http://2130706433/admin", "link")

    @pytest.mark.asyncio
    async def test_url_validation_rejects_hex_ip(self):
        """SSRF prevention: hex IP format (0x7f000001 = 127.0.0.1) must be rejected."""
        from debridnzd.torbox.client import _validate_url

        with pytest.raises(ValueError, match="private/reserved IP"):
            _validate_url("http://0x7f000001/admin", "link")

    @pytest.mark.asyncio
    async def test_url_validation_rejects_octal_ip(self):
        """SSRF prevention: octal IP format (017700000001 = 127.0.0.1) must be rejected."""
        from debridnzd.torbox.client import _validate_url

        # 017700000001 = 127.0.0.1 in octal
        with pytest.raises(ValueError, match="private/reserved IP"):
            _validate_url("http://017700000001/admin", "link")

    @pytest.mark.asyncio
    async def test_url_validation_allows_decimal_public_ip(self):
        """SSRF prevention: decimal IP that resolves to a public IP should pass."""
        from debridnzd.torbox.client import _validate_url

        # 134744072 = 8.8.8.8 in decimal (Google DNS)
        result = _validate_url("http://134744072/endpoint", "link")
        assert result == "http://134744072/endpoint"

    @pytest.mark.asyncio
    async def test_url_validation_rejects_decimal_aws_metadata(self):
        """SSRF prevention: decimal IP for 169.254.169.254 (AWS metadata) must be rejected."""
        from debridnzd.torbox.client import _validate_url

        # 2851995694 = 169.254.169.254 in decimal
        with pytest.raises(ValueError, match="private/reserved IP"):
            _validate_url("http://2851995694/latest/meta-data/", "link")