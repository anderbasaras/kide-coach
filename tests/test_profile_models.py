import datetime as dt

import pytest
from pydantic import ValidationError

from kide.profile import Profile
from tests.conftest import exercise


def validate(data) -> Profile:
    return Profile.model_validate(data)


def error_text(data) -> str:
    with pytest.raises(ValidationError) as info:
        validate(data)
    return str(info.value)


# --- Valid profiles ---------------------------------------------------------


def test_valid_profile_and_defaults(profile_data):
    profile = validate(profile_data)
    assert profile.schema_version == 1
    assert profile.schedule.checkin_time == dt.time(9, 0)
    assert profile.schedule.weekly_summary.day == "monday"
    assert profile.weight_logging.default_mode == "per_side"
    assert [s.id for s in profile.training.rotation] == ["upper", "lower"]


def test_minimal_profile_needs_identity_goals_and_training(profile_data):
    minimal = {key: profile_data[key] for key in ("identity", "goals", "training")}
    assert validate(minimal).body.weight_kg is None
    assert "identity" in error_text({"goals": profile_data["goals"]})


# --- Prescription inheritance (D-012) ---------------------------------------


def test_exercise_inherits_defaults(profile_data):
    profile = validate(profile_data)
    _, pulldown = list(profile.iter_exercises())[1]
    rx = profile.prescription(pulldown)
    assert rx.scheme == "straight_sets"
    assert (rx.sets, str(rx.reps), rx.load_increment_kg) == (3, "10-12", 2.5)
    assert rx.loading.stack_kg == [45, 50, 55]  # sorted


def test_overrides_and_rir_merge_key_by_key(profile_data):
    profile = validate(profile_data)
    _, bench = next(profile.iter_exercises())
    rx = profile.prescription(bench)
    assert rx.scheme == "top_set_backoff"
    assert str(rx.top_set_reps) == "6-8"
    assert rx.sets is None  # inherited straight-set fields are dropped
    assert str(rx.rir.top_set) == "0-1"
    assert str(rx.rir.last_set) == "0"  # inherited from defaults


def test_loading_defaults_to_weight_logging(profile_data):
    profile_data["weight_logging"] = {"default_mode": "per_dumbbell"}
    del exercise(profile_data, 1, 0)["loading"]
    profile = validate(profile_data)
    _, goblet = list(profile.iter_exercises())[2]
    assert profile.prescription(goblet).loading.mode == "per_dumbbell"


def test_missing_prescription_field_names_session_and_exercise(profile_data):
    del exercise(profile_data)["top_set_reps"]
    text = error_text(profile_data)
    assert "session 'upper', exercise 'bench_press'" in text
    assert "top_set_reps" in text


def test_field_of_the_other_scheme_is_rejected(profile_data):
    exercise(profile_data, 0, 1)["top_set_reps"] = 8
    assert "top_set_reps does not apply to straight_sets" in error_text(profile_data)


def test_bodyweight_needs_no_increment(profile_data):
    del profile_data["training"]["defaults"]["load_increment_kg"]
    rotation = profile_data["training"]["rotation"]
    rotation[:] = [
        {
            "id": "core",
            "name": "Core",
            "exercises": [{"id": "plank", "name": "Plank", "loading": {"mode": "bodyweight"}}],
        }
    ]
    validate(profile_data)


# --- current_load (D-015) -----------------------------------------------------


def test_current_load_must_match_scheme(profile_data):
    exercise(profile_data)["current_load"] = {"working_kg": 30}
    assert "current_load.working_kg does not apply to top_set_backoff" in error_text(profile_data)


def test_added_weight_needs_bodyweight_and_allows_negative(profile_data):
    exercise(profile_data, 1, 0).update(
        loading={"mode": "added_weight"}, current_load={"working_kg": -10}
    )
    validate(profile_data)
    del profile_data["body"]["weight_kg"]
    assert "needs body.weight_kg" in error_text(profile_data)


def test_negative_load_rejected_for_plates(profile_data):
    exercise(profile_data)["current_load"]["top_kg"] = -5
    assert "cannot be negative" in error_text(profile_data)


# --- Loading -----------------------------------------------------------------


def test_base_weight_only_for_plates(profile_data):
    exercise(profile_data, 0, 1)["loading"]["base_weight_kg"] = 5
    assert "base_weight_kg only applies to per_side or total" in error_text(profile_data)


def test_stack_only_for_machines(profile_data):
    exercise(profile_data)["loading"]["stack_kg"] = [10, 20]
    assert "stack_kg only applies to machine_stack" in error_text(profile_data)


# --- Ranges --------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "expected"), [(8, "8"), ("6-8", "6-8"), ([6, 8], "6-8")])
def test_range_formats(profile_data, raw, expected):
    exercise(profile_data)["top_set_reps"] = raw
    profile = validate(profile_data)
    assert str(profile.prescription(profile.training.rotation[0].exercises[0]).top_set_reps) == (
        expected
    )


@pytest.mark.parametrize(
    ("raw", "message"),
    [("8-6", "min (8) is greater than max (6)"), ("six", "range like '6-8'"), (0, "between 1")],
)
def test_bad_ranges(profile_data, raw, message):
    exercise(profile_data)["top_set_reps"] = raw
    assert message in error_text(profile_data)


# --- Rotation --------------------------------------------------------------------


def test_rotation_cannot_be_empty(profile_data):
    profile_data["training"]["rotation"] = []
    assert "at least 1 item" in error_text(profile_data)


def test_session_ids_unique(profile_data):
    profile_data["training"]["rotation"][1]["id"] = "upper"
    assert "repeated session ids: upper" in error_text(profile_data)


def test_exercise_ids_unique_within_session(profile_data):
    exercise(profile_data, 0, 1)["id"] = "bench_press"
    assert "repeated exercise ids: bench_press" in error_text(profile_data)


def test_ids_are_slugs(profile_data):
    profile_data["training"]["rotation"][0]["id"] = "Torso A"
    assert "is not a valid id" in error_text(profile_data)


def test_superset_needs_two_exercises(profile_data):
    exercise(profile_data)["superset"] = "A"
    assert "superset groups with a single exercise: A" in error_text(profile_data)
    exercise(profile_data, 0, 1)["superset"] = "A"
    validate(profile_data)


# --- Block (D-013) and rules (D-014) ----------------------------------------------


def test_block_is_represented(profile_data):
    profile_data["training"]["block"] = {
        "weeks": 5,
        "deload_week": 5,
        "deload": {"load_pct": 80, "sets": 2, "rir": 4},
    }
    profile_data["training"]["planning_rules"] = ["No heavy legs the day after a class"]
    profile = validate(profile_data)
    assert profile.training.block is not None
    assert profile.training.block.deload is not None
    assert profile.training.block.deload.load_pct == 80


def test_deload_week_after_block_end(profile_data):
    profile_data["training"]["block"] = {"weeks": 4, "deload_week": 5, "deload": {"load_pct": 80}}
    assert "deload_week (5) is after the last week (4)" in error_text(profile_data)


# --- Identity and schedule ------------------------------------------------------


def test_timezone_typo_gets_suggestion(profile_data):
    profile_data["identity"]["timezone"] = "Europe/Madird"
    assert "Did you mean 'Europe/Madrid'?" in error_text(profile_data)


def test_unquoted_time_gets_hint(profile_data):
    profile_data["schedule"] = {"checkin_time": 570}  # what YAML makes of 9:30
    assert "write the time in quotes" in error_text(profile_data)


@pytest.mark.parametrize("raw", ["25:00", "9h", "09:60"])
def test_invalid_times(profile_data, raw):
    profile_data["schedule"] = {"checkin_time": raw}
    assert "24-hour time" in error_text(profile_data)


def test_times_parse(profile_data):
    profile_data["schedule"] = {
        "checkin_time": "7:30",
        "weekly_summary": {"day": "sunday", "time": "20:00"},
    }
    schedule = validate(profile_data).schedule
    assert schedule.checkin_time == dt.time(7, 30)
    assert schedule.weekly_summary.time == dt.time(20, 0)


# --- Neutral values (principle 6) ---------------------------------------------------


@pytest.mark.parametrize(
    ("section", "value", "hint"),
    [
        ("class_types", "crossfit", "functional_fitness"),
        ("class_types", "HYROX", "hybrid_race"),
    ],
)
def test_brand_names_rejected_with_hint(profile_data, section, value, hint):
    profile_data["schedule"] = {"box": {section: [value]}}
    assert f"use the neutral value '{hint}'" in error_text(profile_data)


def test_goal_brand_hint(profile_data):
    profile_data["goals"]["secondary"] = ["hyrox_performance"]
    assert "'hybrid_race'" in error_text(profile_data)


def test_primary_goal_not_repeated(profile_data):
    profile_data["goals"]["secondary"] = ["hypertrophy"]
    assert "already the primary goal" in error_text(profile_data)


# --- Other sections -------------------------------------------------------------------


def test_unknown_field_is_rejected(profile_data):
    profile_data["body"]["wieght_kg"] = 70
    assert "wieght_kg" in error_text(profile_data)


def test_competitions_experience_lifestyle(profile_data):
    profile_data["competitions"] = [
        {"name": "City Hybrid", "date": dt.date(2027, 3, 14), "type": "hybrid_race"}
    ]
    profile_data["experience"] = {
        "gym_years": 6,
        "box_level": "beginner",
        "past_competitions": [
            {"name": "Spring Throwdown", "when": "2026-04", "type": "functional_fitness"}
        ],
    }
    profile_data["lifestyle"] = {"sleep_hours": 7.5, "daily_steps": 8000, "job_activity": "mixed"}
    profile = validate(profile_data)
    assert profile.competitions[0].date == dt.date(2027, 3, 14)
    assert profile.experience.past_competitions[0].when == "2026-04"


def test_partial_date_format(profile_data):
    profile_data["experience"] = {
        "past_competitions": [{"name": "X", "when": "2026-13", "type": "other"}]
    }
    assert "expected YYYY, YYYY-MM or YYYY-MM-DD" in error_text(profile_data)


def test_maxes(profile_data):
    profile_data["maxes"] = [
        {"exercise": "bench_press", "value_kg": 80},
        {"exercise": "pull_up", "kind": "rep_max", "value_kg": 10, "reps": 8},
    ]
    assert len(validate(profile_data).maxes) == 2
    profile_data["maxes"][0]["reps"] = 3
    assert "use kind: rep_max" in error_text(profile_data)


def test_volume_targets(profile_data):
    profile_data["volume_targets"] = {"chest": "10-16", "back": 14}
    targets = validate(profile_data).volume_targets
    assert str(targets["chest"]) == "10-16"
    profile_data["volume_targets"] = {"chest": "10-80"}
    assert "weekly sets must be between 0 and 60" in error_text(profile_data)
