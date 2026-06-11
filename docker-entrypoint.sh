#!/bin/sh
set -e

# Fix ownership of the data directory.
# Docker named volumes are typically created with root:root ownership,
# but the application runs as the debridnzbd user (UID 1000).
# This ensures the application can create and access files inside /data.
chown -R debridnzbd:debridnzbd /data 2>/dev/null || true

# Drop privileges and exec the command.
# gosu switches from root to the debridnzbd user before running the app.
exec gosu debridnzbd "$@"