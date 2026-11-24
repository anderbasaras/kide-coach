"""Bot application: startup checks, handler wiring and long polling.

``kide run`` calls :func:`run`. Every startup problem (missing settings, invalid
profile, database or token errors) becomes a :class:`StartupError` with a one-line,
actionable message; the CLI prints it and exits with code 1.
"""

from __future__ import annotations

import logging

from alembic.util.exc import CommandError
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from telegram import Update
from telegram.error import InvalidToken, NetworkError
from telegram.ext import Application, ApplicationBuilder

from kide.bot.auth import make_auth_guard
from kide.bot.handlers import checkin, commands
from kide.bot.menu import sync_menus
from kide.bot.scheduler import start_checkin_jobs
from kide.bot.services import SERVICES_KEY, Services
from kide.config import ConfigError, Settings, get_settings
from kide.i18n import Translator
from kide.logging_config import configure_logging
from kide.profile import ProfileError, load_profile
from kide.storage import make_engine, make_session_factory, upgrade_database
from kide.storage.repositories import unit_of_work

logger = logging.getLogger(__name__)

# Only the update types the bot handles; everything else is not even downloaded.
ALLOWED_UPDATES = [Update.MESSAGE, Update.EDITED_MESSAGE, Update.CALLBACK_QUERY]


class StartupError(RuntimeError):
    """The bot cannot start. The message says what to fix."""


def load_settings() -> Settings:
    try:
        return get_settings()
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']).upper()}: {error['msg']}"
            for error in exc.errors()
        )
        raise StartupError(f"Invalid setting(s) in .env or the environment: {problems}") from None


def prepare_services(settings: Settings) -> Services:
    """Check settings, data folder, database and profile, in that order."""
    try:
        settings.require("telegram_bot_token", "allowed_chat_ids")
    except ConfigError as exc:
        raise StartupError(str(exc)) from None

    try:
        settings.ensure_data_dir()
    except OSError as exc:
        raise StartupError(f"Cannot create DATA_DIR '{settings.data_dir}': {exc}") from None

    try:
        upgrade_database(settings.database_url)
    except (SQLAlchemyError, CommandError, OSError) as exc:
        raise StartupError(
            f"Cannot open or upgrade the database at {settings.database_url}: {exc}"
        ) from None

    try:
        profile = load_profile(settings.profile_path)
    except ProfileError as exc:
        raise StartupError(str(exc)) from None

    session_factory = make_session_factory(make_engine(settings.database_url))
    with unit_of_work(session_factory) as repos:
        user = repos.users.get_or_create(profile.identity.name)
        user_id, chosen_language = user.id, user.language
    return Services(
        settings=settings,
        profile=profile,
        translator=Translator.from_directory(default_locale=profile.identity.language),
        session_factory=session_factory,
        user_id=user_id,
        chosen_language=chosen_language,
    )


async def _post_init(application: Application) -> None:
    """Runs once the bot is connected, before polling starts."""
    services = application.bot_data[SERVICES_KEY]
    await sync_menus(
        application.bot,
        services.translator,
        services.settings.allowed_chat_ids,
        services.locale,
    )
    if application.job_queue is None:  # pragma: no cover - dependency problem
        raise StartupError(
            "JobQueue is not available: install python-telegram-bot[job-queue] (uv sync)."
        )
    await start_checkin_jobs(application.bot, application.job_queue, services)
    logger.info("Kide is running. Language: %s. Press Ctrl+C to stop.", services.locale)


def build_application(services: Services) -> Application:
    token = services.settings.telegram_bot_token
    assert token is not None  # checked by prepare_services
    application = ApplicationBuilder().token(token.get_secret_value()).post_init(_post_init).build()
    application.bot_data[SERVICES_KEY] = services

    application.add_handler(make_auth_guard(services.settings.allowed_chat_ids), group=-1)
    commands.register(application, services)
    checkin.register(application, services)
    return application


def run() -> None:
    """Start the bot and block until it is stopped (Ctrl+C)."""
    settings = load_settings()
    token = settings.telegram_bot_token.get_secret_value() if settings.telegram_bot_token else ""
    configure_logging(settings.log_level, secrets=[token])

    services = prepare_services(settings)
    application = build_application(services)
    logger.info(
        "Starting Kide: %d allowed chat(s), profile %s, database %s",
        len(settings.allowed_chat_ids),
        settings.profile_path,
        settings.database_url,
    )
    try:
        application.run_polling(allowed_updates=ALLOWED_UPDATES)
    except KeyboardInterrupt:
        # A second Ctrl+C while shutting down (common on Windows). Everything is
        # already saved: each step commits its own transaction.
        logger.info("Kide stopped before shutdown finished")
        return
    except InvalidToken:
        raise StartupError(
            "Telegram rejected TELEGRAM_BOT_TOKEN. Copy it again from @BotFather (/token)."
        ) from None
    except NetworkError as exc:
        raise StartupError(f"Cannot reach Telegram ({exc}). Check your connection.") from None
    logger.info("Kide stopped")
