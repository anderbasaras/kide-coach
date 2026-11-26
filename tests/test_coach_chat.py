"""The coach chat: context of the last days, memory, prompt and handler (fake Claude)."""

import datetime as dt
import logging
from pathlib import Path

import anthropic
import pytest

from kide.bot.coach import (
    HISTORY_TURNS,
    ChatHistory,
    recent_days,
    split_message,
    today_header,
)
from kide.bot.handlers import chat as coach
from kide.catalog.movements import default_movements
from kide.i18n import Translator
from kide.llm.client import LlmClient
from kide.llm.prompts.coach import coach_system_prompt, profile_summary
from kide.llm.schemas import Activity, ParsedRecord, Wod
from kide.profile import Profile, load_profile
from kide.storage import DayType, LlmCall
from kide.storage.repositories import unit_of_work
from tests.bot_helpers import NOW, TODAY, make_context
from tests.llm_fakes import FakeSdk, api_error, make_message
from tests.telegram_factories import message_update

EXAMPLE = load_profile(Path(__file__).parent.parent / "profile.example.yaml")
TRANSLATOR = Translator.from_directory()


def t_en(key, **params):
    return TRANSLATOR.t(key, locale="en", **params)


WOD = Wod.model_validate(
    {
        "format": "amrap",
        "confidence": "high",
        "blocks": [
            {
                "format": "amrap",
                "duration_min": 12,
                "movements": [{"name": "T2B", "movement_id": "toes_to_bar", "reps": 10}],
            }
        ],
    }
)
RUN = Activity.model_validate(
    {"type": "run", "duration_min": 40, "distance_km": 8, "rpe": 5, "confidence": "high"}
)


def add_session(services, day, day_type, *, raw=None, notes=None, record=None, photos=()):
    with unit_of_work(services.session_factory) as repos:
        checkin = repos.checkins.get_or_create(services.user_id, day)
        training = repos.sessions.create(checkin, day_type, raw_input=raw)
        repos.sessions.set_note(training, notes)
        if record is not None:
            repos.sessions.set_parsed(training, record.to_json())
        for index, path in enumerate(photos):
            repos.sessions.add_photo(training, path, f"U{index}")


# --- Context of the last days -----------------------------------------------------------


def test_recent_days_describe_logs_without_photos(services):
    add_session(
        services,
        TODAY - dt.timedelta(days=1),
        DayType.BOX,
        raw="AMRAP 12",
        record=ParsedRecord.draft(WOD).confirmed(),
        photos=["photos/2026/11/board.jpg"],
    )
    add_session(services, TODAY - dt.timedelta(days=2), DayType.BOX, raw="EMOM 10 thrusters")
    add_session(
        services,
        TODAY - dt.timedelta(days=3),
        DayType.OTHER,
        notes="Rodaje suave",
        record=ParsedRecord.draft(RUN),
    )
    add_session(services, TODAY - dt.timedelta(days=4), DayType.GYM, notes="Pierna, rodilla ok")
    add_session(services, TODAY - dt.timedelta(days=9), DayType.REST)  # too old
    with unit_of_work(services.session_factory) as repos:
        sessions = repos.sessions.between(services.user_id, TODAY - dt.timedelta(days=6), TODAY)
        text = recent_days(sessions, TODAY, t_en, default_movements())

    lines = text.splitlines()
    assert lines[0] == "Training log, last 7 days (oldest first):"
    assert lines[1] == "2026-11-17 Tue:"
    assert lines[-2] == "2026-11-23 Mon (today):"
    assert lines[-1] == "- nothing logged"
    assert "- Box class (confirmed):\n  WOD · AMRAP\n  AMRAP · 12 min\n  • 10 Toes-to-bar" in text
    assert "(1 photo(s), not shared)" in text
    assert "board.jpg" not in text
    assert "WOD as written by the athlete (not read):\n  EMOM 10 thrusters" in text
    assert (
        "- Other activity (not confirmed yet):\n  Run\n  8 km · 40 min · 5:00 /km · RPE 5/10"
        in text
    )
    assert "Athlete's note: Rodaje suave" in text
    assert "- Gym session\n  Athlete's note: Pierna, rodilla ok" in text
    assert text.count("nothing logged") == 3


def test_long_unread_text_is_truncated(services):
    add_session(services, TODAY, DayType.BOX, raw="x" * 2000)
    with unit_of_work(services.session_factory) as repos:
        text = recent_days(
            repos.sessions.between(services.user_id, TODAY, TODAY),
            TODAY,
            t_en,
            default_movements(),
            days=1,
        )
    assert "x" * 599 + "…" in text
    assert "x" * 601 not in text


def test_today_header_counts_days_to_competitions():
    header = today_header(EXAMPLE, dt.date(2027, 4, 7))
    assert header.splitlines() == [
        "Today is Wednesday 2027-04-07 (athlete's time zone).",
        "City Hybrid Race (hybrid_race) is in 10 days.",
    ]
    assert "City Hybrid Race" not in today_header(EXAMPLE, dt.date(2027, 4, 18))


# --- Memory and message size ------------------------------------------------------------


def test_history_keeps_the_last_turns_and_forgets_after_a_pause():
    history = ChatHistory()
    assert history.messages(NOW) == []
    for index in range(HISTORY_TURNS + 2):
        history.add(NOW, f"q{index}", f"a{index}")
    messages = history.messages(NOW + dt.timedelta(minutes=59))
    assert len(messages) == 2 * HISTORY_TURNS
    assert messages[0] == {"role": "user", "content": "q2"}
    assert messages[-1] == {"role": "assistant", "content": f"a{HISTORY_TURNS + 1}"}
    assert history.messages(NOW + dt.timedelta(minutes=61)) == []


def test_history_drops_old_turns_when_too_long():
    history = ChatHistory()
    history.add(NOW, "q" * 7000, "a")
    history.add(NOW, "q" * 7000, "b")
    assert [answer for _, answer in history.turns] == ["b"]


def test_split_message():
    paragraphs = "\n\n".join(["p" * 3000, "q" * 3000])
    assert split_message(paragraphs) == ["p" * 3000, "q" * 3000]
    assert split_message("short") == ["short"]
    assert [len(part) for part in split_message("y" * 9000)] == [4000, 4000, 1000]


# --- Prompt -----------------------------------------------------------------------------


def test_coach_prompt_has_rules_profile_and_language_but_no_personal_identifiers():
    prompt = coach_system_prompt(EXAMPLE, "es")
    assert "Answer in Spanish" in prompt
    assert "stop training and see a doctor" in prompt
    assert "performance-enhancing drugs" in prompt
    assert "Kide's load engine decides the gym sessions" in prompt
    assert "primary: hypertrophy" in prompt
    assert "Barbell bench press" in prompt
    summary = profile_summary(EXAMPLE)
    assert "identity" not in summary and "equipment" not in summary
    assert "Sam" not in prompt and "Europe/London" not in prompt


def test_profile_summary_skips_empty_sections(profile_data):
    summary = profile_summary(Profile.model_validate(profile_data))
    assert "competitions" not in summary
    assert "maxes" not in summary
    assert summary["gym_rotation"][0]["exercises"] == ["Bench press", "Lat pulldown"]


# --- Handler ----------------------------------------------------------------------------


@pytest.fixture
def sdk(services):
    fake = FakeSdk()
    services.llm = LlmClient(
        fake,
        chat_model="claude-opus-5-5",
        parse_model="claude-haiku-5-5",
        session_factory=services.session_factory,
        user_id=services.user_id,
    )
    return fake


@pytest.fixture
def context(services, bot):
    return make_context(services, bot)


async def say(context, bot, text):
    from tests.bot_helpers import bind

    await coach.coach_reply(bind(message_update(text=text), bot), context)


def replies(bot):
    return [call.kwargs["text"] for call in bot.send_message.await_args_list]


@pytest.mark.anyio
async def test_answer_with_profile_today_and_the_last_days(services, bot, context, sdk):
    add_session(
        services,
        TODAY - dt.timedelta(days=1),
        DayType.BOX,
        record=ParsedRecord.draft(WOD).confirmed(),
    )
    sdk.queue(make_message("Hoy toca descansar las piernas.", model="claude-opus-5-5"))
    await say(context, bot, "¿Qué hago hoy?")

    assert replies(bot) == ["Hoy toca descansar las piernas."]
    bot.send_chat_action.assert_awaited()
    request = sdk.requests[0]
    assert request["model"] == "claude-opus-5-5"
    assert "output_config" not in request  # the model's default effort
    stable, dynamic = request["system"]
    assert stable["cache_control"] == {"type": "ephemeral"}
    assert "Answer in Spanish" in stable["text"]  # the services fixture is in Spanish
    assert "Today is Monday 2026-11-23" in dynamic["text"]
    assert "Box class (confirmed)" in dynamic["text"]
    assert request["messages"] == [{"role": "user", "content": "¿Qué hago hoy?"}]
    with unit_of_work(services.session_factory) as repos:
        assert [c.purpose for c in repos.session.query(LlmCall)] == ["chat"]


@pytest.mark.anyio
async def test_the_conversation_is_remembered_for_a_while(services, bot, context, sdk):
    sdk.queue(make_message("Respuesta 1"), make_message("Respuesta 2"), make_message("3"))
    await say(context, bot, "Pregunta 1")
    await say(context, bot, "Pregunta 2")
    assert sdk.requests[1]["messages"] == [
        {"role": "user", "content": "Pregunta 1"},
        {"role": "assistant", "content": "Respuesta 1"},
        {"role": "user", "content": "Pregunta 2"},
    ]
    services.clock = lambda: NOW + dt.timedelta(hours=2)
    await say(context, bot, "Pregunta 3")
    assert sdk.requests[2]["messages"] == [{"role": "user", "content": "Pregunta 3"}]


@pytest.mark.anyio
async def test_long_answers_are_split(services, bot, context, sdk):
    sdk.queue(make_message("a" * 3000 + "\n\n" + "b" * 3000))
    await say(context, bot, "Plan completo")
    assert [len(text) for text in replies(bot)] == [3000, 3000]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (api_error(anthropic.InternalServerError, 500), "Ahora mismo no puedo contactar"),
        (api_error(anthropic.AuthenticationError, 401), "Claude ha rechazado la petición"),
        (make_message("   "), "No tengo respuesta para eso"),
    ],
)
async def test_errors_and_empty_answers(services, bot, context, sdk, response, expected):
    sdk.queue(response)
    await say(context, bot, "Hola")
    assert replies(bot)[-1].startswith(expected)
    assert coach._history(context).turns == []


@pytest.mark.anyio
async def test_without_claude_the_help_text_is_sent(services, bot, context):
    await say(context, bot, "Hola")
    assert replies(bot)[-1].startswith("El chat con el entrenador necesita Claude")


@pytest.mark.anyio
async def test_nothing_of_the_chat_goes_to_the_log(services, bot, context, sdk, caplog):
    sdk.queue(make_message("RESPUESTA SECRETA"))
    with caplog.at_level(logging.DEBUG):
        await say(context, bot, "PREGUNTA SECRETA")
    assert "purpose=chat" in caplog.text
    assert "SECRETA" not in caplog.text
