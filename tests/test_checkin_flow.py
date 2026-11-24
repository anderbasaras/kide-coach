"""The /today flows end to end, with a mocked bot and a real temporary database."""

import datetime as dt
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from telegram.ext import ConversationHandler

from kide.bot.handlers import checkin as flow
from kide.bot.photos import MAX_DOWNLOAD_BYTES
from kide.bot.rules import Flow
from kide.storage import DayType
from kide.storage.repositories import unit_of_work
from tests.bot_helpers import (
    NOW,
    bind,
    fake_downloads,
    make_bot,
    make_context,
    make_services,
    sent_texts,
)
from tests.telegram_factories import (
    ALLOWED_CHAT,
    callback_update,
    document_update,
    edited_update,
    message_update,
    photo_update,
    sticker_update,
)

TODAY = dt.date(2026, 11, 23)  # NOW is 08:30 UTC = 09:30 in Berlin (profile time zone)


@pytest.fixture
def services(tmp_path, profile_data):
    profile_data["identity"]["language"] = "es"
    return make_services(tmp_path, profile_data)


@pytest.fixture
def bot():
    bot = make_bot()
    fake_downloads(bot)
    return bot


class Chat:
    """One user talking to the bot: keeps user_data between updates, like PTB does."""

    def __init__(self, services, bot):
        self.services = services
        self.bot = bot
        self.user_data: dict = {}
        self.job_queue = MagicMock(name="job_queue")
        self.job_queue.get_jobs_by_name.return_value = []

    async def send(self, handler, update):
        context = make_context(self.services, self.bot, self.user_data, self.job_queue)
        return await handler(bind(update, self.bot), context)

    async def tap(self, handler, data):
        return await self.send(handler, callback_update(data=data))

    def texts(self):
        return sent_texts(self.bot)

    def reset_bot(self):
        self.bot.reset_mock()


@pytest.fixture
def chat(services, bot):
    return Chat(services, bot)


def day_sessions(services, day=TODAY):
    with unit_of_work(services.session_factory) as repos:
        sessions = repos.sessions.for_day(services.user_id, day)
        checkin = repos.checkins.get(services.user_id, day)
        return (
            [(s.type, s.raw_input, s.notes, [p.path for p in s.photos]) for s in sessions],
            checkin,
        )


# --- Asking ----------------------------------------------------------------------------


@pytest.mark.anyio
async def test_today_sends_the_question_with_four_options(chat, services):
    await chat.send(flow.today, message_update(text="/hoy"))
    call = chat.bot.send_message.await_args
    assert call.kwargs["text"] == "¿Qué toca hoy?"
    labels = [b.text for row in call.kwargs["reply_markup"].inline_keyboard for b in row]
    assert labels == ["Gimnasio", "Box", "Descanso", "Otro"]
    _, checkin = day_sessions(services)
    assert checkin.sent_at == NOW
    assert checkin.chat_id == ALLOWED_CHAT


@pytest.mark.anyio
async def test_asking_again_keeps_one_checkin_and_the_first_send_time(chat, services):
    await chat.send(flow.today, message_update(text="/hoy"))
    services.clock = lambda: NOW + dt.timedelta(hours=1)
    await chat.send(flow.today, message_update(text="/today"))
    assert chat.bot.send_message.await_count == 2
    _, checkin = day_sessions(services)
    assert checkin.sent_at == NOW


# --- Gym / Rest / Other ----------------------------------------------------------------


@pytest.mark.anyio
async def test_gym_with_a_note(chat, services):
    state = await chat.tap(flow.choice_made, f"ci:{TODAY}:gym")
    assert state is Flow.NOTE
    assert chat.bot.edit_message_text.await_args.kwargs["text"] == "Hoy: Gimnasio"
    assert "¿Quieres añadir una nota?" in chat.texts()[-1]

    state = await chat.send(flow.note_text, message_update(text="  Pierna cargada del lunes "))
    assert state == ConversationHandler.END
    assert chat.texts()[-1] == "Guardado: Gimnasio, con tu nota."
    sessions, checkin = day_sessions(services)
    assert sessions == [(DayType.GYM, None, "Pierna cargada del lunes", [])]
    assert checkin.choice is DayType.GYM
    assert checkin.answered_at == NOW


@pytest.mark.anyio
@pytest.mark.parametrize(("choice", "label"), [("rest", "Descanso"), ("other", "Otro")])
async def test_rest_and_other_without_note(chat, services, choice, label):
    assert await chat.tap(flow.choice_made, f"ci:{TODAY}:{choice}") is Flow.NOTE
    session_id = chat.user_data[flow.SESSION_KEY]
    state = await chat.tap(flow.note_skip, f"note:{session_id}")
    assert state == ConversationHandler.END
    assert chat.texts()[-1] == f"Guardado: {label}."
    chat.bot.edit_message_reply_markup.assert_awaited()  # the button disappears
    sessions, _ = day_sessions(services)
    assert sessions == [(DayType(choice), None, None, [])]
    assert flow.SESSION_KEY not in chat.user_data


# --- Box -------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_box_with_texts_and_a_photo(chat, services):
    assert await chat.tap(flow.choice_made, f"ci:{TODAY}:box") is Flow.WOD
    assert "Mándame el WOD" in chat.texts()[-1]
    session_id = chat.user_data[flow.SESSION_KEY]

    assert await chat.send(flow.wod_text, message_update(text="AMRAP 12'")) is Flow.WOD
    assert await chat.send(flow.wod_text, message_update(text="10 wall balls")) is Flow.WOD
    assert await chat.send(flow.wod_photo, photo_update("PH1")) is Flow.WOD
    assert chat.texts()[-1] == "Recibido. Fotos: 1 · Texto: Sí. Manda más o pulsa «Listo»."

    state = await chat.tap(flow.wod_done, f"wod:{session_id}")
    assert state == ConversationHandler.END
    assert chat.texts()[-1] == "WOD guardado. Fotos: 1 · Texto: Sí."

    sessions, checkin = day_sessions(services)
    expected_photo = f"photos/2026/11/2026-11-23_s{session_id}_PH1.jpg"
    assert sessions == [(DayType.BOX, "AMRAP 12'\n\n10 wall balls", None, [expected_photo])]
    assert (services.data_dir / expected_photo).read_bytes() == b"fake image"
    assert checkin.choice is DayType.BOX


@pytest.mark.anyio
async def test_an_album_is_acknowledged_once(chat, services):
    await chat.tap(flow.choice_made, f"ci:{TODAY}:box")
    chat.reset_bot()
    previous = MagicMock(name="previous ack")
    for index, unique_id in enumerate(["A1", "A2", "A3"]):
        chat.job_queue.get_jobs_by_name.return_value = [previous] if index else []
        await chat.send(flow.wod_photo, photo_update(unique_id, media_group_id="album-1"))

    assert chat.bot.send_message.await_count == 0  # no message per photo
    assert chat.job_queue.run_once.call_count == 3
    assert previous.schedule_removal.call_count == 2  # each photo postpones the ack
    sessions, _ = day_sessions(services)
    assert len(sessions[0][3]) == 3

    job_call = chat.job_queue.run_once.call_args
    job = SimpleNamespace(chat_id=job_call.kwargs["chat_id"], data=job_call.kwargs["data"])
    context = make_context(services, chat.bot)
    context.job = job
    await job_call.args[0](context)
    assert chat.texts() == ["Recibido. Fotos: 3 · Texto: No. Manda más o pulsa «Listo»."]


@pytest.mark.anyio
async def test_image_sent_as_a_file(chat, services):
    await chat.tap(flow.choice_made, f"ci:{TODAY}:box")
    await chat.send(flow.wod_photo, document_update("DOC1", mime_type="image/png"))
    sessions, _ = day_sessions(services)
    assert sessions[0][3][0].endswith("_DOC1.png")


@pytest.mark.anyio
async def test_the_same_photo_twice_is_stored_once(chat, services):
    await chat.tap(flow.choice_made, f"ci:{TODAY}:box")
    await chat.send(flow.wod_photo, photo_update("PH1"))
    await chat.send(flow.wod_photo, photo_update("PH1"))
    sessions, _ = day_sessions(services)
    assert len(sessions[0][3]) == 1


@pytest.mark.anyio
async def test_too_large_file_is_explained(chat, services):
    await chat.tap(flow.choice_made, f"ci:{TODAY}:box")
    state = await chat.send(flow.wod_photo, document_update(file_size=MAX_DOWNLOAD_BYTES + 1))
    assert state is Flow.WOD
    assert "20 MB" in chat.texts()[-1]


@pytest.mark.anyio
async def test_unsupported_message_keeps_waiting_for_the_wod(chat):
    await chat.tap(flow.choice_made, f"ci:{TODAY}:box")
    assert await chat.send(flow.wod_unsupported, sticker_update()) is Flow.WOD
    assert chat.texts()[-1] == "Mándame el WOD como texto o foto, o pulsa «Listo»."


@pytest.mark.anyio
async def test_box_without_wod(chat, services):
    await chat.tap(flow.choice_made, f"ci:{TODAY}:box")
    session_id = chat.user_data[flow.SESSION_KEY]
    await chat.tap(flow.wod_done, f"wod:{session_id}")
    assert chat.texts()[-1] == "Box apuntado, sin WOD."
    sessions, _ = day_sessions(services)
    assert sessions == [(DayType.BOX, None, None, [])]


@pytest.mark.anyio
async def test_done_still_works_after_a_restart(chat, services):
    await chat.tap(flow.choice_made, f"ci:{TODAY}:box")
    session_id = chat.user_data[flow.SESSION_KEY]
    await chat.send(flow.wod_text, message_update(text="5 rounds"))
    chat.user_data.clear()  # the conversation state is not persisted (D-029)
    await chat.tap(flow.wod_done, f"wod:{session_id}")
    assert chat.texts()[-1] == "WOD guardado. Fotos: 0 · Texto: Sí."


# --- One answer per day ----------------------------------------------------------------


@pytest.mark.anyio
async def test_today_after_answering_shows_the_day(chat):
    await chat.tap(flow.choice_made, f"ci:{TODAY}:box")
    await chat.send(flow.wod_photo, photo_update("PH1"))
    await chat.send(flow.today, message_update(text="/hoy"))
    assert chat.texts()[-1] == "Hoy ya está apuntado: Box (fotos: 1, texto: No)."


@pytest.mark.anyio
async def test_an_old_button_does_not_answer_twice(chat, services):
    await chat.tap(flow.choice_made, f"ci:{TODAY}:gym")
    state = await chat.tap(flow.choice_made, f"ci:{TODAY}:box")
    assert state == ConversationHandler.END
    assert chat.texts()[-1] == "Ese día ya está apuntado. Escribe /hoy para ver lo de hoy."
    sessions, _ = day_sessions(services)
    assert [s[0] for s in sessions] == [DayType.GYM]


@pytest.mark.anyio
async def test_yesterdays_button_logs_yesterday(chat, services):
    yesterday = TODAY - dt.timedelta(days=1)
    await chat.tap(flow.choice_made, f"ci:{yesterday}:rest")
    assert chat.bot.edit_message_text.await_args.kwargs["text"] == "22/11/2026: Descanso"
    sessions, _ = day_sessions(services, yesterday)
    assert [s[0] for s in sessions] == [DayType.REST]
    assert day_sessions(services)[0] == []


@pytest.mark.anyio
@pytest.mark.parametrize("data", [f"ci:{TODAY + dt.timedelta(days=1)}:gym", "ci:broken"])
async def test_future_or_broken_buttons_are_refused(chat, services, data):
    assert await chat.tap(flow.choice_made, data) == ConversationHandler.END
    assert "Ese botón ya no vale" in chat.texts()[-1]
    assert day_sessions(services)[0] == []


# --- Routing inside the conversation -----------------------------------------------------


def route(state, update):
    """The callback the conversation would use for ``update`` in ``state``."""
    for handler in flow.build_conversation().states[state]:
        if handler.check_update(update):
            return handler.callback
    return None


def test_routing_while_waiting_for_the_wod():
    assert route(Flow.WOD, message_update(text="21-15-9")) is flow.wod_text
    assert route(Flow.WOD, photo_update()) is flow.wod_photo
    assert route(Flow.WOD, document_update()) is flow.wod_photo
    assert route(Flow.WOD, callback_update(data="wod:1")) is flow.wod_done
    assert route(Flow.WOD, sticker_update()) is flow.wod_unsupported
    assert route(Flow.WOD, message_update(text="/ayuda")) is None  # commands still work
    assert route(Flow.WOD, edited_update()) is None  # edits are not added to the WOD


def test_routing_while_waiting_for_a_note():
    assert route(Flow.NOTE, message_update(text="tired")) is flow.note_text
    assert route(Flow.NOTE, callback_update(data="note:1")) is flow.note_skip
    assert route(Flow.NOTE, photo_update()) is flow.note_unsupported
    assert route(Flow.NOTE, message_update(text="/hoy")) is None
