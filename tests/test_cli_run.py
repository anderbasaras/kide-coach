"""`kide run` startup: every failure is one clear line and exit code 1, never a traceback."""

from pathlib import Path

import pytest
from telegram.error import InvalidToken
from telegram.ext import Application, TypeHandler

from kide.bot import app as bot_app
from kide.cli import main
from kide.config import get_settings

EXAMPLE_PROFILE = Path(__file__).parent.parent / "profile.example.yaml"
FAKE_TOKEN = "123456:ABC-fake-token"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Run in an empty folder (no real .env) with logging setup disabled."""
    monkeypatch.chdir(tmp_path)
    for name in ("TELEGRAM_BOT_TOKEN", "ALLOWED_CHAT_IDS", "DATABASE_URL", "PROFILE_PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "var"))
    monkeypatch.setattr(bot_app, "configure_logging", lambda *a, **k: None)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def valid_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "111111")
    monkeypatch.setenv("PROFILE_PATH", str(EXAMPLE_PROFILE))


def run_cli(capsys) -> tuple[int, str]:
    code = main(["run"])
    return code, capsys.readouterr().err


def test_missing_secrets_are_named(capsys):
    code, err = run_cli(capsys)
    assert code == 1
    assert "TELEGRAM_BOT_TOKEN, ALLOWED_CHAT_IDS" in err
    assert "Traceback" not in err


def test_invalid_chat_ids(capsys, monkeypatch):
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "me")
    code, err = run_cli(capsys)
    assert code == 1
    assert "ALLOWED_CHAT_IDS" in err


def test_missing_profile(capsys, valid_env, monkeypatch, tmp_path):
    monkeypatch.setenv("PROFILE_PATH", str(tmp_path / "nope.yaml"))
    code, err = run_cli(capsys)
    assert code == 1
    assert "file not found" in err


def test_invalid_profile(capsys, valid_env, monkeypatch, tmp_path):
    bad = tmp_path / "profile.yaml"
    bad.write_text("identity:\n  name: Alex\n  timezone: Mars/Base\n", encoding="utf-8")
    monkeypatch.setenv("PROFILE_PATH", str(bad))
    code, err = run_cli(capsys)
    assert code == 1
    assert "unknown time zone" in err


def test_unusable_database(capsys, valid_env, monkeypatch, tmp_path):
    folder = tmp_path / "a-folder"
    folder.mkdir()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{folder.as_posix()}")
    code, err = run_cli(capsys)
    assert code == 1
    assert "Cannot open or upgrade the database" in err


def test_rejected_token(capsys, valid_env, monkeypatch):
    def reject(self, **kwargs):
        raise InvalidToken("Not Found")

    monkeypatch.setattr(Application, "run_polling", reject)
    code, err = run_cli(capsys)
    assert code == 1
    assert "@BotFather" in err
    assert FAKE_TOKEN not in err


def test_valid_setup_creates_database_and_polls(capsys, valid_env, monkeypatch, tmp_path):
    calls = {}

    def fake_polling(self, **kwargs):
        calls.update(kwargs)

    monkeypatch.setattr(Application, "run_polling", fake_polling)
    code, _ = run_cli(capsys)
    assert code == 0
    assert (tmp_path / "var" / "kide.db").is_file()
    assert set(calls["allowed_updates"]) == {"message", "edited_message", "callback_query"}


def test_auth_guard_runs_before_every_other_handler(valid_env):
    services = bot_app.prepare_services(get_settings())
    application = bot_app.build_application(services)
    first_group = min(application.handlers)
    assert first_group < 0
    assert isinstance(application.handlers[first_group][0], TypeHandler)


def test_ctrl_c_during_shutdown_is_a_clean_exit(capsys, valid_env, monkeypatch):
    def interrupted(self, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(Application, "run_polling", interrupted)
    code, err = run_cli(capsys)
    assert code == 0
    assert "Traceback" not in err
