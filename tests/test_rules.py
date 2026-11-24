import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from kide.bot.keyboards import checkin_keyboard, parse_checkin, parse_session_id, single_button
from kide.bot.rules import (
    ADD_OPTIONS,
    AnswerVerdict,
    Flow,
    flow_after_choice,
    judge_answer,
    local_date,
    needs_session_pick,
    rest_to_replace,
)
from kide.storage import DayType

MADRID = ZoneInfo("Europe/Madrid")
DAY = dt.date(2026, 11, 23)


# --- Flow transitions --------------------------------------------------------------


@pytest.mark.parametrize(
    ("choice", "flow"),
    [
        (DayType.BOX, Flow.WOD),
        (DayType.GYM, Flow.NOTE),
        (DayType.REST, Flow.NOTE),
        (DayType.OTHER, Flow.NOTE),
    ],
)
def test_box_asks_for_the_wod_and_the_rest_for_a_note(choice, flow):
    assert flow_after_choice(choice) is flow


# --- The user's day ----------------------------------------------------------------


def test_local_date_uses_the_profile_time_zone():
    # 23:30 UTC on the 22nd is already the 23rd in Madrid (UTC+1 in winter).
    assert local_date(dt.datetime(2026, 11, 22, 23, 30, tzinfo=dt.UTC), MADRID) == DAY
    assert local_date(dt.datetime(2026, 11, 22, 22, 59, tzinfo=dt.UTC), MADRID) == DAY - (
        dt.timedelta(days=1)
    )


def test_local_date_rejects_naive_datetimes():
    with pytest.raises(ValueError, match="aware"):
        local_date(dt.datetime(2026, 11, 23, 9, 0), MADRID)


# --- Tapping a check-in button ---------------------------------------------------------


def test_a_fresh_answer_is_accepted():
    assert judge_answer(DAY, DAY, already_answered=False) is AnswerVerdict.ACCEPT


def test_yesterday_unanswered_button_logs_yesterday():
    assert judge_answer(DAY, DAY + dt.timedelta(days=1), False) is AnswerVerdict.ACCEPT


def test_an_answered_day_is_not_answered_again():
    assert judge_answer(DAY, DAY, already_answered=True) is AnswerVerdict.ALREADY_ANSWERED


def test_a_future_day_is_invalid():
    assert judge_answer(DAY + dt.timedelta(days=1), DAY, False) is AnswerVerdict.INVALID_DAY


# --- Check-in buttons ------------------------------------------------------------------


def test_checkin_keyboard_has_four_options_for_the_day():
    keyboard = checkin_keyboard(lambda key: key.rsplit(".", 1)[-1], DAY)
    rows = [[(b.text, b.callback_data) for b in row] for row in keyboard.inline_keyboard]
    assert rows == [
        [("gym", "ci:2026-11-23:gym"), ("box", "ci:2026-11-23:box")],
        [("rest", "ci:2026-11-23:rest"), ("other", "ci:2026-11-23:other")],
    ]


@pytest.mark.parametrize("choice", list(DayType))
def test_checkin_callback_round_trip(choice):
    data = checkin_keyboard(lambda k: k, DAY).inline_keyboard[0][0].callback_data
    assert parse_checkin(data) == (DAY, DayType.GYM)
    assert parse_checkin(f"ci:{DAY.isoformat()}:{choice.value}") == (DAY, choice)


@pytest.mark.parametrize(
    "data", [None, "", "ci", "ci:2026-11-23", "ci:2026-13-40:gym", "ci:2026-11-23:swim", "x:1"]
)
def test_broken_checkin_callbacks_are_rejected(data):
    assert parse_checkin(data) is None


def test_session_buttons():
    data = single_button("Done", "wod", 42).inline_keyboard[0][0].callback_data
    assert data == "wod:42"
    assert parse_session_id(data, "wod") == 42
    assert parse_session_id("wod:abc", "wod") is None
    assert parse_session_id("note:42", "wod") is None


# --- Correcting and adding sessions ----------------------------------------------------

REST, GYM, BOX, OTHER = DayType.REST, DayType.GYM, DayType.BOX, DayType.OTHER


def test_rest_cannot_be_added_as_a_second_session():
    assert REST not in ADD_OPTIONS
    with pytest.raises(ValueError, match="rest"):
        rest_to_replace([BOX], REST)


@pytest.mark.parametrize("new", [GYM, BOX])
def test_training_replaces_a_rest_day(new):
    assert rest_to_replace([REST], new) == 0
    assert rest_to_replace([OTHER, REST], new) == 1  # walk + rest, then gym: rest goes


def test_other_keeps_the_rest_as_active_rest():
    assert rest_to_replace([REST], OTHER) is None


@pytest.mark.parametrize("existing", [[], [BOX], [GYM, OTHER]])
@pytest.mark.parametrize("new", [GYM, BOX, OTHER])
def test_without_rest_sessions_are_appended(existing, new):
    assert rest_to_replace(existing, new) is None


def test_session_pick_only_with_several_sessions():
    assert not needs_session_pick(1)
    assert needs_session_pick(2)
