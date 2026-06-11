#!/bin/sh
set -e

DATA_DIR="/data"

# If running as root, fix ownership of the data directory and drop privileges.
# Docker named volumes are typically created with root:root ownership,
# but the application runs as the debridnzbd user (UID 1000).
if [ "$(id -u)" = "0" ]; then
    # Fix ownership of the data directory for named Docker volumes.
    # On most filesystems (ext4, xfs, btrfs), chown works correctly.
    # On filesystems that don't support chown (NFS with root_squash, CIFS/SMB,
    # FAT32), chown may silently fail (exit code 0 but no ownership change).
    # We always run chmod as a safety net so the app can start regardless.
    chown -R debridnzbd:debridnzbd "$DATA_DIR" 2>/dev/null || true

    # Always ensure the data directory is accessible, even when chown silently
    # failed (NFS root_squash, CIFS/SMB, etc.). On normal filesystems where
    # chown succeeded, this is a no-op since the owner already has full access.
    # On restricted filesystems, this makes directories traversable and files
    # writable so the app (running as UID 1000) can function.
    chmod -R a+rwX "$DATA_DIR" 2>/dev/null || true
    chmod 777 "$DATA_DIR" 2>/dev/null || true

    # Drop privileges to the debridnzbd user.
    # Try each method in order of preference:
    #   gosu    — proper signal handling, exit code forwarding (installed in image)
    #   setpriv — util-linux, works in most Docker environments
    #   su      — POSIX fallback, available everywhere
    #
    # Some container runtimes (rootless Docker, Podman) block setuid/setgid.
    # If all privilege-drop methods fail, we continue as root rather than crash.
    DROPPED=0

    if command -v gosu >/dev/null 2>&1; then
        if gosu debridnzbd id >/dev/null 2>&1; then
            DROPPED=1
            exec gosu debridnzbd "$@"
        fi
    fi

    if [ "$DROPPED" = "0" ] && command -v setpriv >/dev/null 2>&1; then
        if setpriv --reuid=debridnzbd --regid=debridnzbd --init-groups id >/dev/null 2>&1; then
            DROPPED=1
            exec setpriv --reuid=debridnzbd --regid=debridnzbd --init-groups "$@"
        fi
    fi

    if [ "$DROPPED" = "0" ]; then
        # Last resort: su is available on all Linux systems
        if su -s /bin/sh debridnzbd -c "id" >/dev/null 2>&1; then
            DROPPED=1
            exec su -s /bin/sh debridnzbd -c "exec $*"
        fi
    fi

    if [ "$DROPPED" = "0" ]; then
        echo "WARNING: Could not drop privileges to debridnzbd user." >&2
        echo "WARNING: Running as root. This is not recommended for production." >&2
        echo "WARNING: To fix this, ensure your container runtime allows setuid/setgid." >&2
    fi
fi

# Not running as root (or couldn't drop privileges) — just exec the command
exec "$@"