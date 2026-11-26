"""Claude's reading of a box WOD or an "Other" activity, confirmed by the user.

After "Done" (box) or the note/screenshots (other), :func:`interpret` asks the parser
for a structured reading, stores it as a *draft* in ``parsed_json`` and shows a summary
with two buttons:

- Confirm: the draft becomes ``confirmed`` (the only status the engine reads, phase 4).
- Adjust: the user writes the change in their own words; Claude applies it to the
  current reading (no photos are sent again) and the new draft is shown.

An activity without a stated effort first gets a 1-10 RPE keyboard (session RPE,
Foster's modified CR-10 scale), because the engine needs duration x RPE (D-041).

If Claude cannot produce a valid reading, the user is asked to write it (or the missing
part) as text, which is read again together with the photos, or to save the input as is.
API errors keep the input and offer "Try again". The draft lives in the database, so
every button works after a restart (D-029); only the "waiting for your correction" step
needs the open conversation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from telegram import Bot, CallbackQuery, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import ContextTypes, ConversationHandler

from kide.bot.handlers.common import SESSION_KEY, remove_buttons, today_command
from kide.bot.keyboards import (
    REVIEW_ADJUST_PREFIX,
    REVIEW_OK_PREFIX,
    REVIEW_RAW_PREFIX,
    REVIEW_RETRY_PREFIX,
    parse_rpe,
    parse_session_id,
    raw_keyboard,
    review_keyboard,
    rpe_keyboard,
)
from kide.bot.rules import Flow
from kide.bot.services import Services, get_services
from kide.bot.summaries import summarize_activity, summarize_wod
from kide.catalog.movements import default_movements
from kide.llm.client import LlmError, LlmErrorKind
from kide.llm.parsing import AthleteInfo, Kind, ParseOutcome, ParseStatus
from kide.llm.schemas import Activity, ParsedRecord, RecordStatus, Wod
from kide.storage.models import DayType
from kide.storage.repositories import unit_of_work

logger = logging.getLogger(__name__)

MODE_KEY = "kide_review_mode"  # user_data: what the next text in Flow.ADJUST means
MODE_ADJUST = "adjust"  # a correction of the current draft
MODE_CLARIFY = "clarify"  # the WOD/activity written again after a failed reading

_KINDS = {DayType.BOX: Kind.WOD, DayType.OTHER: Kind.ACTIVITY}
_CONFIG_ERRORS = (LlmErrorKind.AUTH, LlmErrorKind.BAD_REQUEST)

State = int  # a Flow value or ConversationHandler.END


def kind_for(day_type: DayType) -> Kind | None:
    """Box sessions hold a WOD and "Other" sessions an activity; nothing else is read."""
    return _KINDS.get(day_type)


def athlete_for(services: Services) -> AthleteInfo:
    """The only profile data a parsing call needs (principle 5)."""
    return AthleteInfo(sex=services.profile.body.sex, language=services.locale)


def summary_text(services: Services, record: ParsedRecord) -> str:
    item = record.item()
    if isinstance(item, Wod):
        return summarize_wod(item, services.t, services.locale, default_movements())
    return summarize_activity(item, services.t)


# --- Session data ----------------------------------------------------------------------


@dataclass(frozen=True)
class _Snapshot:
    """What interpreting a session needs, read in one short transaction."""

    kind: Kind
    text: str | None
    photos: list[str]
    record: ParsedRecord | None


def _snapshot(services: Services, session_id: int) -> _Snapshot | None:
    with unit_of_work(services.session_factory) as repos:
        training = repos.sessions.get(session_id)
        if training is None:
            return None
        kind = kind_for(training.type)
        if kind is None:
            return None
        text = training.raw_input if kind is Kind.WOD else training.notes
        record = ParsedRecord.model_validate(training.parsed_json) if training.parsed_json else None
        return _Snapshot(kind, text, [photo.path for photo in training.photos], record)


def _store(services: Services, session_id: int, record: ParsedRecord | None) -> bool:
    with unit_of_work(services.session_factory) as repos:
        training = repos.sessions.get(session_id)
        if training is None:
            return False
        repos.sessions.set_parsed(training, record.to_json() if record else None)
        return True


# --- Sending ---------------------------------------------------------------------------


async def _typing(bot: Bot, chat_id: int) -> None:
    try:
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except TelegramError as exc:  # cosmetic only
        logger.debug("Could not send the typing action: %s", exc)


async def send_review(
    bot: Bot, services: Services, chat_id: int, session_id: int, record: ParsedRecord
) -> State:
    """Show a draft: the RPE question first if an activity has no effort, else the summary."""
    item = record.item()
    if isinstance(item, Activity) and item.rpe is None:
        await bot.send_message(
            chat_id=chat_id,
            text=f"{summary_text(services, record)}\n\n{services.t('review.rpe_ask')}",
            reply_markup=rpe_keyboard(session_id),
        )
        return Flow.REVIEW
    await bot.send_message(
        chat_id=chat_id,
        text=f"{summary_text(services, record)}\n\n{services.t('review.ask')}",
        reply_markup=review_keyboard(services.t, session_id),
    )
    return Flow.REVIEW


async def _send_llm_error(
    bot: Bot, services: Services, chat_id: int, session_id: int, error: LlmError
) -> None:
    key = "review.llm_error_config" if error.kind in _CONFIG_ERRORS else "review.llm_error"
    await bot.send_message(
        chat_id=chat_id,
        text=services.t(key),
        reply_markup=raw_keyboard(services.t, session_id, retry=True),
    )


async def _after_parse(
    context: ContextTypes.DEFAULT_TYPE,
    services: Services,
    chat_id: int,
    session_id: int,
    outcome: ParseOutcome,
) -> State:
    bot = context.bot
    if outcome.skipped_images:
        await bot.send_message(
            chat_id=chat_id, text=services.t("review.skipped_images", count=outcome.skipped_images)
        )
    if outcome.status is ParseStatus.OK and outcome.item is not None:
        record = ParsedRecord.draft(outcome.item)
        if not _store(services, session_id, record):
            return await _gone(bot, services, chat_id)
        return await send_review(bot, services, chat_id, session_id, record)
    # Invalid reading: ask for the workout (or the missing part) as text.
    context.user_data[SESSION_KEY] = session_id
    context.user_data[MODE_KEY] = MODE_CLARIFY
    await bot.send_message(
        chat_id=chat_id,
        text=services.t("review.invalid"),
        reply_markup=raw_keyboard(services.t, session_id),
    )
    return Flow.ADJUST


async def _gone(bot: Bot, services: Services, chat_id: int) -> State:
    await bot.send_message(
        chat_id=chat_id, text=services.t("review.gone", today=today_command(services))
    )
    return ConversationHandler.END


# --- Entry: interpret a session ---------------------------------------------------------


async def interpret(
    context: ContextTypes.DEFAULT_TYPE,
    services: Services,
    chat_id: int,
    session_id: int,
    clarification: str | None = None,
) -> State | None:
    """Read the session with Claude and show the result.

    Returns the next conversation state, or ``None`` when there is nothing to read
    (no parser configured, not a box/other session, no text and no photo): the caller
    then answers as in phase 2.
    """
    parser = services.parser
    snapshot = _snapshot(services, session_id)
    if parser is None or snapshot is None:
        return None
    bot = context.bot

    if snapshot.record is not None and clarification is None:
        # Already read: never pay twice for the same input (e.g. "Done" tapped again).
        if snapshot.record.status is RecordStatus.CONFIRMED:
            summary = summary_text(services, snapshot.record)
            await bot.send_message(
                chat_id=chat_id, text=f"{services.t('review.already_confirmed')}\n{summary}"
            )
            return ConversationHandler.END
        return await send_review(bot, services, chat_id, session_id, snapshot.record)

    text = snapshot.text
    if clarification:
        text = f"{text}\n\n{clarification}" if text else clarification
    if not (text and text.strip()) and not snapshot.photos:
        return None

    reading = "review.reading_wod" if snapshot.kind is Kind.WOD else "review.reading_activity"
    await bot.send_message(chat_id=chat_id, text=services.t(reading))
    await _typing(bot, chat_id)
    try:
        outcome = await parser.parse(
            snapshot.kind,
            text=text,
            photo_paths=snapshot.photos,
            athlete=athlete_for(services),
            session_id=session_id,
        )
    except LlmError as error:
        await _send_llm_error(bot, services, chat_id, session_id, error)
        return Flow.REVIEW
    if outcome.status is ParseStatus.NO_INPUT:
        if outcome.skipped_images:  # e.g. only an image format that cannot be opened
            await bot.send_message(
                chat_id=chat_id,
                text=services.t("review.skipped_images", count=outcome.skipped_images),
            )
        return None
    return await _after_parse(context, services, chat_id, session_id, outcome)


# --- Buttons ---------------------------------------------------------------------------


async def _query_session(update: Update, prefix: str) -> tuple[CallbackQuery | None, int | None]:
    query = update.callback_query
    if query is None:
        return None, None
    await query.answer()
    return query, parse_session_id(query.data, prefix)


async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    query, session_id = await _query_session(update, REVIEW_OK_PREFIX)
    chat = update.effective_chat
    if query is None or chat is None:
        return ConversationHandler.END
    services = get_services(context)
    await remove_buttons(query.message)
    _clear(context)
    snapshot = _snapshot(services, session_id) if session_id is not None else None
    if snapshot is None or snapshot.record is None:
        return await _gone(context.bot, services, chat.id)
    record = snapshot.record.confirmed()
    _store(services, session_id, record)
    key = "review.confirmed_wod" if snapshot.kind is Kind.WOD else "review.confirmed_activity"
    await context.bot.send_message(chat_id=chat.id, text=services.t(key))
    logger.info("Reading confirmed: session=%s kind=%s", session_id, snapshot.kind.value)
    return ConversationHandler.END


async def save_raw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    """Keep what the user sent and drop any reading."""
    query, session_id = await _query_session(update, REVIEW_RAW_PREFIX)
    chat = update.effective_chat
    if query is None or chat is None:
        return ConversationHandler.END
    services = get_services(context)
    await remove_buttons(query.message)
    _clear(context)
    if session_id is None or not _store(services, session_id, None):
        return await _gone(context.bot, services, chat.id)
    await context.bot.send_message(chat_id=chat.id, text=services.t("review.saved_raw"))
    return ConversationHandler.END


async def retry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    query, session_id = await _query_session(update, REVIEW_RETRY_PREFIX)
    chat = update.effective_chat
    if query is None or chat is None or session_id is None:
        return ConversationHandler.END
    services = get_services(context)
    await remove_buttons(query.message)
    state = await interpret(context, services, chat.id, session_id)
    if state is None:
        return await _gone(context.bot, services, chat.id)
    return state


async def set_rpe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    query = update.callback_query
    chat = update.effective_chat
    if query is None or chat is None:
        return ConversationHandler.END
    await query.answer()
    services = get_services(context)
    parsed = parse_rpe(query.data)
    await remove_buttons(query.message)
    if parsed is None:
        return ConversationHandler.END
    session_id, rpe = parsed
    snapshot = _snapshot(services, session_id)
    if snapshot is None or snapshot.record is None or snapshot.kind is not Kind.ACTIVITY:
        return await _gone(context.bot, services, chat.id)
    activity = snapshot.record.item()
    assert isinstance(activity, Activity)
    record = ParsedRecord.draft(activity.model_copy(update={"rpe": rpe}))
    _store(services, session_id, record)
    return await send_review(context.bot, services, chat.id, session_id, record)


async def adjust_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    """ "Adjust": wait for the correction. Also an entry point (works after a restart)."""
    query, session_id = await _query_session(update, REVIEW_ADJUST_PREFIX)
    chat = update.effective_chat
    if query is None or chat is None or session_id is None:
        return ConversationHandler.END
    services = get_services(context)
    await remove_buttons(query.message)
    snapshot = _snapshot(services, session_id)
    if snapshot is None or snapshot.record is None:
        return await _gone(context.bot, services, chat.id)
    context.user_data[SESSION_KEY] = session_id
    context.user_data[MODE_KEY] = MODE_ADJUST
    await context.bot.send_message(chat_id=chat.id, text=services.t("review.adjust_prompt"))
    return Flow.ADJUST


async def adjust_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    """The user's correction (or, after a failed reading, the workout written as text)."""
    message = update.effective_message
    session_id = context.user_data.get(SESSION_KEY) if context.user_data is not None else None
    if message is None or message.text is None or not isinstance(session_id, int):
        return ConversationHandler.END
    services = get_services(context)
    chat_id = message.chat_id
    mode = context.user_data.get(MODE_KEY)
    if mode == MODE_CLARIFY:
        state = await interpret(context, services, chat_id, session_id, clarification=message.text)
        return state if state is not None else ConversationHandler.END

    snapshot = _snapshot(services, session_id)
    if snapshot is None or snapshot.record is None or services.parser is None:
        _clear(context)
        return await _gone(context.bot, services, chat_id)
    await context.bot.send_message(chat_id=chat_id, text=services.t("review.adjusting"))
    await _typing(context.bot, chat_id)
    try:
        outcome = await services.parser.adjust(
            snapshot.kind,
            current=snapshot.record.item(),
            original_text=snapshot.text,
            correction=message.text,
            athlete=athlete_for(services),
            session_id=session_id,
        )
    except LlmError as error:
        await _send_llm_error(context.bot, services, chat_id, session_id, error)
        return Flow.ADJUST
    if not outcome.ok or outcome.item is None:
        await context.bot.send_message(chat_id=chat_id, text=services.t("review.adjust_failed"))
        return Flow.ADJUST
    record = ParsedRecord.draft(outcome.item)
    _store(services, session_id, record)
    context.user_data.pop(MODE_KEY, None)
    return await send_review(context.bot, services, chat_id, session_id, record)


async def adjust_unsupported(update: Update, context: ContextTypes.DEFAULT_TYPE) -> State:
    if update.effective_message:
        await update.effective_message.reply_text(get_services(context).t("review.unsupported"))
    return Flow.ADJUST


def _clear(context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data is not None:
        context.user_data.pop(SESSION_KEY, None)
        context.user_data.pop(MODE_KEY, None)
