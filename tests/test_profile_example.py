from pathlib import Path

from kide.profile import load_profile

EXAMPLE = Path(__file__).resolve().parents[1] / "profile.example.yaml"


def test_example_profile_loads():
    profile = load_profile(EXAMPLE)
    assert [session.id for session in profile.training.rotation] == [
        "upper_a",
        "lower_a",
        "upper_b",
        "lower_b",
    ]
    assert profile.schedule.box.classes_per_week == 2
    assert profile.competitions[0].type == "hybrid_race"


def test_every_example_exercise_resolves():
    profile = load_profile(EXAMPLE)
    schemes = {profile.prescription(exercise).scheme for _, exercise in profile.iter_exercises()}
    assert schemes == {"top_set_backoff", "straight_sets"}


def test_example_uses_no_brand_names():
    text = EXAMPLE.read_text(encoding="utf-8").lower()
    assert "crossfit" not in text
    assert "hyrox" not in text
