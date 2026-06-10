"""Entry point for DebridNZBd CLI.

Supports subcommands:

  debridnzbd run [--host HOST] [--port PORT]
      Start the server (default when no subcommand is given).

  debridnzbd reset-password [-u USERNAME] [-p PASSWORD] [--temp] [--db-path PATH]
      Reset web UI credentials. Use --temp to generate temporary credentials
      (like first launch), or provide --username and --password to set
      permanent credentials. If --password is omitted, it will be prompted
      interactively.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the CLI."""
    parser = argparse.ArgumentParser(
        prog="debridnzbd",
        description="SABnzbd-compatible API server powered by Torbox",
    )
    subparsers = parser.add_subparsers(dest="command")

    # 'run' subcommand -- start the server
    run_parser = subparsers.add_parser("run", help="Start the server (default)")
    run_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind host (default: 127.0.0.1)",
    )
    run_parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Bind port (default: 8080)",
    )

    # 'reset-password' subcommand -- reset web UI credentials
    reset_parser = subparsers.add_parser(
        "reset-password",
        help="Reset web UI credentials",
    )
    reset_parser.add_argument(
        "-u",
        "--username",
        help="New username (required unless --temp is used)",
    )
    reset_parser.add_argument(
        "-p",
        "--password",
        help="New password (prompted interactively if not provided)",
    )
    reset_parser.add_argument(
        "--temp",
        action="store_true",
        help="Generate temporary credentials (like first launch)",
    )
    reset_parser.add_argument(
        "--db-path",
        default="admin/debridnzbd.db",
        help="Path to database file (default: admin/debridnzbd.db)",
    )

    return parser


async def do_reset_password(args: argparse.Namespace) -> None:
    """Reset web UI credentials in the database.

    Opens the database, runs migrations, seeds defaults, and either
    generates temporary credentials (--temp) or sets permanent credentials
    (--username + --password).
    """
    from debridnzbd.db.database import Database
    from debridnzbd.core.config_store import ConfigStore

    db_path = Path(args.db_path)

    # Validate arguments before touching the database
    if args.temp:
        if args.username or args.password:
            print("Warning: --temp ignores --username and --password", file=sys.stderr)
    else:
        if not args.username:
            print("Error: --username is required (or use --temp)", file=sys.stderr)
            sys.exit(1)
        if len(args.username.strip()) < 3:
            print("Error: Username must be at least 3 characters", file=sys.stderr)
            sys.exit(1)
        if not args.password:
            try:
                args.password = getpass.getpass("New password: ")
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.", file=sys.stderr)
                sys.exit(1)
            if not args.password:
                print("Error: Password cannot be empty", file=sys.stderr)
                sys.exit(1)
        if len(args.password) < 6:
            print("Error: Password must be at least 6 characters", file=sys.stderr)
            sys.exit(1)

    # Ensure admin directory exists with restrictive permissions
    admin_dir = db_path.parent
    admin_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(str(admin_dir), 0o700)
    except OSError:
        pass  # Not critical for CLI tool

    # Open database, run migrations, seed defaults
    db = Database(db_path)
    try:
        await db.initialize()
        config = ConfigStore(db)
        await config.seed_defaults()

        if args.temp:
            username, password = await config.generate_temp_credentials()
            print("=" * 60)
            print("TEMPORARY CREDENTIALS GENERATED")
            print(f"  Username: {username}")
            print(f"  Password: {password}")
            print(f"  Database: {db_path}")
            print("=" * 60)
            print()
            print("Log in to the web UI to complete the setup wizard.")
        else:
            await config.set_web_credentials(
                args.username.strip(),
                args.password,
            )
            print("Credentials updated successfully:")
            print(f"  Username: {args.username.strip()}")
            print(f"  Database: {db_path}")

        # Set restrictive permissions on database file
        try:
            os.chmod(str(db_path), 0o600)
        except OSError:
            pass  # Not critical for CLI tool

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        await db.close()


def run_server(args: argparse.Namespace) -> None:
    """Start the uvicorn server."""
    import uvicorn

    uvicorn.run(
        "debridnzbd.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
    )


def main() -> None:
    """Main entry point for the CLI."""
    parser = build_parser()
    args = parser.parse_args()

    # Default to 'run' if no subcommand given
    if args.command is None:
        args.command = "run"
        args.host = "127.0.0.1"
        args.port = 8080

    if args.command == "run":
        run_server(args)
    elif args.command == "reset-password":
        asyncio.run(do_reset_password(args))


if __name__ == "__main__":
    main()