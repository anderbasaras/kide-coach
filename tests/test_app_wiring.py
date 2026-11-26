"""Updates through the real Application: auth guard, commands and the check-in flow wired
together. Only the Bot API calls are replaced; nothing goes to the network."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.ext import ExtBot

from kide.bot.app import build_application
from kide.storage import DayType
from kide.storage.repositories import unit_of_work
from tests.bot_helpers import make_services
from tests.telegram_factories import (
    STRANGER_CHAT,
    callback_update,
    document_update,
    edited_update,
    message_update,
    photo_update,
    sticker_update,
    voice_update,
)


@pytest.fixture
def api(monkeypatch):
    """Replace the Bot API methods used by the handlers on every ExtBot."""
    calls = MagicMock()
    for method in (
        "send_message",
        "edit_message_text",
        "edit_message_reply_markup",
        "answer_callback_query",
        "set_my_commands",
        "send_chat_action",
    ):
        mock = AsyncMock()
        calls.attach_mock(mock, method)
        monkeypatch.setattr(ExtBot, method, mock)

    async def download(path):
        from pathlib import Path

        Path(path).write_bytes(b"img")

    telegram_file = MagicMock()
    telegram_file.download_to_drive = AsyncMock(side_effect=download)
    get_file = AsyncMock(return_value=telegram_file)
    calls.attach_mock(get_file, "get_file")
    monkeypatch.setattr(ExtBot, "get_file", get_file)
    return calls


def texts(api):
    return [kw.get("text") for name, _a, kw in api.mock_calls if name == "send_message"]


@pytest.fixture
async def app(tmp_path, profile_data, api):
    profile_data["identity"]["language"] = "es"
    services = make_services(tmp_path, profile_data)
    application = build_application(services)
    async with application:  # initialize() without polling
        await application.start()  # starts the JobQueue (conversation timeout)
        yield application, services
        await application.stop()


async def feed(application, update):
    update.set_bot(application.bot)
    for obj in (update.message, update.edited_message, update.callback_query):
        if obj is not None:
            obj.set_bot(application.bot)
            if getattr(obj, "message", None) is not None:
                obj.message.set_bot(application.bot)
            for photo in getattr(obj, "photo", ()) or ():
                photo.set_bot(application.bot)
    await application.process_update(update)


@pytest.fixture(autouse=True)
def no_get_me(monkeypatch):
    """initialize() asks Telegram who the bot is; answer locally."""
    from telegram import User

    me = User(id=1, first_name="Kide", is_bot=True, username="kide_test_bot")

    async def get_me(self, *args, **kwargs):
        self._bot_user = me  # what the real get_me stores
        return me

    monkeypatch.setattr(ExtBot, "get_me", get_me)


@pytest.mark.anyio
async def test_full_box_day_through_the_application(app, api):
    application, services = app
    day = services.clock().date()

    await feed(application, message_update(text="/hoy"))
    assert texts(api)[-1] == "¿Qué toca hoy?"

    await feed(application, callback_update(data=f"ci:{day}:box"))
    assert "Mándame el WOD" in texts(api)[-1]

    await feed(application, message_update(text="EMOM 10: 5 thrusters"))
    await feed(application, photo_update("PH9"))
    with unit_of_work(services.session_factory) as repos:
        (training,) = repos.sessions.for_day(services.user_id, day)
        session_id = training.id
    await feed(application, callback_update(data=f"wod:{session_id}"))
    assert texts(api)[-1] == "WOD guardado. Fotos: 1 · Texto: Sí."

    # After "Done" a text is no longer part of the WOD.
    await feed(application, message_update(text="something else"))
    with unit_of_work(services.session_factory) as repos:
        (training,) = repos.sessions.for_day(services.user_id, day)
        assert training.type is DayType.BOX
        assert training.raw_input == "EMOM 10: 5 thrusters"
        assert len(training.photos) == 1


@pytest.mark.anyio
async def test_box_read_adjusted_and_confirmed_through_the_application(app, api):
    """Claude's reading, Adjust (a message in the open conversation) and Confirm."""
    from kide.catalog.movements import default_movements
    from kide.llm.client import LlmClient
    from kide.llm.parsing import Parser
    from tests.llm_fakes import FakeSdk, tool_message

    application, services = app
    wod = {
        "format": "emom",
        "confidence": "high",
        "blocks": [
            {"format": "emom", "duration_min": 10, "movements": [{"name": "TH", "reps": 5}]}
        ],
    }
    sdk = FakeSdk(
        tool_message("record_wod", wod),
        tool_message("record_wod", {**wod, "blocks": [{**wod["blocks"][0], "duration_min": 12}]}),
    )
    services.llm = LlmClient(
        sdk,
        chat_model="claude-opus-5-5",
        parse_model="claude-haiku-5-5",
        session_factory=services.session_factory,
        user_id=services.user_id,
    )
    services.parser = Parser(services.llm, default_movements(), services.data_dir)
    day = services.clock().date()

    await feed(application, callback_update(data=f"ci:{day}:box"))
    await feed(application, message_update(text="EMOM 10: 5 thrusters"))
    with unit_of_work(services.session_factory) as repos:
        session_id = repos.sessions.for_day(services.user_id, day)[0].id
    await feed(application, callback_update(data=f"wod:{session_id}"))
    assert texts(api)[-1].startswith("WOD · EMOM\nEMOM · 10 min")

    await feed(application, callback_update(data=f"radj:{session_id}"))
    await feed(application, message_update(text="eran 12 minutos"))
    assert texts(api)[-1].startswith("WOD · EMOM\nEMOM · 12 min")

    await feed(application, callback_update(data=f"rok:{session_id}"))
    assert texts(api)[-1] == "WOD guardado."
    with unit_of_work(services.session_factory) as repos:
        record = repos.sessions.get(session_id).parsed_json
    assert record["status"] == "confirmed"
    assert record["data"]["blocks"][0]["movements"][0]["movement_id"] == "thruster"


@pytest.mark.anyio
async def test_review_buttons_work_after_a_restart(app, api):
    """No open conversation (e.g. the bot restarted): Confirm still confirms the draft."""
    from kide.llm.schemas import ParsedRecord, Wod

    application, services = app
    day = services.clock().date()
    wod = Wod.model_validate(
        {
            "format": "amrap",
            "confidence": "high",
            "blocks": [{"format": "amrap", "movements": [{"name": "Burpees", "reps": 5}]}],
        }
    )
    # A box session with a draft, created without any conversation (as after a restart).
    with unit_of_work(services.session_factory) as repos:
        checkin = repos.checkins.get_or_create(services.user_id, day)
        training = repos.sessions.create(checkin, DayType.BOX, raw_input="5 burpees")
        repos.sessions.set_parsed(training, ParsedRecord.draft(wod).to_json())
        session_id = training.id

    await feed(application, callback_update(data=f"rok:{session_id}"))
    assert texts(api)[-1] == "WOD guardado."


@pytest.mark.anyio
async def test_stranger_gets_nothing_through_the_application(app, api):
    application, services = app
    day = services.clock().date()
    await feed(application, message_update(STRANGER_CHAT, text="/hoy"))
    await feed(application, callback_update(STRANGER_CHAT, data=f"ci:{day}:gym"))
    assert api.mock_calls == []
    with unit_of_work(services.session_factory) as repos:
        assert repos.sessions.for_day(services.user_id, day) == []


@pytest.mark.anyio
async def test_spanish_and_english_command_names_both_work(app, api):
    application, _ = app
    await feed(application, message_update(text="/ayuda"))
    await feed(application, message_update(text="/help"))
    first, second = texts(api)
    assert first == second


@pytest.mark.anyio
async def test_real_job_queue_gets_the_daily_and_backup_jobs(app, api):
    from kide.bot import scheduler

    application, services = app
    await scheduler.start_checkin_jobs(application.bot, application.job_queue, services)
    (daily,) = application.job_queue.get_jobs_by_name(scheduler.DAILY_JOB)
    (backup,) = application.job_queue.get_jobs_by_name(scheduler.BACKUP_JOB)
    assert daily.job.misfire_grace_time is None
    assert daily.next_t == scheduler.next_checkin_at(
        services.clock(), services.profile.schedule.checkin_time, services.tz
    )
    assert backup.next_t is not None


@pytest.mark.anyio
async def test_correcting_while_a_flow_is_still_open(app, api):
    """Gym is chosen and its note flow is open; Correct -> Box must open the WOD flow."""
    application, services = app
    day = services.clock().date()
    await feed(application, callback_update(data=f"ci:{day}:gym"))  # note flow open
    with unit_of_work(services.session_factory) as repos:
        (training,) = repos.sessions.for_day(services.user_id, day)
        session_id = training.id

    await feed(application, message_update(text="/hoy"))
    assert texts(api)[-1].startswith("Hoy ya está apuntado: Gimnasio")
    await feed(application, callback_update(data=f"dfix:{day}"))
    await feed(application, callback_update(data=f"fset:{session_id}:box"))
    assert "Mándame el WOD" in texts(api)[-1]

    await feed(application, message_update(text="For time: 50 cal row"))
    with unit_of_work(services.session_factory) as repos:
        (training,) = repos.sessions.for_day(services.user_id, day)
        assert training.type is DayType.BOX
        assert training.raw_input == "For time: 50 cal row"
        assert training.notes is None  # the text went to the WOD, not to a gym note


# --- Unusual messages and errors (step 8) -------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("factory", "expected"),
    [
        (sticker_update, "No puedo usar ese tipo de mensaje"),
        (voice_update, "No puedo usar ese tipo de mensaje"),
        (lambda: document_update(mime_type="application/pdf"), "No puedo usar ese tipo"),
        (edited_update, "No proceso mensajes editados"),
        (lambda: message_update(text="hola, ¿qué tal?"), "Todavía no sé conversar"),
        (lambda: message_update(text="/foo"), "No conozco ese comando. Escribe /ayuda"),
        (lambda: photo_update("LOOSE"), "elige primero la sesión con /hoy"),
    ],
)
async def test_unusual_messages_get_a_help_text(app, api, factory, expected):
    application, services = app
    await feed(application, factory())
    assert expected in texts(api)[-1]
    with unit_of_work(services.session_factory) as repos:  # nothing is stored
        assert repos.sessions.for_day(services.user_id, services.clock().date()) == []


@pytest.mark.anyio
async def test_old_buttons_only_stop_the_spinner(app, api):
    application, _ = app
    await feed(application, callback_update(data="from-an-old-version:1"))
    assert texts(api) == []
    api.answer_callback_query.assert_awaited()


@pytest.mark.anyio
async def test_a_failing_handler_does_not_stop_the_bot(app, api, monkeypatch, caplog):
    from kide.bot.handlers import commands

    application, _ = app

    def broken(services):
        raise RuntimeError("boom")

    with monkeypatch.context() as patch:
        patch.setattr(commands, "help_text", broken)
        await feed(application, message_update(text="/ayuda secreto"))
    assert texts(api)[-1] == "Algo ha fallado. Vuelve a intentarlo en un momento."
    assert "Error handling update=message" in caplog.text
    assert "secreto" not in caplog.text  # the message content is never logged

    await feed(application, message_update(text="/ayuda"))  # still answering
    assert "Estos son mis comandos" in texts(api)[-1]
