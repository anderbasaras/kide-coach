"""Everything the handlers need, built once at startup and kept in ``bot_data``.

Handlers get it with :func:`get_services` instead of importing globals, so tests can
pass their own profile, database and clock.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy.orm import Session, sessionmaker
from telegram.ext import ContextTypes

from kide.config import Settings
from kide.i18n import Translator
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
    clock: Callable[[], dt.datetime] = field(default=utc_now)

    def t(self, key: str, **params: object) -> str:
        """Translate ``key`` in the user's language."""
        return self.translator.t(key, locale=self.locale, **params)

    @property
    def locale(self) -> str:
        """Language for the bot user. The /language choice is added in step 3."""
        return self.profile.identity.language


def get_services(context: ContextTypes.DEFAULT_TYPE) -> Services:
    services = context.bot_data[SERVICES_KEY]
    if not isinstance(services, Services):  # pragma: no cover - programming error
        raise TypeError("bot_data['services'] is not a Services instance")
    return services
