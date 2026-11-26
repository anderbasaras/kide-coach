import datetime as dt
import logging

import anthropic
import httpx2
import pytest

from kide.config import Settings
from kide.llm.client import (
    LlmClient,
    LlmError,
    LlmErrorKind,
    Purpose,
    cached_system,
    classify_error,
)
from kide.storage import DayType, LlmCall, make_engine, make_session_factory, upgrade_database
from kide.storage.repositories import unit_of_work
from tests.llm_fakes import FakeSdk, api_error, make_message, tool_message

pytestmark = pytest.mark.anyio


@pytest.fixture
def factory(tmp_path):
    url = f"sqlite:///{(tmp_path / 'kide.db').as_posix()}"
    upgrade_database(url)
    return make_session_factory(make_engine(url))


def make_client(factory, sdk, **kwargs):
    ticks = iter([10.0, 10.25, 20.0, 20.5, 30.0, 30.5])
    return LlmClient(
        sdk,
        chat_model="claude-opus-5-5",
        parse_model="claude-haiku-5-5",
        session_factory=factory,
        user_id=None,
        clock=lambda: next(ticks),
        **kwargs,
    )


def calls(factory):
    with unit_of_work(factory) as repos:
        return list(repos.session.query(LlmCall).order_by(LlmCall.id))


async def test_call_sends_the_request_and_records_tokens_and_cost(factory):
    sdk = FakeSdk(
        make_message(
            "Hola",
            model="claude-opus-5-5",
            input_tokens=50,
            output_tokens=10,
            cache_write=2000,
            cache_read=0,
        )
    )
    client = make_client(factory, sdk)
    result = await client.call(
        purpose=Purpose.CHAT,
        model=client.chat_model,
        system=cached_system("stable rules", "today's context"),
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=500,
    )
    assert result.text == "Hola"
    assert result.latency_ms == 250
    request = sdk.requests[0]
    assert request["model"] == "claude-opus-5-5"
    assert request["max_tokens"] == 500
    assert "tools" not in request
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in request["system"][1]

    [row] = calls(factory)
    assert (row.purpose, row.model, row.success) == ("chat", "claude-opus-5-5", True)
    assert (row.input_tokens, row.output_tokens) == (50, 10)
    assert (row.cache_creation_input_tokens, row.cache_read_input_tokens) == (2000, 0)
    # 50*4 + 2000*5 + 10*20 per million
    assert float(row.estimated_cost_usd) == pytest.approx(0.0104)
    assert row.latency_ms == 250
    assert result.call_id == row.id


async def test_tools_and_tool_choice_are_passed_and_tool_uses_read(factory, tmp_path):
    sdk = FakeSdk(tool_message("record_wod", {"format": "amrap"}))
    client = make_client(factory, sdk)
    tools = [{"name": "record_wod", "description": "d", "input_schema": {"type": "object"}}]
    result = await client.call(
        purpose=Purpose.WOD_PARSE_TEXT,
        model=client.parse_model,
        system=cached_system("rules"),
        messages=[{"role": "user", "content": "AMRAP 12"}],
        max_tokens=100,
        tools=tools,
        tool_choice={"type": "tool", "name": "record_wod"},
        timeout_s=5,
    )
    assert sdk.requests[0]["tools"] == tools
    assert sdk.requests[0]["tool_choice"] == {"type": "tool", "name": "record_wod"}
    assert sdk.requests[0]["timeout"] == 5
    assert [use.input for use in result.tool_uses("record_wod")] == [{"format": "amrap"}]
    assert result.tool_uses("other") == []


async def test_session_id_is_stored(factory):
    sdk = FakeSdk(make_message())
    client = make_client(factory, sdk)
    with unit_of_work(factory) as repos:
        user = repos.users.get_or_create("Alex")
        checkin = repos.checkins.get_or_create(user.id, dt.date(2026, 1, 1))
        session_id = repos.sessions.create(checkin, DayType.BOX).id
    await client.call(
        purpose=Purpose.WOD_PARSE_PHOTO,
        model="claude-haiku-5-5",
        system=[],
        messages=[],
        max_tokens=10,
        session_id=session_id,
    )
    with unit_of_work(factory) as repos:
        assert [c.purpose for c in repos.llm_calls.for_session(session_id)] == ["wod_parse_photo"]


async def test_failed_call_is_recorded_without_the_api_message(factory, caplog):
    sdk = FakeSdk(api_error(anthropic.RateLimitError, 429, "slow down please"))
    client = make_client(factory, sdk)
    with caplog.at_level(logging.INFO, logger="kide"), pytest.raises(LlmError) as info:
        await client.call(
            purpose=Purpose.CHAT, model="claude-opus-5-5", system=[], messages=[], max_tokens=10
        )
    assert info.value.kind is LlmErrorKind.RATE_LIMIT
    [row] = calls(factory)
    assert row.success is False
    assert row.error == "rate_limit: RateLimitError (429)"
    assert info.value.call_id == row.id
    assert info.value.detail == "slow down please"  # for operator tools only
    assert "slow down" not in caplog.text
    assert "slow down" not in (row.error or "")


async def test_unknown_model_price_logs_a_warning(factory, caplog):
    sdk = FakeSdk(make_message(model="claude-new-model"))
    client = make_client(factory, sdk)
    with caplog.at_level(logging.INFO, logger="kide"):
        result = await client.call(
            purpose=Purpose.CHECK, model="claude-new-model", system=[], messages=[], max_tokens=5
        )
    assert result.cost_usd is None
    assert "No price known" in caplog.text
    assert calls(factory)[0].estimated_cost_usd is None


async def test_logs_have_counters_but_no_content(factory, caplog):
    sdk = FakeSdk(make_message("SECRET ANSWER"))
    client = make_client(factory, sdk)
    with caplog.at_level(logging.DEBUG):
        await client.call(
            purpose=Purpose.CHAT,
            model="claude-sonnet-5-5",
            system=cached_system("SECRET SYSTEM"),
            messages=[{"role": "user", "content": "SECRET QUESTION"}],
            max_tokens=10,
        )
    assert "purpose=chat" in caplog.text
    assert "SECRET" not in caplog.text


def _request():
    return httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (lambda: anthropic.APITimeoutError(request=_request()), LlmErrorKind.TIMEOUT),
        (lambda: anthropic.APIConnectionError(request=_request()), LlmErrorKind.CONNECTION),
        (lambda: api_error(anthropic.RateLimitError, 429), LlmErrorKind.RATE_LIMIT),
        (lambda: api_error(anthropic.OverloadedError, 529), LlmErrorKind.OVERLOADED),
        (lambda: api_error(anthropic.AuthenticationError, 401), LlmErrorKind.AUTH),
        (lambda: api_error(anthropic.PermissionDeniedError, 403), LlmErrorKind.AUTH),
        (lambda: api_error(anthropic.BadRequestError, 400), LlmErrorKind.BAD_REQUEST),
        (lambda: api_error(anthropic.InternalServerError, 500), LlmErrorKind.OTHER),
    ],
)
def test_error_classification(error, kind):
    assert classify_error(error()) is kind


def test_from_settings_needs_key_and_model(factory):
    no_key = Settings(_env_file=None, anthropic_model="claude-opus-5-5")
    no_model = Settings(_env_file=None, anthropic_api_key="sk-test")
    assert LlmClient.from_settings(no_key, factory, None) is None
    assert LlmClient.from_settings(no_model, factory, None) is None

    same = Settings(_env_file=None, anthropic_api_key="sk-test", anthropic_model="claude-opus-5-5")
    client = LlmClient.from_settings(same, factory, None)
    assert (client.chat_model, client.parse_model) == ("claude-opus-5-5", "claude-opus-5-5")

    split = Settings(
        _env_file=None,
        anthropic_api_key="sk-test",
        anthropic_model="claude-opus-5-5",
        anthropic_parse_model="claude-haiku-5-5",
    )
    client = LlmClient.from_settings(split, factory, None)
    assert (client.chat_model, client.parse_model) == ("claude-opus-5-5", "claude-haiku-5-5")


def test_cached_system_without_dynamic_part():
    assert cached_system("rules") == [
        {"type": "text", "text": "rules", "cache_control": {"type": "ephemeral"}}
    ]
