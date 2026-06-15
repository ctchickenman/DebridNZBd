"""CDN file downloader for DebridNZBd.

Streams completed files from Torbox CDN links to local disk.
Respects the cdn_download_concurrency config to limit parallel downloads.
Files are written to the configured complete_dir with atomic writes
(temp file + rename) to avoid partial files on crash.

Uses aiofiles for async file I/O to avoid blocking the event loop
during large file downloads.
"""

import asyncio
import logging
import os
import re
from pathlib import Path
from urllib.parse import urlparse, unquote

import httpx

logger = logging.getLogger(__name__)


def cleanup_stale_temp_files(dest_dir: str) -> int:
    """Remove stale .part temp files left from interrupted downloads.

    Called at startup to clean up any temp files from crashes or
    interrupted downloads. Returns the number of files removed.
    """
    dest_path = Path(dest_dir).resolve()
    if not dest_path.is_dir():
        return 0

    count = 0
    for temp_file in dest_path.glob(".tmp_*.part"):
        try:
            temp_file.unlink()
            logger.info("CDN download: cleaned up stale temp file %s", temp_file)
            count += 1
        except OSError as e:
            logger.warning("CDN download: failed to remove stale temp file %s: %s", temp_file, e)
    return count

# Maximum time to wait for the CDN server to send each chunk (seconds)
_STREAM_TIMEOUT = 300.0

# Chunk size for streaming downloads (64 KiB)
_CHUNK_SIZE = 65536


def resolve_filename(url: str, content_disposition: str | None = None) -> str | None:
    """Extract a filename from a CDN response.

    Priority order:
    1. Content-Disposition header (filename*= or filename=)
    2. URL path component (last segment)
    3. None if no filename can be determined

    Returns a sanitized filename safe for the local filesystem,
    or None if no reasonable filename can be extracted.
    """
    # Try Content-Disposition first
    if content_disposition:
        # RFC 5987 filename*= (UTF-8 encoded)
        match = re.search(
            r"""filename\*=\s*(?:UTF-8|utf-8)?'[^']*'?([^;\s]+)""",
            content_disposition,
        )
        if match:
            name = unquote(match.group(1))
            return _sanitize_filename(name)

        # Regular filename= parameter
        match = re.search(
            r'filename\s*=\s*"(?P<name>[^"]+)"',
            content_disposition,
        )
        if match:
            return _sanitize_filename(match.group("name"))

        # Unquoted filename
        match = re.search(
            r"filename\s*=\s*(?P<name>[^;\s]+)",
            content_disposition,
        )
        if match:
            return _sanitize_filename(match.group("name"))

    # Try URL path
    parsed = urlparse(url)
    path = unquote(parsed.path)
    if path and path != "/":
        # Get the last path component
        name = path.rstrip("/").rsplit("/", 1)[-1]
        if name and "." in name:
            return _sanitize_filename(name)

    return None


def _sanitize_filename(name: str) -> str:
    """Remove or replace characters that are unsafe for local filesystems.

    Keeps alphanumeric, dots, dashes, underscores, spaces, and parentheses.
    Truncates to 255 characters (filesystem limit).
    Strips leading/trailing whitespace and dots.
    """
    # Replace unsafe characters with underscores
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    # Collapse multiple underscores/spaces
    sanitized = re.sub(r'[_\s]+', ' ', sanitized).strip()
    # Remove leading dots (hidden files) and trailing dots/spaces
    sanitized = sanitized.lstrip(".").rstrip(". ")
    # Truncate to filesystem limit
    if len(sanitized) > 255:
        # Preserve extension if possible
        base, ext = os.path.splitext(sanitized)
        if ext and len(ext) < 20:
            max_base = 255 - len(ext)
            sanitized = base[:max_base] + ext
        else:
            sanitized = sanitized[:255]
    return sanitized or "download"


async def download_file(
    url: str,
    dest_dir: str,
    filename: str | None = None,
    semaphore: "asyncio.Semaphore | None" = None,
) -> str | None:
    """Download a file from a CDN URL to local disk.

    Streams the response in chunks to avoid loading large files into memory.
    Writes to a temporary file first, then renames to the final path
    for atomic writes (no partial files on crash).

    Uses aiofiles for async file I/O to avoid blocking the event loop.

    Args:
        url: The CDN URL to download from.
        dest_dir: The destination directory (e.g. complete_dir).
        filename: The filename to use. If None, it will be determined
                  from the Content-Disposition header or URL path.
        semaphore: An optional asyncio.Semaphore to limit concurrent downloads.

    Returns:
        The local file path on success, or None on failure.
    """
    # Apply concurrency limit if provided
    if semaphore is not None:
        await semaphore.acquire()
    try:
        return await _do_download(url, dest_dir, filename)
    finally:
        if semaphore is not None:
            semaphore.release()


async def _do_download(url: str, dest_dir: str, filename: str | None) -> str | None:
    """Internal download implementation. See download_file() for docs."""
    try:
        import aiofiles
    except ImportError:
        # Fallback to sync I/O if aiofiles is not available
        return await _do_download_sync(url, dest_dir, filename)

    # Resolve to absolute path so the return value is usable from anywhere
    dest_path = Path(dest_dir).resolve()
    try:
        created = not dest_path.exists()
        dest_path.mkdir(parents=True, exist_ok=True)
        if created:
            logger.info("CDN download: created directory %s", dest_path)
    except OSError as e:
        logger.error("CDN download: failed to create directory %s: %s", dest_dir, e)
        return None

    # Use a dedicated client with redirects enabled and long timeouts
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(_STREAM_TIMEOUT, connect=30.0),
    ) as client:
        try:
            async with client.stream("GET", url) as response:
                if response.status_code >= 400:
                    logger.error(
                        "CDN download: HTTP %d for %s", response.status_code, url[:200]
                    )
                    return None

                # Determine filename
                if not filename:
                    content_disposition = response.headers.get("content-disposition")
                    filename = resolve_filename(url, content_disposition)

                if not filename:
                    filename = "download"

                # Check Content-Length for verification
                content_length = response.headers.get("content-length")
                expected_size = int(content_length) if content_length else None
                size_info = f" ({_format_size(expected_size)})" if expected_size else ""

                final_path = dest_path / filename

                # If the file already exists, skip the download and return
                # the existing path. This handles the race condition where
                # another process (provider_download or state_sync) already
                # downloaded the same file.
                if final_path.exists():
                    file_size = final_path.stat().st_size
                    logger.info(
                        "CDN download: file already exists at %s (%s), skipping download",
                        final_path, _format_size(file_size),
                    )
                    return str(final_path)

                # Avoid name collisions with different files — append counter
                base, ext = os.path.splitext(filename)
                counter = 1
                while final_path.exists():
                    filename = f"{base} ({counter}){ext}"
                    final_path = dest_path / filename
                    counter += 1
                    if counter > 100:
                        logger.error("CDN download: too many duplicate filenames for %s", filename)
                        return None

                # Write to temp file first for atomic rename
                temp_path = dest_path / f".tmp_{filename}.part"

                logger.info(
                    "CDN download: starting %s -> %s%s",
                    url[:100], final_path, size_info,
                )

                bytes_written = 0
                try:
                    async with aiofiles.open(temp_path, "wb") as f:
                        async for chunk in response.aiter_bytes(chunk_size=_CHUNK_SIZE):
                            await f.write(chunk)
                            bytes_written += len(chunk)
                except Exception:
                    # Clean up partial download
                    try:
                        await asyncio.to_thread(temp_path.unlink, missing_ok=True)
                        logger.info("CDN download: removed partial temp file %s", temp_path)
                    except OSError:
                        pass
                    raise

                # Verify download size matches Content-Length
                if expected_size is not None and bytes_written != expected_size:
                    logger.warning(
                        "CDN download: size mismatch for %s — expected %d bytes, got %d bytes",
                        final_path.name, expected_size, bytes_written,
                    )

                # Atomic rename from temp to final
                try:
                    await asyncio.to_thread(temp_path.rename, final_path)
                    logger.info("CDN download: renamed temp file %s → %s", temp_path.name, final_path.name)
                except OSError:
                    # On some systems, rename across filesystems fails.
                    # Fall back to copy+delete.
                    import shutil
                    logger.info(
                        "CDN download: rename failed, falling back to copy for %s",
                        final_path,
                    )
                    await asyncio.to_thread(shutil.copy2, str(temp_path), str(final_path))
                    logger.info("CDN download: copied temp file %s → %s", temp_path.name, final_path.name)
                    await asyncio.to_thread(temp_path.unlink, missing_ok=True)
                    logger.info("CDN download: removed temp file %s", temp_path)

                # Set world-readable permissions so *arr clients and other
                # services can access the downloaded file regardless of umask.
                try:
                    await asyncio.to_thread(os.chmod, final_path, 0o666)
                except OSError:
                    logger.debug("CDN download: could not chmod %s", final_path)

                logger.info(
                    "CDN download: completed %s (%s)",
                    final_path.name, _format_size(bytes_written),
                )
                return str(final_path)

        except httpx.TimeoutException:
            logger.error("CDN download: timeout downloading from %s", url[:200])
            return None
        except OSError as e:
            logger.error("CDN download: file I/O error: %s", e)
            return None
        except Exception:
            logger.exception("CDN download: unexpected error for %s", url[:200])
            return None


async def _do_download_sync(url: str, dest_dir: str, filename: str | None) -> str | None:
    """Fallback download using synchronous file I/O when aiofiles is unavailable.

    This blocks the event loop during writes, so it should only be used
    as a fallback when aiofiles cannot be installed.
    """
    # Resolve to absolute path so the return value is usable from anywhere
    dest_path = Path(dest_dir).resolve()
    try:
        created = not dest_path.exists()
        dest_path.mkdir(parents=True, exist_ok=True)
        if created:
            logger.info("CDN download: created directory %s", dest_path)
    except OSError as e:
        logger.error("CDN download: failed to create directory %s: %s", dest_dir, e)
        return None

    # Use a dedicated client with redirects enabled and long timeouts
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(_STREAM_TIMEOUT, connect=30.0),
    ) as client:
        try:
            async with client.stream("GET", url) as response:
                if response.status_code >= 400:
                    logger.error(
                        "CDN download: HTTP %d for %s", response.status_code, url[:200]
                    )
                    return None

                # Determine filename
                if not filename:
                    content_disposition = response.headers.get("content-disposition")
                    filename = resolve_filename(url, content_disposition)

                if not filename:
                    filename = "download"

                # Check Content-Length for verification
                content_length = response.headers.get("content-length")
                expected_size = int(content_length) if content_length else None
                size_info = f" ({_format_size(expected_size)})" if expected_size else ""

                final_path = dest_path / filename

                # If the file already exists, skip the download and return
                # the existing path. This handles the race condition where
                # another process already downloaded the same file.
                if final_path.exists():
                    file_size = final_path.stat().st_size
                    logger.info(
                        "CDN download: file already exists at %s (%s), skipping download",
                        final_path, _format_size(file_size),
                    )
                    return str(final_path)

                # Avoid name collisions with different files — append counter
                base, ext = os.path.splitext(filename)
                counter = 1
                while final_path.exists():
                    filename = f"{base} ({counter}){ext}"
                    final_path = dest_path / filename
                    counter += 1
                    if counter > 100:
                        logger.error("CDN download: too many duplicate filenames for %s", filename)
                        return None

                # Write to temp file first for atomic rename
                temp_path = dest_path / f".tmp_{filename}.part"

                logger.info(
                    "CDN download: starting %s -> %s%s",
                    url[:100], final_path, size_info,
                )

                bytes_written = 0
                try:
                    with open(temp_path, "wb") as f:
                        async for chunk in response.aiter_bytes(chunk_size=_CHUNK_SIZE):
                            f.write(chunk)
                            bytes_written += len(chunk)
                except Exception:
                    # Clean up partial download
                    try:
                        temp_path.unlink(missing_ok=True)
                        logger.info("CDN download: removed partial temp file %s", temp_path)
                    except OSError:
                        pass
                    raise

                # Verify download size matches Content-Length
                if expected_size is not None and bytes_written != expected_size:
                    logger.warning(
                        "CDN download: size mismatch for %s — expected %d bytes, got %d bytes",
                        final_path.name, expected_size, bytes_written,
                    )

                # Atomic rename from temp to final
                try:
                    temp_path.rename(final_path)
                    logger.info("CDN download: renamed temp file %s → %s", temp_path.name, final_path.name)
                except OSError:
                    # On some systems, rename across filesystems fails.
                    # Fall back to copy+delete.
                    import shutil
                    logger.info(
                        "CDN download: rename failed, falling back to copy for %s",
                        final_path,
                    )
                    shutil.copy2(str(temp_path), str(final_path))
                    logger.info("CDN download: copied temp file %s → %s", temp_path.name, final_path.name)
                    temp_path.unlink(missing_ok=True)
                    logger.info("CDN download: removed temp file %s", temp_path)

                # Set world-readable permissions so *arr clients and other
                # services can access the downloaded file regardless of umask.
                try:
                    os.chmod(final_path, 0o666)
                except OSError:
                    logger.debug("CDN download: could not chmod %s", final_path)

                logger.info(
                    "CDN download: completed %s (%s)",
                    final_path.name, _format_size(bytes_written),
                )
                return str(final_path)

        except httpx.TimeoutException:
            logger.error("CDN download: timeout downloading from %s", url[:200])
            return None
        except OSError as e:
            logger.error("CDN download: file I/O error: %s", e)
            return None
        except Exception:
            logger.exception("CDN download: unexpected error for %s", url[:200])
            return None


async def move_to_category_dir(
    local_path: str,
    category: str,
    config: object,
) -> str | None:
    """Move a downloaded file from the incomplete dir to the category-specific complete dir.

    Resolves the final destination based on the category's ``dir`` field:
    - Default category (``*``) or empty dir → ``complete_dir``
    - Named category with a dir → ``complete_dir / dir``

    Creates the target directory if needed. If the file already exists at
    the destination, the source file is removed and the existing path is
    returned. Handles cross-filesystem moves via ``shutil.move``.

    Args:
        local_path: The current path of the downloaded file (in incomplete dir).
        category: The job's category name (e.g. ``"*"``, ``"movies"``, ``"tv"``).
        config: The ConfigStore instance (used to read folder paths and look up
                category dirs).

    Returns:
        The final path after the move, or None on failure.
    """
    import shutil

    from pathlib import Path

    # Read the complete dir from config
    complete_dir = await config.get("folders", "complete_dir", "downloads/complete")

    # Look up the category's dir field from the database
    category_dir = ""
    db = getattr(config, "db", None) if config else None
    if db and db.conn and category and category != "*":
        try:
            cursor = await db.conn.execute(
                "SELECT dir FROM categories WHERE name = ?", (category,)
            )
            row = await cursor.fetchone()
            if row and row[0]:
                category_dir = row[0]
        except Exception:
            logger.warning(
                "CDN download: failed to look up category dir for '%s', "
                "using complete_dir",
                category,
                exc_info=True,
            )

    # Resolve final destination directory
    if category_dir:
        final_dir = Path(complete_dir) / category_dir
    else:
        final_dir = Path(complete_dir)

    # Path traversal check: ensure the resolved final directory is within
    # the configured complete directory. A malicious category_dir like
    # "../../etc" would escape the download directory.
    try:
        final_dir_resolved = final_dir.resolve()
        complete_dir_resolved = Path(complete_dir).resolve()
        final_dir_resolved.relative_to(complete_dir_resolved)
    except ValueError:
        logger.error(
            "CDN download: category dir '%s' escapes complete_dir '%s'",
            category_dir, complete_dir,
        )
        # Fall back to the default complete directory
        final_dir = Path(complete_dir)

    # Create the target directory if it doesn't exist
    try:
        created = not final_dir.exists()
        final_dir.mkdir(parents=True, exist_ok=True)
        if created:
            logger.info("CDN download: created directory %s", final_dir)
    except OSError as e:
        logger.error("CDN download: failed to create directory %s: %s", final_dir, e)
        return None

    # Resolve the final file path
    source = Path(local_path)
    final_path = final_dir / source.name

    # If the file already exists at the destination, remove the source and
    # return the existing path. Also ensure the existing file has
    # world-readable permissions.
    if final_path.exists():
        logger.info(
            "CDN download: file already exists at %s, removing source %s",
            final_path, source,
        )
        try:
            await asyncio.to_thread(source.unlink, missing_ok=True)
        except OSError:
            logger.warning("CDN download: failed to remove source file %s", source)
        try:
            await asyncio.to_thread(os.chmod, final_path, 0o666)
        except OSError:
            logger.debug("CDN download: could not chmod %s", final_path)
        return str(final_path)

    # Move the file (handles cross-filesystem moves)
    try:
        await asyncio.to_thread(shutil.move, str(source), str(final_path))
    except Exception:
        logger.exception("CDN download: failed to move %s to %s", source, final_path)
        return None

    # Ensure world-readable permissions after the move (shutil.move
    # preserves source permissions, which may be restrictive if umask
    # was applied). This ensures *arr clients and other services can
    # always read/write the file.
    try:
        await asyncio.to_thread(os.chmod, final_path, 0o666)
    except OSError:
        logger.debug("CDN download: could not chmod %s", final_path)
        return None

    logger.info("CDN download: moved %s → %s", source.name, final_path)
    return str(final_path)


def _format_size(size_bytes: int) -> str:
    """Format a byte count as a human-readable size string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"