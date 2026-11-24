"""Command names and the Telegram command menu, per language (D-026).

Every command has an English id (``today``) and a localized name in each locale file
(``commands.today.name``: ``hoy`` in Spanish). All names work in every language; the
menu shows the names of the user's language.

Menus are set at three levels (Telegram picks the most specific one):

1. default scope, no language: the profile language;
2. default scope per ``language_code``: for clients in that language;
3. each allowed chat: the language chosen with /language, so the menu follows that
   choice even if Telegram itself is in another language.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Collection

from telegram import Bot, BotCommand, BotCommandScopeChat
from telegram.error import TelegramError

from kide.i18n import Translator

logger = logging.getLogger(__name__)

# Shown in the menu, in this order. /start is left out: Telegram already shows a button.
MENU_COMMANDS = ("today", "help", "language")
# Every command the bot understands (menu or not).
ALL_COMMANDS = ("start", *MENU_COMMANDS)

COMMAND_NAME_RE = re.compile(r"^[a-z0-9_]{1,32}$")  # Telegram's rule for command names


def command_name(translator: Translator, command: str, locale: str) -> str:
    return translator.t(f"commands.{command}.name", locale=locale)


def command_names(translator: Translator, command: str) -> list[str]:
    """Every name of ``command`` across locales, English id first, without duplicates."""
    names = [command]
    for locale in translator.available_locales:
        name = command_name(translator, command, locale)
        if name not in names:
            names.append(name)
    return names


def menu_for(translator: Translator, locale: str) -> list[BotCommand]:
    return [
        BotCommand(
            command_name(translator, command, locale),
            translator.t(f"commands.{command}.description", locale=locale),
        )
        for command in MENU_COMMANDS
    ]


async def set_default_menus(bot: Bot, translator: Translator) -> None:
    """Menus for clients that have no chat-specific menu (levels 1 and 2)."""
    await bot.set_my_commands(menu_for(translator, translator.default_locale))
    for locale in translator.available_locales:
        await bot.set_my_commands(menu_for(translator, locale), language_code=locale)


async def set_chat_menus(
    bot: Bot, translator: Translator, chat_ids: Collection[int], locale: str
) -> None:
    """Menu of each allowed chat in the user's language (level 3)."""
    for chat_id in chat_ids:
        await bot.set_my_commands(menu_for(translator, locale), scope=BotCommandScopeChat(chat_id))


async def sync_menus(
    bot: Bot, translator: Translator, chat_ids: Collection[int], locale: str
) -> None:
    """Set every menu. A failure is logged, never fatal: the commands work without menu."""
    try:
        await set_default_menus(bot, translator)
        await set_chat_menus(bot, translator, chat_ids, locale)
    except TelegramError as exc:
        logger.warning("Could not update the command menu: %s", exc)
