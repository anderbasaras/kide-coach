"""Inline keyboards and their callback data.

Callback data is ``<prefix>:<payload>`` (Telegram allows at most 64 bytes). Each
feature has its own prefix so its handler can match on it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from kide.i18n import Translator
from kide.storage.models import DayType

LANGUAGE_PREFIX = "lang"
CHECKIN_PREFIX = "ci"  # ci:<YYYY-MM-DD>:<gym|box|rest|other>
WOD_DONE_PREFIX = "wod"  # wod:<session id>
NOTE_SKIP_PREFIX = "note"  # note:<session id>
DAY_FIX_PREFIX = "dfix"  # dfix:<YYYY-MM-DD>       "Correct" on the day summary
DAY_ADD_PREFIX = "dadd"  # dadd:<YYYY-MM-DD>       "Add session" on the day summary
FIX_PICK_PREFIX = "fpick"  # fpick:<session id>    which session to correct
FIX_SET_PREFIX = "fset"  # fset:<session id>:<type> the corrected type
FIX_DELETE_PREFIX = "fdel"  # fdel:<session id>    delete that session
ADD_SET_PREFIX = "aset"  # aset:<YYYY-MM-DD>:<type> the added session
REVIEW_OK_PREFIX = "rok"  # rok:<session id>       confirm Claude's reading
REVIEW_ADJUST_PREFIX = "radj"  # radj:<session id> correct it in your own words
REVIEW_RAW_PREFIX = "rraw"  # rraw:<session id>    keep the input, no interpretation
REVIEW_RETRY_PREFIX = "rtry"  # rtry:<session id>  try reading again after an API error
RPE_PREFIX = "rpe"  # rpe:<session id>:<1-10>      perceived effort of an activity

Translate = Callable[[str], str]
MAX_CALLBACK_BYTES = 64


def callback_data(prefix: str, *parts: str) -> str:
    data = ":".join((prefix, *parts))
    if len(data.encode()) > MAX_CALLBACK_BYTES:
        raise ValueError(f"callback data longer than {MAX_CALLBACK_BYTES} bytes: {data!r}")
    return data


def parse_callback(data: str | None, prefix: str) -> list[str] | None:
    """Payload parts if ``data`` starts with ``prefix``, else ``None``."""
    if not data:
        return None
    head, _, rest = data.partition(":")
    if head != prefix:
        return None
    return rest.split(":") if rest else []


def language_keyboard(translator: Translator) -> InlineKeyboardMarkup:
    """One button per available language, each labelled in its own language."""
    buttons = [
        InlineKeyboardButton(
            translator.t("meta.language_name", locale=locale),
            callback_data=callback_data(LANGUAGE_PREFIX, locale),
        )
        for locale in translator.available_locales
    ]
    return InlineKeyboardMarkup([buttons])


def checkin_keyboard(t: Translate, day: dt.date) -> InlineKeyboardMarkup:
    """Gym / Box / Rest / Other, two per row. The day travels in the callback data."""

    def button(choice: DayType) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            t(f"checkin.options.{choice.value}"),
            callback_data=callback_data(CHECKIN_PREFIX, day.isoformat(), choice.value),
        )

    return InlineKeyboardMarkup(
        [
            [button(DayType.GYM), button(DayType.BOX)],
            [button(DayType.REST), button(DayType.OTHER)],
        ]
    )


def parse_checkin(data: str | None, prefix: str = CHECKIN_PREFIX) -> tuple[dt.date, DayType] | None:
    """``(day, choice)`` from ``<prefix>:<date>:<type>`` data, or ``None`` if not valid."""
    parts = parse_callback(data, prefix)
    if not parts or len(parts) != 2:
        return None
    try:
        return dt.date.fromisoformat(parts[0]), DayType(parts[1])
    except ValueError:
        return None


def parse_day(data: str | None, prefix: str) -> dt.date | None:
    parts = parse_callback(data, prefix)
    if not parts or len(parts) != 1:
        return None
    try:
        return dt.date.fromisoformat(parts[0])
    except ValueError:
        return None


def parse_session_choice(data: str | None, prefix: str) -> tuple[int, DayType] | None:
    """``(session id, type)`` from ``<prefix>:<id>:<type>`` data."""
    parts = parse_callback(data, prefix)
    if not parts or len(parts) != 2 or not parts[0].isdigit():
        return None
    try:
        return int(parts[0]), DayType(parts[1])
    except ValueError:
        return None


def single_button(text: str, prefix: str, session_id: int) -> InlineKeyboardMarkup:
    button = InlineKeyboardButton(text, callback_data=callback_data(prefix, str(session_id)))
    return InlineKeyboardMarkup([[button]])


def review_keyboard(t: Translate, session_id: int) -> InlineKeyboardMarkup:
    """Confirm / Adjust under Claude's reading."""
    sid = str(session_id)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t("review.confirm_button"), callback_data=callback_data(REVIEW_OK_PREFIX, sid)
                ),
                InlineKeyboardButton(
                    t("review.adjust_button"),
                    callback_data=callback_data(REVIEW_ADJUST_PREFIX, sid),
                ),
            ]
        ]
    )


def raw_keyboard(t: Translate, session_id: int, retry: bool = False) -> InlineKeyboardMarkup:
    """ "Save without interpretation", and "Try again" after an API error."""
    sid = str(session_id)
    row = [
        InlineKeyboardButton(
            t("review.raw_button"), callback_data=callback_data(REVIEW_RAW_PREFIX, sid)
        )
    ]
    if retry:
        row.insert(
            0,
            InlineKeyboardButton(
                t("review.retry_button"), callback_data=callback_data(REVIEW_RETRY_PREFIX, sid)
            ),
        )
    return InlineKeyboardMarkup([row])


def rpe_keyboard(session_id: int) -> InlineKeyboardMarkup:
    """1-10 in two rows of five."""
    buttons = [
        InlineKeyboardButton(
            str(value), callback_data=callback_data(RPE_PREFIX, str(session_id), str(value))
        )
        for value in range(1, 11)
    ]
    return InlineKeyboardMarkup([buttons[:5], buttons[5:]])


def parse_rpe(data: str | None) -> tuple[int, int] | None:
    """``(session id, rpe)`` from ``rpe:<id>:<1-10>``."""
    parts = parse_callback(data, RPE_PREFIX)
    if not parts or len(parts) != 2 or not all(part.isdigit() for part in parts):
        return None
    session_id, value = int(parts[0]), int(parts[1])
    return (session_id, value) if 1 <= value <= 10 else None


def parse_session_id(data: str | None, prefix: str) -> int | None:
    parts = parse_callback(data, prefix)
    if not parts or len(parts) != 1 or not parts[0].isdigit():
        return None
    return int(parts[0])
