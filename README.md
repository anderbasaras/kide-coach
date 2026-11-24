# Kide

[![CI](https://github.com/anderbasaras/kide-coach/actions/workflows/ci.yml/badge.svg)](https://github.com/anderbasaras/kide-coach/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)

> **Status: work in progress.** The bot runs and logs your day (check-in, box WODs as text or
> photos, notes and screenshots), without AI yet. This README will grow with the project.

Kide (*"companion"* in Basque) is a self-hosted, open-source **hybrid training coach that lives in
Telegram**. It is built for people who combine gym training (hypertrophy / strength) with classes at
a functional fitness box and, sometimes, hybrid fitness races.

Every morning Kide asks what you are doing today:

- **Box** → send the WOD as text or a photo of the whiteboard; Kide structures and logs it.
- **Gym** → Kide plans the session from your rotation, taking into account the load of the last
  days and your weekly volume per muscle group.
- **Rest / other** → logged, with at most a light suggestion.

## Design principles

- **Self-hosted.** Each user runs their own bot with their own Telegram token and Anthropic API key.
  There is no central server and no third-party data.
- **Claude interprets, the engine decides.** Claude turns text and photos into structured data and
  writes the messages. Load, volume, rotation and progression are decided by deterministic, tested
  code.
- **Nothing personal in the code.** Everything about you lives in your profile and your local
  database.

## Project structure

```text
kide-coach/
  src/kide/
    bot/        Telegram app, handlers, keyboards, scheduler
    llm/        Anthropic client, prompts, structured-output schemas
    engine/     pure load engine: catalog, load, planner, progression
    profile/    profile schema, YAML loader, onboarding
    storage/    SQLite + SQLAlchemy models, repositories, migrations
    i18n/       translator and locale files (en, es)
    reports/    weekly summaries
    config.py   settings read from .env
    cli.py      `kide` command-line entry point
  data/         versioned catalogs (movements, exercises, rotation templates)
  docs/         design documentation
  tests/
```

User data (SQLite database, photos, exports) goes to `DATA_DIR` (`./var/` by default), which is
git-ignored.

## Your profile

Everything Kide knows about you lives in `profile.yaml` (git-ignored). Start from the
commented example and validate it:

```bash
cp profile.example.yaml profile.yaml
uv run kide profile check
```

Errors point to the line and field, e.g.
`line 3 · identity.timezone: unknown time zone 'Europe/Madird'. Did you mean 'Europe/Madrid'?`

## Database

Kide stores your data in a local SQLite file (`$DATA_DIR/kide.db`, `./var/kide.db` by
default). Create it, or bring it up to date after an upgrade, with:

```bash
uv run kide db upgrade
```

The schema is versioned with Alembic, so upgrades never require deleting your data.

## Running the bot (development)

### 1. Create your bot

1. In Telegram, open **@BotFather** (check the blue verified badge) and send `/newbot`.
2. Choose a display name (e.g. `Kide`) and a username ending in `bot`. You can change the
   display name later with `/setname`.
3. Copy the token. It is a password: keep it only in your `.env` (`/revoke` gives you a new one).
4. Send `/setjoingroups`, pick your bot and choose **Disable**, so nobody can add it to a group.

### 2. Configure and find your chat id

```bash
cp .env.example .env    # Windows PowerShell: Copy-Item .env.example .env
```

Set `TELEGRAM_BOT_TOKEN`, and set `ALLOWED_CHAT_IDS=1` for now (an id that belongs to nobody).
Then run the bot and write anything to it from your phone:

```bash
uv run kide run
```

The bot does not answer chats that are not allowed. It logs them, without their content:

```text
WARNING  kide.bot.auth: Ignored update from unauthorized chat: chat_id=123456789 chat_type=private update=message
```

Stop the bot (Ctrl+C), put that number in `ALLOWED_CHAT_IDS` and start it again. Several ids
can be separated by commas; the first one receives the scheduled check-in.

### 3. Use it

`kide run` checks your settings, creates or upgrades the database and validates your profile
before connecting; any problem is reported in one line.

| Command | Spanish alias | What it does |
|---|---|---|
| `/today` | `/hoy` | "What's on today?": Gym / Box / Rest / Other. On an answered day: correct it or add a session |
| `/help` | `/ayuda` | List of commands |
| `/language` | `/idioma` | Switch between English and Spanish (overrides the profile, kept after restarts) |
| `/start` | | Greeting, profile check and medical disclaimer |

- **Daily check-in** at `schedule.checkin_time`, in `identity.timezone` (daylight saving
  changes included). One per day: it is not repeated after a restart, and if the bot starts
  after that time it asks once.
- **Box**: send the WOD as text and/or one or more photos (albums and images sent as files
  too), then tap *Done*.
- **Gym / Rest / Other**: an optional note and optional screenshots (Strava, your watch...).
- **Optional reminder** if the check-in gets no answer: `schedule.checkin_reminder` in the
  profile (off by default).

Everything stays on your computer: the database is `$DATA_DIR/kide.db` and photos go to
`$DATA_DIR/photos/YYYY/MM/` (`./var/` by default). Nothing is sent to any AI service yet.

## Development

Requires [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 for you if needed).

```bash
uv sync                      # create .venv and install dependencies
uv run pre-commit install    # lint and format on every commit
uv run ruff check . && uv run ruff format --check .
uv run pytest
```

Plain pip also works: `python -m venv .venv && pip install -e . pytest ruff`.

## Disclaimer

**Kide is not medical advice.** It does not diagnose injuries or illnesses. If you feel acute pain,
chest pain or dizziness, stop training and see a doctor. Consult a qualified professional before
starting or changing a training programme.

Kide is an independent project and is not affiliated with, endorsed by or sponsored by HYROX,
CrossFit or any other brand. Those names are used only to describe the kinds of training Kide
supports.

## License

[MIT](LICENSE) © 2026 Ander Basaras Montalvo
