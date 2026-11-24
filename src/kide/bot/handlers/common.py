"""Helpers shared by the check-in handlers: texts about the day and starting a flow."""

from __future__ import annotations

import datetime as dt
import logging

from telegram import Bot, InlineKeyboardMarkup, Message
from telegram.error import TelegramError
from telegram.ext import ContextTypes, filters

from kide.bot.keyboards import NOTE_SKIP_PREFIX, WOD_DONE_PREFIX, single_button
from kide.bot.menu import command_name
from kide.bot.rules import Flow, flow_after_choice, local_date
from kide.bot.services import Services
from kide.storage.models import DayType, TrainingSession

logger = logging.getLogger(__name__)

SESSION_KEY = "kide_session_id"  # user_data: the session the open flow is filling
IMAGE = filters.PHOTO | filters.Document.IMAGE  # photos, and images sent as files


# --- Texts ---------------------------------------------------------------------------


def choice_label(services: Services, choice: DayType) -> str:
    return services.t(f"checkin.options.{choice.value}")


def yes_no(services: Services, value: bool) -> str:
    return services.t("common.yes" if value else "common.no")


def describe_session(services: Services, training: TrainingSession) -> str:
    label = choice_label(services, training.type)
    if training.type is DayType.BOX:
        return services.t(
            "checkin.summary_box",
            choice=label,
            photos=len(training.photos),
            text=yes_no(services, bool(training.raw_input)),
        )
    photos = len(training.photos)
    if training.notes and photos:
        return services.t("checkin.summary_note_photos", choice=label, photos=photos)
    if training.notes:
        return services.t("checkin.summary_note", choice=label)
    if photos:
        return services.t("checkin.summary_photos", choice=label, photos=photos)
    return services.t("checkin.summary_plain", choice=label)


def describe_day(services: Services, sessions: list[TrainingSession]) -> str:
    return " + ".join(describe_session(services, training) for training in sessions)


def format_day(services: Services, day: dt.date) -> str:
    return day.strftime(services.t("formats.date"))


def today_command(services: Services) -> str:
    return command_name(services.translator, "today", services.locale)


def today_for(services: Services) -> dt.date:
    return local_date(services.clock(), services.tz)


# --- Telegram helpers ----------------------------------------------------------------


async def safe_edit(
    message: object, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    """Edit a bot message; old messages may no longer be editable, which is fine."""
    if not isinstance(message, Message):
        return
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramError as exc:
        logger.debug("Could not edit a message: %s", exc)


async def remove_buttons(message: object) -> None:
    if not isinstance(message, Message):
        return
    try:
        await message.edit_reply_markup(reply_markup=None)
    except TelegramError as exc:
        logger.debug("Could not remove the buttons: %s", exc)


async def enter_flow(
    bot: Bot,
    context: ContextTypes.DEFAULT_TYPE,
    services: Services,
    chat_id: int,
    session_id: int,
    choice: DayType,
) -> Flow:
    """Ask for what the chosen option needs (the WOD or a note) and open its flow."""
    context.user_data[SESSION_KEY] = session_id
    flow = flow_after_choice(choice)
    if flow is Flow.WOD:
        await bot.send_message(
            chat_id=chat_id,
            text=services.t("wod.ask"),
            reply_markup=single_button(services.t("wod.done_button"), WOD_DONE_PREFIX, session_id),
        )
    else:
        await bot.send_message(
            chat_id=chat_id,
            text=services.t("note.ask", choice=choice_label(services, choice)),
            reply_markup=single_button(
                services.t("note.skip_button"), NOTE_SKIP_PREFIX, session_id
            ),
        )
    return flow
