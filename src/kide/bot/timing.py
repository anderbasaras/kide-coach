"""When the daily check-in is due, in the profile time zone. Pure: the caller passes ``now``.

The check-in time is a *local* wall-clock time (``schedule.checkin_time``, e.g. 09:00).
Every calculation starts from the local date, so 09:00 stays 09:00 after a daylight
saving change. Two edge cases, both on the change days:

- a time that does not exist (spring forward, e.g. 02:30 in Europe/Madrid on the last
  Sunday of March) moves forward by the gap: 02:30 -> 03:30;
- a time that exists twice (fall back, e.g. 02:30 on the last Sunday of October) uses
  the first occurrence.

Both follow from ``fold=0`` in :mod:`zoneinfo` (PEP 495). All results are aware UTC
datetimes, which is what the scheduler and the database use (D-024).
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from kide.bot.rules import local_date

# The job may fire a little before the due time (clock resolution); treat it as on time.
EARLY_TOLERANCE = dt.timedelta(seconds=60)


def local_to_utc(day: dt.date, at: dt.time, tz: ZoneInfo) -> dt.datetime:
    """``day`` at local wall time ``at`` in ``tz``, as UTC (gap -> forward, overlap -> first)."""
    local = dt.datetime.combine(day, at.replace(tzinfo=None), tzinfo=tz).replace(fold=0)
    return local.astimezone(dt.UTC)


def checkin_due_at(day: dt.date, checkin_time: dt.time, tz: ZoneInfo) -> dt.datetime:
    """When the check-in of ``day`` is due, in UTC."""
    return local_to_utc(day, checkin_time, tz)


def next_checkin_at(now: dt.datetime, checkin_time: dt.time, tz: ZoneInfo) -> dt.datetime:
    """The next due time strictly after ``now``: today's if still ahead, else tomorrow's."""
    today = local_date(now, tz)
    due = checkin_due_at(today, checkin_time, tz)
    if due > now:
        return due
    return checkin_due_at(today + dt.timedelta(days=1), checkin_time, tz)


def should_send_checkin(
    now: dt.datetime,
    checkin_time: dt.time,
    tz: ZoneInfo,
    *,
    already_sent: bool,
    already_answered: bool,
) -> bool:
    """One check-in per day: send once today's time has passed, unless asked or answered.

    This one rule covers the scheduled job, a start after the check-in time and the
    backup check: if the question was already sent today (scheduled or with /today) or
    the day is answered, nothing is sent again.
    """
    if already_sent or already_answered:
        return False
    due = checkin_due_at(local_date(now, tz), checkin_time, tz)
    return now >= due - EARLY_TOLERANCE
