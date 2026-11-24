"""/start, /help and /language (and their localized aliases).

/start is provisional until the onboarding of phase 6: it greets, confirms that the
profile is loaded and shows the medical disclaimer. The profile is validated at
startup, so if the bot is running the profile is valid.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from kide.bot.keyboards import LANGUAGE_PREFIX, language_keyboard, parse_callback
from kide.bot.menu import MENU_COMMANDS, command_name, command_names, set_chat_menus
from kide.bot.services import Services, get_services
from kide.storage.repositories import unit_of_work

logger = logging.getLogger(__name__)


def start_text(services: Services) -> str:
    profile = services.profile
    rotation = " → ".join(session.name for session in profile.training.rotation)
    locale = services.locale
    return "\n\n".join(
        [
            services.t("start.greeting", name=profile.identity.name),
            services.t(
                "start.profile",
                rotation=rotation,
                time=f"{profile.schedule.checkin_time:%H:%M}",
                timezone=profile.identity.timezone,
            ),
            services.t(
                "start.next",
                today=command_name(services.translator, "today", locale),
                help=command_name(services.translator, "help", locale),
            ),
            services.t("safety.medical_disclaimer"),
        ]
    )


def help_text(services: Services) -> str:
    locale = services.locale
    lines = [services.t("help.intro"), ""]
    lines += [
        services.t(
            "help.command",
            name=command_name(services.translator, command, locale),
            description=services.t(f"commands.{command}.description"),
        )
        for command in MENU_COMMANDS
    ]
    lines += ["", services.t("help.footer", time=f"{services.profile.schedule.checkin_time:%H:%M}")]
    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(start_text(get_services(context)))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(help_text(get_services(context)))


async def language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = get_services(context)
    if update.effective_message:
        await update.effective_message.reply_text(
            services.t("language.prompt"), reply_markup=language_keyboard(services.translator)
        )


async def language_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    services = get_services(context)
    parts = parse_callback(query.data, LANGUAGE_PREFIX) or []
    locale = parts[0] if parts else ""
    if locale not in services.translator.available_locales:
        await query.answer()
        return

    with unit_of_work(services.session_factory) as repos:
        user = repos.users.get_or_create(services.profile.identity.name)
        repos.users.set_language(user, locale)
    services.chosen_language = locale
    logger.info("Language set to %s", locale)

    await query.answer()
    await query.edit_message_text(services.t("language.changed"))
    try:
        await set_chat_menus(
            context.bot, services.translator, services.settings.allowed_chat_ids, locale
        )
    except TelegramError as exc:  # the choice is saved; the menu updates on next start
        logger.warning("Could not update the command menu: %s", exc)


def register(application: Application, services: Services) -> None:
    translator = services.translator
    application.add_handler(CommandHandler(command_names(translator, "start"), start))
    application.add_handler(CommandHandler(command_names(translator, "help"), help_command))
    application.add_handler(CommandHandler(command_names(translator, "language"), language))
    application.add_handler(CallbackQueryHandler(language_chosen, pattern=f"^{LANGUAGE_PREFIX}:"))
