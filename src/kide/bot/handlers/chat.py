"""Free chat with the coach: any text outside a flow (replaces ``fallback.text``).

Each message is answered by the chat model with the coach system prompt (rules and the
profile, cached), today's date, the last 7 days of training (structured data and the
user's notes, never photos) and the last few exchanges kept in memory.

Safety limits live in the system prompt and are tested with real cases (``kide eval``);
deterministic detection of warning signs in the logs is phase 5.
"""

from __future__ import annotations

import datetime as dt
import logging

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from kide.bot.coach import ChatHistory, recent_days, split_message, today_header
from kide.bot.handlers.common import today_for
from kide.bot.menu import command_name
from kide.bot.services import Services, get_services
from kide.catalog.movements import default_movements
from kide.llm.client import LlmError, LlmErrorKind, Purpose, cached_system
from kide.llm.prompts.coach import coach_system_prompt
from kide.storage.repositories import unit_of_work

logger = logging.getLogger(__name__)

HISTORY_KEY = "kide_coach_history"  # chat_data
CHAT_MAX_TOKENS = 4096  # thinking + answer
CHAT_EFFORT: str | None = None  # the model's default (medium on Opus 5.5)


def _history(context: ContextTypes.DEFAULT_TYPE) -> ChatHistory:
    history = context.chat_data.get(HISTORY_KEY)
    if not isinstance(history, ChatHistory):
        history = ChatHistory()
        context.chat_data[HISTORY_KEY] = history
    return history


def build_context(services: Services, today: dt.date) -> str:
    """The uncached part of the system prompt: today and the last days of training."""
    start = today - dt.timedelta(days=6)
    with unit_of_work(services.session_factory) as repos:
        sessions = repos.sessions.between(services.user_id, start, today)

        def t_en(key: str, **params: object) -> str:
            return services.t(key, locale="en", **params)

        log = recent_days(sessions, today, t_en, default_movements())
    return f"{today_header(services.profile, today)}\n\n{log}"


async def coach_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not message.text:
        return
    services = get_services(context)
    if services.llm is None:
        locale = services.locale
        await message.reply_text(
            services.t(
                "fallback.text",
                today=command_name(services.translator, "today", locale),
                help=command_name(services.translator, "help", locale),
            )
        )
        return

    try:
        await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
    except TelegramError as exc:  # cosmetic only
        logger.debug("Could not send the typing action: %s", exc)

    now = services.clock()
    history = _history(context)
    system = cached_system(
        coach_system_prompt(services.profile, services.locale),
        build_context(services, today_for(services)),
    )
    messages = [*history.messages(now), {"role": "user", "content": message.text}]
    try:
        result = await services.llm.call(
            purpose=Purpose.CHAT,
            model=services.llm.chat_model,
            system=system,
            messages=messages,
            max_tokens=CHAT_MAX_TOKENS,
            effort=CHAT_EFFORT,
        )
    except LlmError as error:
        key = (
            "chat.error_config"
            if error.kind in (LlmErrorKind.AUTH, LlmErrorKind.BAD_REQUEST)
            else "chat.error"
        )
        await message.reply_text(services.t(key))
        return

    answer = result.text.strip()
    if not answer:
        await message.reply_text(services.t("chat.empty"))
        return
    for chunk in split_message(answer):
        await message.reply_text(chunk)
    history.add(now, message.text, answer)


def register(application: Application) -> None:
    """Before ``fallback.register()``: text in an open flow is still caught by the flow."""
    application.add_handler(
        MessageHandler(filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND, coach_reply)
    )
