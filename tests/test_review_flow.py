"""Box and Other read by a (fake) Claude: summary, Confirm, Adjust, RPE, errors."""

import anthropic
import pytest
from telegram.ext import ConversationHandler

from kide.bot.handlers import checkin as flow
from kide.bot.handlers import review
from kide.bot.rules import Flow
from kide.catalog.movements import default_movements
from kide.llm.client import LlmClient
from kide.llm.parsing import Parser
from kide.storage import LlmCall
from kide.storage.repositories import unit_of_work
from tests.bot_helpers import TODAY
from tests.llm_fakes import FakeSdk, api_error, make_message, tool_message
from tests.telegram_factories import message_update, photo_update, sticker_update

pytestmark = pytest.mark.anyio

WOD = {
    "format": "amrap",
    "confidence": "high",
    "blocks": [
        {
            "format": "amrap",
            "duration_min": 12,
            "movements": [{"name": "T2B", "reps": 10}, {"name": "WB", "reps": 15, "load_kg": 9}],
        }
    ],
}
RUN = {"type": "run", "duration_min": 45, "distance_km": 9, "confidence": "high"}


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
    services.parser = Parser(services.llm, default_movements(), services.data_dir)
    return fake


def parsed(services, session_id):
    with unit_of_work(services.session_factory) as repos:
        return repos.sessions.get(session_id).parsed_json


def llm_purposes(services):
    with unit_of_work(services.session_factory) as repos:
        return [
            (c.purpose, c.session_id) for c in repos.session.query(LlmCall).order_by(LlmCall.id)
        ]


def last_buttons(chat):
    markup = chat.bot.send_message.await_args.kwargs.get("reply_markup")
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


async def start_box(chat, text="AMRAP 12: 10 T2B, 15 WB"):
    await chat.tap(flow.choice_made, f"ci:{TODAY}:box")
    session_id = chat.user_data[flow.SESSION_KEY]
    await chat.send(flow.wod_text, message_update(text=text))
    return session_id


async def test_box_is_read_shown_and_confirmed(chat, services, sdk):
    session_id = await start_box(chat)
    sdk.queue(tool_message("record_wod", WOD))
    state = await chat.tap(flow.wod_done, f"wod:{session_id}")

    assert state is Flow.REVIEW
    texts = chat.texts()
    assert texts[-2] == "Leyendo el WOD…"
    assert texts[-1].startswith(
        "WOD · AMRAP\nAMRAP · 12 min\n• 10 Pies a barra\n• 15 Wall ball · 9 kg"
    )
    assert texts[-1].endswith("pulsa «Ajustar» y dímelo con tus palabras.")
    assert last_buttons(chat) == [
        ("✅ Confirmar", f"rok:{session_id}"),
        ("✏️ Ajustar", f"radj:{session_id}"),
    ]
    chat.bot.send_chat_action.assert_awaited()
    draft = parsed(services, session_id)
    assert (draft["kind"], draft["status"]) == ("wod", "draft")
    assert draft["data"]["blocks"][0]["movements"][0]["movement_id"] == "toes_to_bar"
    assert llm_purposes(services) == [("wod_parse_text", session_id)]

    state = await chat.tap(review.confirm, f"rok:{session_id}")
    assert state == ConversationHandler.END
    assert chat.texts()[-1] == "WOD guardado."
    assert parsed(services, session_id)["status"] == "confirmed"


async def test_done_again_never_pays_twice(chat, services, sdk):
    session_id = await start_box(chat)
    sdk.queue(tool_message("record_wod", WOD))
    await chat.tap(flow.wod_done, f"wod:{session_id}")
    await chat.tap(flow.wod_done, f"wod:{session_id}")  # draft: shown again
    assert len(sdk.requests) == 1
    assert chat.texts()[-1].startswith("WOD · AMRAP")

    await chat.tap(review.confirm, f"rok:{session_id}")
    await chat.tap(flow.wod_done, f"wod:{session_id}")  # confirmed: just shown
    assert len(sdk.requests) == 1
    assert chat.texts()[-1].startswith("Ya estaba guardado:\nWOD · AMRAP")


async def test_adjust_applies_the_correction_without_photos(chat, services, sdk):
    session_id = await start_box(chat)
    await chat.send(flow.wod_photo, photo_update("PH1"))
    sdk.queue(tool_message("record_wod", WOD))
    await chat.tap(flow.wod_done, f"wod:{session_id}")

    state = await chat.tap(review.adjust_start, f"radj:{session_id}")
    assert state is Flow.ADJUST
    assert chat.texts()[-1].startswith("Dime qué hay que cambiar")

    fixed = {**WOD, "blocks": [{**WOD["blocks"][0], "duration_min": 15}]}
    sdk.queue(tool_message("record_wod", fixed))
    state = await chat.send(review.adjust_text, message_update(text="Eran 15 minutos"))
    assert state is Flow.REVIEW
    assert "AMRAP · 15 min" in chat.texts()[-1]
    content = sdk.requests[-1]["messages"][0]["content"]
    assert [block["type"] for block in content] == ["text"]
    assert "Eran 15 minutos" in content[0]["text"]
    assert parsed(services, session_id)["data"]["blocks"][0]["duration_min"] == 15
    assert [p for p, _ in llm_purposes(services)] == ["wod_parse_text", "wod_adjust"]


async def test_adjust_that_fails_lets_the_user_try_again(chat, services, sdk):
    session_id = await start_box(chat)
    sdk.queue(tool_message("record_wod", WOD))
    await chat.tap(flow.wod_done, f"wod:{session_id}")
    await chat.tap(review.adjust_start, f"radj:{session_id}")
    sdk.queue(make_message("no"), make_message("still no"))
    state = await chat.send(review.adjust_text, message_update(text="???"))
    assert state is Flow.ADJUST
    assert chat.texts()[-1].startswith("No he podido aplicar ese cambio")
    assert await chat.send(review.adjust_unsupported, sticker_update()) is Flow.ADJUST
    assert parsed(services, session_id)["data"]["blocks"][0]["duration_min"] == 12


async def test_unreadable_wod_asks_for_text_and_reads_again(chat, services, sdk):
    session_id = await start_box(chat, text="foto borrosa")
    sdk.queue(make_message("can't"), make_message("still can't"))
    state = await chat.tap(flow.wod_done, f"wod:{session_id}")
    assert state is Flow.ADJUST
    assert chat.texts()[-1].startswith("No he conseguido leerlo bien")
    assert last_buttons(chat) == [("Guardar sin leer", f"rraw:{session_id}")]

    sdk.queue(tool_message("record_wod", WOD))
    state = await chat.send(review.adjust_text, message_update(text="AMRAP 12: 10 T2B, 15 WB"))
    assert state is Flow.REVIEW
    last_request = sdk.requests[-1]["messages"][0]["content"][-1]["text"]
    assert "foto borrosa\n\nAMRAP 12: 10 T2B, 15 WB" in last_request
    assert parsed(services, session_id)["status"] == "draft"


async def test_save_without_reading(chat, services, sdk):
    session_id = await start_box(chat)
    sdk.queue(make_message("can't"), make_message("still can't"))
    await chat.tap(flow.wod_done, f"wod:{session_id}")
    state = await chat.tap(review.save_raw, f"rraw:{session_id}")
    assert state == ConversationHandler.END
    assert chat.texts()[-1] == "Guardado tal como lo mandaste, sin leerlo."
    assert parsed(services, session_id) is None
    assert review.MODE_KEY not in chat.user_data


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (api_error(anthropic.InternalServerError, 500), "Ahora mismo no puedo hablar con Claude"),
        (api_error(anthropic.BadRequestError, 400), "Claude ha rechazado la petición"),
    ],
)
async def test_api_error_keeps_the_input_and_offers_retry(chat, services, sdk, error, expected):
    session_id = await start_box(chat)
    sdk.queue(error)
    state = await chat.tap(flow.wod_done, f"wod:{session_id}")
    assert state is Flow.REVIEW
    assert chat.texts()[-1].startswith(expected)
    assert last_buttons(chat) == [
        ("Reintentar", f"rtry:{session_id}"),
        ("Guardar sin leer", f"rraw:{session_id}"),
    ]
    assert parsed(services, session_id) is None

    sdk.queue(tool_message("record_wod", WOD))
    assert await chat.tap(review.retry, f"rtry:{session_id}") is Flow.REVIEW
    assert parsed(services, session_id)["status"] == "draft"


async def test_box_without_wod_is_not_read(chat, services, sdk):
    await chat.tap(flow.choice_made, f"ci:{TODAY}:box")
    session_id = chat.user_data[flow.SESSION_KEY]
    await chat.tap(flow.wod_done, f"wod:{session_id}")
    assert chat.texts()[-1] == "Box apuntado, sin WOD."
    assert sdk.requests == []


async def test_other_without_effort_asks_rpe_then_confirms(chat, services, sdk):
    await chat.tap(flow.choice_made, f"ci:{TODAY}:other")
    session_id = chat.user_data[flow.SESSION_KEY]
    sdk.queue(tool_message("record_activity", RUN))
    state = await chat.send(flow.note_text, message_update(text="Rodaje 9 km en 45'"))
    assert state is Flow.REVIEW
    assert chat.texts()[-2] == "Leyendo la actividad…"
    assert "Carrera\n9 km · 45 min · 5:00 /km" in chat.texts()[-1]
    assert "del 1 al 10" in chat.texts()[-1]
    buttons = last_buttons(chat)
    assert len(buttons) == 10
    assert buttons[6] == ("7", f"rpe:{session_id}:7")

    state = await chat.tap(review.set_rpe, f"rpe:{session_id}:7")
    assert state is Flow.REVIEW
    assert "RPE 7/10" in chat.texts()[-1]
    assert parsed(services, session_id)["data"]["rpe"] == 7

    await chat.tap(review.confirm, f"rok:{session_id}")
    assert chat.texts()[-1] == "Actividad guardada."
    assert parsed(services, session_id)["status"] == "confirmed"
    assert llm_purposes(services) == [("activity_parse", session_id)]


async def test_other_with_stated_effort_goes_straight_to_review(chat, services, sdk):
    await chat.tap(flow.choice_made, f"ci:{TODAY}:other")
    session_id = chat.user_data[flow.SESSION_KEY]
    await chat.send(flow.note_photo, photo_update("STRAVA1"))  # fake bytes: skipped
    sdk.queue(tool_message("record_activity", {**RUN, "rpe": 6}))
    state = await chat.send(flow.note_text, message_update(text="Rodaje, RPE 6"))
    assert state is Flow.REVIEW
    assert "No he podido abrir 1 de las imágenes." in chat.texts()
    assert last_buttons(chat)[0] == ("✅ Confirmar", f"rok:{session_id}")


async def test_only_an_unreadable_screenshot_is_saved_as_before(chat, services, sdk):
    await chat.tap(flow.choice_made, f"ci:{TODAY}:other")
    session_id = chat.user_data[flow.SESSION_KEY]
    await chat.send(flow.note_photo, photo_update("HEIC1"))  # fake bytes: cannot be opened
    state = await chat.tap(flow.note_skip, f"note:{session_id}")
    assert state == ConversationHandler.END
    assert chat.texts()[-2:] == [
        "No he podido abrir 1 de las imágenes.",
        "Guardado: Otro (fotos: 1).",
    ]
    assert sdk.requests == []


async def test_gym_and_rest_notes_are_not_read(chat, services, sdk):
    await chat.tap(flow.choice_made, f"ci:{TODAY}:gym")
    state = await chat.send(flow.note_text, message_update(text="Pierna"))
    assert state == ConversationHandler.END
    assert chat.texts()[-1] == "Guardado: Gimnasio (con nota)."
    assert sdk.requests == []


async def test_buttons_for_a_deleted_session(chat, services, sdk):
    for handler, data in (
        (review.confirm, "rok:999"),
        (review.save_raw, "rraw:999"),
        (review.adjust_start, "radj:999"),
        (review.set_rpe, "rpe:999:5"),
    ):
        assert await chat.tap(handler, data) == ConversationHandler.END
        assert chat.texts()[-1].startswith("Esa lectura ya no existe")


async def test_bad_rpe_button_is_ignored(chat, services, sdk):
    assert await chat.tap(review.set_rpe, "rpe:1:11") == ConversationHandler.END
    assert chat.texts() == []
