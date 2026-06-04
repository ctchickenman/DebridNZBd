"""Disk space utilities.

Provides functions for checking available disk space on configured
download directories. All paths are validated against the configured
base directories to prevent path traversal attacks.
"""

import os
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# Allowed base directories for disk space checks.
# Populated during app startup from config (folders section).
# Only paths under these directories are accepted.
# Using a tuple for immutability — set_allowed_dirs replaces the entire
# reference atomically to avoid race conditions with concurrent reads.
_ALLOWED_BASE_DIRS: tuple[Path, ...] = ()


def set_allowed_dirs(dirs: list[str | Path]) -> None:
    """Set the list of allowed base directories for disk space checks.

    Must be called during app startup with the configured download
    directories. Only paths resolving under these directories will
    be accepted by get_disk_usage() and has_minimum_space().

    The list is stored as an immutable tuple to ensure thread-safe
    atomic replacement — concurrent reads during replacement will
    always see a consistent complete set.

    Args:
        dirs: List of directory paths that are allowed for disk checks.
    """
    global _ALLOWED_BASE_DIRS
    _ALLOWED_BASE_DIRS = tuple(Path(d).resolve() for d in dirs)
    logger.debug("Allowed disk space directories: %s", _ALLOWED_BASE_DIRS)


def _validate_path(path: str | Path) -> Path:
    """Validate that a path is within an allowed base directory.

    Resolves symlinks and prevents path traversal by ensuring the
    canonical path is under one of the configured base directories.

    Args:
        path: The path to validate.

    Returns:
        The resolved Path object.

    Raises:
        ValueError: If the path is outside all allowed base directories,
                    or if no allowed directories are configured.
    """
    if not _ALLOWED_BASE_DIRS:
        raise ValueError(
            "No allowed directories configured. Call set_allowed_dirs() first."
        )

    resolved = Path(path).resolve()

    # Check that the resolved path is under at least one allowed dir.
    # _ALLOWED_BASE_DIRS is a tuple (immutable), so iteration is safe
    # even if set_allowed_dirs() replaces it concurrently.
    for base_dir in _ALLOWED_BASE_DIRS:
        try:
            resolved.relative_to(base_dir)
            return resolved
        except ValueError:
            continue

    # Also allow the path if it exactly matches a base dir
    if resolved in _ALLOWED_BASE_DIRS:
        return resolved

    raise ValueError(
        f"Path '{path}' is outside allowed directories. "
        "Contact the administrator if you need access to this directory."
    )


def get_disk_usage(path: str | Path) -> dict:
    """Get disk usage for the filesystem containing path.

    The path must be within a configured allowed directory to prevent
    path traversal attacks. This function does NOT create directories;
    the caller is responsible for ensuring the directory exists.

    Args:
        path: Path to check disk usage for. Must be under an allowed dir.

    Returns:
        Dict with 'total', 'used', 'free' keys (bytes).

    Raises:
        ValueError: If the path is outside allowed directories.
        FileNotFoundError: If the path does not exist.
    """
    validated = _validate_path(path)

    if not validated.exists():
        raise FileNotFoundError(
            f"Path does not exist: {validated}. "
            "The directory must be created before checking disk usage."
        )

    if not validated.is_dir():
        # If it's a file, use the parent directory
        validated = validated.parent

    usage = shutil.disk_usage(str(validated))
    return {
        "total": usage.total,
        "used": usage.used,
        "free": usage.free,
    }


def has_minimum_space(path: str | Path, minimum_bytes: int) -> bool:
    """Check if filesystem containing path has at least minimum_bytes free.

    Args:
        path: Path to check. Must be under an allowed directory.
        minimum_bytes: Minimum required free bytes.

    Returns:
        True if the filesystem has at least minimum_bytes free.

    Raises:
        ValueError: If the path is outside allowed directories.
        FileNotFoundError: If the path does not exist.
    """
    usage = get_disk_usage(path)
    return usage["free"] >= minimum_bytes