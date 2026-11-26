"""The daily check-in: "What's on today?" and what happens after each option.

Flow (ConversationHandler, one per chat and user):

- The question is sent by /today (or the daily job, step 5) with four buttons.
- Tapping a button is the entry point. The choice is saved at once.
- Correct / Add session (see ``day_edit``) are entry points too.
- Box -> ``Flow.WOD``: every text and photo is saved as it arrives (albums included)
  until "Done".
- Gym / Rest / Other -> ``Flow.NOTE``: optional screenshots (Strava, the watch...) and
  one optional note, or "No note".
- Box and Other are then read by Claude (see ``review``) -> ``Flow.REVIEW`` (Confirm /
  Adjust) and ``Flow.ADJUST`` (the correction in the user's words). Gym and rest
  screenshots are not read in phase 3.

Everything is saved step by step, so nothing is lost if the bot restarts in the middle;
the conversation state itself is not persisted (D-029). "Done" and "No note" also work
after a restart: they only close the flow and show what was saved.
"""

from __future__ import annotations

import datetime as dt
import logging
import warnings

from telegram import Bot, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.warnings import PTBUserWarning

from kide.bot.handlers import day_edit, review
from kide.bot.handlers.common import (
    IMAGE,
    SESSION_KEY,
    choice_label,
    describe_day,
    describe_session,
    enter_flow,
    format_day,
    remove_buttons,
    safe_edit,
    today_command,
    today_for,
    yes_no,
)
from kide.bot.keyboards import (
    ADD_SET_PREFIX,
    CHECKIN_PREFIX,
    FIX_SET_PREFIX,
    NOTE_SKIP_PREFIX,
    REVIEW_ADJUST_PREFIX,
    REVIEW_OK_PREFIX,
    REVIEW_RAW_PREFIX,
    REVIEW_RETRY_PREFIX,
    RPE_PREFIX,
    WOD_DONE_PREFIX,
    checkin_keyboard,
    parse_checkin,
    parse_session_id,
    single_button,
)
from kide.bot.menu import command_names
from kide.bot.photos import PhotoTooLargeError, best_image, download_image, relative_photo_path
from kide.bot.rules import AnswerVerdict, Flow, judge_answer
from kide.bot.services import Services, get_services
from kide.storage.repositories import unit_of_work

logger = logging.getLogger(__name__)

ALBUM_ACK_DELAY_S = 1.5  # an album arrives as one message per photo: acknowledge once
CONVERSATION_TIMEOUT = dt.timedelta(hours=2)

# New messages only: edited messages are not part of a flow (see fallback handlers).
NEW_MESSAGE = filters.UpdateType.MESSAGE & ~filters.COMMAND


# --- Sending the question ------------------------------------------------------------


async def send_checkin_question(bot: Bot, services: Services, chat_id: int, day: dt.date) -> bool:
    """Ask "What's on today?" for ``day`` unless it is already answered.

    Returns True if the question was sent. The first send is recorded in ``sent_at``.
    """
    with unit_of_work(services.session_factory) as repos:
        checkin = repos.checkins.get_or_create(services.user_id, day)
        if checkin.sessions:
            return False
    await bot.send_message(
        chat_id=chat_id,
        text=services.t("checkin.question"),
        reply_markup=checkin_keyboard(services.t, day),
    )
    with unit_of_work(services.session_factory) as repos:
        checkin = repos.checkins.get_or_create(services.user_id, day)
        repos.checkins.mark_sent(checkin, chat_id, services.clock())
    return True


async def today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/today: ask now, or show what is already logged for today."""
    services = get_services(context)
    chat = update.effective_chat
    message = update.effective_message
    if chat is None or message is None:
        return
    day = today_for(services)
    with unit_of_work(services.session_factory) as repos:
        sessions = repos.sessions.for_day(services.user_id, day)
        summary = describe_day(services, sessions) if sessions else None
    if summary:
        await message.reply_text(
            services.t("checkin.already_answered", summary=summary),
            reply_markup=day_edit.day_keyboard(services, day),
        )
        return
    sent = await send_checkin_question(context.bot, services, chat.id, day)
    if sent and context.job_queue is not None:
        # Imported here: the scheduler module imports this one.
        from kide.bot.scheduler import schedule_reminder

        schedule_reminder(context.job_queue, services)


# --- Choosing an option --------------------------------------------------------------


async def choice_made(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    chat = update.effective_chat
    if query is None or chat is None:
        return ConversationHandler.END
    services = get_services(context)
    await query.answer()

    parsed = parse_checkin(query.data)
    today_day = today_for(services)
    if parsed is None:
        verdict = AnswerVerdict.INVALID_DAY
    else:
        day, choice = parsed
        with unit_of_work(services.session_factory) as repos:
            checkin = repos.checkins.get_or_create(services.user_id, day)
            verdict = judge_answer(day, today_day, already_answered=bool(checkin.sessions))
            if verdict is AnswerVerdict.ACCEPT:
                training = repos.sessions.create(checkin, choice)
                repos.checkins.refresh_choice(checkin, services.clock())
                session_id = training.id

    if verdict is not AnswerVerdict.ACCEPT:
        key = (
            "checkin.button_already_answered"
            if verdict is AnswerVerdict.ALREADY_ANSWERED
            else "checkin.invalid_button"
        )
        await context.bot.send_message(
            chat_id=chat.id, text=services.t(key, today=today_command(services))
        )
        return ConversationHandler.END

    label = choice_label(services, choice)
    answered = (
        services.t("checkin.chosen_today", choice=label)
        if day == today_day
        else services.t("checkin.chosen_on", date=format_day(services, day), choice=label)
    )
    await safe_edit(query.message, answered)
    logger.info("Check-in answered for %s: %s", day, choice.value)
    return await enter_flow(context.bot, context, services, chat.id, session_id, choice)


def _open_session_id(context: ContextTypes.DEFAULT_TYPE) -> int | None:
    value = context.user_data.get(SESSION_KEY) if context.user_data is not None else None
    return value if isinstance(value, int) else None


# --- Box: collecting the WOD ---------------------------------------------------------


def _wod_status(services: Services, session_id: int) -> tuple[int, bool] | None:
    """``(photo count, has text)`` of a session, or None if it no longer exists."""
    with unit_of_work(services.session_factory) as repos:
        training = repos.sessions.get(session_id)
        if training is None:
            return None
        return len(training.photos), bool(training.raw_input)


async def send_wod_ack(bot: Bot, services: Services, chat_id: int, session_id: int) -> None:
    status = _wod_status(services, session_id)
    if status is None:
        return
    photos, has_text = status
    await bot.send_message(
        chat_id=chat_id,
        text=services.t("wod.received", photos=photos, text=yes_no(services, has_text)),
        reply_markup=single_button(services.t("wod.done_button"), WOD_DONE_PREFIX, session_id),
    )


async def send_note_photo_ack(bot: Bot, services: Services, chat_id: int, session_id: int) -> None:
    status = _wod_status(services, session_id)
    if status is None:
        return
    await bot.send_message(
        chat_id=chat_id,
        text=services.t("note.photo_received", photos=status[0]),
        reply_markup=single_button(services.t("note.done_button"), NOTE_SKIP_PREFIX, session_id),
    )


async def _send_photo_ack(
    bot: Bot, services: Services, chat_id: int, session_id: int, flow: Flow
) -> None:
    if flow is Flow.WOD:
        await send_wod_ack(bot, services, chat_id, session_id)
    else:
        await send_note_photo_ack(bot, services, chat_id, session_id)


async def _album_ack_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    if job is None or job.chat_id is None or not isinstance(job.data, dict):
        return
    await _send_photo_ack(
        context.bot,
        get_services(context),
        job.chat_id,
        job.data["session_id"],
        Flow(job.data["flow"]),
    )


async def wod_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    services = get_services(context)
    message = update.effective_message
    session_id = _open_session_id(context)
    if message is None or message.text is None or session_id is None:
        return ConversationHandler.END
    with unit_of_work(services.session_factory) as repos:
        training = repos.sessions.get(session_id)
        if training is None:
            return ConversationHandler.END
        repos.sessions.append_text(training, message.text)
    await send_wod_ack(context.bot, services, message.chat_id, session_id)
    return Flow.WOD


async def _receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE, flow: Flow) -> int:
    """Store an image for the open session (the WOD whiteboard, a Strava screenshot...)."""
    services = get_services(context)
    message = update.effective_message
    session_id = _open_session_id(context)
    if message is None or session_id is None:
        return ConversationHandler.END
    found = best_image(message)
    if found is None:
        return flow
    image, extension = found

    with unit_of_work(services.session_factory) as repos:
        training = repos.sessions.get(session_id)
        if training is None:
            return ConversationHandler.END
        if repos.sessions.has_photo(training, image.file_unique_id):
            return flow  # same file delivered twice
        relative = relative_photo_path(training.date, session_id, image.file_unique_id, extension)

    try:
        await download_image(image, services.data_dir, relative)
    except PhotoTooLargeError:
        await message.reply_text(services.t("wod.photo_too_large"))
        return flow
    except (TelegramError, OSError) as exc:
        logger.warning("Could not download a photo for session %s: %s", session_id, exc)
        await message.reply_text(services.t("wod.photo_failed"))
        return flow

    with unit_of_work(services.session_factory) as repos:
        training = repos.sessions.get(session_id)
        if training is not None:
            repos.sessions.add_photo(
                training, relative.as_posix(), image.file_unique_id, message.media_group_id
            )

    if message.media_group_id and context.job_queue is not None:
        name = f"photo-ack:{message.chat_id}:{message.media_group_id}"
        for job in context.job_queue.get_jobs_by_name(name):
            job.schedule_removal()
        context.job_queue.run_once(
            _album_ack_job,
            ALBUM_ACK_DELAY_S,
            data={"session_id": session_id, "flow": int(flow)},
            chat_id=message.chat_id,
            name=name,
        )
    else:
        await _send_photo_ack(context.bot, services, message.chat_id, session_id, flow)
    return flow


async def wod_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _receive_photo(update, context, Flow.WOD)


async def note_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gym / rest / other also take screenshots (Strava, the watch, a notebook page)."""
    return await _receive_photo(update, context, Flow.NOTE)


async def wod_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Close the box flow. Also works after a restart, outside the conversation."""
    query = update.callback_query
    chat = update.effective_chat
    if query is None or chat is None:
        return ConversationHandler.END
    services = get_services(context)
    await query.answer()
    session_id = parse_session_id(query.data, WOD_DONE_PREFIX)
    status = _wod_status(services, session_id) if session_id is not None else None
    await remove_buttons(query.message)
    if context.user_data is not None:
        context.user_data.pop(SESSION_KEY, None)
    if status is None:
        return ConversationHandler.END
    photos, has_text = status
    if session_id is not None and (photos or has_text):
        state = await review.interpret(context, services, chat.id, session_id)
        if state is not None:
            return state
    text = (
        services.t("wod.saved", photos=photos, text=yes_no(services, has_text))
        if photos or has_text
        else services.t("wod.saved_empty")
    )
    await context.bot.send_message(chat_id=chat.id, text=text)
    return ConversationHandler.END


async def wod_unsupported(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_message:
        await update.effective_message.reply_text(get_services(context).t("wod.unsupported"))
    return Flow.WOD


# --- Gym / Rest / Other: optional note -----------------------------------------------


def _session_summary(services: Services, session_id: int | None) -> str | None:
    if session_id is None:
        return None
    with unit_of_work(services.session_factory) as repos:
        training = repos.sessions.get(session_id)
        return describe_session(services, training) if training else None


async def note_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    services = get_services(context)
    message = update.effective_message
    session_id = _open_session_id(context)
    if message is None or message.text is None or session_id is None:
        return ConversationHandler.END
    with unit_of_work(services.session_factory) as repos:
        training = repos.sessions.get(session_id)
        if training is None:
            return ConversationHandler.END
        repos.sessions.set_note(training, message.text)
        summary = describe_session(services, training)
    context.user_data.pop(SESSION_KEY, None)
    state = await review.interpret(context, services, message.chat_id, session_id)
    if state is not None:
        return state
    await message.reply_text(services.t("note.saved", summary=summary))
    return ConversationHandler.END


async def note_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """'No note' / 'Done'. Also works after a restart, outside the conversation."""
    query = update.callback_query
    chat = update.effective_chat
    if query is None or chat is None:
        return ConversationHandler.END
    services = get_services(context)
    await query.answer()
    await remove_buttons(query.message)
    if context.user_data is not None:
        context.user_data.pop(SESSION_KEY, None)
    session_id = parse_session_id(query.data, NOTE_SKIP_PREFIX)
    if session_id is not None:
        state = await review.interpret(context, services, chat.id, session_id)
        if state is not None:
            return state
    summary = _session_summary(services, session_id)
    if summary:
        await context.bot.send_message(
            chat_id=chat.id, text=services.t("note.saved", summary=summary)
        )
    return ConversationHandler.END


async def note_unsupported(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_message:
        await update.effective_message.reply_text(get_services(context).t("note.unsupported"))
    return Flow.NOTE


# --- Wiring ----------------------------------------------------------------------------


def _review_buttons() -> list[CallbackQueryHandler]:
    return [
        CallbackQueryHandler(review.confirm, pattern=f"^{REVIEW_OK_PREFIX}:"),
        CallbackQueryHandler(review.adjust_start, pattern=f"^{REVIEW_ADJUST_PREFIX}:"),
        CallbackQueryHandler(review.save_raw, pattern=f"^{REVIEW_RAW_PREFIX}:"),
        CallbackQueryHandler(review.retry, pattern=f"^{REVIEW_RETRY_PREFIX}:"),
        CallbackQueryHandler(review.set_rpe, pattern=f"^{RPE_PREFIX}:"),
    ]


def build_conversation() -> ConversationHandler:
    with warnings.catch_warnings():
        # per_message=False is intended: the flow mixes buttons and messages, per chat/user.
        warnings.filterwarnings("ignore", category=PTBUserWarning, message=".*per_message.*")
        return ConversationHandler(
            entry_points=[
                CallbackQueryHandler(choice_made, pattern=f"^{CHECKIN_PREFIX}:"),
                # Correcting a session or adding one also opens its flow.
                CallbackQueryHandler(day_edit.fix_set, pattern=f"^{FIX_SET_PREFIX}:"),
                CallbackQueryHandler(day_edit.add_set, pattern=f"^{ADD_SET_PREFIX}:"),
                # "Adjust" waits for a message, so it (re)opens the conversation.
                CallbackQueryHandler(review.adjust_start, pattern=f"^{REVIEW_ADJUST_PREFIX}:"),
            ],
            states={
                Flow.WOD: [
                    MessageHandler(NEW_MESSAGE & filters.TEXT, wod_text),
                    MessageHandler(NEW_MESSAGE & IMAGE, wod_photo),
                    CallbackQueryHandler(wod_done, pattern=f"^{WOD_DONE_PREFIX}:"),
                    MessageHandler(NEW_MESSAGE, wod_unsupported),
                ],
                Flow.NOTE: [
                    MessageHandler(NEW_MESSAGE & filters.TEXT, note_text),
                    MessageHandler(NEW_MESSAGE & IMAGE, note_photo),
                    CallbackQueryHandler(note_skip, pattern=f"^{NOTE_SKIP_PREFIX}:"),
                    MessageHandler(NEW_MESSAGE, note_unsupported),
                ],
                # Free text in REVIEW is not caught: it falls through to the coach chat.
                Flow.REVIEW: _review_buttons(),
                Flow.ADJUST: [
                    MessageHandler(NEW_MESSAGE & filters.TEXT, review.adjust_text),
                    *_review_buttons(),
                    MessageHandler(NEW_MESSAGE, review.adjust_unsupported),
                ],
            },
            fallbacks=[],
            allow_reentry=True,
            conversation_timeout=CONVERSATION_TIMEOUT,
            name="checkin",
        )


def register(application: Application, services: Services) -> None:
    application.add_handler(build_conversation())
    application.add_handler(CommandHandler(command_names(services.translator, "today"), today))
    # Buttons tapped after a restart, when the conversation is no longer open.
    application.add_handler(CallbackQueryHandler(wod_done, pattern=f"^{WOD_DONE_PREFIX}:"))
    application.add_handler(CallbackQueryHandler(note_skip, pattern=f"^{NOTE_SKIP_PREFIX}:"))
    # Review buttons outside the conversation (after a restart or a timeout): the draft
    # is in the database, so they keep working.
    for handler in _review_buttons():
        application.add_handler(handler)
    day_edit.register(application)
