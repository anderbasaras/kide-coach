from pathlib import Path

import pytest
import yaml

from kide.cli import main
from kide.profile import ProfileError, load_profile, parse_profile


def problems_of(text: str) -> list[str]:
    with pytest.raises(ProfileError) as info:
        parse_profile(text)
    return info.value.problems


def test_loads_valid_file(tmp_path, profile_data):
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump(profile_data), encoding="utf-8")
    assert load_profile(path).identity.name == "Alex"


def test_missing_file_explains_what_to_do(tmp_path):
    with pytest.raises(ProfileError, match=r"Copy profile\.example\.yaml"):
        load_profile(tmp_path / "nope.yaml")


def test_empty_file():
    assert problems_of("# only a comment\n") == ["the file is empty"]


def test_top_level_must_be_mapping():
    assert "top level must be a mapping" in problems_of("- a\n- b\n")[0]


def test_yaml_syntax_error_has_line():
    text = "identity:\n  name: Alex\n  language: [en\n"
    (problem,) = problems_of(text)
    assert problem.startswith("line 4")
    assert "invalid YAML" in problem


def test_duplicated_key_is_reported():
    text = "identity:\n  name: Alex\n  name: Bea\n"
    assert problems_of(text) == ["line 3 · identity.name: duplicated key"]


def test_errors_have_path_and_line(profile_data):
    profile_data["body"]["wieght_kg"] = 70
    profile_data["identity"]["timezone"] = "Mars/Base"
    text = yaml.safe_dump(profile_data, sort_keys=False)
    problems = problems_of(text)
    lines = text.splitlines()

    by_path = {problem.split(" · ")[1].split(":")[0]: problem for problem in problems}
    timezone = by_path["identity.timezone"]
    assert "unknown time zone 'Mars/Base'" in timezone
    timezone_line = int(timezone.split(" ")[1])
    assert lines[timezone_line - 1].strip().startswith("timezone:")
    assert "unknown field (check the spelling)" in by_path["body.wieght_kg"]


def test_missing_section_is_explained(profile_data):
    del profile_data["goals"]
    (problem,) = problems_of(yaml.safe_dump(profile_data))
    assert problem.endswith("goals: required field is missing")


def test_exercise_problems_are_listed_one_per_line(profile_data):
    rotation = profile_data["training"]["rotation"]
    del rotation[0]["exercises"][0]["backoff_drop_pct"]
    rotation[1]["exercises"][0]["current_load"] = {"top_kg": 10}
    (problem,) = problems_of(yaml.safe_dump(profile_data))
    assert "session 'upper', exercise 'bench_press'" in problem
    assert "session 'lower', exercise 'goblet_squat'" in problem


def test_error_message_lists_everything(profile_data):
    profile_data["identity"]["timezone"] = "Nowhere"
    profile_data["goals"]["primary"] = "crossfit"
    with pytest.raises(ProfileError) as info:
        parse_profile(yaml.safe_dump(profile_data))
    error = info.value
    assert len(error.problems) == 2
    assert str(error).startswith("Invalid profile (<profile>):\n  - ")


# --- CLI ---------------------------------------------------------------------------


def test_cli_profile_check_ok(tmp_path: Path, profile_data, capsys):
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump(profile_data), encoding="utf-8")
    assert main(["profile", "check", str(path)]) == 0
    assert "rotation: upper -> lower (3 exercises)" in capsys.readouterr().out


def test_cli_profile_check_error(tmp_path: Path, capsys):
    path = tmp_path / "profile.yaml"
    path.write_text("identity:\n  name: Alex\n", encoding="utf-8")
    assert main(["profile", "check", str(path)]) == 1
    assert "required field is missing" in capsys.readouterr().err
