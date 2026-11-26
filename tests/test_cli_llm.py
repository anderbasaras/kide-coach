"""`kide llm check` and `kide llm costs`."""

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from kide import cli
from kide.config import get_settings
from kide.llm.client import LlmClient
from kide.storage import make_engine, make_session_factory, upgrade_database
from kide.storage.repositories import unit_of_work
from tests.llm_fakes import FakeSdk, api_error, make_message

EXAMPLE_PROFILE = Path(__file__).parent.parent / "profile.example.yaml"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "ANTHROPIC_PARSE_MODEL", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "var"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def factory():
    url = get_settings().database_url
    upgrade_database(url)
    return make_session_factory(make_engine(url))


def test_check_names_missing_settings(capsys):
    assert cli.main(["llm", "check"]) == 1
    assert "ANTHROPIC_API_KEY, ANTHROPIC_MODEL" in capsys.readouterr().err


def test_check_calls_each_distinct_model(capsys, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-opus-5-5")
    monkeypatch.setenv("ANTHROPIC_PARSE_MODEL", "claude-haiku-5-5")
    sdk = FakeSdk(make_message(model="claude-opus-5-5"), make_message(model="claude-haiku-5-5"))
    original = LlmClient.from_settings

    def fake_from_settings(settings, session_factory, user_id):
        client = original(settings, session_factory, user_id)
        client._sdk = sdk
        return client

    monkeypatch.setattr(LlmClient, "from_settings", staticmethod(fake_from_settings))
    assert cli.main(["llm", "check"]) == 0
    out = capsys.readouterr().out
    assert "OK chat: claude-opus-5-5" in out
    assert "OK parsing: claude-haiku-5-5" in out
    assert [r["model"] for r in sdk.requests] == ["claude-opus-5-5", "claude-haiku-5-5"]


def test_costs_empty(capsys):
    assert cli.main(["llm", "costs"]) == 0
    assert "No Claude calls recorded" in capsys.readouterr().out


def test_costs_groups_by_purpose_and_model(capsys):
    with unit_of_work(factory()) as repos:
        for cost in ("0.010000", "0.020000"):
            repos.llm_calls.record(
                user_id=None,
                session_id=None,
                purpose="wod_parse_photo",
                model="claude-haiku-5-5",
                input_tokens=1000,
                output_tokens=200,
                estimated_cost_usd=Decimal(cost),
                latency_ms=900,
            )
        repos.llm_calls.record(
            user_id=None,
            session_id=None,
            purpose="chat",
            model="claude-opus-5-5",
            error="timeout: APITimeoutError",
        )
        old = repos.llm_calls.record(
            user_id=None,
            session_id=None,
            purpose="chat",
            model="claude-opus-5-5",
            estimated_cost_usd=Decimal("1"),
        )
        old.created_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=40)

    assert cli.main(["llm", "costs"]) == 0
    out = capsys.readouterr().out
    photo_line = next(line for line in out.splitlines() if line.startswith("wod_parse_photo"))
    assert "0.015000" in photo_line  # mean
    assert "0.030000" in photo_line  # total
    assert "Total: $1.0300" in out

    assert cli.main(["llm", "costs", "--days", "30"]) == 0
    out = capsys.readouterr().out
    chat_line = next(line for line in out.splitlines() if line.startswith("chat"))
    assert chat_line.split()[2:4] == ["1", "1"]  # one call, failed
    assert "Total: $0.0300" in out


def test_check_prints_the_api_reason_on_failure(capsys, monkeypatch):
    import anthropic

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-opus-5-5")
    sdk = FakeSdk(api_error(anthropic.BadRequestError, 400, "Your credit balance is too low"))
    original = LlmClient.from_settings

    def fake_from_settings(settings, session_factory, user_id):
        client = original(settings, session_factory, user_id)
        client._sdk = sdk
        return client

    monkeypatch.setattr(LlmClient, "from_settings", staticmethod(fake_from_settings))
    assert cli.main(["llm", "check"]) == 1
    err = capsys.readouterr().err
    assert "FAIL chat + parsing: claude-opus-5-5 -> bad_request: Your credit balance" in err


def test_parse_prints_summary_json_and_cost(capsys, monkeypatch, tmp_path):
    from PIL import Image

    from tests.llm_fakes import tool_message

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-opus-5-5")
    monkeypatch.setenv("ANTHROPIC_PARSE_MODEL", "claude-haiku-5-5")
    monkeypatch.setenv("PROFILE_PATH", str(EXAMPLE_PROFILE))
    wod = {
        "format": "for_time",
        "confidence": "high",
        "blocks": [{"format": "for_time", "movements": [{"name": "Burpees", "reps": 50}]}],
    }
    sdk = FakeSdk(tool_message("record_wod", wod, model="claude-haiku-5-5", input_tokens=900))
    original = LlmClient.from_settings

    def fake_from_settings(settings, session_factory, user_id):
        client = original(settings, session_factory, user_id)
        client._sdk = sdk
        return client

    monkeypatch.setattr(LlmClient, "from_settings", staticmethod(fake_from_settings))
    photo = tmp_path / "board.jpg"
    Image.new("RGB", (100, 100)).save(photo)
    code = cli.main(["parse", "wod", "--text", "50 burpees", "--photo", str(photo), "--json"])
    out = capsys.readouterr().out
    assert code == 0
    assert "status: ok" in out
    assert "• 50 Burpee" in out
    assert '"movement_id": "burpee"' in out
    assert "call 1: in 900" in out
    assert sdk.requests[0]["messages"][0]["content"][0]["type"] == "image"


def test_parse_rejects_missing_photos(capsys, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-opus-5-5")
    monkeypatch.setenv("PROFILE_PATH", str(EXAMPLE_PROFILE))
    assert cli.main(["parse", "wod", "--photo", "nope.jpg"]) == 1
    assert "file not found" in capsys.readouterr().err
