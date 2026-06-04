"""Configuration store for DebridNZBd.

Manages all application configuration stored in the SQLite `config` table.
Provides type-safe access with defaults, section-based grouping, and
automatic seeding on first run.

Configuration is organized into sections matching SABnzbd's config structure:
  - misc: General settings (host, port, auth, etc.)
  - folders: Directory paths for downloads, scripts, etc.
  - torbox: Torbox API connection and download behavior (replaces 'servers')
  - switches: Queue and post-processing behavior
  - notifications: Email and Apprise notification settings
  - sorting: TV/Movie/Date sort string configuration
  - special: Advanced settings that don't fit elsewhere

Each setting is stored as (section, keyword, value) where value is always
a string — callers must convert to int/float/bool as needed using the
provided helper methods.
"""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite

from debridnzd.db.database import Database

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
#  Security: Sensitive keywords that must be redacted in logs          #
# ------------------------------------------------------------------ #

# These keywords correspond to config values that contain secrets
# (API keys, passwords, tokens). They are redacted in all log output
# and should be masked in API responses to non-admin users.
SENSITIVE_KEYWORDS: frozenset[str] = frozenset({
    "api_key", "nzb_key", "password", "email_password",
    "email_account", "socks5_proxy",
})

# Sections that cannot be deleted via delete_section().
# _internal: schema tracking data
# misc: authentication and core settings (api_key, nzb_key, password, etc.)
# torbox: Torbox API credentials (api_key)
# notifications: email credentials (email_account, email_password)
PROTECTED_SECTIONS: frozenset[str] = frozenset({"_internal", "misc", "torbox", "notifications"})

# Keywords that cannot be modified via set() or deleted in specific sections.
# These control authentication and authorization behavior within their section.
# - misc.api_key / misc.nzb_key: SABnzbd API authentication keys
# - misc.password: web UI authentication password
# - special.disable_api_key: controls whether API auth is required
# Note: torbox.api_key is intentionally NOT restricted here — it's the Torbox
# service credential that must be settable from the web UI config page.
SECTION_RESTRICTED_KEYWORDS: dict[str, frozenset[str]] = {
    "misc": frozenset({"api_key", "nzb_key", "password"}),
    "special": frozenset({"disable_api_key"}),
}


# Keywords that cannot be modified via set() within specific protected sections.
# These are settings that affect security but are section-specific. Modifying
# them could lead to SSRF (base_url), network exposure (host, port), or
# credential exfiltration (https_* keys). They can only be changed through
# dedicated API endpoints that perform additional validation.
SECTION_PROTECTED_KEYWORDS: dict[str, frozenset[str]] = {
    "misc": frozenset({
        "host", "port", "https_enabled", "https_port",
        "https_cert", "https_key", "https_chain",
    }),
    "torbox": frozenset({
        "base_url",
    }),
}

# Maximum length for config values to prevent DoS via excessively large values.
MAX_VALUE_LENGTH = 65536

# Maximum length for section and keyword names to prevent DoS via
# excessively long identifiers. SABnzbd section/keyword names are all
# under 30 characters; 128 provides generous headroom.
MAX_NAME_LENGTH = 128


# ------------------------------------------------------------------ #
#  Default configuration values                                        #
# ------------------------------------------------------------------ #

# All defaults are defined here. On first run, these are seeded into the
# config table. When reading a config value that doesn't exist in the DB,
# the default is returned automatically.

CONFIG_DEFAULTS: dict[str, dict[str, str]] = {
    "misc": {
        "host": "127.0.0.1",
        "port": "8080",
        "username": "",
        "password": "",
        "api_key": "",  # Auto-generated on first run
        "nzb_key": "",  # Auto-generated on first run
        "https_enabled": "0",
        "https_port": "",
        "https_cert": "",
        "https_key": "",
        "https_chain": "",
        "launch_browser": "1",
        "language": "en",
        "theme": "default",
        "check_new_release": "0",
        "external_access": "Full API",
        "socks5_proxy": "",
        "max_line_speed": "0",
        "speedlimit": "100",
        "article_cache_limit": "0",
    },
    "folders": {
        "download_dir": "downloads/incomplete",
        "complete_dir": "downloads/complete",
        "dirscan_dir": "",
        "dirscan_speed": "5",
        "script_dir": "scripts",
        "password_file": "",
        "admin_dir": "admin",
        "backup_dir": "",
        "log_dir": "logs",
        "nzb_backup_dir": "",
        "permissions": "",
        "min_free_space_download": "250M",
        "min_free_space_complete": "100M",
        "auto_resume": "1",
        "email_dir": "",
    },
    "torbox": {
        "api_key": "",
        "base_url": "https://api.torbox.app/v1",
        "default_type": "usenet",
        "auto_check_cached": "1",
        "default_post_processing": "-1",
        "download_on_complete": "1",
        "cdn_download_concurrency": "2",
        "poll_interval": "5",
    },
    "switches": {
        "max_retries": "3",
        "disconnect_on_empty": "0",
        "propagation_delay": "0",
        "duplicate_detection": "0",
        "smart_duplicate_detection": "0",
        "allow_proper": "0",
        "action_on_encrypted": "0",
        "unwanted_extensions": "exe,com,cmd,bat",
        "unwanted_extension_mode": "blacklist",
        "auto_sort_queue": "0",
        "pause_on_pp": "0",
        "download_all_par2": "0",
        "enable_sfv_check": "0",
        "enable_recursive_unpack": "1",
        "ignore_folders_in_archives": "0",
        "pp_only_verified": "1",
        "ignore_samples": "0",
        "deobfuscate_filenames": "0",
        "cleanup_list": ".nfo,.nzb,.sfv",
        "history_retention": "0",
        "replace_spaces": "0",
        "replace_underscores": "0",
        "replace_dots": "0",
        "enable_unrar": "1",
        "enable_unzip": "1",
        "enable_7zip": "1",
        "quota_size": "0",
        "quota_period": "daily",
        "quota_resume": "1",
        "quota_reset_day": "",
    },
    "notifications": {
        "email_enabled": "0",
        "email_server": "",
        "email_to": "",
        "email_from": "",
        "email_account": "",
        "email_password": "",
        "email_on_error": "1",
        "email_on_complete": "0",
        "email_on_disk_full": "1",
        "apprise_urls": "",
        "notify_on_startup": "0",
        "notify_on_shutdown": "0",
        "notify_on_pause": "0",
        "notify_on_resume": "0",
        "notify_on_added": "0",
        "notify_on_pp": "0",
        "notify_on_finished": "0",
        "notify_on_failed": "1",
        "notify_on_queue_finished": "0",
    },
    "sorting": {
        "enable_tv_sorting": "0",
        "tv_sort_string": "%sn/Season %s/%sn - S%0sE%0e - %en.%ext",
        "tv_sort_cats": "tv",
        "enable_movie_sorting": "0",
        "movie_sort_string": "%title (%y)/%title (%y).%ext",
        "movie_sort_cats": "movies",
        "enable_date_sorting": "0",
        "date_sort_string": "%t/%t - %y-%0m-%0d - %desc.%ext",
        "date_sort_cats": "",
    },
    "special": {
        "start_paused": "0",
        "preserve_paused_state": "0",
        "overwrite_files": "0",
        "api_warnings": "1",
        "helpful_warnings": "1",
        "disable_api_key": "0",
        "api_logging": "1",
        "x_frame_options": "1",
        "url_base": "/",
        "size_limit": "0",
        "nomedia_marker": ".nomedia",
        "max_foldername_length": "246",
        "allow_old_ssl_tls": "0",
        "config_lock": "0",
        "debug_mode": "0",
    },
}


class ConfigStore:
    """Async configuration store backed by SQLite.

    Provides section-based access to configuration values with type-safe
    getters and automatic default seeding. Usage::

        store = ConfigStore(db)
        await store.seed_defaults()  # Run once on startup

        # Read values
        host = await store.get("misc", "host", "127.0.0.1")
        port = await store.get_int("misc", "port", 8080)

        # Write values
        await store.set("misc", "port", "9090")

        # Read an entire section
        misc = await store.get_section("misc")

    All values are stored as strings in the database. Type-safe getters
    (get_int, get_bool, get_float) handle conversion.
    """

    def __init__(self, db: Database) -> None:
        """Initialize the ConfigStore with a Database instance.

        Args:
            db: The Database instance to read/write configuration from.
                Must be initialized (db.conn is not None).
        """
        self.db = db

    async def seed_defaults(self) -> None:
        """Insert default configuration values for any missing settings.

        Iterates through CONFIG_DEFAULTS and inserts any (section, keyword)
        pair that doesn't already exist. Also generates API and NZB keys
        if they're still empty strings in the misc section.

        This is called once during application startup to ensure all
        settings have values, even if the user hasn't configured them.
        """
        if self.db.conn is None:
            raise RuntimeError("Database must be initialized before use")

        import secrets

        for section, settings in CONFIG_DEFAULTS.items():
            for keyword, default_value in settings.items():
                # Check if this setting already exists
                cursor = await self.db.conn.execute(
                    "SELECT value FROM config WHERE section = ? AND keyword = ?",
                    (section, keyword),
                )
                row = await cursor.fetchone()
                if row is None:
                    # Insert the default value
                    value = default_value

                    # Auto-generate API and NZB keys on first seed
                    if section == "misc" and keyword == "api_key" and value == "":
                        value = f"apikey_{secrets.token_hex(16)}"
                    elif section == "misc" and keyword == "nzb_key" and value == "":
                        value = f"nzbkey_{secrets.token_hex(16)}"

                    await self.db.conn.execute(
                        "INSERT INTO config (section, keyword, value) VALUES (?, ?, ?)",
                        (section, keyword, value),
                    )

        await self.db.conn.commit()
        logger.info("Configuration defaults seeded")

    # ------------------------------------------------------------------ #
    #  Read methods                                                       #
    # ------------------------------------------------------------------ #

    async def get(self, section: str, keyword: str, default: str = "") -> str:
        """Get a configuration value as a string.

        Args:
            section: The config section (e.g., 'misc', 'torbox').
            keyword: The setting keyword (e.g., 'host', 'api_key').
            default: Value to return if the setting is not found.

        Returns:
            The configuration value, or the default if not found.
        """
        if self.db.conn is None:
            raise RuntimeError("Database must be initialized before use")
        cursor = await self.db.conn.execute(
            "SELECT value FROM config WHERE section = ? AND keyword = ?",
            (section, keyword),
        )
        row = await cursor.fetchone()
        if row is not None and row[0] is not None:
            return str(row[0])
        return default

    async def get_int(self, section: str, keyword: str, default: int = 0) -> int:
        """Get a configuration value as an integer.

        Args:
            section: The config section.
            keyword: The setting keyword.
            default: Value to return if the setting is not found or not a valid int.

        Returns:
            The configuration value as an integer, or the default.
        """
        value = await self.get(section, keyword)
        if value == "":
            return default
        try:
            return int(value)
        except ValueError:
            logger.warning("Invalid integer value for %s.%s: %s, using default %d", section, keyword, value, default)
            return default

    async def get_bool(self, section: str, keyword: str, default: bool = False) -> bool:
        """Get a configuration value as a boolean.

        In the config store, booleans are stored as "0" (false) or "1" (true),
        matching SABnzbd's convention.

        Args:
            section: The config section.
            keyword: The setting keyword.
            default: Value to return if the setting is not found.

        Returns:
            The configuration value as a boolean.
        """
        value = await self.get(section, keyword)
        if value == "":
            return default
        return value.lower() in ("1", "true", "yes", "on")

    async def get_float(self, section: str, keyword: str, default: float = 0.0) -> float:
        """Get a configuration value as a float.

        Args:
            section: The config section.
            keyword: The setting keyword.
            default: Value to return if the setting is not found or invalid.

        Returns:
            The configuration value as a float, or the default.
        """
        value = await self.get(section, keyword)
        if value == "":
            return default
        try:
            return float(value)
        except ValueError:
            logger.warning("Invalid float value for %s.%s: %s, using default %f", section, keyword, value, default)
            return default

    async def get_section(self, section: str, redact_secrets: bool = True) -> dict[str, str]:
        """Get all configuration values for a section as a dictionary.

        Args:
            section: The config section to retrieve.
            redact_secrets: If True (default), mask sensitive values like
                           passwords and API keys. Set to False only for
                           internal operations that need the real values.

        Returns:
            A dictionary mapping keyword → value for all settings in the section.
            Returns an empty dict if the section doesn't exist.
            Sensitive values are replaced with "***" when redact_secrets is True.
        """
        if self.db.conn is None:
            raise RuntimeError("Database must be initialized before use")
        cursor = await self.db.conn.execute(
            "SELECT keyword, value FROM config WHERE section = ? ORDER BY keyword",
            (section,),
        )
        rows = await cursor.fetchall()
        result = {}
        for keyword, value in rows:
            if value is None:
                continue
            if redact_secrets and keyword in SENSITIVE_KEYWORDS:
                result[keyword] = "***"
            else:
                result[keyword] = value
        return result

    async def get_all(self, redact_secrets: bool = True) -> dict[str, dict[str, str]]:
        """Get all configuration as a nested dictionary.

        Args:
            redact_secrets: If True (default), mask sensitive values like
                           passwords and API keys. Set to False only for
                           internal operations that need the real values.

        Returns:
            A dictionary mapping section → {keyword → value}.
            Useful for the SABnzbd API `get_config` mode.
            Sensitive values are replaced with "***" when redact_secrets is True.
        """
        if self.db.conn is None:
            raise RuntimeError("Database must be initialized before use")
        cursor = await self.db.conn.execute(
            "SELECT section, keyword, value FROM config ORDER BY section, keyword"
        )
        rows = await cursor.fetchall()
        result: dict[str, dict[str, str]] = {}
        for section, keyword, value in rows:
            if section == "_internal":
                continue  # Skip internal tracking rows
            if section not in result:
                result[section] = {}
            if redact_secrets and keyword in SENSITIVE_KEYWORDS:
                result[section][keyword] = "***"
            else:
                result[section][keyword] = value or ""
        return result

    # ------------------------------------------------------------------ #
    #  Write methods                                                      #
    # ------------------------------------------------------------------ #

    async def set(self, section: str, keyword: str, value: str) -> None:
        """Set a configuration value.

        Creates the row if it doesn't exist, updates it if it does.
        Uses INSERT OR REPLACE (upsert) for atomicity.

        Security validations:
        - The _internal section cannot be modified (protects schema_version)
        - The misc section cannot be modified (protects api_key, nzb_key, etc.)
        - Security-critical keywords (api_key, nzb_key, disable_api_key)
          cannot be modified even in other sections
        - Values are length-limited to MAX_VALUE_LENGTH to prevent DoS

        Args:
            section: The config section.
            keyword: The setting keyword.
            value: The value to store (always stored as a string).

        Raises:
            ValueError: If the section is protected, keyword is restricted,
                        or value exceeds max length.
            RuntimeError: If the database is not initialized.
        """
        if self.db.conn is None:
            raise RuntimeError("Database must be initialized before use")

        # Validate section and keyword lengths to prevent DoS
        if len(section) > MAX_NAME_LENGTH:
            raise ValueError(
                f"Section name exceeds maximum length ({len(section)} > {MAX_NAME_LENGTH})"
            )
        if len(keyword) > MAX_NAME_LENGTH:
            raise ValueError(
                f"Keyword name exceeds maximum length ({len(keyword)} > {MAX_NAME_LENGTH})"
            )

        # Protect the _internal section from modification
        if section == "_internal":
            raise ValueError("Cannot modify the _internal configuration section")

        # Protect section-specific security-critical keywords from modification.
        # This prevents changing host (binding to 0.0.0.0), port (privilege),
        # torbox.base_url (SSRF via API redirect), and HTTPS settings through
        # the generic set() method. These can only be changed through dedicated
        # API endpoints that perform additional validation.
        if section in SECTION_PROTECTED_KEYWORDS:
            protected = SECTION_PROTECTED_KEYWORDS[section]
            if keyword in protected:
                raise ValueError(
                    f"Cannot modify '{keyword}' in section '{section}' through "
                    "generic set(). Use dedicated API endpoints for this setting."
                )

        # Protect security-critical keywords in specific sections — this prevents
        # modifying misc.api_key, misc.nzb_key, misc.password, or
        # special.disable_api_key through the generic set() method, which would
        # allow auth bypass or credential replacement.
        # Note: torbox.api_key is NOT restricted — it's the Torbox service
        # credential that must be settable from the web UI.
        if section in SECTION_RESTRICTED_KEYWORDS and keyword in SECTION_RESTRICTED_KEYWORDS[section]:
            raise ValueError(
                f"Cannot modify restricted keyword '{keyword}' in section '{section}'. "
                "Use dedicated API endpoints for security-critical settings."
            )

        # Enforce maximum value length
        if len(value) > MAX_VALUE_LENGTH:
            raise ValueError(
                f"Config value for {section}.{keyword} exceeds maximum length "
                f"({len(value)} > {MAX_VALUE_LENGTH})"
            )

        await self.db.conn.execute(
            "INSERT OR REPLACE INTO config (section, keyword, value) VALUES (?, ?, ?)",
            (section, keyword, value),
        )
        await self.db.conn.commit()

        # Redact sensitive values in log output
        log_value = "***REDACTED***" if keyword in SENSITIVE_KEYWORDS else value
        logger.debug("Config set: %s.%s = %s", section, keyword, log_value)

    async def delete(self, section: str, keyword: str) -> bool:
        """Delete a configuration value.

        Args:
            section: The config section.
            keyword: The setting keyword.

        Returns:
            True if a row was deleted, False if the setting didn't exist.

        Raises:
            ValueError: If trying to delete from a protected section or
                        a restricted keyword.
            RuntimeError: If the database is not initialized.
        """
        if self.db.conn is None:
            raise RuntimeError("Database must be initialized before use")

        if section == "_internal":
            raise ValueError("Cannot modify the _internal configuration section")

        # Protect all keys in protected sections from deletion.
        # Even non-restricted keys like host, port, base_url should not be
        # deleted from misc or torbox sections as that could cause DoS or
        # security issues (e.g., deleting host defaults to 0.0.0.0 binding).
        if section in PROTECTED_SECTIONS:
            raise ValueError(
                f"Cannot delete from protected section '{section}'. "
                "This section is required for the application to function."
            )

        # Protect security-critical keywords from deletion in specific sections
        if section in SECTION_RESTRICTED_KEYWORDS and keyword in SECTION_RESTRICTED_KEYWORDS[section]:
            raise ValueError(
                f"Cannot delete restricted keyword '{keyword}' in section '{section}'. "
                "Use dedicated API endpoints for security-critical settings."
            )

        cursor = await self.db.conn.execute(
            "DELETE FROM config WHERE section = ? AND keyword = ?",
            (section, keyword),
        )
        await self.db.conn.commit()
        deleted = cursor.rowcount > 0
        if deleted:
            logger.info("Config deleted: %s.%s", section, keyword)
        return deleted

    async def delete_section(self, section: str) -> int:
        """Delete all configuration values for a section.

        Args:
            section: The config section to delete.

        Returns:
            The number of rows deleted.

        Raises:
            ValueError: If trying to delete a protected section (_internal, misc).
            RuntimeError: If the database is not initialized.
        """
        if self.db.conn is None:
            raise RuntimeError("Database must be initialized before use")

        if section in PROTECTED_SECTIONS:
            raise ValueError(
                f"Cannot delete protected section '{section}'. "
                "This section is required for the application to function."
            )

        cursor = await self.db.conn.execute(
            "DELETE FROM config WHERE section = ?",
            (section,),
        )
        await self.db.conn.commit()
        count = cursor.rowcount
        if count > 0:
            logger.info("Config section deleted: %s (%d keys removed)", section, count)
        return count