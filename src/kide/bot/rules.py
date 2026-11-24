"""Pure rules of the daily check-in: no Telegram, no database, no clock.

Everything here takes plain values (dates, choices, flags) so it can be tested
exhaustively. The handlers in ``bot/handlers`` feed it and act on the result.
"""

from __future__ import annotations

import datetime as dt
import enum
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
