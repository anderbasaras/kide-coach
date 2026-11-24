"""Only the chats in ALLOWED_CHAT_IDS may use the bot.

Anyone can find a Telegram bot and write to it, so every update goes through
:func:`make_auth_guard` before any other handler (group -1). Updates from other
chats are dropped without a reply. The log records the chat id, the chat type and
the kind of update, never the text or the media (principle 5). That log line is
also how a new user finds their own chat id (see the README).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass, field

from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes, TypeHandler

logger = logging.getLogger(__name__)

# Seconds during which repeated updates from the same unauthorized chat are not logged again.
LOG_INTERVAL_S = 600.0


def is_allowed(chat_id: int | None, allowed: Collection[int]) -> bool:
    """True only for a known chat. Updates without a chat (inline queries...) are refused."""
    return chat_id is not None and chat_id in allowed


def update_kind(update: Update) -> str:
    """Name of the field that carries the update, e.g. ``message`` or ``callback_query``."""
    for kind in Update.ALL_TYPES:
        if getattr(update, kind, None) is not None:
            return str(kind)
    return "unknown"


@dataclass
class LogLimiter:
    """Decides whether to log an event for ``key``: at most once per ``interval`` seconds."""

    interval: float = LOG_INTERVAL_S
    _last: dict[int | None, float] = field(default_factory=dict)

    def should_log(self, key: int | None, now: float) -> bool:
        last = self._last.get(key)
        if last is not None and now - last < self.interval:
            return False
        self._last[key] = now
        return True


def make_auth_guard(
    allowed: Collection[int],
    limiter: LogLimiter | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> TypeHandler[Update, ContextTypes.DEFAULT_TYPE]:
    """Handler for group -1 that stops every update from a chat that is not allowed."""
    allowed_ids = frozenset(allowed)
    log_limiter = limiter or LogLimiter()

    async def guard(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        chat_id = chat.id if chat else None
        if is_allowed(chat_id, allowed_ids):
            return
        if log_limiter.should_log(chat_id, clock()):
            logger.warning(
                "Ignored update from unauthorized chat: chat_id=%s chat_type=%s update=%s",
                chat_id,
                chat.type if chat else None,
                update_kind(update),
            )
        raise ApplicationHandlerStop

    return TypeHandler(Update, guard)
