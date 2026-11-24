"""Inline keyboards and their callback data.

Callback data is ``<prefix>:<payload>`` (Telegram allows at most 64 bytes). Each
feature has its own prefix so its handler can match on it.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from kide.i18n import Translator

LANGUAGE_PREFIX = "lang"
MAX_CALLBACK_BYTES = 64


def callback_data(prefix: str, *parts: str) -> str:
    data = ":".join((prefix, *parts))
    if len(data.encode()) > MAX_CALLBACK_BYTES:
        raise ValueError(f"callback data longer than {MAX_CALLBACK_BYTES} bytes: {data!r}")
    return data


def parse_callback(data: str | None, prefix: str) -> list[str] | None:
    """Payload parts if ``data`` starts with ``prefix``, else ``None``."""
    if not data:
        return None
    head, _, rest = data.partition(":")
    if head != prefix:
        return None
    return rest.split(":") if rest else []


def language_keyboard(translator: Translator) -> InlineKeyboardMarkup:
    """One button per available language, each labelled in its own language."""
    buttons = [
        InlineKeyboardButton(
            translator.t("meta.language_name", locale=locale),
            callback_data=callback_data(LANGUAGE_PREFIX, locale),
        )
        for locale in translator.available_locales
    ]
    return InlineKeyboardMarkup([buttons])
