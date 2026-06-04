"""Pydantic models for Torbox API responses.

These models define the structure of responses from the Torbox API endpoints.
They provide type-safe access to response fields and automatic validation.

The Torbox API returns JSON with a top-level structure of:
    {"success": true, "data": {...}, "detail": "..."}

When `success` is true, `data` contains the response payload.
When `success` is false, `detail` contains an error message.

Design notes:
- **Response models** (data received FROM the Torbox API) use `extra = 'ignore'`
  to gracefully handle new fields added by the Torbox API without breaking
  validation. Since we don't control the API, strict rejection of unknown
  fields would cause failures whenever Torbox adds a new field.
- **Request models** (data we SEND to the Torbox API) use `extra = 'forbid'`
  to catch bugs where we accidentally send unexpected fields.
- `progress` fields are constrained to 0.0-1.0 range.
- String fields have max_length constraints where appropriate.

Reference: https://api-docs.torbox.app
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ------------------------------------------------------------------ #
#  Base response model                                                 #
# ------------------------------------------------------------------ #

class TorboxResponse(BaseModel):
    """Base response from any Torbox API endpoint.

    All Torbox API responses include a `success` boolean and either
    a `data` payload (on success) or a `detail` error message (on failure).

    Uses extra='ignore' to be resilient against API changes that add
    new fields — unknown fields are silently dropped rather than causing
    validation errors.
    """
    model_config = ConfigDict(extra="ignore")

    success: bool = True
    detail: str = ""
    data: dict | list | str | int | None = None


# ------------------------------------------------------------------ #
#  User endpoints                                                      #
# ------------------------------------------------------------------ #

class TorboxUserData(BaseModel):
    """User account data from /user/me endpoint.

    Contains the user's plan, bandwidth usage, and subscription status.

    Uses extra='ignore' because the Torbox API may return additional
    fields not documented here — unknown fields are silently dropped.
    """
    model_config = ConfigDict(extra="ignore")

    id: int = 0
    email: str = Field(default="", max_length=256)
    plan: int = Field(default=0, ge=0, le=10)
    is_subscribed: bool = False
    premium_expires_at: str = Field(default="", max_length=64)
    total_downloaded: float = Field(default=0, ge=0)
    customer: str = Field(default="", max_length=128)


# ------------------------------------------------------------------ #
#  Usenet download models                                              #
# ------------------------------------------------------------------ #

class TorboxUsenetDownload(BaseModel):
    """A single usenet download from the mylist endpoint.

    Represents a usenet download submitted to Torbox, with its
    current state and progress information.
    """
    model_config = ConfigDict(extra="ignore")

    id: int = 0
    hash: str = Field(default="", max_length=128)
    status: str = Field(default="", max_length=64)
    created_at: str = Field(default="", max_length=64)
    files: list[dict] = Field(default_factory=list)
    progress: float = Field(default=0, ge=0, le=1)
    size: float = Field(default=0, ge=0)

    @field_validator("files", mode="before")
    @classmethod
    def coerce_none_files(cls, v: list[dict] | None) -> list[dict]:
        """Torbox API may return null for the files field instead of []."""
        return v if v is not None else []


# ------------------------------------------------------------------ #
#  Torrent download models                                             #
# ------------------------------------------------------------------ #

class TorboxTorrentDownload(BaseModel):
    """A single torrent download from the mylist endpoint.

    Represents a torrent download submitted to Torbox, with its
    current state and progress information.
    """
    model_config = ConfigDict(extra="ignore")

    id: int = 0
    hash: str = Field(default="", max_length=128)
    status: str = Field(default="", max_length=64)
    created_at: str = Field(default="", max_length=64)
    name: str = Field(default="", max_length=512)
    progress: float = Field(default=0, ge=0, le=1)
    size: float = Field(default=0, ge=0)
    seeders: int = Field(default=0, ge=0)
    files: list[dict] = Field(default_factory=list)

    @field_validator("files", mode="before")
    @classmethod
    def coerce_none_files(cls, v: list[dict] | None) -> list[dict]:
        """Torbox API may return null for the files field instead of []."""
        return v if v is not None else []


# ------------------------------------------------------------------ #
#  Web download models                                                 #
# ------------------------------------------------------------------ #

class TorboxWebDownload(BaseModel):
    """A single web download from the mylist endpoint.

    Represents a direct-link web download submitted to Torbox.
    """
    model_config = ConfigDict(extra="ignore")

    id: int = 0
    hash: str = Field(default="", max_length=128)
    status: str = Field(default="", max_length=64)
    created_at: str = Field(default="", max_length=64)
    name: str = Field(default="", max_length=512)
    progress: float = Field(default=0, ge=0, le=1)
    size: float = Field(default=0, ge=0)
    files: list[dict] = Field(default_factory=list)

    @field_validator("files", mode="before")
    @classmethod
    def coerce_none_files(cls, v: list[dict] | None) -> list[dict]:
        """Torbox API may return null for the files field instead of []."""
        return v if v is not None else []


# ------------------------------------------------------------------ #
#  CDN download link models                                            #
# ------------------------------------------------------------------ #

class TorboxDownloadLink(BaseModel):
    """A CDN download link from the requestdl endpoints.

    Contains the actual download URL for a completed file on
    Torbox's CDN. Links expire after 3 hours once requested.
    """
    model_config = ConfigDict(extra="ignore")

    url: str = Field(default="", max_length=4096)


# ------------------------------------------------------------------ #
#  Cached availability models                                          #
# ------------------------------------------------------------------ #

class TorboxCachedItem(BaseModel):
    """Result of a cache check for a single hash.

    Indicates whether a given hash (NZB, torrent, or web URL)
    is already cached on Torbox's servers and available for
    immediate download.
    """
    model_config = ConfigDict(extra="ignore")

    hash: str = Field(default="", max_length=128)
    cached: bool = False


# ------------------------------------------------------------------ #
#  Queued download models                                              #
# ------------------------------------------------------------------ #

class TorboxQueuedDownload(BaseModel):
    """A queued download from the getqueued endpoint.

    Represents a download that has been submitted but not yet
    started on Torbox's servers.
    """
    model_config = ConfigDict(extra="ignore")

    id: int = 0
    type: str = Field(default="", max_length=32)
    hash: str = Field(default="", max_length=128)
    created_at: str = Field(default="", max_length=64)

    @field_validator("type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        """Validate that type is a recognized download type.

        Accepts known types and logs a warning for unknown types
        rather than crashing, since the API may add new types.
        """
        valid_types = {"torrent", "usenet", "webdl", ""}
        if v not in valid_types:
            import logging
            logging.getLogger(__name__).warning(
                "Unknown download type: %r — API may have added new types", v
            )
        return v


# ------------------------------------------------------------------ #
#  Hoster list model                                                   #
# ------------------------------------------------------------------ #

class TorboxHoster(BaseModel):
    """A supported web download hoster.

    From the /webdl/hosters endpoint — lists all hosters that
    Torbox supports for direct-link web downloads.
    """
    model_config = ConfigDict(extra="ignore")

    name: str = Field(default="", max_length=256)
    domains: list[str] = Field(default_factory=list)
    url: str = Field(default="", max_length=2048)
    icon: str = Field(default="", max_length=2048)
    status: str = Field(default="", max_length=32)
    type: str = Field(default="", max_length=32)
    note: str = Field(default="", max_length=1024)
    daily_link_limit: int = Field(default=0, ge=0)
    daily_link_used: int = Field(default=0, ge=0)
    daily_bandwidth_limit: float = Field(default=0, ge=0)
    daily_bandwidth_used: float = Field(default=0, ge=0)


# ------------------------------------------------------------------ #
#  Control operation models                                            #
# ------------------------------------------------------------------ #

class TorboxControlOperation(BaseModel):
    """Request body for control operations (pause, resume, delete).

    The operation field specifies what action to take.
    """
    model_config = ConfigDict(extra="forbid")

    id: int = 0
    operation: str = Field(default="", max_length=32)


class TorboxCreateUsenetRequest(BaseModel):
    """Request body for creating a usenet download.

    Either `link` or a file upload is required.
    """
    model_config = ConfigDict(extra="forbid")

    link: str = Field(default="", max_length=2048)  # URL to NZB file
    post_processing: int = Field(default=-1, ge=-1, le=3)
    # -1=default, 0=none, 1=repair, 2=repair+unpack, 3=repair+unpack+delete


class TorboxCreateTorrentRequest(BaseModel):
    """Request body for creating a torrent download.

    Either `magnet` or a .torrent file upload is required.
    """
    model_config = ConfigDict(extra="forbid")

    magnet: str = Field(default="", max_length=4096)  # Magnet link
    allow_zip: bool = False  # Allow zip download
    as_queued: bool = False  # Add as queued instead of starting immediately
    seed: int = Field(default=0, ge=0)  # Seeding time in seconds


class TorboxCreateWebDownloadRequest(BaseModel):
    """Request body for creating a web download.

    The `link` field contains the direct URL to download.
    """
    model_config = ConfigDict(extra="forbid")

    link: str = Field(default="", max_length=2048)  # Direct URL