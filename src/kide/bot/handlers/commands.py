"""Bot commands. /start is provisional until the onboarding of phase 6."""

from __future__ import annotations

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from kide.bot.services import get_services


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = get_services(context)
    message = update.effective_message
    if message is None:
        return
    name = services.profile.identity.name
    await message.reply_text(
        "\n\n".join(
            [services.t("start.greeting", name=name), services.t("safety.medical_disclaimer")]
        )
    )


def register(application: Application) -> None:
    application.add_handler(CommandHandler("start", start))
