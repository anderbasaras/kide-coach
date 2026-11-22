"""`kide` command-line entry point.

Operator tooling for whoever runs the instance. Messages here are in English and do
not go through i18n: they are not shown to the bot user.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from kide.config import get_settings
from kide.profile import ProfileError, load_profile
from kide.storage.db import upgrade_database


def _profile_check(args: argparse.Namespace) -> int:
    path = Path(args.path) if args.path else get_settings().profile_path
    try:
        profile = load_profile(path)
    except ProfileError as exc:
        print(exc, file=sys.stderr)
        return 1
    sessions = profile.training.rotation
    exercises = sum(len(session.exercises) for session in sessions)
    rotation = " -> ".join(session.id for session in sessions)
    print(f"OK: {path}")
    print(f"  {profile.identity.name} · {profile.identity.language} · {profile.identity.timezone}")
    print(f"  rotation: {rotation} ({exercises} exercises)")
    return 0


def _db_upgrade(args: argparse.Namespace) -> int:
    url = args.url or get_settings().database_url
    upgrade_database(url)
    print(f"OK: database at {url} is up to date")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kide", description="Kide hybrid training coach.")
    commands = parser.add_subparsers(dest="command", required=True)

    profile = commands.add_parser("profile", help="profile tools")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    check = profile_commands.add_parser("check", help="validate a profile YAML file")
    check.add_argument("path", nargs="?", help="defaults to PROFILE_PATH (./profile.yaml)")
    check.set_defaults(handler=_profile_check)

    db = commands.add_parser("db", help="database tools")
    db_commands = db.add_subparsers(dest="db_command", required=True)
    upgrade = db_commands.add_parser(
        "upgrade", help="create the database or apply pending migrations"
    )
    upgrade.add_argument("--url", help="defaults to DATABASE_URL ($DATA_DIR/kide.db)")
    upgrade.set_defaults(handler=_db_upgrade)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return the exit code."""
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
