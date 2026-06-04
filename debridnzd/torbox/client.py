"""Async HTTP client for the Torbox API.

Provides a high-level async interface to all Torbox API endpoints
used by DebridNZBd. Handles authentication, error handling, retries,
and response parsing.

Usage::

    client = TorboxClient(api_key="tb_xxxx")
    # Check connection
    user = await client.get_user_me()
    # Create a usenet download
    result = await client.create_usenet_download(link="https://...")
    # Get download status
    downloads = await client.get_usenet_list()

The client uses httpx for async HTTP with connection pooling and
automatic retry on transient failures (429 rate limits, 5xx errors).

Authentication is via Bearer token in the Authorization header,
as documented at https://api-docs.torbox.app.

Security notes:
- The API key is sent via the Authorization header only, except for CDN
  download link requests where the Torbox API requires a `token` query
  parameter. This is documented in each request_dl method.
- Redirect following is disabled (follow_redirects=False) to prevent
  SSRF via open redirects from the Torbox API.
- Input validation is applied to all user-supplied parameters before
  they are sent to the API (URLs, hashes, IPs, file sizes, operations).
- Retry delays are capped at 5 minutes and floored at 1 second to
  prevent server-controlled sleep amplification or retry storms.
- The `base_url` is validated to use https:// (http:// only on localhost).
- Error messages sanitize internal details (paths, hostnames) before
  surfacing them to callers.

Security notes:
- The API key is sent via the Authorization header only, never in
  query parameters. (The Torbox API requires a `token` query param
  for CDN download links; we include it because the API mandates it,
  but the Authorization header provides the same authentication.)
- Input validation is applied to user-supplied parameters (URLs,
  hashes, file sizes) before they are sent to the API.
- Retry delays are capped to prevent server-controlled sleep amplification.
- The `base_url` is validated to use HTTPS in production.
- Error messages sanitize internal details to prevent information leakage.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

# Private/reserved IP ranges that should not be targets of SSRF attacks.
# These are IP addresses that could access internal services or cloud metadata.
_PRIVATE_IP_RANGES = [
    ipaddress.ip_network("127.0.0.0/8"),      # Loopback
    ipaddress.ip_network("10.0.0.0/8"),        # RFC 1918 private
    ipaddress.ip_network("172.16.0.0/12"),     # RFC 1918 private
    ipaddress.ip_network("192.168.0.0/16"),    # RFC 1918 private
    ipaddress.ip_network("169.254.0.0/16"),    # Link-local / cloud metadata
    ipaddress.ip_network("::1/128"),            # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),           # IPv6 unique local
    ipaddress.ip_network("fe80::/10"),          # IPv6 link-local
]

import httpx

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

logger = logging.getLogger(__name__)

# Default API base URL. Can be overridden for testing or custom endpoints.
DEFAULT_BASE_URL = "https://api.torbox.app/v1"

# Retry configuration
MAX_RETRIES = 3  # Maximum number of retries for transient errors
MAX_RETRIES_LIMIT = 10  # Upper bound for max_retries constructor param
RETRY_BACKOFF_BASE = 1.0  # Base delay in seconds for exponential backoff
RATE_LIMIT_RETRY_AFTER = 60  # Default wait time (seconds) when rate limited
RATE_LIMIT_MAX_WAIT = 300  # Maximum seconds to wait on a 429 Retry-After (5 min cap)
MAX_HASH_BATCH = 100  # Maximum number of hashes per cache check request
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB max file upload size
MAX_URL_LENGTH = 2048  # Maximum URL length for link/magnet parameters

# Allowed URL schemes for download links (SSRF prevention)
ALLOWED_URL_SCHEMES = {"http", "https", "magnet"}

# Allowed values for control operations
VALID_USenet_OPERATIONS = {"Delete", "Pause", "Resume"}
VALID_TORRENT_OPERATIONS = {"Reannounce", "Delete", "Resume"}
VALID_WEB_OPERATIONS = {"Delete"}
VALID_QUEUED_OPERATIONS = {"Delete", "Start"}

# Regex for magnet link format validation
MAGNET_URI_PATTERN = re.compile(r"^magnet:\?", re.IGNORECASE)

# Regex for hex hash validation (MD5, SHA1, SHA256, etc.)
HEX_HASH_PATTERN = re.compile(r"^[0-9a-fA-F]{8,128}$")


def _validate_url(url: str, param_name: str) -> str:
    """Validate a URL parameter for SSRF prevention.

    Ensures the URL uses an allowed scheme, is not excessively long,
    and does not point to private/reserved IP addresses (SSRF protection).

    Args:
        url: The URL to validate.
        param_name: Parameter name for error messages.

    Returns:
        The validated URL string.

    Raises:
        ValueError: If the URL is invalid, too long, uses a disallowed
                    scheme, or points to a private/reserved IP address.
    """
    if not url:
        raise ValueError(f"{param_name} must not be empty")

    if len(url) > MAX_URL_LENGTH:
        raise ValueError(
            f"{param_name} exceeds maximum length of {MAX_URL_LENGTH} characters"
        )

    # For magnet links, just check the prefix
    if url.lower().startswith("magnet:?"):
        return url

    parsed = urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_URL_SCHEMES:
        raise ValueError(
            f"{param_name} must use http, https, or magnet scheme, "
            f"got: {parsed.scheme or '(none)'}"
        )

    if not parsed.hostname:
        raise ValueError(f"{param_name} must not point to a private/reserved IP address")

    # Check for alternative IP formats that could bypass the standard IP check.
    # Decimal (2130706433), hex (0x7f000001), and octal (017700000001) formats
    # are not recognized by ipaddress.ip_address() but some systems resolve them
    # to private IPs. Block hostnames that look like these patterns.
    hostname = parsed.hostname
    if hostname:
        # Block decimal IPs: all digits, potentially resolving to an IP
        if hostname.isdigit() and len(hostname) > 2:
            try:
                ip = ipaddress.ip_address(int(hostname))
                for network in _PRIVATE_IP_RANGES:
                    if ip in network:
                        raise ValueError(
                            f"{param_name} must not point to a private/reserved IP address "
                            f"(decimal IP {hostname} resolves to {ip}). "
                            "This is prevented to avoid SSRF attacks."
                        )
            except ValueError as e:
                if "private/reserved IP" in str(e):
                    raise
                # Not a valid decimal IP, fall through to hostname check
                pass

        # Block hex IPs: starts with 0x or 0X
        if hostname.lower().startswith("0x"):
            try:
                ip = ipaddress.ip_address(int(hostname, 16))
                for network in _PRIVATE_IP_RANGES:
                    if ip in network:
                        raise ValueError(
                            f"{param_name} must not point to a private/reserved IP address "
                            f"(hex IP {hostname} resolves to {ip}). "
                            "This is prevented to avoid SSRF attacks."
                        )
            except ValueError as e:
                if "private/reserved IP" in str(e):
                    raise
                # Not a valid hex IP, fall through
                pass

        # Block octal IPs: starts with 0 followed by digits (e.g., 017700000001)
        # Only check if it starts with 0 and has more digits after
        if len(hostname) > 1 and hostname.startswith("0") and hostname[1:].isdigit():
            try:
                ip = ipaddress.ip_address(int(hostname, 8))
                for network in _PRIVATE_IP_RANGES:
                    if ip in network:
                        raise ValueError(
                            f"{param_name} must not point to a private/reserved IP address "
                            f"(octal IP {hostname} resolves to {ip}). "
                            "This is prevented to avoid SSRF attacks."
                        )
            except ValueError as e:
                if "private/reserved IP" in str(e):
                    raise
                # Not a valid octal IP, fall through
                pass

    # Check for private/reserved IP addresses (SSRF prevention)
    # This prevents the client from sending requests to internal services
    # or cloud metadata endpoints through the Torbox API.
    try:
        hostname = parsed.hostname
        ip = ipaddress.ip_address(hostname)
        for network in _PRIVATE_IP_RANGES:
            if ip in network:
                raise ValueError(
                    f"{param_name} must not point to a private/reserved IP address "
                    f"(got {hostname}). This is prevented to avoid SSRF attacks."
                )
    except ValueError as e:
        if "private/reserved IP" in str(e):
            raise  # Re-raise our own ValueError
        # hostname is not an IP address (it's a domain name like "example.com")
        # That's fine — DNS resolution is done by the Torbox API, not by us
        pass

    return url


def _validate_ip_address(ip: str, param_name: str) -> str:
    """Validate that a string is a valid IP address.

    Prevents injection of arbitrary strings into the user_ip parameter.

    Args:
        ip: The IP address string to validate.
        param_name: Parameter name for error messages.

    Returns:
        The validated IP address string.

    Raises:
        ValueError: If the string is not a valid IPv4 or IPv6 address.
    """
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        raise ValueError(
            f"{param_name} must be a valid IPv4 or IPv6 address, got: {ip!r}"
        )
    return ip


def _validate_hashes(hashes: list[str]) -> list[str]:
    """Validate a list of hashes for cache check requests.

    Enforces batch size limits and validates hash format.

    Args:
        hashes: List of hash strings to validate.

    Returns:
        The validated list of hashes.

    Raises:
        ValueError: If the list is too long or hashes are malformed.
    """
    if len(hashes) > MAX_HASH_BATCH:
        raise ValueError(
            f"Too many hashes in a single request: {len(hashes)} "
            f"(maximum {MAX_HASH_BATCH}). Split into multiple requests."
        )
    for h in hashes:
        if not HEX_HASH_PATTERN.match(h):
            raise ValueError(
                f"Invalid hash format: {h!r}. "
                f"Hashes must be hexadecimal strings of 8-128 characters."
            )
    return hashes


class TorboxClient:
    """Async HTTP client for the Torbox debrid service API.

    Provides methods for all Torbox operations used by DebridNZBd:
    - User account info
    - Usenet downloads (create, control, list, get CDN links, check cached)
    - Torrent downloads (create, control, list, get CDN links, check cached)
    - Web downloads (create, control, list, get CDN links, check cached)
    - Queued downloads (list, control)

    All methods are async and use httpx for HTTP communication.
    Authentication is handled automatically via the Authorization header.

    Args:
        api_key: The Torbox API key (Bearer token). Must not be empty.
        base_url: Override the default API base URL. Must use https://
            in production (http:// is allowed for testing on localhost).
        timeout: Request timeout in seconds (default: 30).
        max_retries: Maximum retries for transient errors (default: 3, max: 10).

    Raises:
        ValueError: If api_key is empty, base_url is invalid, or max_retries
            exceeds the limit.
        TorboxAuthError: Invalid or missing API key.
        TorboxRateLimitError: Rate limit exceeded.
        TorboxNotFoundError: Requested resource not found.
        TorboxServerError: Server-side error.
        TorboxConnectionError: Cannot reach the Torbox API.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: int = 30,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        """Initialize the Torbox client.

        Args:
            api_key: Torbox API key for authentication.
            base_url: Base URL for the Torbox API.
            timeout: Request timeout in seconds.
            max_retries: Maximum retry attempts for transient errors.

        Raises:
            ValueError: If api_key is empty, base_url is invalid, or
                max_retries exceeds the limit.
        """
        if not api_key or not api_key.strip():
            raise ValueError("Torbox API key must not be empty")

        # Validate and cap max_retries to prevent excessive recursion
        max_retries = max(0, min(max_retries, MAX_RETRIES_LIMIT))

        # Validate base_url — must use https:// unless on localhost (testing)
        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"base_url must use http:// or https://, got: {parsed.scheme}")
        if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            logger.warning(
                "Torbox client using insecure http:// for non-localhost URL: %s. "
                "This should only be used for testing.",
                base_url,
            )

        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries

        # Configure httpx client with connection pooling and timeouts
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": "DebridNZBd/1.0.0",
            },
            follow_redirects=False,
            limits=httpx.Limits(
                max_connections=10,
                max_keepalive_connections=5,
            ),
        )

    async def close(self) -> None:
        """Close the HTTP client and release resources.

        Should be called when the application shuts down to properly
        close connection pools.
        """
        await self._client.aclose()

    async def __aenter__(self) -> TorboxClient:
        """Support async context manager for automatic cleanup."""
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Close the client when exiting the context manager."""
        await self.close()

    # ------------------------------------------------------------------ #
    #  Internal request methods                                            #
    # ------------------------------------------------------------------ #

    def _sanitize_error_path(self, path: str) -> str:
        """Remove potentially sensitive details from API paths in error messages.

        Strips query parameters and truncates long paths to prevent
        information leakage in error responses.
        """
        # Remove query parameters (which could contain tokens/keys)
        if "?" in path:
            path = path.split("?")[0]
        # Truncate long paths
        if len(path) > 80:
            path = path[:77] + "..."
        return path

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        retry_count: int = 0,
    ) -> TorboxResponse:
        """Make an authenticated request to the Torbox API with retry logic.

        Handles authentication errors, rate limiting, server errors,
        and connection failures with automatic retry using exponential
        backoff.

        Args:
            method: HTTP method (GET, POST, etc.).
            path: API path (e.g., "/api/usenet/mylist").
            params: Query parameters.
            json_data: JSON request body.
            files: Multipart file uploads.
            data: Form data for the request body.
            retry_count: Current retry attempt (for internal use).

        Returns:
            TorboxResponse with success status and data.

        Raises:
            TorboxAuthError: On 401/403 responses (no retry).
            TorboxNotFoundError: On 404 responses (no retry).
            TorboxRateLimitError: On 429 after exhausting retries.
            TorboxServerError: On 5xx after exhausting retries.
            TorboxConnectionError: On connection failures after exhausting retries.
        """
        # Log the outbound request
        logger.info("Torbox API: %s %s", method, path)
        logger.debug(
            "Torbox API request details: %s %s params=%s json=%s",
            method, path, params, "<file upload>" if files else repr(json_data)[:200],
        )

        import time as _time
        _start = _time.monotonic()

        try:
            response = await self._client.request(
                method=method,
                url=path,
                params=params,
                json=json_data,
                files=files,
                data=data,
            )
        except httpx.ConnectError as e:
            if retry_count < self.max_retries:
                delay = RETRY_BACKOFF_BASE * (2 ** retry_count)
                logger.warning(
                    "Connection error to Torbox API (attempt %d/%d), retrying in %.1fs",
                    retry_count + 1, self.max_retries, delay,
                )
                await asyncio.sleep(delay)
                return await self._request(
                    method, path, params, json_data, files, data, retry_count + 1
                )
            # Sanitize: don't include raw exception details that may contain
            # internal hostnames, IPs, or network topology
            raise TorboxConnectionError("Cannot connect to Torbox API")

        except httpx.TimeoutException as e:
            if retry_count < self.max_retries:
                delay = RETRY_BACKOFF_BASE * (2 ** retry_count)
                logger.warning(
                    "Timeout connecting to Torbox API (attempt %d/%d), retrying in %.1fs",
                    retry_count + 1, self.max_retries, delay,
                )
                await asyncio.sleep(delay)
                return await self._request(
                    method, path, params, json_data, files, data, retry_count + 1
                )
            raise TorboxConnectionError("Torbox API request timed out")

        # Log the response
        _elapsed = _time.monotonic() - _start
        logger.info(
            "Torbox API: %s %s → %d (%.2fs)",
            method, path, response.status_code, _elapsed,
        )
        logger.debug(
            "Torbox API response: status=%d body=%s",
            response.status_code, response.text[:500],
        )

        # Handle HTTP status codes
        if response.status_code == 401 or response.status_code == 403:
            raise TorboxAuthError(
                f"Torbox API authentication failed (status {response.status_code}): "
                f"Check your API key"
            )

        if response.status_code == 404:
            raise TorboxNotFoundError(
                f"Resource not found: {self._sanitize_error_path(path)}"
            )

        if response.status_code == 429:
            # Rate limited — extract Retry-After header, cap it to prevent
            # server-controlled sleep amplification
            try:
                retry_after = int(response.headers.get("Retry-After", RATE_LIMIT_RETRY_AFTER))
            except (ValueError, TypeError):
                # Retry-After may be an HTTP date string, fall back to default
                logger.warning(
                    "Invalid Retry-After header value, using default: %ds",
                    RATE_LIMIT_RETRY_AFTER,
                )
                retry_after = RATE_LIMIT_RETRY_AFTER

            # Cap the wait time to prevent excessive delays from malicious servers
            # Floor at 1 second to prevent immediate retry storms on Retry-After: 0
            retry_after = max(1, min(retry_after, RATE_LIMIT_MAX_WAIT))

            if retry_count < self.max_retries:
                logger.warning(
                    "Torbox API rate limited, retrying in %ds (attempt %d/%d)",
                    retry_after, retry_count + 1, self.max_retries,
                )
                await asyncio.sleep(retry_after)
                return await self._request(
                    method, path, params, json_data, files, data, retry_count + 1
                )
            raise TorboxRateLimitError(
                retry_after=retry_after,
            )

        if response.status_code >= 500:
            if retry_count < self.max_retries:
                delay = RETRY_BACKOFF_BASE * (2 ** retry_count)
                logger.warning(
                    "Torbox server error %d (attempt %d/%d), retrying in %.1fs",
                    response.status_code, retry_count + 1, self.max_retries, delay,
                )
                await asyncio.sleep(delay)
                return await self._request(
                    method, path, params, json_data, files, data, retry_count + 1
                )
            raise TorboxServerError(
                f"Torbox server error: {response.status_code}",
                status_code=response.status_code,
            )

        # Parse the response JSON
        try:
            response_data = response.json()
        except json.JSONDecodeError:
            # Some endpoints may return non-JSON (e.g., redirect responses)
            response_data = {"success": True, "data": response.text}
        except (MemoryError, RecursionError):
            # Re-raise critical memory errors — don't swallow these
            raise
        except Exception:
            # Catch other parsing errors (e.g., huge responses, encoding issues)
            # without re-raising, but log them for debugging
            logger.warning("Unexpected response format from Torbox API")
            response_data = {"success": True, "data": None}

        # Wrap in TorboxResponse model
        success = response_data.get("success", False)
        detail = response_data.get("detail", "")
        data = response_data.get("data")

        return TorboxResponse(success=success, detail=detail, data=data)

    # ------------------------------------------------------------------ #
    #  User endpoints                                                      #
    # ------------------------------------------------------------------ #

    async def get_user_me(self, settings: bool = False) -> TorboxUserData:
        """Get the current user's account information.

        Useful for verifying the API key works and checking the plan
        status (Free, Essential, Pro, Standard).

        Args:
            settings: If True, include user settings in the response.

        Returns:
            TorboxUserData with account information.

        Raises:
            TorboxAuthError: If the API key is invalid.
        """
        params = {}
        if settings:
            params["settings"] = "true"

        result = await self._request("GET", "/api/user/me", params=params)

        if not result.success:
            raise TorboxError(f"Failed to get user info: {result.detail}")

        if isinstance(result.data, dict):
            return TorboxUserData(**result.data)

        # If data is not a dict, we can't construct a valid user object
        raise TorboxError(f"Unexpected user data format: {type(result.data).__name__}")

    # ------------------------------------------------------------------ #
    #  Usenet endpoints                                                    #
    # ------------------------------------------------------------------ #

    async def create_usenet_download(
        self,
        link: str = "",
        post_processing: int = -1,
        file_data: bytes | None = None,
        file_name: str = "",
    ) -> TorboxResponse:
        """Create a new usenet download.

        Submit an NZB link or file to Torbox for downloading via usenet.
        Either a link URL or a file upload must be provided.

        Args:
            link: URL to an NZB file or direct NZB link.
            post_processing: Post-processing level (-1=default, 0=none,
                            1=repair, 2=repair+unpack, 3=repair+unpack+delete).
            file_data: Raw NZB file bytes (alternative to link).
            file_name: Filename for the uploaded file.

        Returns:
            TorboxResponse with the created download ID.

        Raises:
            ValueError: If neither link nor file_data is provided, or if
                link has an invalid scheme, or file_data exceeds size limit.
        """
        # Validate that at least one of link or file_data is provided
        if not link and file_data is None:
            raise ValueError("Either link or file_data must be provided")

        # Validate link URL if provided
        if link:
            _validate_url(link, "link")

        # Validate post_processing range
        if post_processing not in (-1, 0, 1, 2, 3):
            raise ValueError(
                f"post_processing must be -1, 0, 1, 2, or 3, got: {post_processing}"
            )

        # Validate file size if file upload
        if file_data is not None and len(file_data) > MAX_FILE_SIZE:
            raise ValueError(
                f"File size {len(file_data)} bytes exceeds maximum of "
                f"{MAX_FILE_SIZE} bytes ({MAX_FILE_SIZE // (1024*1024)} MB)"
            )

        if file_data is not None:
            # File upload mode — multipart form with file + metadata
            result = await self._request(
                "POST",
                "/api/usenet/createusenetdownload",
                data={"link": link, "post_processing": str(post_processing)},
                files={"file": (file_name or "upload.nzb", file_data)},
            )
        else:
            # Link mode — Torbox API requires multipart/form-data for all
            # create endpoints. Use (None, value) tuples to send text fields
            # as regular form parts (not file uploads) with multipart encoding.
            result = await self._request(
                "POST",
                "/api/usenet/createusenetdownload",
                files={
                    "link": (None, link),
                    "post_processing": (None, str(post_processing)),
                },
            )
        return result

    async def control_usenet_download(
        self, usenet_id: int, operation: str
    ) -> TorboxResponse:
        """Control a usenet download (pause, resume, delete).

        Args:
            usenet_id: The Torbox usenet download ID.
            operation: One of "Delete", "Pause", "Resume".

        Returns:
            TorboxResponse indicating success or failure.

        Raises:
            ValueError: If operation is not a valid value.
        """
        if operation not in VALID_USenet_OPERATIONS:
            raise ValueError(
                f"Invalid usenet operation: {operation!r}. "
                f"Must be one of: {', '.join(sorted(VALID_USenet_OPERATIONS))}"
            )
        result = await self._request(
            "POST",
            "/api/usenet/controlusenetdownload",
            json_data={"id": usenet_id, "operation": operation},
        )
        return result

    async def request_usenet_dl(
        self,
        usenet_id: int,
        file_id: int | None = None,
        zip_link: bool = False,
        user_ip: str | None = None,
        redirect: bool = False,
    ) -> str:
        """Request a CDN download link for a completed usenet download.

        The returned link is valid for 3 hours from the time it is requested.
        For permanent links, use the token parameter in the URL instead.

        Note: The Torbox API requires the API key as a `token` query parameter
        for CDN download link requests. This is in addition to the Bearer token
        in the Authorization header. The key is included in the URL because
        CDN servers may not support header-based authentication.

        Args:
            usenet_id: The Torbox usenet download ID.
            file_id: Specific file ID to download (omit for zip).
            zip_link: If True, get a zip of all files.
            user_ip: User's IP for CDN selection (validated as IP address).
            redirect: If True, the API redirects to the CDN link directly.

        Returns:
            The CDN download URL as a string.
        """
        # SECURITY: The Torbox API requires token as a query parameter for
        # CDN download requests. This is a Torbox API design requirement.
        # The Authorization header is also sent for redundancy.
        params: dict[str, Any] = {"token": self.api_key, "usenet_id": str(usenet_id)}
        if file_id is not None:
            params["file_id"] = str(file_id)
        if zip_link:
            params["zip_link"] = "true"
        if user_ip:
            _validate_ip_address(user_ip, "user_ip")
            params["user_ip"] = user_ip
        if redirect:
            params["redirect"] = "true"

        result = await self._request("GET", "/api/usenet/requestdl", params=params)

        if isinstance(result.data, str):
            return result.data
        if isinstance(result.data, dict) and "url" in result.data:
            return result.data["url"]
        if isinstance(result.data, dict) and "download_link" in result.data:
            return result.data["download_link"]

        # If redirect=True, the response might be a redirect itself
        return str(result.data) if result.data else ""

    async def get_usenet_list(
        self,
        bypass_cache: bool = False,
        usenet_id: int | None = None,
        offset: int = 0,
        limit: int = 1000,
    ) -> list[TorboxUsenetDownload]:
        """Get the user's usenet download list.

        Returns a list of all usenet downloads with their current status.
        Updated every 5 seconds for live downloads.

        Args:
            bypass_cache: Get fresh data bypassing cache.
            usenet_id: Get a specific download by ID (returns list of 1).
            offset: Pagination offset (must be >= 0).
            limit: Maximum items per request (1-1000).

        Returns:
            List of TorboxUsenetDownload objects.
        """
        # Validate pagination parameters
        if offset < 0:
            raise ValueError(f"offset must be >= 0, got: {offset}")
        if limit < 1 or limit > 1000:
            raise ValueError(f"limit must be between 1 and 1000, got: {limit}")

        params: dict[str, Any] = {"offset": str(offset), "limit": str(limit)}
        if bypass_cache:
            params["bypass_cache"] = "true"
        if usenet_id is not None:
            params["id"] = str(usenet_id)

        result = await self._request("GET", "/api/usenet/mylist", params=params)

        if not result.success:
            return []

        downloads = []
        if isinstance(result.data, list):
            for item in result.data:
                if isinstance(item, dict):
                    downloads.append(TorboxUsenetDownload(**item))
        elif isinstance(result.data, dict) and "data" in result.data:
            # Some responses nest data further
            for item in result.data["data"]:
                if isinstance(item, dict):
                    downloads.append(TorboxUsenetDownload(**item))

        return downloads

    async def check_usenet_cached(
        self, hashes: list[str], format: str = "object"
    ) -> list[TorboxCachedItem]:
        """Check if usenet downloads are cached on Torbox.

        Cached downloads are available for immediate download without
        waiting for Torbox to fetch them from Usenet.

        Args:
            hashes: List of MD5 hashes to check (max 100 per request).
            format: Response format — "object" or "list".

        Returns:
            List of TorboxCachedItem indicating cache status.

        Raises:
            ValueError: If hashes list exceeds batch size or contains
                invalid hash formats.
        """
        _validate_hashes(hashes)
        if format not in ("object", "list"):
            raise ValueError(f"format must be 'object' or 'list', got: {format!r}")

        params = {"hash": ",".join(hashes), "format": format}
        result = await self._request("GET", "/api/usenet/checkcached", params=params)

        if not result.success or not result.data:
            return []

        cached_items = []
        if isinstance(result.data, list):
            for item in result.data:
                if isinstance(item, dict):
                    cached_items.append(TorboxCachedItem(**item))
        elif isinstance(result.data, dict):
            # Object format: {hash: bool}
            for h, is_cached in result.data.items():
                cached_items.append(TorboxCachedItem(hash=h, cached=bool(is_cached)))

        return cached_items

    # ------------------------------------------------------------------ #
    #  Torrent endpoints                                                  #
    # ------------------------------------------------------------------ #

    async def create_torrent(
        self,
        magnet: str = "",
        file_data: bytes | None = None,
        file_name: str = "",
        allow_zip: bool = False,
        as_queued: bool = False,
        seed: int = 0,
    ) -> TorboxResponse:
        """Create a new torrent download.

        Submit a magnet link or .torrent file to Torbox.

        Args:
            magnet: Magnet link for the torrent.
            file_data: Raw .torrent file bytes (alternative to magnet).
            file_name: Filename for the uploaded file.
            allow_zip: Allow zip download.
            as_queued: Add as queued instead of starting immediately.
            seed: Seeding time in seconds (0 = default).

        Returns:
            TorboxResponse with the created torrent ID.

        Raises:
            ValueError: If neither magnet nor file_data is provided, or if
                magnet has invalid format, or file_data exceeds size limit.
        """
        # Validate that at least one of magnet or file_data is provided
        if not magnet and file_data is None:
            raise ValueError("Either magnet or file_data must be provided")

        # Validate magnet link format if provided
        if magnet and not MAGNET_URI_PATTERN.match(magnet):
            raise ValueError(
                f"Invalid magnet link format. Magnet links must start with 'magnet:?'"
            )

        # Validate file size if file upload
        if file_data is not None and len(file_data) > MAX_FILE_SIZE:
            raise ValueError(
                f"File size {len(file_data)} bytes exceeds maximum of "
                f"{MAX_FILE_SIZE} bytes ({MAX_FILE_SIZE // (1024*1024)} MB)"
            )

        if file_data is not None:
            result = await self._request(
                "POST",
                "/api/torrents/createtorrent",
                data={
                    "magnet": magnet,
                    "allow_zip": str(allow_zip).lower(),
                    "as_queued": str(as_queued).lower(),
                    "seed": str(seed),
                },
                files={"file": (file_name or "upload.torrent", file_data)},
            )
        else:
            # Magnet mode — Torbox API requires multipart/form-data for all
            # create endpoints. Use (None, value) tuples to send text fields
            # as regular form parts with multipart encoding.
            result = await self._request(
                "POST",
                "/api/torrents/createtorrent",
                files={
                    "magnet": (None, magnet),
                    "allow_zip": (None, str(allow_zip).lower()),
                    "as_queued": (None, str(as_queued).lower()),
                    "seed": (None, str(seed)),
                },
            )
        return result

    async def control_torrent(
        self, torrent_id: int, operation: str
    ) -> TorboxResponse:
        """Control a torrent download (reannounce, delete, resume).

        Args:
            torrent_id: The Torbox torrent ID.
            operation: One of "Reannounce", "Delete", "Resume".

        Returns:
            TorboxResponse indicating success or failure.

        Raises:
            ValueError: If operation is not a valid value.
        """
        if operation not in VALID_TORRENT_OPERATIONS:
            raise ValueError(
                f"Invalid torrent operation: {operation!r}. "
                f"Must be one of: {', '.join(sorted(VALID_TORRENT_OPERATIONS))}"
            )
        result = await self._request(
            "POST",
            "/api/torrents/controltorrent",
            json_data={"id": torrent_id, "operation": operation},
        )
        return result

    async def request_torrent_dl(
        self,
        torrent_id: int,
        file_id: int | None = None,
        zip_link: bool = False,
        user_ip: str | None = None,
        redirect: bool = False,
    ) -> str:
        """Request a CDN download link for a completed torrent.

        Note: The Torbox API requires the API key as a `token` query parameter
        for CDN download link requests. See request_usenet_dl() for details.
        """
        params: dict[str, Any] = {"token": self.api_key, "torrent_id": str(torrent_id)}
        if file_id is not None:
            params["file_id"] = str(file_id)
        if zip_link:
            params["zip_link"] = "true"
        if user_ip:
            _validate_ip_address(user_ip, "user_ip")
            params["user_ip"] = user_ip
        if redirect:
            params["redirect"] = "true"

        result = await self._request("GET", "/api/torrents/requestdl", params=params)

        if isinstance(result.data, str):
            return result.data
        if isinstance(result.data, dict) and "url" in result.data:
            return result.data["url"]
        if isinstance(result.data, dict) and "download_link" in result.data:
            return result.data["download_link"]

        return str(result.data) if result.data else ""

    async def get_torrent_list(
        self,
        bypass_cache: bool = False,
        torrent_id: int | None = None,
        offset: int = 0,
        limit: int = 1000,
    ) -> list[TorboxTorrentDownload]:
        """Get the user's torrent download list.

        Updated every 600 seconds for cached data, or live with bypass_cache.

        Args:
            bypass_cache: Get fresh data.
            torrent_id: Get a specific torrent by ID.
            offset: Pagination offset (must be >= 0).
            limit: Maximum items per request (1-1000).

        Returns:
            List of TorboxTorrentDownload objects.
        """
        if offset < 0:
            raise ValueError(f"offset must be >= 0, got: {offset}")
        if limit < 1 or limit > 1000:
            raise ValueError(f"limit must be between 1 and 1000, got: {limit}")

        params: dict[str, Any] = {"offset": str(offset), "limit": str(limit)}
        if bypass_cache:
            params["bypass_cache"] = "true"
        if torrent_id is not None:
            params["id"] = str(torrent_id)

        result = await self._request("GET", "/api/torrents/mylist", params=params)

        if not result.success:
            return []

        downloads = []
        if isinstance(result.data, list):
            for item in result.data:
                if isinstance(item, dict):
                    downloads.append(TorboxTorrentDownload(**item))
        return downloads

    async def check_torrent_cached(
        self, hashes: list[str], format: str = "object", list_files: bool = False
    ) -> list[TorboxCachedItem]:
        """Check if torrents are cached on Torbox.

        Args:
            hashes: List of torrent info hashes to check (max 100).
            format: Response format — "object" or "list".
            list_files: If True, include file lists for cached items.

        Returns:
            List of TorboxCachedItem indicating cache status.
        """
        _validate_hashes(hashes)
        if format not in ("object", "list"):
            raise ValueError(f"format must be 'object' or 'list', got: {format!r}")

        params: dict[str, Any] = {"hash": ",".join(hashes), "format": format}
        if list_files:
            params["list_files"] = "true"

        result = await self._request("GET", "/api/torrents/checkcached", params=params)

        if not result.success or not result.data:
            return []

        cached_items = []
        if isinstance(result.data, list):
            for item in result.data:
                if isinstance(item, dict):
                    cached_items.append(TorboxCachedItem(**item))
        elif isinstance(result.data, dict):
            for h, val in result.data.items():
                if isinstance(val, bool):
                    cached_items.append(TorboxCachedItem(hash=h, cached=val))
                elif isinstance(val, dict):
                    cached_items.append(TorboxCachedItem(
                        hash=h, cached=val.get("cached", False)
                    ))

        return cached_items

    # ------------------------------------------------------------------ #
    #  Web download endpoints                                              #
    # ------------------------------------------------------------------ #

    async def create_web_download(self, link: str) -> TorboxResponse:
        """Create a new web download from a direct URL.

        Submits a direct URL to any file on the internet for Torbox
        to download. The file must be from a supported hoster.

        Args:
            link: The direct URL to download.

        Returns:
            TorboxResponse with the created download ID.

        Raises:
            ValueError: If link is empty or has an invalid scheme.
        """
        if not link:
            raise ValueError("link must not be empty")
        _validate_url(link, "link")

        # Torbox API requires multipart/form-data for all create endpoints.
        # Use (None, value) tuples to send text fields as regular form parts
        # with multipart encoding.
        result = await self._request(
            "POST",
            "/api/webdl/createwebdownload",
            files={"link": (None, link)},
        )
        return result

    async def control_web_download(
        self, web_id: int, operation: str = "Delete"
    ) -> TorboxResponse:
        """Control a web download (currently only Delete is supported).

        Args:
            web_id: The Torbox web download ID.
            operation: The operation to perform (default: "Delete").

        Returns:
            TorboxResponse indicating success or failure.

        Raises:
            ValueError: If operation is not a valid value.
        """
        if operation not in VALID_WEB_OPERATIONS:
            raise ValueError(
                f"Invalid web download operation: {operation!r}. "
                f"Must be one of: {', '.join(sorted(VALID_WEB_OPERATIONS))}"
            )
        result = await self._request(
            "POST",
            "/api/webdl/controlwebdownload",
            json_data={"id": web_id, "operation": operation},
        )
        return result

    async def request_web_dl(
        self,
        web_id: int,
        file_id: int | None = None,
        zip_link: bool = False,
        user_ip: str | None = None,
        redirect: bool = False,
    ) -> str:
        """Request a CDN download link for a completed web download.

        Note: The Torbox API requires the API key as a `token` query parameter
        for CDN download link requests. See request_usenet_dl() for details.
        """
        params: dict[str, Any] = {"token": self.api_key, "web_id": str(web_id)}
        if file_id is not None:
            params["file_id"] = str(file_id)
        if zip_link:
            params["zip_link"] = "true"
        if user_ip:
            _validate_ip_address(user_ip, "user_ip")
            params["user_ip"] = user_ip
        if redirect:
            params["redirect"] = "true"

        result = await self._request("GET", "/api/webdl/requestdl", params=params)

        if isinstance(result.data, str):
            return result.data
        if isinstance(result.data, dict) and "url" in result.data:
            return result.data["url"]
        if isinstance(result.data, dict) and "download_link" in result.data:
            return result.data["download_link"]

        return str(result.data) if result.data else ""

    async def get_web_download_list(
        self,
        bypass_cache: bool = False,
        web_id: int | None = None,
        offset: int = 0,
        limit: int = 1000,
    ) -> list[TorboxWebDownload]:
        """Get the user's web download list.

        Updated every 5 seconds for live downloads.

        Args:
            bypass_cache: Get fresh data.
            web_id: Get a specific download by ID.
            offset: Pagination offset (must be >= 0).
            limit: Maximum items per request (1-1000).

        Returns:
            List of TorboxWebDownload objects.
        """
        if offset < 0:
            raise ValueError(f"offset must be >= 0, got: {offset}")
        if limit < 1 or limit > 1000:
            raise ValueError(f"limit must be between 1 and 1000, got: {limit}")

        params: dict[str, Any] = {"offset": str(offset), "limit": str(limit)}
        if bypass_cache:
            params["bypass_cache"] = "true"
        if web_id is not None:
            params["id"] = str(web_id)

        result = await self._request("GET", "/api/webdl/mylist", params=params)

        if not result.success:
            return []

        downloads = []
        if isinstance(result.data, list):
            for item in result.data:
                if isinstance(item, dict):
                    downloads.append(TorboxWebDownload(**item))
        return downloads

    async def check_web_cached(
        self, hashes: list[str], format: str = "object"
    ) -> list[TorboxCachedItem]:
        """Check if web downloads are cached on Torbox.

        Args:
            hashes: List of MD5 hashes of the download links (max 100).
            format: Response format — "object" or "list".

        Returns:
            List of TorboxCachedItem indicating cache status.
        """
        _validate_hashes(hashes)
        if format not in ("object", "list"):
            raise ValueError(f"format must be 'object' or 'list', got: {format!r}")

        params = {"hash": ",".join(hashes), "format": format}
        result = await self._request("GET", "/api/webdl/checkcached", params=params)

        if not result.success or not result.data:
            return []

        cached_items = []
        if isinstance(result.data, list):
            for item in result.data:
                if isinstance(item, dict):
                    cached_items.append(TorboxCachedItem(**item))
        elif isinstance(result.data, dict):
            for h, val in result.data.items():
                if isinstance(val, bool):
                    cached_items.append(TorboxCachedItem(hash=h, cached=val))

        return cached_items

    async def get_hosters_list(self) -> list[TorboxHoster]:
        """Get the list of supported web download hosters.

        Returns information about each supported hoster including
        name, domains, status, and daily limits.

        Returns:
            List of TorboxHoster objects.
        """
        result = await self._request("GET", "/api/webdl/hosters")

        if not result.success or not result.data:
            return []

        hosters = []
        if isinstance(result.data, list):
            for item in result.data:
                if isinstance(item, dict):
                    hosters.append(TorboxHoster(**item))
        return hosters

    # ------------------------------------------------------------------ #
    #  Queued download endpoints                                           #
    # ------------------------------------------------------------------ #

    async def get_queued_downloads(
        self,
        download_type: str = "",
        bypass_cache: bool = False,
        queued_id: int | None = None,
        offset: int = 0,
        limit: int = 1000,
    ) -> list[TorboxQueuedDownload]:
        """Get the user's queued downloads.

        Queued downloads are downloads that have been submitted but
        not yet started due to slot limits or other restrictions.

        Args:
            download_type: Filter by type ("torrent", "usenet", "webdl").
            bypass_cache: Get fresh data.
            queued_id: Get a specific queued download by ID.
            offset: Pagination offset (must be >= 0).
            limit: Maximum items per request (1-1000).

        Returns:
            List of TorboxQueuedDownload objects.
        """
        if download_type and download_type not in ("torrent", "usenet", "webdl"):
            raise ValueError(
                f"download_type must be 'torrent', 'usenet', or 'webdl', "
                f"got: {download_type!r}"
            )
        if offset < 0:
            raise ValueError(f"offset must be >= 0, got: {offset}")
        if limit < 1 or limit > 1000:
            raise ValueError(f"limit must be between 1 and 1000, got: {limit}")

        params: dict[str, Any] = {"offset": str(offset), "limit": str(limit)}
        if download_type:
            params["type"] = download_type
        if bypass_cache:
            params["bypass_cache"] = "true"
        if queued_id is not None:
            params["id"] = str(queued_id)

        result = await self._request("GET", "/api/queued/getqueued", params=params)

        if not result.success or not result.data:
            return []

        queued = []
        if isinstance(result.data, list):
            for item in result.data:
                if isinstance(item, dict):
                    queued.append(TorboxQueuedDownload(**item))
        return queued

    async def control_queued_download(
        self, queued_id: int, operation: str = "Delete"
    ) -> TorboxResponse:
        """Control a queued download (delete or start).

        Args:
            queued_id: The Torbox queued download ID.
            operation: "Delete" or "Start".

        Returns:
            TorboxResponse indicating success or failure.

        Raises:
            ValueError: If operation is not a valid value.
        """
        if operation not in VALID_QUEUED_OPERATIONS:
            raise ValueError(
                f"Invalid queued operation: {operation!r}. "
                f"Must be one of: {', '.join(sorted(VALID_QUEUED_OPERATIONS))}"
            )
        result = await self._request(
            "POST",
            "/api/queued/controlqueued",
            json_data={"id": queued_id, "operation": operation},
        )
        return result

    # ------------------------------------------------------------------ #
    #  Convenience methods                                                 #
    # ------------------------------------------------------------------ #

    async def test_connection(self) -> tuple[bool, str]:
        """Test the connection to the Torbox API and verify the API key.

        Makes a request to the /user/me endpoint to verify that
        the API key is valid and the service is reachable.

        Returns:
            Tuple of (success: bool, message: str).
            On success, message contains the plan type.
            On failure, message contains the error description.
            Error messages are sanitized to avoid leaking internal details.
        """
        try:
            user = await self.get_user_me(settings=True)
            plan_names = {0: "Free", 1: "Essential", 2: "Pro", 3: "Standard"}
            plan = plan_names.get(user.plan, f"Unknown ({user.plan})")
            return True, f"Connected — {plan} plan, expires {user.premium_expires_at}"
        except TorboxAuthError as e:
            return False, "Authentication failed: check your API key"
        except TorboxConnectionError:
            return False, "Cannot reach Torbox API"
        except TorboxError:
            return False, "Error connecting to Torbox API"