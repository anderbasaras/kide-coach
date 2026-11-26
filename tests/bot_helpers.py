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
from tests.telegram_factories import ALLOWED_CHAT, callback_update

NOW = dt.datetime(2026, 11, 23, 8, 30, tzinfo=dt.UTC)
TODAY = dt.date(2026, 11, 23)  # NOW is 09:30 in Berlin, the time zone of the test profile


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
        "send_chat_action",
    ):
        # Unnamed so the calls are also recorded, in order, in bot.mock_calls.
        setattr(bot, method, AsyncMock())
    bot.defaults = None
    return bot


def make_context(
    services: Services, bot: MagicMock, user_data: dict | None = None, job_queue: Any = None
) -> SimpleNamespace:
    return SimpleNamespace(
        bot=bot,
        bot_data={SERVICES_KEY: services},
        user_data={} if user_data is None else user_data,
        chat_data={},
        job_queue=job_queue,
        job=None,
    )


def fake_downloads(bot: MagicMock) -> None:
    """Make ``bot.get_file(...).download_to_drive(path)`` write a small file."""
    from pathlib import Path

    async def download(path):
        Path(path).write_bytes(b"fake image")
        return Path(path)

    telegram_file = MagicMock(name="file")
    telegram_file.download_to_drive = AsyncMock(side_effect=download)
    bot.get_file.return_value = telegram_file


def bind(update: Update, bot: MagicMock) -> Update:
    """Attach the mocked bot to an update and the objects inside it."""
    for obj in (update, update.message, update.edited_message, update.callback_query):
        if obj is not None:
            obj.set_bot(bot)
    msg = update.message
    if msg is not None:
        for photo in msg.photo:
            photo.set_bot(bot)
        if msg.document is not None:
            msg.document.set_bot(bot)
    if update.callback_query and update.callback_query.message:
        update.callback_query.message.set_bot(bot)
    return update


def sent_texts(bot: MagicMock) -> list[str]:
    """Texts sent or edited by the bot, in the order the user would see them."""
    return [
        call.kwargs.get("text", "")
        for name, _args, kwargs in bot.mock_calls
        if name in ("send_message", "edit_message_text")
        for call in [SimpleNamespace(kwargs=kwargs)]
    ]


class Chat:
    """One user talking to the bot: keeps user_data between updates, like PTB does."""

    def __init__(self, services, bot):
        self.services = services
        self.bot = bot
        self.user_data: dict = {}
        self.job_queue = MagicMock(name="job_queue")
        self.job_queue.get_jobs_by_name.return_value = []

    async def send(self, handler, update):
        context = make_context(self.services, self.bot, self.user_data, self.job_queue)
        return await handler(bind(update, self.bot), context)

    async def tap(self, handler, data):
        return await self.send(handler, callback_update(data=data))

    def texts(self):
        return sent_texts(self.bot)

    def reset_bot(self):
        self.bot.reset_mock()


def day_sessions(services, day=TODAY):
    with unit_of_work(services.session_factory) as repos:
        sessions = repos.sessions.for_day(services.user_id, day)
        checkin = repos.checkins.get(services.user_id, day)
        return (
            [(s.type, s.raw_input, s.notes, [p.path for p in s.photos]) for s in sessions],
            checkin,
        )
