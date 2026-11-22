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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kide", description="Kide hybrid training coach.")
    commands = parser.add_subparsers(dest="command", required=True)

    profile = commands.add_parser("profile", help="profile tools")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    check = profile_commands.add_parser("check", help="validate a profile YAML file")
    check.add_argument("path", nargs="?", help="defaults to PROFILE_PATH (./profile.yaml)")
    check.set_defaults(handler=_profile_check)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return the exit code."""
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
