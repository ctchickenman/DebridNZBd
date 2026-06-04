"""Database management for DebridNZBd.

Provides async SQLite connection management, schema creation, and migration
support. The database is a single file at `<admin_dir>/debridnzd.db` and
stores all persistent state: configuration, download queue, history,
categories, sorters, schedules, and warnings.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

# Current schema version — used to determine which migrations to run.
# Increment this when adding new migrations.
SCHEMA_VERSION = 2


class Database:
    """Async SQLite database manager for DebridNZBd.

    Handles connection lifecycle, schema initialization, and migrations.
    Usage::

        db = Database(Path("admin/debridnzd.db"))
        await db.initialize()
        # ... use db.conn for queries ...
        await db.close()

    The database file is created automatically if it doesn't exist.
    Migrations run on each startup to ensure the schema is up to date.
    """

    def __init__(self, db_path: Path) -> None:
        """Initialize the Database manager.

        Args:
            db_path: Absolute path to the SQLite database file.
                     Parent directories are created automatically.
        """
        self.db_path = db_path
        # The aiosqlite connection object — set during initialize()
        self.conn: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """Open the database connection, create parent directories, and run migrations.

        This must be called before any database operations. It:
        1. Creates parent directories if they don't exist
        2. Opens an aiosqlite connection with WAL mode for concurrency
        3. Enables foreign keys
        4. Runs any pending migrations
        """
        # Ensure the parent directory exists
        db_parent = self.db_path.parent
        if not db_parent.exists():
            logger.info("Creating database parent directory: %s", db_parent)
        db_parent.mkdir(parents=True, exist_ok=True)

        db_exists = self.db_path.exists()
        logger.info("Opening database at %s (%s)", self.db_path, "existing" if db_exists else "new")

        self.conn = await aiosqlite.connect(str(self.db_path))

        # Use WAL mode for better concurrent read performance — the state
        # sync poller reads frequently while API handlers may write.
        await self.conn.execute("PRAGMA journal_mode=WAL")

        # Enable foreign key enforcement so ON DELETE CASCADE works.
        await self.conn.execute("PRAGMA foreign_keys=ON")

        # Run migrations to create or update the schema
        await self._run_migrations()

        await self.conn.commit()
        logger.info("Database initialized successfully")

    async def close(self) -> None:
        """Close the database connection gracefully.

        Safe to call even if initialize() was never called or already closed.
        """
        if self.conn is not None:
            await self.conn.close()
            self.conn = None
            logger.info("Database connection closed")

    async def _run_migrations(self) -> None:
        """Run all pending database migrations.

        Migrations are tracked by the `schema_version` row in the `config`
        table. Each migration is idempotent — it checks for the existence of
        tables/columns before creating them, so it's safe to run on an
        already-migrated database.
        """
        # Ensure the config table exists first — it stores the schema version
        await self._ensure_config_table()

        current_version = await self._get_schema_version()
        logger.info("Current schema version: %d, target: %d", current_version, SCHEMA_VERSION)

        if current_version < SCHEMA_VERSION:
            # Run each migration in sequence
            for version in range(current_version + 1, SCHEMA_VERSION + 1):
                migration_func = getattr(self, f"_migration_{version:03d}", None)
                if migration_func is not None:
                    logger.info("Running migration %d...", version)
                    await migration_func()
                else:
                    logger.warning("No migration function for version %d", version)

            await self._set_schema_version(SCHEMA_VERSION)
            logger.info("Migrations complete. Schema version: %d", SCHEMA_VERSION)
        else:
            logger.info("Schema is up to date, no migrations needed")

    async def _ensure_config_table(self) -> None:
        """Create the config table if it doesn't exist.

        The config table must exist before we can check the schema version.
        It uses a composite primary key of (section, keyword) for efficient
        lookups like "get all settings in the 'misc' section".
        """
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS config (
                section  TEXT NOT NULL,
                keyword  TEXT NOT NULL,
                value    TEXT,
                PRIMARY KEY (section, keyword)
            )
        """)
        await self.conn.commit()

    async def _get_schema_version(self) -> int:
        """Read the current schema version from the config table.

        Returns 0 if no version has been recorded yet (fresh database).
        """
        cursor = await self.conn.execute(
            "SELECT value FROM config WHERE section = '_internal' AND keyword = 'schema_version'"
        )
        row = await cursor.fetchone()
        if row is not None and row[0] is not None:
            return int(row[0])
        return 0

    async def _set_schema_version(self, version: int) -> None:
        """Write the current schema version to the config table."""
        await self.conn.execute(
            "INSERT OR REPLACE INTO config (section, keyword, value) VALUES (?, ?, ?)",
            ("_internal", "schema_version", str(version)),
        )
        await self.conn.commit()

    # ------------------------------------------------------------------ #
    #  Migration 001: Initial schema                                      #
    # ------------------------------------------------------------------ #

    async def _migration_001(self) -> None:
        """Create the initial database schema.

        Creates all tables needed for DebridNZBd operation:
        - config: Key-value configuration store
        - jobs: Active download queue (maps SABnzbd nzo_ids to Torbox downloads)
        - history: Completed/failed download records
        - categories: Download categories with default settings
        - sorters: Custom file sorting rules
        - schedules: Cron-like scheduled tasks
        - warnings: Active warning messages shown in the UI
        """
        # --- Jobs table: active download queue ---
        # Each row represents one download that is either queued, downloading,
        # or otherwise active. When a download completes or fails, it moves
        # to the history table.
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                nzo_id          TEXT PRIMARY KEY,
                filename        TEXT NOT NULL,
                password        TEXT DEFAULT '',
                nzo_url         TEXT DEFAULT '',
                category        TEXT DEFAULT '*',
                script          TEXT DEFAULT 'Default',
                priority        INTEGER DEFAULT 0,
                pp              INTEGER DEFAULT -1,
                status          TEXT DEFAULT 'Queued',
                size            REAL DEFAULT 0,
                sizeleft        REAL DEFAULT 0,
                percentage      REAL DEFAULT 0,
                time_added      REAL NOT NULL,
                time_started    REAL,
                time_completed  REAL,
                avg_age         TEXT DEFAULT '',
                torbox_id       TEXT,
                torbox_type     TEXT,
                torbox_hash     TEXT,
                torbox_state    TEXT,
                cdn_link        TEXT,
                local_path      TEXT,
                position        INTEGER DEFAULT 0,
                labels          TEXT DEFAULT '[]',
                stage_log       TEXT DEFAULT '[]',
                fail_message    TEXT DEFAULT '',
                speed           REAL DEFAULT 0,
                download_time   REAL DEFAULT 0
            )
        """)

        # --- History table: completed/failed downloads ---
        # Archived jobs retain all the information SABnzbd clients expect
        # in the history response, including timing data and file paths.
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS history (
                nzo_id          TEXT PRIMARY KEY,
                name            TEXT NOT NULL,
                status          TEXT NOT NULL,
                size            REAL DEFAULT 0,
                category        TEXT DEFAULT '*',
                pp              TEXT DEFAULT '',
                storage         TEXT DEFAULT '',
                path            TEXT DEFAULT '',
                download_time    REAL DEFAULT 0,
                postproc_time    REAL DEFAULT 0,
                completed       REAL NOT NULL,
                time_added      REAL NOT NULL,
                duplicate_key   TEXT DEFAULT '',
                fail_message    TEXT DEFAULT '',
                stage_log       TEXT DEFAULT '[]',
                archive         INTEGER DEFAULT 0,
                torbox_id       TEXT,
                torbox_type     TEXT
            )
        """)

        # --- Categories table ---
        # Maps to SABnzbd's category system. The '*' category is the default
        # that applies when no category is specified.
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                name         TEXT PRIMARY KEY,
                priority     INTEGER DEFAULT 0,
                pp           INTEGER DEFAULT -1,
                script       TEXT DEFAULT '',
                dir          TEXT DEFAULT '',
                newzbin      TEXT DEFAULT '',
                order_index  INTEGER DEFAULT 0
            )
        """)

        # --- Sorters table ---
        # Custom file sorting rules for TV shows, movies, and date-based content.
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS sorters (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT NOT NULL,
                sort_string     TEXT NOT NULL,
                enabled         INTEGER DEFAULT 1,
                job_types       TEXT DEFAULT '',
                categories      TEXT DEFAULT '',
                min_filesize    INTEGER DEFAULT 0,
                multi_part_label TEXT DEFAULT '',
                order_index     INTEGER DEFAULT 0
            )
        """)

        # --- Schedules table ---
        # Time-based actions (pause/resume/speedlimit at specific times).
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS schedules (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                minute      INTEGER DEFAULT 0,
                hour        INTEGER DEFAULT 0,
                day_of_week TEXT DEFAULT '*',
                action      TEXT NOT NULL,
                argument    TEXT DEFAULT ''
            )
        """)

        # --- Warnings table ---
        # Active warning/error messages shown in the UI and /api?mode=warnings.
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS warnings (
                id    INTEGER PRIMARY KEY AUTOINCREMENT,
                text  TEXT NOT NULL,
                type  TEXT DEFAULT 'WARNING',
                time  REAL NOT NULL
            )
        """)

        # --- Indexes for fast lookups ---
        # These indexes support the most common query patterns:
        # - Finding active jobs by status (queue display)
        # - Finding jobs by Torbox ID (state sync poller)
        # - Finding history by status (history display)
        # - Finding recent history (dashboard stats)
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_torbox ON jobs(torbox_id, torbox_type)"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_position ON jobs(position)"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_history_status ON history(status)"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_history_completed ON history(completed)"
        )

        # Seed the default categories — these match SABnzbd's defaults
        # and provide a starting point for users.
        await self._seed_default_categories()

        await self.conn.commit()
        logger.info("Migration 001: Created initial schema (jobs, history, categories, sorters, schedules, warnings tables + indexes)")

    async def _seed_default_categories(self) -> None:
        """Insert default categories into the categories table.

        The '*' category is the SABnzbd default that applies when no
        category is specified. Additional categories for common content
        types provide sensible defaults for processing and folder paths.
        """
        defaults = [
            # name,      priority, pp,  script,    dir,        newzbin, order_index
            ("*",        0,        -1,  "Default", "",          "",      0),
            ("movies",   0,         3,  "Default", "movies",    "",      1),
            ("tv",       0,         2,  "Default", "tv",         "",      2),
            ("audio",    0,         1,  "Default", "audio",      "",      3),
            ("software", 0,         1,  "Default", "software",   "",      4),
        ]
        await self.conn.executemany(
            "INSERT OR IGNORE INTO categories (name, priority, pp, script, dir, newzbin, order_index) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            defaults,
        )

    # ------------------------------------------------------------------ #
    #  Migration 002: Add nzo_url to history table                        #
    # ------------------------------------------------------------------ #

    async def _migration_002(self) -> None:
        """Add nzo_url column to the history table.

        The nzo_url column stores the original download URL so that
        failed downloads can be retried by re-submitting the URL to
        Torbox. Without this column, retry cannot re-submit the download.
        """
        # Check if column already exists (idempotent migration)
        cursor = await self.conn.execute("PRAGMA table_info(history)")
        columns = [row[1] for row in await cursor.fetchall()]
        if "nzo_url" not in columns:
            await self.conn.execute(
                "ALTER TABLE history ADD COLUMN nzo_url TEXT DEFAULT ''"
            )
            await self.conn.commit()
            logger.info("Migration 002: Added nzo_url column to history table")
        else:
            logger.info("Migration 002: nzo_url column already exists, skipping")


# ------------------------------------------------------------------ #
#  Module-level database instance                                     #
# ------------------------------------------------------------------ #

# Global database instance — initialized by the app lifespan handler
# and used throughout the application via `from debridnzd.db.database import db`.
db: Database | None = None


async def init_database(db_path: Path) -> Database:
    """Create and initialize the global database instance.

    This is called once during application startup by the FastAPI lifespan
    handler. It creates the Database object, opens the connection, runs
    migrations, and stores it as the module-level `db` singleton.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        The initialized Database instance (also stored as `debridezd.db.database.db`).
    """
    global db
    db = Database(db_path)
    await db.initialize()
    return db


async def close_database() -> None:
    """Close the global database connection.

    Called during application shutdown by the FastAPI lifespan handler.
    """
    global db
    if db is not None:
        await db.close()
        db = None