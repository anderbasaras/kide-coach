import pytest
import yaml
from telegram.ext import ApplicationBuilder, CommandHandler

from kide.bot.app import prepare_services
from kide.bot.handlers import commands
from kide.storage.repositories import unit_of_work
from tests.bot_helpers import bind, make_context, sent_texts
from tests.telegram_factories import callback_update, message_update


async def call(handler, update, services, bot):
    await handler(bind(update, bot), make_context(services, bot))


# --- /start and /help -----------------------------------------------------------------


@pytest.mark.anyio
async def test_start_greets_checks_profile_and_warns(services, bot):
    await call(commands.start, message_update(text="/start"), services, bot)
    (text,) = sent_texts(bot)
    assert text.startswith("Hola, Alex. Soy Kide")
    assert "Rotación: Upper → Lower" in text
    assert "09:00 (Europe/Berlin)" in text
    assert "/hoy" in text
    assert "no es consejo médico" in text


@pytest.mark.anyio
async def test_help_lists_commands_in_the_user_language(services, bot):
    await call(commands.help_command, message_update(text="/ayuda"), services, bot)
    (text,) = sent_texts(bot)
    assert "/hoy · ¿Qué toca hoy? (check-in)" in text
    assert "/idioma · Cambiar el idioma" in text


@pytest.mark.anyio
async def test_help_follows_the_chosen_language(services, bot):
    services.chosen_language = "en"
    await call(commands.help_command, message_update(text="/help"), services, bot)
    assert "/today · What's on today? (check-in)" in sent_texts(bot)[0]


# --- /language ------------------------------------------------------------------------


@pytest.mark.anyio
async def test_language_shows_buttons(services, bot):
    await call(commands.language, message_update(text="/idioma"), services, bot)
    markup = bot.send_message.await_args.kwargs["reply_markup"]
    assert [b.callback_data for b in markup.inline_keyboard[0]] == ["lang:en", "lang:es"]


@pytest.mark.anyio
async def test_choosing_a_language_saves_it_and_updates_the_menu(services, bot):
    await call(commands.language_chosen, callback_update(data="lang:en"), services, bot)

    assert services.chosen_language == "en"
    assert services.locale == "en"
    with unit_of_work(services.session_factory) as repos:
        assert repos.users.get_or_create("Alex").language == "en"
    assert sent_texts(bot) == ["Done, I'll speak English from now on."]
    bot.answer_callback_query.assert_awaited()
    menu = bot.set_my_commands.await_args
    assert menu.args[0][0].command == "today"
    assert menu.kwargs["scope"].chat_id == services.settings.allowed_chat_ids[0]


@pytest.mark.anyio
async def test_unknown_language_is_ignored(services, bot):
    await call(commands.language_chosen, callback_update(data="lang:xx"), services, bot)
    assert services.chosen_language is None
    assert sent_texts(bot) == []
    bot.answer_callback_query.assert_awaited()  # stops the spinner on the button


def test_chosen_language_survives_a_restart(tmp_path, profile_data, services):
    """A new Services built from the same database starts with the stored choice."""
    with unit_of_work(services.session_factory) as repos:
        repos.users.set_language(repos.users.get_or_create("Alex"), "en")

    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(yaml.safe_dump(profile_data), encoding="utf-8")
    settings = services.settings.model_copy(update={"profile_path": profile_path})
    restarted = prepare_services(settings)
    assert restarted.chosen_language == "en"
    assert restarted.locale == "en"


# --- Registration ---------------------------------------------------------------------


def test_every_alias_is_registered(services):
    application = ApplicationBuilder().token("123:fake").build()
    commands.register(application, services)
    registered = set()
    for handler in application.handlers[0]:
        if isinstance(handler, CommandHandler):
            registered |= handler.commands
    assert {"start", "help", "ayuda", "language", "idioma"} <= registered
