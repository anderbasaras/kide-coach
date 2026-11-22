from pathlib import Path

import pytest
from pydantic import ValidationError

from kide.config import ConfigError, Settings

ENV_VARS = (
    "TELEGRAM_BOT_TOKEN",
    "ALLOWED_CHAT_IDS",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "DATA_DIR",
    "DATABASE_URL",
    "PROFILE_PATH",
    "LOG_LEVEL",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def make(**env: str) -> Settings:
    """Build settings from explicit env vars only (ignores any local .env)."""
    with pytest.MonkeyPatch.context() as mp:
        for key, value in env.items():
            mp.setenv(key, value)
        return Settings(_env_file=None)


def test_defaults_need_no_secrets():
    settings = make()
    assert settings.telegram_bot_token is None
    assert settings.allowed_chat_ids == []
    assert settings.data_dir == Path("var")
    assert settings.database_url == "sqlite:///var/kide.db"
    assert settings.log_level == "INFO"


def test_chat_ids_from_comma_separated_string():
    settings = make(ALLOWED_CHAT_IDS=" 123456789, -1001234567890 ,")
    assert settings.allowed_chat_ids == [123456789, -1001234567890]


def test_empty_chat_ids():
    assert make(ALLOWED_CHAT_IDS="").allowed_chat_ids == []


@pytest.mark.parametrize("raw", ["abc", "12,x", "0"])
def test_invalid_chat_ids_rejected(raw):
    with pytest.raises(ValidationError):
        make(ALLOWED_CHAT_IDS=raw)


def test_database_url_follows_data_dir():
    assert make(DATA_DIR="/srv/kide").database_url == "sqlite:////srv/kide/kide.db"


def test_explicit_database_url_wins():
    settings = make(DATA_DIR="/srv/kide", DATABASE_URL="sqlite:///other.db")
    assert settings.database_url == "sqlite:///other.db"


def test_log_level_is_case_insensitive():
    assert make(LOG_LEVEL="debug").log_level == "DEBUG"
    with pytest.raises(ValidationError):
        make(LOG_LEVEL="verbose")


def test_empty_model_is_none():
    assert make(ANTHROPIC_MODEL="").anthropic_model is None


def test_secrets_are_masked():
    settings = make(TELEGRAM_BOT_TOKEN="123:secret-token")
    assert "secret-token" not in repr(settings)
    assert settings.telegram_bot_token is not None
    assert settings.telegram_bot_token.get_secret_value() == "123:secret-token"


def test_require_lists_every_missing_setting():
    settings = make(TELEGRAM_BOT_TOKEN="123:abc")
    settings.require("telegram_bot_token")
    with pytest.raises(ConfigError, match="ALLOWED_CHAT_IDS, ANTHROPIC_API_KEY"):
        settings.require("telegram_bot_token", "allowed_chat_ids", "anthropic_api_key")


def test_reads_env_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("ALLOWED_CHAT_IDS=42\nLOG_LEVEL=warning\n", encoding="utf-8")
    settings = Settings(_env_file=env_file)
    assert settings.allowed_chat_ids == [42]
    assert settings.log_level == "WARNING"


def test_ensure_data_dir_creates_folder(tmp_path):
    settings = make(DATA_DIR=str(tmp_path / "nested" / "var"))
    assert settings.ensure_data_dir().is_dir()
