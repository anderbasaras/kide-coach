"""Everything the handlers need, built once at startup and kept in ``bot_data``.

Handlers get it with :func:`get_services` instead of importing globals, so tests can
pass their own profile, database and clock.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session, sessionmaker
from telegram.ext import ContextTypes

from kide.config import Settings
from kide.i18n import Translator
from kide.llm.client import LlmClient
from kide.profile import Profile

SERVICES_KEY = "services"


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass
class Services:
    settings: Settings
    profile: Profile
    translator: Translator
    session_factory: sessionmaker[Session]
    user_id: int
    # Language chosen with /language (stored in users.language), None = profile language.
    chosen_language: str | None = None
    clock: Callable[[], dt.datetime] = field(default=utc_now)
    # None when ANTHROPIC_API_KEY or ANTHROPIC_MODEL is missing: the bot keeps working
    # as in phase 2 (inputs saved, not interpreted; no coach chat).
    llm: LlmClient | None = None

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.profile.identity.timezone)

    @property
    def data_dir(self) -> Path:
        return self.settings.data_dir

    @property
    def locale(self) -> str:
        """Language of the bot user: the /language choice wins over the profile (D-027)."""
        if self.chosen_language in self.translator.available_locales:
            return self.chosen_language
        return self.translator.default_locale

    def t(self, key: str, locale: str | None = None, **params: object) -> str:
        """Translate ``key`` in the user's language (or ``locale``)."""
        return self.translator.t(key, locale=locale or self.locale, **params)


def get_services(context: ContextTypes.DEFAULT_TYPE) -> Services:
    services = context.bot_data[SERVICES_KEY]
    if not isinstance(services, Services):  # pragma: no cover - programming error
        raise TypeError("bot_data['services'] is not a Services instance")
    return services
