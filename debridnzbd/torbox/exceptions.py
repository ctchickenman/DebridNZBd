"""Torbox API exception classes.

Provides a hierarchy of exceptions for handling Torbox API errors,
including authentication failures, rate limiting, and network issues.
These are used by the TorboxClient to report errors to the caller
in a structured way.
"""

from __future__ import annotations


class TorboxError(Exception):
    """Base exception for all Torbox API errors.

    All Torbox-related exceptions inherit from this class, allowing
    callers to catch all Torbox errors with a single except clause.
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class TorboxAuthError(TorboxError):
    """Authentication error — invalid or missing API key.

    Raised when the Torbox API returns a 401 or 403 status code,
    indicating that the provided API key is invalid, expired, or
    missing from the request.
    """

    def __init__(self, message: str = "Invalid or missing Torbox API key") -> None:
        super().__init__(message, status_code=401)


class TorboxRateLimitError(TorboxError):
    """Rate limit error — too many requests.

    Raised when the Torbox API returns a 429 status code.
    The client will automatically retry with exponential backoff
    before raising this exception if retries are exhausted.
    """

    def __init__(
        self,
        message: str = "Torbox API rate limit exceeded",
        retry_after: int | None = None,
    ) -> None:
        self.retry_after = retry_after
        super().__init__(message, status_code=429)


class TorboxNotFoundError(TorboxError):
    """Resource not found error.

    Raised when the Torbox API returns a 404 status code,
    indicating that the requested download, torrent, or other
    resource does not exist.
    """

    def __init__(self, message: str = "Resource not found on Torbox") -> None:
        super().__init__(message, status_code=404)


class TorboxServerError(TorboxError):
    """Server error — Torbox is experiencing issues.

    Raised when the Torbox API returns a 5xx status code.
    The client will retry these automatically before raising.
    """

    def __init__(self, message: str = "Torbox server error", status_code: int = 500) -> None:
        super().__init__(message, status_code=status_code)


class TorboxConnectionError(TorboxError):
    """Connection error — cannot reach the Torbox API.

    Raised when the HTTP client cannot establish a connection
    to the Torbox API server (DNS failure, network unreachable,
    connection refused, timeout, etc.).
    """

    def __init__(self, message: str = "Cannot connect to Torbox API") -> None:
        super().__init__(message)