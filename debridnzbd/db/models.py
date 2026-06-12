"""Pydantic models for SABnzbd API responses.

These models define the exact JSON response shapes that SABnzbd-compatible
clients expect. Every response follows the pattern:
    {"status": true/false, ...data...}

or on error:
    {"status": false, "error": "message"}

The field names and types match SABnzbd's API output exactly, including
using camelCase (which SABnzbd uses) rather than Python's snake_case.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_serializer
from typing import Any


# ------------------------------------------------------------------ #
#  Common response wrappers                                           #
# ------------------------------------------------------------------ #

class SABnzbdResponse(BaseModel):
    """Base response wrapper matching SABnzbd's standard format.

    All SABnzbd API responses include a "status" field. On success,
    status is true and additional fields are present. On error, status
    is false and an "error" field contains the message.
    """
    model_config = ConfigDict(extra="forbid")

    status: bool = True


class SABnzbdError(BaseModel):
    """Error response from SABnzbd API."""
    model_config = ConfigDict(extra="forbid")

    status: bool = False
    error: str


# ------------------------------------------------------------------ #
#  Queue response models                                               #
# ------------------------------------------------------------------ #

class QueueSlot(BaseModel):
    """A single download in the queue.

    Field names match SABnzbd's queue slot format exactly — clients
    like Sonarr and Radarr parse these specific field names.

    Security: The `password` field is always masked to "***" when
    serialized to JSON, regardless of the internal value.
    """
    model_config = ConfigDict(extra="forbid")

    status: str = "Queued"
    index: int = 0
    password: str = ""  # Masked in API responses; never return plaintext
    avg_age: str = ""
    time_added: float = 0
    script: str = "Default"
    direct_unpack: str = ""
    mb: float = 0
    mbleft: float = 0
    mbmissing: float = 0
    size: str = "0 B"
    sizeleft: str = "0 B"
    filename: str = ""
    labels: list[str] = Field(default_factory=list)
    priority: int = 0
    cat: str = "*"
    timeleft: str = ""
    percentage: str = "0"
    nzo_id: str = ""
    unpackopts: str = ""
    stalled: bool = False
    stall_duration: str = ""
    storage: str = ""
    path: str = ""

    @field_serializer("password")
    @classmethod
    def mask_password(cls, v: str) -> str:
        """Always mask the password field in serialized output."""
        return "***"


class QueueResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    """Full queue response matching SABnzbd's ?mode=queue output.

    Contains queue-level metadata (speed, paused state, etc.) plus
    an array of individual download slots.
    """
    status: bool = True
    speedlimit: str = "100"
    speedlimit_abs: str = "0"
    paused: bool = False
    noofslots: int = 0
    timeleft: str = "0:00:00"
    speed: str = "0"
    kbpersec: str = "0"
    size: str = "0 B"
    sizeleft: str = "0 B"
    mb: float = 0
    mbleft: float = 0
    slots: list[QueueSlot] = Field(default_factory=list)
    # Disk space info
    diskspace1: str = "0"
    diskspace2: str = "0"
    diskspacex1: str = "0"
    diskspacex2: str = "0"
    # Warnings and version
    have_warnings: str = "0"
    finishaction: str = ""
    paused_all: bool = False
    quota: str = ""
    left_quota: str = ""


# ------------------------------------------------------------------ #
#  History response models                                             #
# ------------------------------------------------------------------ #

class HistorySlot(BaseModel):
    """A completed or failed download in history.

    Matches SABnzbd's history slot format with timing data,
    file paths, and status information.

    Security: The `password` field is always masked to "***" when
    serialized to JSON, regardless of the internal value.
    """
    model_config = ConfigDict(extra="forbid")
    action_line: str = ""
    duplicate_key: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)
    fail_message: str = ""
    loaded: bool = False
    size: str = "0 B"
    category: str = "*"
    pp: str = ""
    retry: int = 0
    script: str = ""
    nzb_name: str = ""
    download_time: int = 0
    storage: str = ""
    has_rating: bool = False
    status: str = "Completed"
    script_line: str = ""
    completed: int = 0
    time_added: float = 0
    nzo_id: str = ""
    downloaded: int = 0
    report: str = ""
    password: str = ""  # Masked in API responses; never return plaintext
    path: str = ""
    postproc_time: int = 0
    name: str = ""
    url: str = ""
    md5sum: str = ""
    archive: bool = False
    bytes: int = 0
    url_info: str = ""
    stage_log: list[dict[str, Any]] = Field(default_factory=list)

    @field_serializer("password")
    @classmethod
    def mask_password(cls, v: str) -> str:
        """Always mask the password field in serialized output."""
        return "***"


class HistoryResponse(BaseModel):
    """Full history response matching SABnzbd's ?mode=history output."""
    model_config = ConfigDict(extra="forbid")
    status: bool = True
    noofslots: int = 0
    ppslots: int = 0
    day_size: str = "0 B"
    week_size: str = "0 B"
    month_size: str = "0 B"
    total_size: str = "0 B"
    last_history_update: float = 0
    slots: list[HistorySlot] = Field(default_factory=list)


# ------------------------------------------------------------------ #
#  Status response models                                              #
# ------------------------------------------------------------------ #

class ServerInfo(BaseModel):
    """Torbox connection info presented as a SABnzbd "server" entry.

    Since we're replacing NNTP servers with Torbox, we present
    Torbox as a single "server" in the status response. This
    keeps *arr clients happy that expect server stats.
    """
    model_config = ConfigDict(extra="forbid")
    servername: str = "Torbox"
    servertotalconn: int = 1
    serverssl: bool = True
    serveractiveconn: int = 1
    serveroptional: bool = False
    serveractive: bool = True
    servererror: str = ""
    serverpriority: int = 0
    serverbps: str = "0"
    serverconnections: list[dict[str, Any]] = Field(default_factory=list)


class StatusResponse(BaseModel):
    """Full status response matching SABnzbd's ?mode=fullstatus output.

    Contains system information, download stats, disk space,
    and server (Torbox) connection info.
    """
    model_config = ConfigDict(extra="forbid")
    status: bool = True
    localipv4: str = "127.0.0.1"
    ipv6: str = ""
    publicipv4: str = ""
    dnslookup: str = ""
    folders: list[str] = Field(default_factory=list)
    cpumodel: str = ""
    pystone: float = 0
    loadavg: list[float] = Field(default_factory=list)
    downloaddir: str = ""
    downloaddirspeed: str = ""
    completedir: str = ""
    completedirspeed: str = ""
    loglevel: str = "1"
    logfile: str = ""
    configfn: str = ""
    nt: bool = False
    darwin: bool = False
    confighelpuri: str = ""
    uptime: str = "0d 0h 0m"
    color_scheme: str = "default"
    webdir: str = ""
    active_lang: str = "en"
    restart_req: bool = False
    power_options: bool = False
    pp_pause_event: bool = False
    pid: int = 0
    weblogfile: str = ""
    new_release: str = ""
    new_rel_url: str = ""
    have_warnings: str = "0"
    warnings: list[str] = Field(default_factory=list)
    servers: list[ServerInfo] = Field(default_factory=list)
    # Speed and quota info
    speed: str = "0"
    kbpersec: str = "0"
    speedlimit: str = "100"
    speedlimit_abs: str = "0"
    paused: bool = False
    paused_all: bool = False
    quota: str = ""
    left_quota: str = ""
    # Disk space info (same fields as QueueResponse)
    diskspace1: str = "0"
    diskspace2: str = "0"
    diskspacex1: str = "0"
    diskspacex2: str = "0"


class VersionResponse(BaseModel):
    """Response for ?mode=version — no auth required."""
    model_config = ConfigDict(extra="forbid")

    status: bool = True
    version: str = "1.0.0"


class AuthResponse(BaseModel):
    """Response for ?mode=auth — reports available auth methods."""
    model_config = ConfigDict(extra="forbid")

    status: bool = True
    auth: str = "apikey"


# ------------------------------------------------------------------ #
#  Config response models                                              #
# ------------------------------------------------------------------ #

class ConfigSection(BaseModel):
    """A single configuration section with its settings."""
    # Dynamic keyword → value mapping; each section has different keys
    pass


class ConfigResponse(BaseModel):
    """Response for ?mode=get_config.

    SABnzbd returns nested objects for sections like servers, categories,
    and sorters, but flat keyword → value pairs for misc, switches, etc.

    Security: The config dict should be populated from ConfigStore.get_all()
    with redact_secrets=True to mask API keys and passwords.
    """
    model_config = ConfigDict(extra="forbid")

    status: bool = True
    config: dict[str, Any] = Field(default_factory=dict)


class CategoriesResponse(BaseModel):
    """Response for ?mode=get_cats — list of available categories."""
    model_config = ConfigDict(extra="forbid")

    status: bool = True
    categories: list[str] = Field(default_factory=list)


class ScriptsResponse(BaseModel):
    """Response for ?mode=get_scripts — list of available scripts."""
    model_config = ConfigDict(extra="forbid")

    status: bool = True
    scripts: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------ #
#  Warning models                                                      #
# ------------------------------------------------------------------ #

class WarningEntry(BaseModel):
    """A single warning entry."""
    model_config = ConfigDict(extra="forbid")

    text: str
    type: str = "WARNING"
    time: float


class WarningsResponse(BaseModel):
    """Response for ?mode=warnings — list of active warnings."""
    model_config = ConfigDict(extra="forbid")

    status: bool = True
    warnings: list[WarningEntry] = Field(default_factory=list)