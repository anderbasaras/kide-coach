"""Pure rules of the daily check-in: no Telegram, no database, no clock.

Everything here takes plain values (dates, choices, flags) so it can be tested
exhaustively. The handlers in ``bot/handlers`` feed it and act on the result.
"""

from __future__ import annotations

import datetime as dt
import enum
from collections.abc import Sequence
from zoneinfo import ZoneInfo

from kide.storage.models import DayType


class Flow(enum.IntEnum):
    """Conversation states after the user picks an option."""

    WOD = 1  # box: waiting for the WOD (texts and/or photos) until "Done"
    NOTE = 2  # gym / rest / other: waiting for an optional note


def flow_after_choice(choice: DayType) -> Flow:
    """Box asks for the WOD; every other option asks for an optional note."""
    return Flow.WOD if choice is DayType.BOX else Flow.NOTE


def local_date(now: dt.datetime, tz: ZoneInfo) -> dt.date:
    """The user's calendar day: a check-in belongs to the date in the profile time zone."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(tz).date()


class AnswerVerdict(enum.StrEnum):
    """What to do with a tap on a check-in button."""

    ACCEPT = "accept"
    ALREADY_ANSWERED = "already_answered"  # that day already has a session: use /today
    INVALID_DAY = "invalid_day"  # a date in the future (forged or broken button)


def judge_answer(button_day: dt.date, today: dt.date, already_answered: bool) -> AnswerVerdict:
    """A button answers the check-in of the day it was sent for, even if tapped later.

    Answering yesterday's question at 00:30 logs it for yesterday, which is what happened.
    A day that already has an answer is changed with /today (correct / add a session),
    not by tapping an old button again.
    """
    if button_day > today:
        return AnswerVerdict.INVALID_DAY
    if already_answered:
        return AnswerVerdict.ALREADY_ANSWERED
    return AnswerVerdict.ACCEPT


# --- Correcting a day and adding sessions ---------------------------------------------

# A second session can be gym, box or other; "rest" is only reachable by correcting.
ADD_OPTIONS = (DayType.GYM, DayType.BOX, DayType.OTHER)
TRAINING_TYPES = frozenset({DayType.GYM, DayType.BOX})


def rest_to_replace(existing: Sequence[DayType], new: DayType) -> int | None:
    """Index of the rest session that a new session replaces, or None to append it.

    Gym or box on a day logged as rest contradicts it, so the rest is replaced. Other
    (a walk, a hike) keeps the rest: that is active rest, and the engine tells them
    apart later.
    """
    if new not in ADD_OPTIONS:
        raise ValueError(f"{new.value} cannot be added as a second session")
    if new not in TRAINING_TYPES:
        return None
    for index, day_type in enumerate(existing):
        if day_type is DayType.REST:
            return index
    return None


def needs_session_pick(session_count: int) -> bool:
    """With one session, "Correct" goes straight to it; with several, the user picks."""
    return session_count > 1
