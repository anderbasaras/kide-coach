import logging

import pytest
from telegram import Update
from telegram.ext import ApplicationHandlerStop

from kide.bot.auth import LogLimiter, is_allowed, make_auth_guard, update_kind
from tests.telegram_factories import (
    ALLOWED_CHAT,
    STRANGER_CHAT,
    callback_update,
    edited_update,
    message_update,
)

# --- Pure rules ---------------------------------------------------------------------


def test_only_listed_chats_are_allowed():
    assert is_allowed(ALLOWED_CHAT, {ALLOWED_CHAT})
    assert not is_allowed(STRANGER_CHAT, {ALLOWED_CHAT})


def test_updates_without_chat_are_refused():
    assert not is_allowed(None, {ALLOWED_CHAT})


def test_update_kind():
    assert update_kind(message_update()) == "message"
    assert update_kind(edited_update()) == "edited_message"
    assert update_kind(callback_update()) == "callback_query"
    assert update_kind(Update(update_id=9)) == "unknown"


def test_log_limiter_logs_once_per_interval_and_chat():
    limiter = LogLimiter(interval=600)
    assert limiter.should_log(1, now=0)
    assert not limiter.should_log(1, now=599)
    assert limiter.should_log(2, now=599)
    assert limiter.should_log(1, now=600)


# --- Guard handler ------------------------------------------------------------------


async def run_guard(update, limiter=None, clock=lambda: 0.0):
    handler = make_auth_guard({ALLOWED_CHAT}, limiter=limiter, clock=clock)
    assert handler.check_update(update)  # the guard sees every Update
    return await handler.callback(update, None)


@pytest.mark.anyio
@pytest.mark.parametrize("factory", [message_update, edited_update, callback_update])
async def test_allowed_chat_passes(factory):
    assert await run_guard(factory(ALLOWED_CHAT)) is None


@pytest.mark.anyio
@pytest.mark.parametrize("factory", [message_update, edited_update, callback_update])
async def test_stranger_is_stopped_on_every_update_type(factory):
    with pytest.raises(ApplicationHandlerStop):
        await run_guard(factory(STRANGER_CHAT))


@pytest.mark.anyio
async def test_stranger_is_logged_without_content(caplog):
    caplog.set_level(logging.WARNING, logger="kide.bot.auth")
    with pytest.raises(ApplicationHandlerStop):
        await run_guard(message_update(STRANGER_CHAT, text="my secret WOD 21-15-9"))
    assert f"chat_id={STRANGER_CHAT}" in caplog.text
    assert "chat_type=private" in caplog.text
    assert "update=message" in caplog.text
    assert "secret" not in caplog.text


@pytest.mark.anyio
async def test_callback_data_is_not_logged(caplog):
    caplog.set_level(logging.WARNING, logger="kide.bot.auth")
    with pytest.raises(ApplicationHandlerStop):
        await run_guard(callback_update(STRANGER_CHAT, data="private-payload"))
    assert "update=callback_query" in caplog.text
    assert "private-payload" not in caplog.text


@pytest.mark.anyio
async def test_repeated_strangers_do_not_flood_the_log(caplog):
    caplog.set_level(logging.WARNING, logger="kide.bot.auth")
    limiter = LogLimiter(interval=600)
    for _ in range(5):
        with pytest.raises(ApplicationHandlerStop):
            await run_guard(message_update(STRANGER_CHAT), limiter=limiter)
    assert caplog.text.count("unauthorized chat") == 1
