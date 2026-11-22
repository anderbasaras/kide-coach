"""Application settings read from the environment and an optional ``.env`` file.

Secrets are optional at load time so that commands which do not need them
(e.g. ``kide db upgrade``) work without a Telegram token or an API key. Each
component calls :meth:`Settings.require` for the secrets it actually needs.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

DEFAULT_DB_FILENAME = "kide.db"


class ConfigError(RuntimeError):
    """Raised when a required setting is missing or invalid."""


class Settings(BaseSettings):
    """Runtime configuration. Field names map to upper-case environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    telegram_bot_token: SecretStr | None = None
    # One instance serves one person (D-011); several chats may share that profile.
    allowed_chat_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str | None = None
    # User data (database, photos, exports) lives here, never in the repo (D-010).
    data_dir: Path = Path("var")
    # Defaults to a SQLite file inside data_dir when not set.
    database_url: str = ""
    profile_path: Path = Path("profile.yaml")
    log_level: LogLevel = "INFO"

    @field_validator("allowed_chat_ids", mode="before")
    @classmethod
    def _parse_chat_ids(cls, value: object) -> object:
        """Accept a comma-separated string such as ``"123, -100456"``."""
        if isinstance(value, str):
            parts = [part.strip() for part in value.split(",")]
            return [part for part in parts if part]
        return value

    @field_validator("allowed_chat_ids")
    @classmethod
    def _non_zero_chat_ids(cls, value: list[int]) -> list[int]:
        if any(chat_id == 0 for chat_id in value):
            raise ValueError("chat ids must be non-zero integers")
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("anthropic_model", mode="before")
    @classmethod
    def _empty_model_is_none(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def _default_database_url(self) -> Settings:
        if not self.database_url.strip():
            db_path = (self.data_dir / DEFAULT_DB_FILENAME).as_posix()
            self.database_url = f"sqlite:///{db_path}"
        return self

    def require(self, *names: str) -> None:
        """Raise :class:`ConfigError` listing every setting in ``names`` that is empty."""
        missing = [name.upper() for name in names if not _is_set(getattr(self, name))]
        if missing:
            raise ConfigError(
                f"Missing required setting(s): {', '.join(missing)}. "
                "Set them in your .env file (see .env.example)."
            )

    def ensure_data_dir(self) -> Path:
        """Create ``data_dir`` if needed and return it."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return self.data_dir


def _is_set(value: object) -> bool:
    if isinstance(value, SecretStr):
        return bool(value.get_secret_value().strip())
    if isinstance(value, str | list | tuple):
        return bool(value)
    return value is not None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings (read once)."""
    return Settings()
