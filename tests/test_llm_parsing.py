import base64
import io

import pytest
from PIL import Image

from kide.catalog.movements import default_movements
from kide.llm.client import LlmClient
from kide.llm.parsing import AthleteInfo, Kind, Parser, ParseStatus, image_block
from kide.llm.prompts.parsing import athlete_context, parse_system_prompt
from kide.llm.schemas import Activity, Wod
from kide.storage import LlmCall, make_engine, make_session_factory, upgrade_database
from kide.storage.repositories import unit_of_work
from tests.llm_fakes import FakeSdk, make_message, tool_message

pytestmark = pytest.mark.anyio

ATHLETE = AthleteInfo(sex="male", language="es")

VALID_WOD = {
    "format": "amrap",
    "confidence": "high",
    "blocks": [
        {
            "format": "amrap",
            "duration_min": 12,
            "movements": [
                {"name": "T2B", "reps": 10},
                {"name": "Wall balls", "reps": 15, "load_kg": 9, "load_text": "9/6"},
            ],
        }
    ],
}
VALID_ACTIVITY = {
    "type": "hike",
    "duration_min": 180,
    "elevation_gain_m": 900,
    "confidence": "high",
}


@pytest.fixture
def setup(tmp_path):
    url = f"sqlite:///{(tmp_path / 'kide.db').as_posix()}"
    upgrade_database(url)
    factory = make_session_factory(make_engine(url))
    sdk = FakeSdk()
    llm = LlmClient(
        sdk,
        chat_model="claude-opus-5-5",
        parse_model="claude-haiku-5-5",
        session_factory=factory,
        user_id=None,
    )
    data_dir = tmp_path / "var"
    (data_dir / "photos").mkdir(parents=True)
    parser = Parser(llm, default_movements(), data_dir, max_image_edge=1000)
    return parser, sdk, factory, data_dir


def save_image(data_dir, name="board.jpg", size=(3000, 1500)):
    Image.new("RGB", size, "white").save(data_dir / "photos" / name, format="JPEG")
    return f"photos/{name}"


def purposes(factory):
    with unit_of_work(factory) as repos:
        return [c.purpose for c in repos.session.query(LlmCall).order_by(LlmCall.id)]


async def test_text_wod_is_parsed_and_normalised(setup):
    parser, sdk, factory, _ = setup
    sdk.queue(tool_message("record_wod", VALID_WOD, model="claude-haiku-5-5"))
    outcome = await parser.parse(
        Kind.WOD, text="AMRAP 12: 10 T2B, 15 WB", photo_paths=[], athlete=ATHLETE, session_id=None
    )
    assert outcome.status is ParseStatus.OK
    assert isinstance(outcome.item, Wod)
    assert [m.movement_id for m in outcome.item.movements()] == ["toes_to_bar", "wall_ball"]
    assert outcome.attempts == 1

    request = sdk.requests[0]
    assert request["model"] == "claude-haiku-5-5"
    assert [tool["name"] for tool in request["tools"]] == ["record_wod", "record_activity"]
    assert request["tool_choice"] == {"type": "tool", "name": "record_wod"}
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "sex male" in request["system"][1]["text"]
    assert "AMRAP 12: 10 T2B, 15 WB" in request["messages"][0]["content"][-1]["text"]
    assert purposes(factory) == ["wod_parse_text"]


async def test_photo_wod_sends_a_shrunk_jpeg(setup):
    parser, sdk, factory, data_dir = setup
    sdk.queue(tool_message("record_wod", VALID_WOD))
    path = save_image(data_dir)
    outcome = await parser.parse(Kind.WOD, text=None, photo_paths=[path], athlete=ATHLETE)
    assert outcome.ok
    request = sdk.requests[0]
    image = request["messages"][0]["content"][0]
    assert image["source"]["media_type"] == "image/jpeg"
    decoded = Image.open(io.BytesIO(base64.b64decode(image["source"]["data"])))
    assert max(decoded.size) == 1000
    assert request["timeout"] == 120.0
    assert purposes(factory) == ["wod_parse_photo"]


async def test_invalid_input_is_retried_with_the_errors(setup):
    parser, sdk, _, _ = setup
    bad = {**VALID_WOD, "confidence": "sure"}
    sdk.queue(tool_message("record_wod", bad), tool_message("record_wod", VALID_WOD))
    outcome = await parser.parse(Kind.WOD, text="AMRAP", photo_paths=[], athlete=ATHLETE)
    assert outcome.ok
    assert outcome.attempts == 2
    assert len(outcome.call_ids) == 2
    retry = sdk.requests[1]["messages"]
    assert retry[1]["role"] == "assistant"
    assert retry[1]["content"][0]["type"] == "tool_use"
    result = retry[2]["content"][0]
    assert result["type"] == "tool_result"
    assert result["is_error"] is True
    assert "confidence" in result["content"]


async def test_still_invalid_after_the_retry(setup):
    parser, sdk, _, _ = setup
    bad = {**VALID_WOD, "blocks": []}
    sdk.queue(tool_message("record_wod", bad), tool_message("record_wod", bad))
    outcome = await parser.parse(Kind.WOD, text="?", photo_paths=[], athlete=ATHLETE)
    assert outcome.status is ParseStatus.INVALID
    assert outcome.item is None
    assert outcome.attempts == 2


async def test_no_tool_call_is_invalid(setup):
    parser, sdk, _, _ = setup
    sdk.queue(make_message("I can't read this.", stop_reason="refusal"))
    outcome = await parser.parse(Kind.WOD, text="?", photo_paths=[], athlete=ATHLETE)
    assert outcome.status is ParseStatus.INVALID
    assert outcome.attempts == 1


async def test_nothing_to_read(setup):
    parser, sdk, _, data_dir = setup
    (data_dir / "photos" / "broken.jpg").write_bytes(b"not an image")
    outcome = await parser.parse(
        Kind.WOD,
        text="  ",
        photo_paths=["photos/broken.jpg", "photos/missing.jpg"],
        athlete=ATHLETE,
    )
    assert outcome.status is ParseStatus.NO_INPUT
    assert outcome.skipped_images == 2
    assert sdk.requests == []


async def test_activity_is_parsed(setup):
    parser, sdk, factory, data_dir = setup
    sdk.queue(tool_message("record_activity", VALID_ACTIVITY))
    path = save_image(data_dir, "watch.png", (600, 1200))
    outcome = await parser.parse(
        Kind.ACTIVITY, text="Gorbea con barro", photo_paths=[path], athlete=ATHLETE
    )
    assert isinstance(outcome.item, Activity)
    assert sdk.requests[0]["tool_choice"]["name"] == "record_activity"
    assert purposes(factory) == ["activity_parse"]


async def test_adjust_sends_the_reading_and_the_correction_but_no_photos(setup):
    parser, sdk, factory, _ = setup
    fixed = {**VALID_WOD, "blocks": [{**VALID_WOD["blocks"][0], "duration_min": 15}]}
    sdk.queue(tool_message("record_wod", fixed))
    current = Wod.model_validate(VALID_WOD)
    outcome = await parser.adjust(
        Kind.WOD,
        current=current,
        original_text="AMRAP 12",
        correction="Eran 15 minutos",
        athlete=ATHLETE,
        session_id=None,
    )
    assert outcome.item.blocks[0].duration_min == 15
    content = sdk.requests[0]["messages"][0]["content"]
    assert [block["type"] for block in content] == ["text"]
    assert "Eran 15 minutos" in content[0]["text"]
    assert '"duration_min": 12' in content[0]["text"]
    assert purposes(factory) == ["wod_adjust"]


def test_image_block_handles_exif_rotation(tmp_path):
    image = Image.new("RGB", (400, 200), "white")
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 degrees
    path = tmp_path / "rotated.jpg"
    image.save(path, format="JPEG", exif=exif)
    block = image_block(path, max_edge=1000)
    decoded = Image.open(io.BytesIO(base64.b64decode(block["source"]["data"])))
    assert decoded.size == (200, 400)


def test_prompt_contents():
    prompt = parse_system_prompt(default_movements())
    assert "toes_to_bar: Toes-to-bar; t2b" in prompt
    assert "never copy a person's name" in prompt
    assert "rpe" in prompt
    assert athlete_context(None, "en").startswith("Athlete: sex not stated")
    assert athlete_context("female", "es").endswith("Spanish.")
