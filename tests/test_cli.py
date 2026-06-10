"""Tests for the DebridNZBd CLI (debridnzbd.__main__).

Tests the reset-password subcommand for credential management.
"""

import argparse
import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio

from debridnzbd.__main__ import build_parser, do_reset_password
from debridnzbd.core.config_store import ConfigStore
from debridnzbd.db.database import Database


@pytest_asyncio.fixture
async def fresh_db(tmp_path: Path) -> Database:
    """Create a fresh database for testing."""
    admin_dir = tmp_path / "admin"
    admin_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    db_path = admin_dir / "debridnzbd.db"
    db = Database(db_path)
    await db.initialize()
    config = ConfigStore(db)
    await config.seed_defaults()
    yield db
    await db.close()


def _make_args(**overrides) -> argparse.Namespace:
    """Create a mock argparse Namespace for reset-password."""
    defaults = {
        "command": "reset-password",
        "username": None,
        "password": None,
        "temp": False,
        "db_path": "admin/debridnzbd.db",
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ------------------------------------------------------------------ #
#  Parser tests                                                        #
# ------------------------------------------------------------------ #


def test_parser_run_defaults():
    """run subcommand should have default host and port."""
    parser = build_parser()
    args = parser.parse_args(["run"])
    assert args.command == "run"
    assert args.host == "127.0.0.1"
    assert args.port == 8080


def test_parser_run_custom_host_port():
    """run subcommand should accept custom host and port."""
    parser = build_parser()
    args = parser.parse_args(["run", "--host", "0.0.0.0", "--port", "9090"])
    assert args.host == "0.0.0.0"
    assert args.port == 9090


def test_parser_reset_password_temp():
    """reset-password --temp should parse correctly."""
    parser = build_parser()
    args = parser.parse_args(["reset-password", "--temp"])
    assert args.command == "reset-password"
    assert args.temp is True
    assert args.username is None
    assert args.password is None


def test_parser_reset_password_explicit():
    """reset-password -u -p should parse correctly."""
    parser = build_parser()
    args = parser.parse_args(["reset-password", "-u", "myadmin", "-p", "mypassword123"])
    assert args.command == "reset-password"
    assert args.temp is False
    assert args.username == "myadmin"
    assert args.password == "mypassword123"


def test_parser_reset_password_db_path():
    """reset-password --db-path should parse correctly."""
    parser = build_parser()
    args = parser.parse_args(["reset-password", "--temp", "--db-path", "/custom/path/db.db"])
    assert args.db_path == "/custom/path/db.db"


def test_parser_no_subcommand_defaults_to_run():
    """No subcommand should parse with command=None."""
    parser = build_parser()
    args = parser.parse_args([])
    assert args.command is None  # main() defaults to 'run'


# ------------------------------------------------------------------ #
#  reset-password --temp tests                                          #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_reset_password_temp(fresh_db: Database, tmp_path: Path):
    """--temp should generate temporary credentials."""
    db_path = tmp_path / "admin" / "debridnzbd.db"
    args = _make_args(temp=True, db_path=str(db_path))

    with patch("builtins.print") as mock_print:
        await do_reset_password(args)

    # Verify temp credentials were set
    config = ConfigStore(fresh_db)
    username = await config.get("misc", "username")
    assert username == "admin"
    assert await config.get_bool("misc", "temp_credentials", False) is True
    assert await config.get_bool("misc", "setup_complete", True) is False

    # Verify output was printed
    mock_print.assert_called()
    output = "\n".join(
        str(call.args[0]) if call.args else ""
        for call in mock_print.call_args_list
    )
    assert "TEMPORARY CREDENTIALS GENERATED" in output
    assert "Username: admin" in output


@pytest.mark.asyncio
async def test_reset_password_temp_creates_db(tmp_path: Path):
    """--temp should create the database if it doesn't exist."""
    db_path = tmp_path / "newadmin" / "debridnzbd.db"
    assert not db_path.exists()

    args = _make_args(temp=True, db_path=str(db_path))
    with patch("builtins.print"):
        await do_reset_password(args)

    assert db_path.exists()


@pytest.mark.asyncio
async def test_reset_password_temp_warns_on_extra_args(fresh_db: Database, tmp_path: Path):
    """--temp with --username or --password should print a warning."""
    db_path = tmp_path / "admin" / "debridnzbd.db"
    args = _make_args(temp=True, username="ignored", password="alsoignored", db_path=str(db_path))

    with patch("builtins.print") as mock_print:
        await do_reset_password(args)

    # Warning should be printed to stderr
    warning_calls = [
        call for call in mock_print.call_args_list
        if call.kwargs.get("file") is not None
    ]
    # The function prints to stderr for the warning
    assert any("Warning" in str(call) or "ignores" in str(call) for call in mock_print.call_args_list)


# ------------------------------------------------------------------ #
#  reset-password explicit credentials tests                             #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_reset_password_explicit(fresh_db: Database, tmp_path: Path):
    """Setting explicit credentials should update the database."""
    db_path = tmp_path / "admin" / "debridnzbd.db"
    args = _make_args(username="myadmin", password="mypassword123", db_path=str(db_path))

    with patch("builtins.print") as mock_print:
        await do_reset_password(args)

    config = ConfigStore(fresh_db)
    assert await config.get("misc", "username") == "myadmin"
    assert await config.get_bool("misc", "temp_credentials", True) is False
    assert await config.get_bool("misc", "setup_complete", False) is True

    mock_print.assert_called()
    output = "\n".join(
        str(call.args[0]) if call.args else ""
        for call in mock_print.call_args_list
    )
    assert "Credentials updated successfully" in output
    assert "myadmin" in output


@pytest.mark.asyncio
async def test_reset_password_preserves_other_config(fresh_db: Database, tmp_path: Path):
    """Resetting credentials should not change other config values."""
    db_path = tmp_path / "admin" / "debridnzbd.db"
    config = ConfigStore(fresh_db)

    # Set some config values before resetting password
    await config.set("torbox", "api_key", "my_torbox_key")
    await config.set("folders", "cache_dir", "/tmp/cache")

    args = _make_args(username="newadmin", password="newpassword123", db_path=str(db_path))
    with patch("builtins.print"):
        await do_reset_password(args)

    # Verify other config is preserved
    assert await config.get("torbox", "api_key") == "my_torbox_key"
    assert await config.get("folders", "cache_dir") == "/tmp/cache"


# ------------------------------------------------------------------ #
#  Validation error tests                                               #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_reset_password_no_username_exits():
    """reset-password without --username or --temp should exit with error."""
    args = _make_args(username=None, password=None, temp=False, db_path="/tmp/test.db")
    with pytest.raises(SystemExit) as exc_info:
        await do_reset_password(args)
    assert exc_info.value.code == 1


@pytest.mark.asyncio
async def test_reset_password_short_username_exits():
    """reset-password with username < 3 chars should exit with error."""
    args = _make_args(username="ab", password="password123", temp=False, db_path="/tmp/test.db")
    with pytest.raises(SystemExit) as exc_info:
        await do_reset_password(args)
    assert exc_info.value.code == 1


@pytest.mark.asyncio
async def test_reset_password_short_password_exits():
    """reset-password with password < 6 chars should exit with error."""
    args = _make_args(username="admin", password="12345", temp=False, db_path="/tmp/test.db")
    with pytest.raises(SystemExit) as exc_info:
        await do_reset_password(args)
    assert exc_info.value.code == 1


@pytest.mark.asyncio
async def test_reset_password_interactive_prompt(tmp_path: Path):
    """reset-password with --username but no --password should prompt."""
    admin_dir = tmp_path / "admin"
    admin_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    db_path = admin_dir / "debridnzbd.db"

    args = _make_args(username="myadmin", password=None, temp=False, db_path=str(db_path))

    # Mock getpass.getpass to return a password
    with patch("getpass.getpass", return_value="prompted_password123"):
        with patch("builtins.print"):
            await do_reset_password(args)

    # Verify the database was updated with the prompted password
    db = Database(db_path)
    await db.initialize()
    config = ConfigStore(db)
    await config.seed_defaults()
    assert await config.get("misc", "username") == "myadmin"
    assert await config.get_bool("misc", "setup_complete", False) is True
    await db.close()


@pytest.mark.asyncio
async def test_reset_password_interactive_abort(tmp_path: Path):
    """reset-password with keyboard interrupt during prompt should exit."""
    db_path = tmp_path / "admin" / "debridnzbd.db"
    args = _make_args(username="myadmin", password=None, temp=False, db_path=str(db_path))

    # Mock getpass.getpass to raise KeyboardInterrupt
    with patch("getpass.getpass", side_effect=KeyboardInterrupt()):
        with pytest.raises(SystemExit) as exc_info:
            await do_reset_password(args)
    assert exc_info.value.code == 1