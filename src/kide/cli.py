"""`kide` command-line entry point.

Operator tooling for whoever runs the instance. Messages here are in English and do
not go through i18n: they are not shown to the bot user.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from kide.config import ConfigError, Settings, get_settings
from kide.profile import ProfileError, load_profile
from kide.storage.db import make_engine, make_session_factory, upgrade_database
from kide.storage.models import LlmCall


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


def _run(_args: argparse.Namespace) -> int:
    # Imported here so that `kide db` and `kide profile` do not load the Telegram stack.
    from kide.bot.app import StartupError, run

    try:
        run()
    except StartupError as exc:
        print(f"kide run: {exc}", file=sys.stderr)
        return 1
    return 0


def _llm_check(_args: argparse.Namespace) -> int:
    """One tiny call per configured model: checks the key and the model names."""
    from kide.llm.client import LlmClient, LlmError, Purpose

    settings = get_settings()
    try:
        settings.require("anthropic_api_key", "anthropic_model")
    except ConfigError as exc:
        print(f"kide llm check: {exc}", file=sys.stderr)
        return 1
    factory = _session_factory(settings)
    client = LlmClient.from_settings(settings, factory, user_id=None)
    assert client is not None  # both settings are present
    failures = 0
    for role, model in _distinct_models(client.chat_model, client.parse_model):
        try:
            result = asyncio.run(
                client.call(
                    purpose=Purpose.CHECK,
                    model=model,
                    system=[{"type": "text", "text": "Reply with the single word OK."}],
                    messages=[{"role": "user", "content": "Ping"}],
                    max_tokens=16,
                )
            )
        except LlmError as exc:
            failures += 1
            detail = f": {exc.detail}" if exc.detail else ""
            print(f"FAIL {role}: {model} -> {exc.kind.value}{detail}", file=sys.stderr)
            continue
        cost = f"${result.cost_usd}" if result.cost_usd is not None else "unknown price"
        print(
            f"OK {role}: {result.model} · {result.usage.input} in / {result.usage.output} out"
            f" · {cost} · {result.latency_ms} ms"
        )
    return 1 if failures else 0


def _distinct_models(chat: str, parse: str) -> list[tuple[str, str]]:
    if chat == parse:
        return [("chat + parsing", chat)]
    return [("chat", chat), ("parsing", parse)]


def _session_factory(settings: Settings) -> sessionmaker[Session]:
    upgrade_database(settings.database_url)
    return make_session_factory(make_engine(settings.database_url))


def _llm_costs(args: argparse.Namespace) -> int:
    """Calls, tokens and estimated cost per purpose and model, from ``llm_calls``."""
    from kide.storage.repositories import unit_of_work

    settings = get_settings()
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=args.days) if args.days else None
    with unit_of_work(_session_factory(settings)) as repos:
        rows = repos.llm_calls.summary(since)
    period = f"last {args.days} days" if args.days else "all time"
    if not rows:
        print(f"No Claude calls recorded ({period}).")
        return 0
    header = (
        f"{'purpose':<16} {'model':<20} {'calls':>5} {'fail':>4} {'in':>9} {'out':>8}"
        f" {'cache_w':>8} {'cache_r':>9} {'mean $':>10} {'total $':>10} {'mean ms':>8}"
    )
    print(f"Claude usage ({period}); costs are estimates (kide/llm/pricing.py)")
    print(header)
    print("-" * len(header))
    total = 0.0
    for row in rows:
        mean = f"{row.mean_cost_usd:.6f}" if row.mean_cost_usd is not None else "?"
        cost = f"{row.cost_usd:.6f}" if row.cost_usd is not None else "?"
        latency = f"{row.mean_latency_ms:.0f}" if row.mean_latency_ms is not None else "-"
        total += float(row.cost_usd or 0)
        print(
            f"{row.purpose:<16} {row.model[:20]:<20} {row.calls:>5} {row.failed:>4}"
            f" {row.input_tokens:>9} {row.output_tokens:>8} {row.cache_creation_input_tokens:>8}"
            f" {row.cache_read_input_tokens:>9} {mean:>10} {cost:>10} {latency:>8}"
        )
    print(f"Total: ${total:.4f}")
    return 0


def _parse(args: argparse.Namespace) -> int:
    """Read a WOD or an activity with Claude and print the summary, JSON and cost."""
    import json

    from kide.bot.summaries import summarize_activity, summarize_wod
    from kide.catalog.movements import default_movements
    from kide.i18n import Translator
    from kide.llm.client import LlmClient, LlmError
    from kide.llm.parsing import AthleteInfo, Kind, Parser
    from kide.llm.schemas import Wod
    from kide.storage.repositories import unit_of_work

    settings = get_settings()
    try:
        settings.require("anthropic_api_key", "anthropic_model")
        profile = load_profile(settings.profile_path)
    except (ConfigError, ProfileError) as exc:
        print(f"kide parse: {exc}", file=sys.stderr)
        return 1
    photos = [str(Path(photo).resolve()) for photo in args.photo or []]
    missing = [photo for photo in photos if not Path(photo).is_file()]
    if missing:
        print(f"kide parse: file not found: {', '.join(missing)}", file=sys.stderr)
        return 1
    factory = _session_factory(settings)
    client = LlmClient.from_settings(settings, factory, user_id=None)
    assert client is not None
    catalog = default_movements()
    parser = Parser(client, catalog, settings.data_dir)
    language = profile.identity.language
    athlete = AthleteInfo(sex=profile.body.sex, language=language)
    try:
        outcome = asyncio.run(
            parser.parse(Kind(args.kind), text=args.text, photo_paths=photos, athlete=athlete)
        )
    except LlmError as exc:
        detail = f": {exc.detail}" if exc.detail else ""
        print(f"kide parse: {exc.kind.value}{detail}", file=sys.stderr)
        return 1
    with unit_of_work(factory) as repos:
        calls = [repos.session.get(LlmCall, call_id) for call_id in outcome.call_ids]
        cost = sum(float(call.estimated_cost_usd or 0) for call in calls if call)
        tokens = [
            (
                c.input_tokens,
                c.cache_creation_input_tokens,
                c.cache_read_input_tokens,
                c.output_tokens,
            )
            for c in calls
            if c
        ]
    print(f"status: {outcome.status.value} · attempts: {outcome.attempts}")
    if outcome.skipped_images:
        print(f"skipped images: {outcome.skipped_images}")
    if outcome.item is not None:
        translator = Translator.from_directory(default_locale=language)

        def t(key: str, **params: object) -> str:
            return translator.t(key, locale=language, **params)

        item = outcome.item
        summary = (
            summarize_wod(item, t, language, catalog)
            if isinstance(item, Wod)
            else summarize_activity(item, t)
        )
        print("\n" + summary + "\n")
        if args.json:
            print(
                json.dumps(
                    item.model_dump(mode="json", exclude_none=True), indent=2, ensure_ascii=False
                )
            )
    for index, (fresh, written, read, out) in enumerate(tokens, start=1):
        print(f"call {index}: in {fresh} · cache write {written} · cache read {read} · out {out}")
    print(f"cost: ${cost:.6f}")
    return 0 if outcome.ok else 1


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

    run = commands.add_parser("run", help="start the Telegram bot (long polling)")
    run.set_defaults(handler=_run)

    llm = commands.add_parser("llm", help="Claude (Anthropic API) tools")
    llm_commands = llm.add_subparsers(dest="llm_command", required=True)
    llm_check = llm_commands.add_parser(
        "check", help="make one tiny call per configured model (checks key and model names)"
    )
    llm_check.set_defaults(handler=_llm_check)
    costs = llm_commands.add_parser("costs", help="tokens and estimated cost per purpose")
    costs.add_argument("--days", type=int, help="only the last N days")
    costs.set_defaults(handler=_llm_costs)

    parse = commands.add_parser(
        "parse", help="read a WOD or an activity with Claude (testing: only the cost is recorded)"
    )
    parse.add_argument("kind", choices=["wod", "activity"])
    parse.add_argument("--text", help="what you would write to the bot")
    parse.add_argument("--photo", action="append", help="image file; repeat for several")
    parse.add_argument("--json", action="store_true", help="also print the structured JSON")
    parse.set_defaults(handler=_parse)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return the exit code."""
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
