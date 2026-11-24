"""Last-resort handlers and the global error handler.

They run only when nothing else matched (they are registered last in group 0), so
inside a flow the flow's own handlers win. Every reply is a short help text; nothing is
stored and the content is never logged.

- edited messages: Kide does not process edits ("send a new message");
- photos outside a flow: choose the session first with /today;
- other text and unknown commands: point to /today and /help (free chat with the coach
  arrives in phase 3);
- anything else (stickers, voice, audio, video, documents, locations...): help text;
- buttons from an old version or an expired flow: stop the spinner, nothing else.

The error handler logs the failure with the update kind and chat id, never its
content, and tells the user something went wrong. The bot keeps running.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters

from kide.bot.auth import update_kind
from kide.bot.handlers.common import IMAGE
from kide.bot.menu import command_name
from kide.bot.services import Services, get_services

logger = logging.getLogger(__name__)


def _commands(services: Services) -> dict[str, str]:
    locale = services.locale
    return {
        "today": command_name(services.translator, "today", locale),
        "help": command_name(services.translator, "help", locale),
    }


async def _reply(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str) -> None:
    message = update.effective_message
    if message is None:
        return
    services = get_services(context)
    await message.reply_text(services.t(key, **_commands(services)))


async def edited(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, context, "fallback.edited")


async def photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, context, "fallback.photo")


async def text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, context, "fallback.text")


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, context, "fallback.unknown_command")


async def unsupported(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, context, "fallback.unsupported")


async def stale_button(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.callback_query is not None:
        await update.callback_query.answer()


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log any exception raised by a handler or a job and keep the bot running."""
    error = context.error
    if isinstance(update, Update):
        chat = update.effective_chat
        logger.error(
            "Error handling update=%s chat_id=%s: %s",
            update_kind(update),
            chat.id if chat else None,
            type(error).__name__,
            exc_info=error,
        )
        if chat is not None and chat.id in get_services(context).settings.allowed_chat_ids:
            try:
                await context.bot.send_message(
                    chat_id=chat.id, text=get_services(context).t("errors.generic")
                )
            except TelegramError as exc:  # e.g. the network is down: just log it
                logger.warning("Could not tell the user about the error: %s", exc)
    else:  # a job (check-in, reminder) or the polling loop
        logger.error("Error outside an update: %s", type(error).__name__, exc_info=error)


def register(application: Application) -> None:
    """Call last: these handlers catch whatever the others did not."""
    new_message = filters.UpdateType.MESSAGE
    application.add_handler(MessageHandler(filters.UpdateType.EDITED, edited))
    application.add_handler(MessageHandler(new_message & IMAGE, photo))
    application.add_handler(MessageHandler(new_message & filters.COMMAND, unknown_command))
    application.add_handler(MessageHandler(new_message & filters.TEXT, text))
    application.add_handler(MessageHandler(new_message, unsupported))
    application.add_handler(CallbackQueryHandler(stale_button))
    application.add_error_handler(on_error)
