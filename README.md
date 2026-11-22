# Kide

[![CI](https://github.com/anderbasaras/kide-coach/actions/workflows/ci.yml/badge.svg)](https://github.com/anderbasaras/kide-coach/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)

> **Status: work in progress.** Kide is not usable yet. This README will grow with the project.

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
