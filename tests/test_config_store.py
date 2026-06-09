"""Tests for the ConfigStore module.

Validates configuration reading, writing, type conversion,
default seeding, and section-level operations.
"""

import pytest
import pytest_asyncio
from pathlib import Path

from debridnzbd.db.database import Database
from debridnzbd.core.config_store import ConfigStore, CONFIG_DEFAULTS


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> ConfigStore:
    """Create a ConfigStore backed by a fresh test database.

    The database is initialized and defaults are seeded before returning.
    """
    db_path = tmp_path / "test_config.db"
    database = Database(db_path)
    await database.initialize()
    config = ConfigStore(database)
    await config.seed_defaults()
    yield config
    await database.close()


class TestSeedDefaults:
    """Test that default configuration values are seeded correctly."""

    @pytest.mark.asyncio
    async def test_all_sections_seeded(self, store: ConfigStore) -> None:
        """All CONFIG_DEFAULTS sections exist in the database."""
        all_config = await store.get_all()
        for section in CONFIG_DEFAULTS:
            assert section in all_config, f"Section '{section}' should be seeded"

    @pytest.mark.asyncio
    async def test_misc_defaults(self, store: ConfigStore) -> None:
        """misc section has expected default values."""
        host = await store.get("misc", "host")
        assert host == "127.0.0.1"

        port = await store.get("misc", "port")
        assert port == "8080"

        https_enabled = await store.get("misc", "https_enabled")
        assert https_enabled == "0"

    @pytest.mark.asyncio
    async def test_torbox_defaults(self, store: ConfigStore) -> None:
        """torbox section has expected default values."""
        base_url = await store.get("torbox", "base_url")
        assert base_url == "https://api.torbox.app/v1"

        default_type = await store.get("torbox", "default_type")
        assert default_type == "usenet"

        poll_interval = await store.get("torbox", "poll_interval")
        assert poll_interval == "5"

    @pytest.mark.asyncio
    async def test_folders_defaults(self, store: ConfigStore) -> None:
        """folders section has expected default values."""
        download_dir = await store.get("folders", "download_dir")
        assert download_dir == "downloads/incomplete"

        complete_dir = await store.get("folders", "complete_dir")
        assert complete_dir == "downloads/complete"

    @pytest.mark.asyncio
    async def test_api_key_auto_generated(self, store: ConfigStore) -> None:
        """API and NZB keys are auto-generated if empty in defaults."""
        api_key = await store.get("misc", "api_key")
        assert api_key.startswith("apikey_")
        assert len(api_key) > 10  # Not just the prefix

        nzb_key = await store.get("misc", "nzb_key")
        assert nzb_key.startswith("nzbkey_")
        assert len(nzb_key) > 10

    @pytest.mark.asyncio
    async def test_seed_defaults_idempotent(self, store: ConfigStore) -> None:
        """Calling seed_defaults twice doesn't overwrite existing values."""
        # Set a custom value in a non-protected section
        await store.set("switches", "max_retries", "5")

        # Re-seed
        await store.seed_defaults()

        # The custom value should be preserved
        retries = await store.get("switches", "max_retries")
        assert retries == "5"


class TestReadMethods:
    """Test configuration reading with type conversions."""

    @pytest.mark.asyncio
    async def test_get_string(self, store: ConfigStore) -> None:
        """get() returns string values correctly."""
        host = await store.get("misc", "host")
        assert isinstance(host, str)
        assert host == "127.0.0.1"

    @pytest.mark.asyncio
    async def test_get_missing_returns_default(self, store: ConfigStore) -> None:
        """get() returns the provided default for missing keys."""
        value = await store.get("misc", "nonexistent_key", "fallback")
        assert value == "fallback"

    @pytest.mark.asyncio
    async def test_get_int(self, store: ConfigStore) -> None:
        """get_int() returns integer values correctly."""
        port = await store.get_int("misc", "port")
        assert port == 8080
        assert isinstance(port, int)

    @pytest.mark.asyncio
    async def test_get_int_default(self, store: ConfigStore) -> None:
        """get_int() returns default for missing or invalid keys."""
        value = await store.get_int("misc", "nonexistent", 42)
        assert value == 42

    @pytest.mark.asyncio
    async def test_get_bool_true(self, store: ConfigStore) -> None:
        """get_bool() returns True for "1" values."""
        launch = await store.get_bool("misc", "launch_browser")
        assert launch is True

    @pytest.mark.asyncio
    async def test_get_bool_false(self, store: ConfigStore) -> None:
        """get_bool() returns False for "0" values."""
        https = await store.get_bool("misc", "https_enabled")
        assert https is False

    @pytest.mark.asyncio
    async def test_get_bool_default(self, store: ConfigStore) -> None:
        """get_bool() returns default for missing keys."""
        value = await store.get_bool("misc", "nonexistent", True)
        assert value is True

    @pytest.mark.asyncio
    async def test_get_bool_case_insensitive(self, store: ConfigStore) -> None:
        """get_bool() is case-insensitive for truthy values."""
        await store.set("switches", "test_bool_upper", "TRUE")
        assert await store.get_bool("switches", "test_bool_upper") is True
        await store.set("switches", "test_bool_yes_upper", "YES")
        assert await store.get_bool("switches", "test_bool_yes_upper") is True
        await store.set("switches", "test_bool_on", "on")
        assert await store.get_bool("switches", "test_bool_on") is True

    @pytest.mark.asyncio
    async def test_get_float(self, store: ConfigStore) -> None:
        """get_float() returns float values correctly."""
        # Store a float value and read it back
        await store.set("misc", "test_float", "3.14")
        value = await store.get_float("misc", "test_float")
        assert abs(value - 3.14) < 0.001

    @pytest.mark.asyncio
    async def test_get_section(self, store: ConfigStore) -> None:
        """get_section() returns all values for a section as a dict."""
        misc = await store.get_section("misc")
        assert isinstance(misc, dict)
        assert "host" in misc
        assert "port" in misc
        assert misc["host"] == "127.0.0.1"
        assert misc["port"] == "8080"

    @pytest.mark.asyncio
    async def test_get_all(self, store: ConfigStore) -> None:
        """get_all() returns nested dict of all sections."""
        all_config = await store.get_all()
        assert "misc" in all_config
        assert "torbox" in all_config
        assert "folders" in all_config
        assert "_internal" not in all_config  # Internal rows are excluded

    @pytest.mark.asyncio
    async def test_get_int_invalid_value(self, store: ConfigStore) -> None:
        """get_int() returns default for non-numeric values."""
        await store.set("switches", "bad_int", "not_a_number")
        value = await store.get_int("switches", "bad_int", 99)
        assert value == 99

    @pytest.mark.asyncio
    async def test_get_float_invalid_value(self, store: ConfigStore) -> None:
        """get_float() returns default for non-numeric values."""
        await store.set("switches", "bad_float", "not_a_number")
        value = await store.get_float("switches", "bad_float", 1.5)
        assert value == 1.5


class TestWriteMethods:
    """Test configuration writing and deletion."""

    @pytest.mark.asyncio
    async def test_set_new_value(self, store: ConfigStore) -> None:
        """set() creates a new config entry."""
        await store.set("switches", "custom_key", "custom_value")
        value = await store.get("switches", "custom_key")
        assert value == "custom_value"

    @pytest.mark.asyncio
    async def test_set_overwrites_existing(self, store: ConfigStore) -> None:
        """set() overwrites an existing value."""
        await store.set("switches", "max_retries", "5")
        retries = await store.get("switches", "max_retries")
        assert retries == "5"

    @pytest.mark.asyncio
    async def test_delete_existing(self, store: ConfigStore) -> None:
        """delete() removes a setting and returns True."""
        # Use a non-protected section for delete testing
        await store.set("custom_section", "test_key", "test_value")
        result = await store.delete("custom_section", "test_key")
        assert result is True
        value = await store.get("custom_section", "test_key", "default_fallback")
        assert value == "default_fallback"

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, store: ConfigStore) -> None:
        """delete() returns False for a setting that doesn't exist."""
        result = await store.delete("custom_section", "nonexistent_key")
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_section(self, store: ConfigStore) -> None:
        """delete_section() removes all settings in a section."""
        # Add a custom section
        await store.set("custom_section", "key1", "val1")
        await store.set("custom_section", "key2", "val2")

        count = await store.delete_section("custom_section")
        assert count == 2

        section = await store.get_section("custom_section")
        assert section == {}


class TestConfigDefaults:
    """Test that CONFIG_DEFAULTS has all expected sections and keys."""

    def test_all_sections_present(self) -> None:
        """All expected sections exist in CONFIG_DEFAULTS."""
        expected_sections = [
            "misc", "folders", "torbox", "switches",
            "notifications", "sorting", "special"
        ]
        for section in expected_sections:
            assert section in CONFIG_DEFAULTS, f"Section '{section}' missing from CONFIG_DEFAULTS"

    def test_misc_section_has_required_keys(self) -> None:
        """misc section contains all required configuration keys."""
        misc = CONFIG_DEFAULTS["misc"]
        required_keys = ["host", "port", "username", "password", "api_key", "nzb_key"]
        for key in required_keys:
            assert key in misc, f"Key 'misc.{key}' missing from CONFIG_DEFAULTS"

    def test_torbox_section_has_required_keys(self) -> None:
        """torbox section contains all required configuration keys."""
        torbox = CONFIG_DEFAULTS["torbox"]
        required_keys = ["api_key", "base_url", "default_type", "poll_interval"]
        for key in required_keys:
            assert key in torbox, f"Key 'torbox.{key}' missing from CONFIG_DEFAULTS"

    def test_folders_section_has_required_keys(self) -> None:
        """folders section contains all required configuration keys."""
        folders = CONFIG_DEFAULTS["folders"]
        required_keys = ["download_dir", "complete_dir", "admin_dir"]
        for key in required_keys:
            assert key in folders, f"Key 'folders.{key}' missing from CONFIG_DEFAULTS"


class TestSecurityProtections:
    """Test round 2+ security hardening in ConfigStore.

    Validates that:
    - Restricted keywords cannot be deleted via delete()
    - Protected sections block all deletes via delete()
    - Protected sections cannot be deleted via delete_section()
    - get_section() redacts sensitive values by default
    - get_section() returns plaintext when redact_secrets=False
    - password is included in SECTION_RESTRICTED_KEYWORDS
    - Name length validation prevents DoS
    - notifications section is protected
    """

    @pytest.mark.asyncio
    async def test_delete_rejects_restricted_api_key_in_misc(self, store: ConfigStore) -> None:
        """delete() must reject attempts to delete restricted keyword 'api_key' in misc."""
        with pytest.raises(ValueError, match="protected"):
            await store.delete("misc", "api_key")

    @pytest.mark.asyncio
    async def test_delete_rejects_restricted_nzb_key_in_misc(self, store: ConfigStore) -> None:
        """delete() must reject attempts to delete restricted keyword 'nzb_key' in misc."""
        with pytest.raises(ValueError, match="protected"):
            await store.delete("misc", "nzb_key")

    @pytest.mark.asyncio
    async def test_delete_rejects_restricted_password_in_misc(self, store: ConfigStore) -> None:
        """delete() must reject attempts to delete restricted keyword 'password' in misc."""
        with pytest.raises(ValueError, match="protected"):
            await store.delete("misc", "password")

    @pytest.mark.asyncio
    async def test_delete_rejects_restricted_disable_api_key_in_special(self, store: ConfigStore) -> None:
        """delete() must reject attempts to delete 'disable_api_key' in special."""
        # special is not a protected section (deletion of whole section is allowed),
        # but disable_api_key is a restricted keyword within it.
        with pytest.raises(ValueError, match="restricted"):
            await store.delete("special", "disable_api_key")

    @pytest.mark.asyncio
    async def test_delete_rejects_protected_section_misc(self, store: ConfigStore) -> None:
        """delete() must reject all deletes from protected section 'misc'."""
        with pytest.raises(ValueError, match="protected"):
            await store.delete("misc", "host")

    @pytest.mark.asyncio
    async def test_delete_rejects_protected_section_torbox(self, store: ConfigStore) -> None:
        """delete() must reject all deletes from protected section 'torbox'."""
        with pytest.raises(ValueError, match="protected"):
            await store.delete("torbox", "base_url")

    @pytest.mark.asyncio
    async def test_delete_allows_normal_keyword(self, store: ConfigStore) -> None:
        """delete() should still work for non-protected, non-restricted keywords."""
        await store.set("switches", "custom_key", "custom_value")
        result = await store.delete("switches", "custom_key")
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_section_rejects_protected_misc(self, store: ConfigStore) -> None:
        """delete_section() must reject attempts to delete protected section 'misc'."""
        with pytest.raises(ValueError, match="protected"):
            await store.delete_section("misc")

    @pytest.mark.asyncio
    async def test_delete_section_rejects_protected_torbox(self, store: ConfigStore) -> None:
        """delete_section() must reject attempts to delete protected section 'torbox'."""
        with pytest.raises(ValueError, match="protected"):
            await store.delete_section("torbox")

    @pytest.mark.asyncio
    async def test_delete_section_rejects_protected_notifications(self, store: ConfigStore) -> None:
        """delete_section() must reject attempts to delete protected section 'notifications'."""
        with pytest.raises(ValueError, match="protected"):
            await store.delete_section("notifications")

    @pytest.mark.asyncio
    async def test_delete_section_allows_custom_section(self, store: ConfigStore) -> None:
        """delete_section() should still work for non-protected sections."""
        await store.set("custom_section", "key1", "val1")
        count = await store.delete_section("custom_section")
        assert count >= 1

    @pytest.mark.asyncio
    async def test_get_section_redacts_password(self, store: ConfigStore) -> None:
        """get_section() must redact password by default."""
        # Password is seeded as empty string by defaults; verify it appears as "***"
        section = await store.get_section("misc")
        assert section["password"] == "***"

    @pytest.mark.asyncio
    async def test_get_section_redacts_api_key(self, store: ConfigStore) -> None:
        """get_section() must redact api_key by default."""
        section = await store.get_section("misc")
        # api_key is auto-generated, so it should be masked
        assert section["api_key"] == "***"

    @pytest.mark.asyncio
    async def test_get_section_no_redact_when_disabled(self, store: ConfigStore) -> None:
        """get_section(redact_secrets=False) returns actual secret values."""
        section = await store.get_section("misc", redact_secrets=False)
        # The api_key is auto-generated, so we can see its real value
        assert section["api_key"] != "***"
        assert len(section["api_key"]) > 0

    @pytest.mark.asyncio
    async def test_set_rejects_password_in_misc(self, store: ConfigStore) -> None:
        """set() must reject attempts to write restricted keyword 'password' in misc."""
        with pytest.raises(ValueError, match="restricted"):
            await store.set("misc", "password", "new_password")

    @pytest.mark.asyncio
    async def test_set_rejects_api_key_in_misc(self, store: ConfigStore) -> None:
        """set() must reject attempts to write restricted keyword 'api_key' in misc."""
        with pytest.raises(ValueError, match="restricted"):
            await store.set("misc", "api_key", "new_key")

    @pytest.mark.asyncio
    async def test_set_allows_torbox_api_key(self, store: ConfigStore) -> None:
        """set() must allow writing torbox.api_key — it's the Torbox service credential."""
        await store.set("torbox", "api_key", "tb_test_key_123")
        result = await store.get("torbox", "api_key")
        assert result == "tb_test_key_123"

    @pytest.mark.asyncio
    async def test_set_rejects_oversized_section_name(self, store: ConfigStore) -> None:
        """set() must reject section names exceeding MAX_NAME_LENGTH."""
        from debridnzbd.core.config_store import MAX_NAME_LENGTH
        long_section = "a" * (MAX_NAME_LENGTH + 1)
        with pytest.raises(ValueError, match="Section name exceeds maximum length"):
            await store.set(long_section, "key", "value")

    @pytest.mark.asyncio
    async def test_set_rejects_oversized_keyword_name(self, store: ConfigStore) -> None:
        """set() must reject keyword names exceeding MAX_NAME_LENGTH."""
        from debridnzbd.core.config_store import MAX_NAME_LENGTH
        long_keyword = "a" * (MAX_NAME_LENGTH + 1)
        with pytest.raises(ValueError, match="Keyword name exceeds maximum length"):
            await store.set("switches", long_keyword, "value")

    # --- Round 4 security tests ---

    @pytest.mark.asyncio
    async def test_set_rejects_misc_host(self, store: ConfigStore) -> None:
        """set() must reject modifying misc.host (security-sensitive)."""
        with pytest.raises(ValueError, match="Cannot modify.*host.*through generic set"):
            await store.set("misc", "host", "0.0.0.0")

    @pytest.mark.asyncio
    async def test_set_rejects_misc_port(self, store: ConfigStore) -> None:
        """set() must reject modifying misc.port (security-sensitive)."""
        with pytest.raises(ValueError, match="Cannot modify.*port.*through generic set"):
            await store.set("misc", "port", "443")

    @pytest.mark.asyncio
    async def test_set_rejects_misc_https_enabled(self, store: ConfigStore) -> None:
        """set() must reject modifying misc.https_enabled (security-sensitive)."""
        with pytest.raises(ValueError, match="Cannot modify.*https_enabled.*through generic set"):
            await store.set("misc", "https_enabled", "1")

    @pytest.mark.asyncio
    async def test_set_rejects_torbox_base_url(self, store: ConfigStore) -> None:
        """set() must reject modifying torbox.base_url (SSRF prevention)."""
        with pytest.raises(ValueError, match="Cannot modify.*base_url.*through generic set"):
            await store.set("torbox", "base_url", "https://evil.example.com/v1")

    @pytest.mark.asyncio
    async def test_set_allows_misc_max_line_speed(self, store: ConfigStore) -> None:
        """set() should still allow modifying non-protected misc keywords."""
        await store.set("misc", "max_line_speed", "0")
        result = await store.get("misc", "max_line_speed")
        assert result == "0"

    @pytest.mark.asyncio
    async def test_set_allows_torbox_poll_interval(self, store: ConfigStore) -> None:
        """set() should still allow modifying non-protected torbox keywords."""
        await store.set("torbox", "poll_interval", "10")
        result = await store.get("torbox", "poll_interval")
        assert result == "10"


# ------------------------------------------------------------------ #
#  Credential management tests (generate_temp_credentials,             #
#  set_web_credentials)                                                #
# ------------------------------------------------------------------ #


class TestCredentialManagement:
    """Tests for generate_temp_credentials() and set_web_credentials()."""

    @pytest.mark.asyncio
    async def test_set_rejects_username_in_misc(self, store: ConfigStore) -> None:
        """set() must reject writing restricted keyword 'username' in misc."""
        with pytest.raises(ValueError, match="restricted"):
            await store.set("misc", "username", "admin")

    @pytest.mark.asyncio
    async def test_delete_rejects_restricted_username_in_misc(self, store: ConfigStore) -> None:
        """delete() must reject deleting restricted keyword 'username' in misc."""
        with pytest.raises(ValueError, match="protected"):
            await store.delete("misc", "username")

    @pytest.mark.asyncio
    async def test_generate_temp_credentials(self, store: ConfigStore) -> None:
        """generate_temp_credentials() should create admin + random password."""
        username, password = await store.generate_temp_credentials()
        assert username == "admin"
        assert len(password) == 16  # secrets.token_hex(8)
        assert await store.get("misc", "temp_credentials") == "1"
        assert await store.get("misc", "setup_complete") == "0"

    @pytest.mark.asyncio
    async def test_set_web_credentials_validates_username_length(self, store: ConfigStore) -> None:
        """set_web_credentials() should reject usernames shorter than 3 chars."""
        with pytest.raises(ValueError, match="[Uu]sername"):
            await store.set_web_credentials("ab", "password123")

    @pytest.mark.asyncio
    async def test_set_web_credentials_validates_password_length(self, store: ConfigStore) -> None:
        """set_web_credentials() should reject passwords shorter than 6 chars."""
        with pytest.raises(ValueError, match="[Pp]assword"):
            await store.set_web_credentials("admin", "12345")

    @pytest.mark.asyncio
    async def test_set_web_credentials_validates_cidr(self, store: ConfigStore) -> None:
        """set_web_credentials() should reject invalid CIDR ranges."""
        with pytest.raises(ValueError, match="[Cc]IDR|[Tt]rusted|[Ii]nvalid"):
            await store.set_web_credentials("admin", "password123", trusted_networks="not-a-cidr")

    @pytest.mark.asyncio
    async def test_set_web_credentials_stores_values(self, store: ConfigStore) -> None:
        """set_web_credentials() should store credentials and clear temp flags."""
        await store.set_web_credentials("myuser", "mypassword123", trusted_networks="10.0.0.0/8")
        # Username and password should be stored (can't read via get() for restricted
        # keys, but can verify via DB directly)
        assert await store.get_bool("misc", "temp_credentials", True) is False
        assert await store.get_bool("misc", "setup_complete", False) is True
        assert await store.get("misc", "trusted_networks") == "10.0.0.0/8"

    @pytest.mark.asyncio
    async def test_set_web_credentials_clears_temp_flags(self, store: ConfigStore) -> None:
        """set_web_credentials() should clear temp_credentials and set setup_complete."""
        # First, generate temp credentials
        await store.generate_temp_credentials()
        assert await store.get_bool("misc", "temp_credentials", False) is True
        assert await store.get_bool("misc", "setup_complete", True) is False

        # Now set real credentials
        await store.set_web_credentials("realuser", "realpassword123")
        assert await store.get_bool("misc", "temp_credentials", True) is False
        assert await store.get_bool("misc", "setup_complete", False) is True