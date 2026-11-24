"""Correcting a logged day and adding a second session (/today on an answered day)."""

import datetime as dt

import pytest
from telegram.ext import ConversationHandler

from kide.bot.handlers import checkin as flow
from kide.bot.handlers import day_edit
from kide.bot.rules import Flow
from kide.storage import DayType
from kide.storage.repositories import unit_of_work
from tests.bot_helpers import TODAY, day_sessions
from tests.telegram_factories import callback_update, message_update, photo_update

GYM, BOX, REST, OTHER = DayType.GYM, DayType.BOX, DayType.REST, DayType.OTHER


def markup_data(bot):
    """Callback data of the buttons in the last message sent or edited."""
    for name, _args, kwargs in reversed(bot.mock_calls):
        if name in ("send_message", "edit_message_text") and kwargs.get("reply_markup"):
            rows = kwargs["reply_markup"].inline_keyboard
            return [button.callback_data for row in rows for button in row]
    return []


async def answer_box_with_photo(chat):
    await chat.tap(flow.choice_made, f"ci:{TODAY}:box")
    session_id = chat.user_data[flow.SESSION_KEY]
    await chat.send(flow.wod_photo, photo_update("PH1"))
    await chat.tap(flow.wod_done, f"wod:{session_id}")
    return session_id


def session_ids(services, day=TODAY):
    with unit_of_work(services.session_factory) as repos:
        return [s.id for s in repos.sessions.for_day(services.user_id, day)]


# --- Summary --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_answered_day_shows_correct_and_add(chat):
    await chat.tap(flow.choice_made, f"ci:{TODAY}:gym")
    await chat.send(flow.today, message_update(text="/hoy"))
    assert chat.texts()[-1] == "Hoy ya está apuntado: Gimnasio."
    assert markup_data(chat.bot) == [f"dfix:{TODAY}", f"dadd:{TODAY}"]


# --- Correct --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_correcting_the_only_session_starts_it_over(chat, services):
    session_id = await answer_box_with_photo(chat)
    photo = services.data_dir / day_sessions(services)[0][0][3][0]
    assert photo.is_file()

    await chat.tap(day_edit.fix_start, f"dfix:{TODAY}")  # one session: no pick
    assert "Box (fotos: 1, texto: No). Elige lo que hiciste de verdad" in chat.texts()[-1]
    assert f"fset:{session_id}:gym" in markup_data(chat.bot)
    assert f"fdel:{session_id}" in markup_data(chat.bot)

    state = await chat.tap(day_edit.fix_set, f"fset:{session_id}:gym")
    assert state is Flow.NOTE  # the flow of the new type opens
    assert "Corregido: Box → Gimnasio" in chat.texts()
    sessions, checkin = day_sessions(services)
    assert sessions == [(GYM, None, None, [])]
    assert checkin.choice is GYM
    assert not photo.exists()  # the replaced photo is deleted too


@pytest.mark.anyio
async def test_correcting_to_box_asks_for_the_wod_again(chat, services):
    await chat.tap(flow.choice_made, f"ci:{TODAY}:rest")
    (session_id,) = session_ids(services)
    assert await chat.tap(day_edit.fix_set, f"fset:{session_id}:box") is Flow.WOD
    assert "Mándame el WOD" in chat.texts()[-1]


@pytest.mark.anyio
async def test_with_several_sessions_the_user_picks_one(chat, services):
    await chat.tap(flow.choice_made, f"ci:{TODAY}:box")
    await chat.tap(day_edit.add_set, f"aset:{TODAY}:gym")
    first, second = session_ids(services)

    await chat.tap(day_edit.fix_start, f"dfix:{TODAY}")
    assert chat.texts()[-1] == "¿Qué sesión quieres corregir?"
    assert markup_data(chat.bot) == [f"fpick:{first}", f"fpick:{second}"]

    await chat.tap(day_edit.fix_pick, f"fpick:{second}")
    assert chat.texts()[-1].startswith("Gimnasio. Elige lo que hiciste de verdad")


@pytest.mark.anyio
async def test_deleting_one_of_two_sessions(chat, services):
    await chat.tap(flow.choice_made, f"ci:{TODAY}:box")
    await chat.tap(day_edit.add_set, f"aset:{TODAY}:gym")
    box_id, _ = session_ids(services)
    await chat.tap(day_edit.fix_delete, f"fdel:{box_id}")
    assert chat.texts()[-1] == "Sesión borrada. Queda apuntado: Gimnasio."
    sessions, checkin = day_sessions(services)
    assert [s[0] for s in sessions] == [GYM]
    assert checkin.choice is GYM  # the day follows its first remaining session


@pytest.mark.anyio
async def test_deleting_the_only_session_lets_the_day_be_answered_again(chat, services):
    session_id = await answer_box_with_photo(chat)
    await chat.tap(day_edit.fix_delete, f"fdel:{session_id}")
    assert "escribe /hoy para responder otra vez" in chat.texts()[-1]
    _, checkin = day_sessions(services)
    assert checkin.choice is None

    await chat.send(flow.today, message_update(text="/hoy"))
    assert chat.texts()[-1] == "¿Qué toca hoy?"
    assert list(services.data_dir.rglob("*.jpg")) == []


@pytest.mark.anyio
async def test_a_session_that_no_longer_exists(chat):
    state = await chat.tap(day_edit.fix_set, "fset:999:gym")
    assert state == ConversationHandler.END
    assert chat.texts()[-1] == "Esa sesión ya no existe."


# --- Add session ------------------------------------------------------------------------


@pytest.mark.anyio
async def test_box_in_the_morning_and_gym_in_the_evening(chat, services):
    await answer_box_with_photo(chat)
    await chat.tap(day_edit.add_start, f"dadd:{TODAY}")
    assert chat.texts()[-1] == "¿Qué sesión añades?"
    assert markup_data(chat.bot) == [
        f"aset:{TODAY}:gym",
        f"aset:{TODAY}:box",
        f"aset:{TODAY}:other",
    ]

    assert await chat.tap(day_edit.add_set, f"aset:{TODAY}:gym") is Flow.NOTE
    assert "Añadida: Gimnasio" in chat.texts()
    await chat.send(flow.note_text, message_update(text="Torso A"))
    sessions, checkin = day_sessions(services)
    assert [(s[0], s[2]) for s in sessions] == [(BOX, None), (GYM, "Torso A")]
    assert checkin.choice is BOX  # the first session of the day


@pytest.mark.anyio
async def test_gym_on_a_rest_day_replaces_the_rest(chat, services):
    await chat.tap(flow.choice_made, f"ci:{TODAY}:rest")
    await chat.tap(day_edit.add_set, f"aset:{TODAY}:gym")
    assert "Cambio el descanso por Gimnasio" in chat.texts()
    sessions, checkin = day_sessions(services)
    assert [s[0] for s in sessions] == [GYM]
    assert checkin.choice is GYM


@pytest.mark.anyio
async def test_a_walk_on_a_rest_day_keeps_the_rest(chat, services):
    await chat.tap(flow.choice_made, f"ci:{TODAY}:rest")
    await chat.tap(day_edit.add_set, f"aset:{TODAY}:other")
    sessions, checkin = day_sessions(services)
    assert [s[0] for s in sessions] == [REST, OTHER]
    assert checkin.choice is REST


@pytest.mark.anyio
@pytest.mark.parametrize(
    "data", [f"aset:{TODAY}:rest", f"aset:{TODAY + dt.timedelta(days=1)}:gym", "aset:x"]
)
async def test_invalid_add_buttons_do_nothing(chat, services, data):
    assert await chat.tap(day_edit.add_set, data) == ConversationHandler.END
    assert day_sessions(services)[0] == []


# --- Wiring ---------------------------------------------------------------------------


def test_corrected_and_added_types_open_the_conversation():
    conversation = flow.build_conversation()
    entry = conversation.entry_points
    assert any(h.check_update(callback_update(data="fset:1:gym")) for h in entry)
    assert any(h.check_update(callback_update(data=f"aset:{TODAY}:box")) for h in entry)
    assert not any(h.check_update(callback_update(data="fdel:1")) for h in entry)
