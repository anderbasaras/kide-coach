"""Test doubles for handlers: real Services on a temporary database and a mocked Bot.

Handlers call ``message.reply_text`` / ``query.edit_message_text``, which python-telegram-bot
turns into ``bot.send_message`` / ``bot.edit_message_text``. The mocked bot records those
calls, so tests check what the user would see without any network.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from telegram import Update

from kide.bot.services import SERVICES_KEY, Services
from kide.config import Settings
from kide.i18n import Translator
from kide.profile import Profile
from kide.storage import make_engine, make_session_factory, upgrade_database
from kide.storage.repositories import unit_of_work
from tests.telegram_factories import ALLOWED_CHAT

NOW = dt.datetime(2026, 11, 23, 8, 30, tzinfo=dt.UTC)


def make_services(tmp_path, profile_data: dict[str, Any], **overrides: Any) -> Services:
    url = f"sqlite:///{(tmp_path / 'kide.db').as_posix()}"
    upgrade_database(url)
    settings = Settings(
        _env_file=None,
        telegram_bot_token="123:fake",
        allowed_chat_ids=[ALLOWED_CHAT],
        data_dir=tmp_path / "var",
        database_url=url,
    )
    profile = Profile.model_validate(profile_data)
    factory = make_session_factory(make_engine(url))
    with unit_of_work(factory) as repos:
        user_id = repos.users.get_or_create(profile.identity.name).id
    services = Services(
        settings=settings,
        profile=profile,
        translator=Translator.from_directory(default_locale=profile.identity.language),
        session_factory=factory,
        user_id=user_id,
        clock=lambda: NOW,
    )
    for name, value in overrides.items():
        setattr(services, name, value)
    return services


def make_bot() -> MagicMock:
    bot = MagicMock(name="bot")
    for method in (
        "send_message",
        "edit_message_text",
        "edit_message_reply_markup",
        "answer_callback_query",
        "set_my_commands",
        "get_file",
    ):
        setattr(bot, method, AsyncMock(name=method))
    bot.defaults = None
    return bot


def make_context(services: Services, bot: MagicMock) -> SimpleNamespace:
    return SimpleNamespace(
        bot=bot, bot_data={SERVICES_KEY: services}, user_data={}, chat_data={}, job_queue=None
    )


def bind(update: Update, bot: MagicMock) -> Update:
    """Attach the mocked bot to an update and the objects inside it."""
    for obj in (update, update.message, update.edited_message, update.callback_query):
        if obj is not None:
            obj.set_bot(bot)
    if update.callback_query and update.callback_query.message:
        update.callback_query.message.set_bot(bot)
    return update


def sent_texts(bot: MagicMock) -> list[str]:
    """Texts sent or edited by the bot, in order."""
    calls = bot.send_message.await_args_list + bot.edit_message_text.await_args_list
    return [call.kwargs.get("text", "") for call in calls]
