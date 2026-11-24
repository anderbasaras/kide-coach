import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from kide.bot.timing import checkin_due_at, local_to_utc, next_checkin_at, should_send_checkin

MADRID = ZoneInfo("Europe/Madrid")
NEW_YORK = ZoneInfo("America/New_York")
NINE = dt.time(9, 0)


def utc(*args: int) -> dt.datetime:
    return dt.datetime(*args, tzinfo=dt.UTC)


# --- Next check-in -------------------------------------------------------------------


def test_today_if_still_ahead():
    # 07:00 UTC = 08:00 in Madrid (winter, UTC+1) -> today at 09:00 local = 08:00 UTC.
    assert next_checkin_at(utc(2026, 11, 24, 7, 0), NINE, MADRID) == utc(2026, 11, 24, 8, 0)


def test_tomorrow_once_passed_or_exactly_now():
    assert next_checkin_at(utc(2026, 11, 24, 8, 0), NINE, MADRID) == utc(2026, 11, 25, 8, 0)
    assert next_checkin_at(utc(2026, 11, 24, 20, 0), NINE, MADRID) == utc(2026, 11, 25, 8, 0)


def test_late_evening_utc_is_already_tomorrow_in_madrid():
    # 23:30 UTC on the 24th is 00:30 on the 25th in Madrid: the next one is the 25th.
    assert next_checkin_at(utc(2026, 11, 24, 23, 30), NINE, MADRID) == utc(2026, 11, 25, 8, 0)


def test_spring_forward_keeps_nine_local():
    # Europe/Madrid moves to UTC+2 on Sunday 2027-03-28.
    saturday_after_nine = utc(2027, 3, 27, 8, 0, 1)
    assert next_checkin_at(saturday_after_nine, NINE, MADRID) == utc(2027, 3, 28, 7, 0)
    assert utc(2027, 3, 28, 7, 0).astimezone(MADRID).time() == NINE


def test_fall_back_keeps_nine_local():
    # Europe/Madrid goes back to UTC+1 on Sunday 2026-10-25.
    saturday_after_nine = utc(2026, 10, 24, 7, 0, 1)
    assert next_checkin_at(saturday_after_nine, NINE, MADRID) == utc(2026, 10, 25, 8, 0)
    assert utc(2026, 10, 25, 8, 0).astimezone(MADRID).time() == NINE


def test_a_time_that_does_not_exist_moves_forward_by_the_gap():
    # 02:30 does not exist in Madrid on 2027-03-28 (02:00 -> 03:00): it becomes 03:30.
    due = local_to_utc(dt.date(2027, 3, 28), dt.time(2, 30), MADRID)
    assert due == utc(2027, 3, 28, 1, 30)
    assert due.astimezone(MADRID).time() == dt.time(3, 30)


def test_a_time_that_exists_twice_uses_the_first_one():
    # 02:30 happens twice in Madrid on 2026-10-25: first at UTC+2 (00:30 UTC).
    due = local_to_utc(dt.date(2026, 10, 25), dt.time(2, 30), MADRID)
    assert due == utc(2026, 10, 25, 0, 30)


def test_other_time_zones():
    # 09:00 in New York in winter is 14:00 UTC.
    assert checkin_due_at(dt.date(2026, 11, 24), NINE, NEW_YORK) == utc(2026, 11, 24, 14, 0)


def test_results_are_aware_utc():
    due = next_checkin_at(utc(2026, 11, 24, 7, 0), NINE, MADRID)
    assert due.tzinfo is dt.UTC


# --- One check-in per day --------------------------------------------------------------

DUE = utc(2026, 11, 24, 8, 0)  # 09:00 in Madrid


def send(now, sent=False, answered=False):
    return should_send_checkin(now, NINE, MADRID, already_sent=sent, already_answered=answered)


def test_not_before_the_time():
    assert not send(DUE - dt.timedelta(minutes=5))


def test_at_or_after_the_time():
    assert send(DUE)
    assert send(DUE + dt.timedelta(hours=3))


def test_a_job_firing_a_few_seconds_early_counts_as_on_time():
    assert send(DUE - dt.timedelta(seconds=30))


def test_started_late_in_the_day_asks_once():
    assert send(utc(2026, 11, 24, 21, 0))  # 22:00 in Madrid, not asked yet


@pytest.mark.parametrize(("sent", "answered"), [(True, False), (False, True), (True, True)])
def test_never_twice(sent, answered):
    assert not send(DUE + dt.timedelta(hours=1), sent=sent, answered=answered)


def test_after_midnight_the_new_day_waits_for_its_own_time():
    # 00:30 in Madrid on the 25th: yesterday is over, today's time has not come yet.
    assert not send(utc(2026, 11, 24, 23, 30))
