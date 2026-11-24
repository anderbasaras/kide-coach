import logging

import pytest
from telegram import Update

from kide.bot.handlers import fallback
from tests.bot_helpers import make_context
from tests.telegram_factories import STRANGER_CHAT, message_update


def error_context(services, bot, error):
    context = make_context(services, bot)
    context.error = error
    return context


@pytest.mark.anyio
async def test_job_errors_are_logged_without_reply(services, bot, caplog):
    caplog.set_level(logging.ERROR, logger="kide.bot.handlers.fallback")
    await fallback.on_error(None, error_context(services, bot, RuntimeError("job broke")))
    assert "Error outside an update: RuntimeError" in caplog.text
    bot.send_message.assert_not_awaited()


@pytest.mark.anyio
async def test_updates_without_chat_are_logged_without_reply(services, bot, caplog):
    caplog.set_level(logging.ERROR, logger="kide.bot.handlers.fallback")
    await fallback.on_error(Update(update_id=1), error_context(services, bot, ValueError()))
    assert "chat_id=None" in caplog.text
    bot.send_message.assert_not_awaited()


@pytest.mark.anyio
async def test_never_replies_to_a_chat_that_is_not_allowed(services, bot):
    update = message_update(STRANGER_CHAT, text="x")
    await fallback.on_error(update, error_context(services, bot, RuntimeError()))
    bot.send_message.assert_not_awaited()
