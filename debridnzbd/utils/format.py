"""Shared formatting utilities for DebridNZBd.

Provides consistent size, speed, and time formatting across the API
and web UI. Extracted from web/routes.py to avoid duplication.
"""


def format_size(bytes_val: float) -> str:
    """Convert bytes to human-readable size string.

    Args:
        bytes_val: Size in bytes.

    Returns:
        Human-readable string like "1.5 GB" or "0 B".
    """
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(bytes_val) < 1024.0:
            return f"{bytes_val:.1f} {unit}" if unit != "B" else f"{int(bytes_val)} {unit}"
        bytes_val /= 1024.0
    return f"{bytes_val:.1f} PB"


def format_speed(bytes_per_sec: float) -> str:
    """Convert bytes per second to human-readable speed string.

    Args:
        bytes_per_sec: Speed in bytes per second.

    Returns:
        Human-readable string like "1.5 MB/s" or "0 B/s".
    """
    if bytes_per_sec <= 0:
        return "0 B/s"
    return format_size(bytes_per_sec) + "/s"


def format_timeleft(seconds: float) -> str:
    """Format a duration in seconds as 'H:MM:SS'.

    Args:
        seconds: Duration in seconds. Zero or negative returns '0:00:00'.

    Returns:
        String in 'H:MM:SS' format.
    """
    if seconds <= 0:
        return "0:00:00"
    s = int(seconds)
    h, remainder = divmod(s, 3600)
    m, s = divmod(remainder, 60)
    return f"{h}:{m:02d}:{s:02d}"


def format_uptime(start_time: float, now: float | None = None) -> str:
    """Format seconds since start as 'Xd Xh Xm' string.

    Args:
        start_time: Unix timestamp when the server started.
        now: Current Unix timestamp (defaults to time.time()).

    Returns:
        String like '1d 2h 30m' or '45m'.
    """
    import time as _time

    if now is None:
        now = _time.time()
    elapsed = int(now - start_time)
    days, remainder = divmod(elapsed, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts) if parts else "0m"


def format_timestamp(ts: float) -> str:
    """Format a Unix timestamp as 'YYYY-MM-DD HH:MM'.

    Args:
        ts: Unix timestamp.

    Returns:
        Formatted datetime string in UTC.
    """
    import datetime

    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime(
        "%Y-%m-%d %H:%M"
    )