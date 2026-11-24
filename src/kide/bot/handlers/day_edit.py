"""Correcting what was logged for a day and adding more sessions to it.

/today on an answered day shows the summary with two buttons:

- **Correct**: pick the session (if there are several), then its right type, or delete
  it. The corrected session starts over: its text, note and photos are replaced, and
  the flow of the new type opens (the WOD for box, a note for the rest).
- **Add session**: gym, box or other. Gym or box replace a rest session of that day;
  other keeps it (active rest). See :func:`kide.bot.rules.rest_to_replace`.

Choosing the corrected or added type is an entry point of the check-in conversation
(``checkin.build_conversation``), because it opens a flow.
"""

from __future__ import annotations

import datetime as dt
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, ConversationHandler

from kide.bot.handlers.common import (
    choice_label,
    describe_day,
    describe_session,
    enter_flow,
    remove_buttons,
    safe_edit,
    today_command,
    today_for,
)
from kide.bot.keyboards import (
    ADD_SET_PREFIX,
    DAY_ADD_PREFIX,
    DAY_FIX_PREFIX,
    FIX_DELETE_PREFIX,
    FIX_PICK_PREFIX,
    FIX_SET_PREFIX,
    callback_data,
    parse_checkin,
    parse_day,
    parse_session_choice,
    parse_session_id,
)
from kide.bot.photos import delete_photos
from kide.bot.rules import ADD_OPTIONS, needs_session_pick, rest_to_replace
from kide.bot.services import Services, get_services
from kide.storage.models import DayType
from kide.storage.repositories import unit_of_work

logger = logging.getLogger(__name__)


# --- Keyboards ---------------------------------------------------------------------


def day_keyboard(services: Services, day: dt.date) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    services.t("day.fix_button"),
                    callback_data=callback_data(DAY_FIX_PREFIX, day.isoformat()),
                ),
                InlineKeyboardButton(
                    services.t("day.add_button"),
                    callback_data=callback_data(DAY_ADD_PREFIX, day.isoformat()),
                ),
            ]
        ]
    )


def _type_buttons(
    services: Services, types: tuple[DayType, ...], data: list[str]
) -> list[list[InlineKeyboardButton]]:
    buttons = [
        InlineKeyboardButton(choice_label(services, day_type), callback_data=item)
        for day_type, item in zip(types, data, strict=True)
    ]
    return [buttons[i : i + 2] for i in range(0, len(buttons), 2)]


def correction_keyboard(services: Services, session_id: int) -> InlineKeyboardMarkup:
    types = (DayType.GYM, DayType.BOX, DayType.REST, DayType.OTHER)
    data = [callback_data(FIX_SET_PREFIX, str(session_id), t.value) for t in types]
    rows = _type_buttons(services, types, data)
    rows.append(
        [
            InlineKeyboardButton(
                services.t("day.delete_button"),
                callback_data=callback_data(FIX_DELETE_PREFIX, str(session_id)),
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def add_keyboard(services: Services, day: dt.date) -> InlineKeyboardMarkup:
    data = [callback_data(ADD_SET_PREFIX, day.isoformat(), t.value) for t in ADD_OPTIONS]
    return InlineKeyboardMarkup(_type_buttons(services, ADD_OPTIONS, data))


# --- Correct -------------------------------------------------------------------------


async def _show_correction(query_message: object, services: Services, session_id: int) -> None:
    with unit_of_work(services.session_factory) as repos:
        training = repos.sessions.get(session_id)
        found = training is not None and training.user_id == services.user_id
        current = describe_session(services, training) if found and training else ""
    if not found:
        await safe_edit(query_message, services.t("day.gone"))
        return
    await safe_edit(
        query_message,
        services.t("day.choose_type", session=current),
        reply_markup=correction_keyboard(services, session_id),
    )


async def fix_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """'Correct': go straight to the only session, or ask which one."""
    query = update.callback_query
    if query is None:
        return
    services = get_services(context)
    await query.answer()
    day = parse_day(query.data, DAY_FIX_PREFIX)
    if day is None:
        return
    with unit_of_work(services.session_factory) as repos:
        sessions = repos.sessions.for_day(services.user_id, day)
        listed = [(s.id, describe_session(services, s)) for s in sessions]
    if not listed:
        await safe_edit(query.message, services.t("day.nothing", today=today_command(services)))
        return
    if not needs_session_pick(len(listed)):
        await _show_correction(query.message, services, listed[0][0])
        return
    rows = [
        [
            InlineKeyboardButton(
                f"{number}. {text}", callback_data=callback_data(FIX_PICK_PREFIX, str(sid))
            )
        ]
        for number, (sid, text) in enumerate(listed, start=1)
    ]
    await safe_edit(
        query.message, services.t("day.pick_session"), reply_markup=InlineKeyboardMarkup(rows)
    )


async def fix_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    services = get_services(context)
    await query.answer()
    session_id = parse_session_id(query.data, FIX_PICK_PREFIX)
    if session_id is not None:
        await _show_correction(query.message, services, session_id)


async def fix_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """The corrected type: start the session over and open the flow of that type."""
    query = update.callback_query
    chat = update.effective_chat
    if query is None or chat is None:
        return ConversationHandler.END
    services = get_services(context)
    await query.answer()
    parsed = parse_session_choice(query.data, FIX_SET_PREFIX)
    if parsed is None:
        return ConversationHandler.END
    session_id, new_type = parsed

    old_label: str | None = None
    removed: list[str] = []
    with unit_of_work(services.session_factory) as repos:
        training = repos.sessions.get(session_id)
        if training is not None and training.user_id == services.user_id:
            old_label = choice_label(services, training.type)
            removed = repos.sessions.reset(training, new_type)
            if training.checkin is not None:
                repos.checkins.refresh_choice(training.checkin, services.clock())
    if old_label is None:
        await safe_edit(query.message, services.t("day.gone"))
        return ConversationHandler.END
    delete_photos(services.data_dir, removed)
    logger.info("Session %s corrected to %s", session_id, new_type.value)

    await safe_edit(
        query.message,
        services.t("day.corrected", old=old_label, new=choice_label(services, new_type)),
    )
    return await enter_flow(context.bot, context, services, chat.id, session_id, new_type)


async def fix_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    services = get_services(context)
    await query.answer()
    session_id = parse_session_id(query.data, FIX_DELETE_PREFIX)
    if session_id is None:
        return
    found = False
    removed: list[str] = []
    summary: str | None = None
    with unit_of_work(services.session_factory) as repos:
        training = repos.sessions.get(session_id)
        if training is not None and training.user_id == services.user_id:
            found = True
            checkin, day = training.checkin, training.date
            removed = repos.sessions.delete(training)
            if checkin is not None:
                repos.checkins.refresh_choice(checkin, services.clock())
            left = repos.sessions.for_day(services.user_id, day)
            summary = describe_day(services, left) if left else None
    if not found:
        await safe_edit(query.message, services.t("day.gone"))
        return
    delete_photos(services.data_dir, removed)
    logger.info("Session %s deleted", session_id)
    if summary:
        await safe_edit(query.message, services.t("day.deleted", summary=summary))
    else:
        await safe_edit(
            query.message, services.t("day.deleted_empty", today=today_command(services))
        )


# --- Add session -----------------------------------------------------------------------


async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    services = get_services(context)
    await query.answer()
    day = parse_day(query.data, DAY_ADD_PREFIX)
    if day is None:
        return
    await safe_edit(
        query.message, services.t("day.add_type"), reply_markup=add_keyboard(services, day)
    )


async def add_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """The added type: a new session (or the rest it replaces) and its flow."""
    query = update.callback_query
    chat = update.effective_chat
    if query is None or chat is None:
        return ConversationHandler.END
    services = get_services(context)
    await query.answer()
    parsed = parse_checkin(query.data, ADD_SET_PREFIX)
    if parsed is None or parsed[1] not in ADD_OPTIONS or parsed[0] > today_for(services):
        await remove_buttons(query.message)
        return ConversationHandler.END
    day, new_type = parsed

    with unit_of_work(services.session_factory) as repos:
        checkin = repos.checkins.get_or_create(services.user_id, day)
        sessions = repos.sessions.for_day(services.user_id, day)
        index = rest_to_replace([s.type for s in sessions], new_type)
        if index is None:
            session_id = repos.sessions.create(checkin, new_type).id
            replaced_rest = False
        else:
            target = sessions[index]
            repos.sessions.reset(target, new_type)  # a rest session has no photos
            session_id = target.id
            replaced_rest = True
        repos.checkins.refresh_choice(checkin, services.clock())
    logger.info("Session added for %s: %s", day, new_type.value)

    label = choice_label(services, new_type)
    key = "day.rest_replaced" if replaced_rest else "day.added"
    await safe_edit(query.message, services.t(key, choice=label))
    return await enter_flow(context.bot, context, services, chat.id, session_id, new_type)


def register(application: Application) -> None:
    """Buttons that do not open a flow (the ones that do are conversation entry points)."""
    application.add_handler(CallbackQueryHandler(fix_start, pattern=f"^{DAY_FIX_PREFIX}:"))
    application.add_handler(CallbackQueryHandler(fix_pick, pattern=f"^{FIX_PICK_PREFIX}:"))
    application.add_handler(CallbackQueryHandler(fix_delete, pattern=f"^{FIX_DELETE_PREFIX}:"))
    application.add_handler(CallbackQueryHandler(add_start, pattern=f"^{DAY_ADD_PREFIX}:"))
