import pytest
from telegram import BotCommandScopeChat
from telegram.error import NetworkError

from kide.bot.keyboards import callback_data, language_keyboard, parse_callback
from kide.bot.menu import (
    ALL_COMMANDS,
    COMMAND_NAME_RE,
    command_names,
    menu_for,
    sync_menus,
)
from kide.i18n import Translator
from tests.bot_helpers import make_bot

T = Translator.from_directory(default_locale="es")


# --- Command names ------------------------------------------------------------------


@pytest.mark.parametrize("locale", T.available_locales)
def test_command_names_are_valid_for_telegram(locale):
    for command in ALL_COMMANDS:
        assert COMMAND_NAME_RE.fullmatch(T.t(f"commands.{command}.name", locale=locale))


def test_english_names_are_the_command_ids():
    assert [T.t(f"commands.{c}.name", locale="en") for c in ALL_COMMANDS] == list(ALL_COMMANDS)


@pytest.mark.parametrize("locale", T.available_locales)
def test_names_do_not_clash_within_a_language(locale):
    names = [T.t(f"commands.{c}.name", locale=locale) for c in ALL_COMMANDS]
    assert len(names) == len(set(names))


def test_spanish_aliases():
    assert command_names(T, "today") == ["today", "hoy"]
    assert command_names(T, "help") == ["help", "ayuda"]
    assert command_names(T, "language") == ["language", "idioma"]
    assert command_names(T, "start") == ["start"]


def test_menu_per_language():
    first = menu_for(T, "es")[0]
    assert (first.command, first.description) == ("hoy", "¿Qué toca hoy? (check-in)")
    assert [c.command for c in menu_for(T, "en")] == ["today", "help", "language"]


# --- Menus in Telegram ----------------------------------------------------------------


@pytest.mark.anyio
async def test_sync_menus_sets_default_language_and_chat_menus():
    bot = make_bot()
    await sync_menus(bot, T, [111, 222], locale="en")
    calls = bot.set_my_commands.await_args_list
    default = [c for c in calls if not c.kwargs]
    per_language = {c.kwargs["language_code"]: c for c in calls if "language_code" in c.kwargs}
    per_chat = {c.kwargs["scope"].chat_id: c for c in calls if "scope" in c.kwargs}

    assert default[0].args[0][0].command == "hoy"  # profile language (es)
    assert set(per_language) == set(T.available_locales)
    assert set(per_chat) == {111, 222}
    assert all(isinstance(c.kwargs["scope"], BotCommandScopeChat) for c in per_chat.values())
    assert per_chat[111].args[0][0].command == "today"  # the /language choice


@pytest.mark.anyio
async def test_menu_errors_are_not_fatal(caplog):
    bot = make_bot()
    bot.set_my_commands.side_effect = NetworkError("down")
    await sync_menus(bot, T, [111], locale="es")
    assert "Could not update the command menu" in caplog.text


# --- Callback data --------------------------------------------------------------------


def test_callback_data_round_trip():
    data = callback_data("ci", "2026-11-23", "box")
    assert data == "ci:2026-11-23:box"
    assert parse_callback(data, "ci") == ["2026-11-23", "box"]
    assert parse_callback(data, "lang") is None
    assert parse_callback(None, "ci") is None
    assert parse_callback("ci", "ci") == []


def test_callback_data_respects_telegram_limit():
    with pytest.raises(ValueError, match="64 bytes"):
        callback_data("x", "y" * 70)


def test_language_keyboard_labels_each_language_in_itself():
    buttons = language_keyboard(T).inline_keyboard[0]
    assert {b.text: b.callback_data for b in buttons} == {
        "English": "lang:en",
        "Español": "lang:es",
    }
