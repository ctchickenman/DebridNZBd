# ------------------------------------------------------------------ #
#  DebridNZBd Docker Image                                            #
#                                                                      #
#  Multi-stage build:                                                  #
#  1. Builder — installs the package and dependencies into a venv      #
#  2. Runtime — copies the venv into a minimal image, runs as non-root #
#                                                                      #
#  Build:                                                              #
#    docker build -t debridnzbd:latest .                                #
#                                                                      #
#  Run:                                                                #
#    docker run -p 8080:8080 -v debridnzbd-data:/data debridnzbd:latest  #
#                                                                      #
#  All data (database, downloads, config, logs) is stored in /data.    #
#  The application uses relative paths from the working directory,     #
#  so setting WORKDIR to /data is sufficient.                         #
# ------------------------------------------------------------------ #

# ---- Stage 1: Builder ----
FROM python:3.12-slim-bookworm AS builder

WORKDIR /build

# Copy only what's needed for installation.
# pyproject.toml + README.md are needed by hatchling for metadata.
# debridnzbd/ is the package source.
COPY pyproject.toml README.md ./
COPY debridnzbd/ debridnzbd/

# Create a virtual environment and install the package with extras.
# [notifications] includes apprise for push notifications.
# guessit is installed separately for sorting support.
# --no-cache-dir reduces image size by not caching pip downloads.
# --disable-pip-version-check avoids unnecessary network requests.
RUN python -m venv /opt/debridnzbd && \
    /opt/debridnzbd/bin/pip install --no-cache-dir --disable-pip-version-check \
    ".[notifications]" && \
    /opt/debridnzbd/bin/pip install --no-cache-dir --disable-pip-version-check \
    guessit


# ---- Stage 2: Runtime ----
FROM python:3.12-slim-bookworm

# OCI image labels for container registries
LABEL org.opencontainers.image.title="DebridNZBd" \
      org.opencontainers.image.description="SABnzbd-compatible API server powered by Torbox" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.source="https://github.com/user/debridnzbd" \
      org.opencontainers.image.licenses="MIT"

# Create a non-root user for security.
# Fixed UID/GID 1000:1000 matches the typical first user on Linux systems,
# making bind-mount ownership straightforward for most users.
RUN groupadd --gid 1000 debridnzbd && \
    useradd --uid 1000 --gid debridnzbd --create-home debridnzbd

# Copy the virtual environment from the builder stage.
# This includes the application and all its dependencies, but NOT
# pip, setuptools, or other build artifacts.
COPY --from=builder /opt/debridnzbd /opt/debridnzbd

# Make the venv binaries available on PATH (uvicorn, python, etc.)
ENV PATH="/opt/debridnzbd/bin:${PATH}"

# Create the data directory owned by the non-root user.
# The application creates subdirectories (admin, downloads, logs, scripts)
# at startup relative to the working directory.
RUN mkdir -p /data && chown debridnzbd:debridnzbd /data

# Set the working directory to /data.
# The app uses relative paths (admin/, downloads/incomplete/, etc.)
# which resolve from CWD. This keeps all persistent data under /data.
WORKDIR /data

# Expose the default API port (SABnzbd convention).
EXPOSE 8080

# Declare /data as a volume for persistent storage.
# This documents the mount point and ensures data survives container recreation.
VOLUME ["/data"]

# Health check using the unauthenticated /api?mode=version endpoint.
# Uses Python's built-in urllib to avoid installing curl in the slim image.
# The start period allows the lifespan handler to initialize the database
# and seed config defaults before health checks begin.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api?mode=version')" || exit 1

# Switch to the non-root user for all subsequent operations.
USER debridnzbd

# Run uvicorn directly with --host 0.0.0.0 for container networking.
# Do NOT use the "debridnzbd" entry point — it hardcodes host=127.0.0.1
# which is unreachable from outside the container.
# The JSON array form ensures uvicorn runs as PID 1 for proper signal handling
# (SIGTERM for graceful shutdown).
# Users can override host/port/workers at docker run time:
#   docker run debridnzbd uvicorn debridnzbd.app:create_app --factory --host 0.0.0.0 --port 9090
CMD ["uvicorn", "debridnzbd.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]