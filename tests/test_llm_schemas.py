import pytest
from pydantic import ValidationError

from kide.catalog.movements import default_movements
from kide.llm.schemas import (
    Activity,
    ActivityType,
    ParsedRecord,
    RecordStatus,
    Wod,
    WodFormat,
    apply_catalog,
    tool_input_schema,
)


def wod(**overrides):
    data = {
        "format": "for_time",
        "confidence": "high",
        "blocks": [
            {
                "format": "for_time",
                "time_cap_min": 10,
                "movements": [
                    {"name": "Thrusters", "reps_per_round": [21, 15, 9], "load_text": "43/29"},
                    {"name": "Pull-ups", "movement_id": "pull_up", "reps_per_round": [21, 15, 9]},
                ],
            }
        ],
    }
    data.update(overrides)
    return data


def test_valid_wod():
    parsed = Wod.model_validate(wod(title="Fran"))
    assert parsed.format is WodFormat.FOR_TIME
    assert [m.name for m in parsed.movements()] == ["Thrusters", "Pull-ups"]


def test_format_becomes_mixed_when_main_blocks_differ_and_ignores_the_warmup():
    data = wod(format="amrap")
    data["blocks"].insert(
        0, {"format": "rounds", "is_warmup": True, "movements": [{"name": "row", "calories": 10}]}
    )
    data["blocks"].append(
        {"format": "strength", "sets": 5, "movements": [{"name": "BS", "reps": 5}]}
    )
    assert Wod.model_validate(data).format is WodFormat.MIXED
    data["blocks"].pop()
    data["format"] = "mixed"
    assert Wod.model_validate(data).format is WodFormat.FOR_TIME
    assert len(Wod.model_validate(data).movements(include_warmup=True)) == 3


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("blocks", 0, "movements", 0, "load_kg"), 4000),
        (("blocks", 0, "movements", 0, "distance_m"), -5),
        (("blocks", 0, "movements", 0, "reps"), 0),
        (("blocks", 0, "time_cap_min"), 0),
        (("blocks", 0, "format"), "tabata_ish"),
        (("confidence",), "maybe"),
        (("blocks", 0, "movements", 0, "colour"), "red"),
    ],
)
def test_misreadings_are_rejected(path, value):
    data = wod()
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValidationError):
        Wod.model_validate(data)


def test_reps_and_ladder_are_exclusive():
    data = wod()
    data["blocks"][0]["movements"][0]["reps"] = 5
    with pytest.raises(ValidationError, match="either reps or reps_per_round"):
        Wod.model_validate(data)


def test_empty_blocks_or_movements_are_rejected():
    with pytest.raises(ValidationError):
        Wod.model_validate(wod(blocks=[]))
    data = wod()
    data["blocks"][0]["movements"] = []
    with pytest.raises(ValidationError):
        Wod.model_validate(data)


def test_apply_catalog_fixes_ids():
    data = wod()
    data["blocks"][0]["movements"].append({"name": "Wall balls", "movement_id": "made_up"})
    data["blocks"][0]["movements"].append({"name": "Zumba"})
    fixed = apply_catalog(Wod.model_validate(data), default_movements())
    assert [m.movement_id for m in fixed.movements()] == ["thruster", "pull_up", "wall_ball", None]


def test_activity_pace_is_derived_for_runs_only():
    run = Activity(type=ActivityType.RUN, duration_min=50, distance_km=10, confidence="high")
    assert run.pace_s_per_km == 300
    stated = run.model_copy(update={"avg_pace_s_per_km": 290})
    assert stated.pace_s_per_km == 290
    bike = Activity(type=ActivityType.BIKE, duration_min=60, distance_km=30, confidence="high")
    assert bike.pace_s_per_km is None


@pytest.mark.parametrize(
    ("field", "value"), [("rpe", 11), ("rpe", 0), ("avg_hr_bpm", 300), ("distance_km", 0)]
)
def test_activity_bounds(field, value):
    with pytest.raises(ValidationError):
        Activity.model_validate({"type": "run", "confidence": "high", field: value})


def test_parsed_record_round_trip():
    item = Wod.model_validate(wod())
    record = ParsedRecord.draft(item)
    stored = record.to_json()
    assert stored["kind"] == "wod"
    assert stored["status"] == "draft"
    assert stored["schema_version"] == 1
    again = ParsedRecord.model_validate(stored)
    assert again.item() == item
    assert again.confirmed().status is RecordStatus.CONFIRMED

    activity = Activity(type=ActivityType.HIKE, duration_min=180, confidence="medium")
    assert ParsedRecord.draft(activity).item() == activity


def test_tool_schemas_have_no_refs():
    import json

    for model in (Wod, Activity):
        schema = tool_input_schema(model)
        assert "$ref" not in json.dumps(schema)
        assert schema["type"] == "object"
    wod_schema = tool_input_schema(Wod)
    movement = wod_schema["properties"]["blocks"]["items"]["properties"]["movements"]["items"]
    assert "movement_id" in movement["properties"]
