"""Locale loading and key lookup with English fallback.

Locale files are nested YAML mappings under ``locales/<code>.yaml``. Keys are
addressed with dots (``checkin.options.gym``) and values may contain
``str.format`` placeholders (``{name}``).

Lookup order: requested locale -> default locale -> English -> the key itself.
A missing key never crashes the bot; it is logged so it can be fixed.
"""

from __future__ import annotations

import logging
import string
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

LOCALES_DIR = Path(__file__).parent / "locales"
FALLBACK_LOCALE = "en"


class LocaleError(ValueError):
    """Raised when a locale file is malformed."""


def normalize_locale(code: str) -> str:
    """Reduce a language tag to its primary subtag: ``"es-ES"`` -> ``"es"``."""
    return code.strip().replace("_", "-").split("-")[0].lower()


def flatten(tree: Mapping[str, Any], prefix: str = "") -> dict[str, str]:
    """Flatten a nested mapping into ``{"a.b.c": "text"}``. Leaves must be strings."""
    flat: dict[str, str] = {}
    for raw_key, value in tree.items():
        if not isinstance(raw_key, str):
            # YAML 1.1 turns unquoted yes/no/on/off into booleans: quote such keys.
            raise LocaleError(f"key {raw_key!r} under '{prefix or '<root>'}' must be quoted text")
        key = f"{prefix}.{raw_key}" if prefix else raw_key
        if isinstance(value, Mapping):
            flat.update(flatten(value, key))
        elif isinstance(value, str):
            flat[key] = value
        else:
            raise LocaleError(f"'{key}' must be text, got {type(value).__name__}")
    return flat


def load_locale_file(path: Path) -> dict[str, str]:
    """Read one locale file and return its flattened keys."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise LocaleError(f"{path.name}: invalid YAML: {exc}") from exc
    if not isinstance(data, Mapping):
        raise LocaleError(f"{path.name}: top level must be a mapping")
    try:
        return flatten(data)
    except LocaleError as exc:
        raise LocaleError(f"{path.name}: {exc}") from exc


def placeholders(text: str) -> set[str]:
    """Names of the ``{placeholders}`` used in ``text``."""
    return {field for _, field, _, _ in string.Formatter().parse(text) if field}


class _KeepMissing(dict[str, Any]):
    """Leaves unknown placeholders visible instead of raising KeyError."""

    def __missing__(self, key: str) -> str:
        logger.warning("Missing i18n parameter: %s", key)
        return "{" + key + "}"


class Translator:
    """Translate dotted keys into user-facing text."""

    def __init__(
        self,
        catalogs: Mapping[str, Mapping[str, str]],
        default_locale: str = FALLBACK_LOCALE,
    ) -> None:
        self._catalogs = {normalize_locale(code): dict(keys) for code, keys in catalogs.items()}
        if FALLBACK_LOCALE not in self._catalogs:
            raise LocaleError(f"the fallback locale '{FALLBACK_LOCALE}' is required")
        self.default_locale = self._supported_or_fallback(default_locale)

    @classmethod
    def from_directory(
        cls, directory: Path = LOCALES_DIR, default_locale: str = FALLBACK_LOCALE
    ) -> Translator:
        """Load every ``*.yaml`` file in ``directory``; the file stem is the locale code."""
        catalogs = {path.stem: load_locale_file(path) for path in sorted(directory.glob("*.yaml"))}
        return cls(catalogs, default_locale)

    @property
    def available_locales(self) -> list[str]:
        return sorted(self._catalogs)

    def t(self, key: str, locale: str | None = None, **params: Any) -> str:
        """Return the text for ``key`` in ``locale`` (or the default), formatted with ``params``."""
        for code in self._lookup_chain(locale):
            text = self._catalogs[code].get(key)
            if text is not None:
                return text.format_map(_KeepMissing(params)) if params else text
        logger.warning("Missing i18n key: %s", key)
        return key

    def _lookup_chain(self, locale: str | None) -> list[str]:
        chain: list[str] = []
        requested = normalize_locale(locale) if locale else None
        for code in (requested, self.default_locale, FALLBACK_LOCALE):
            if code and code in self._catalogs and code not in chain:
                chain.append(code)
        return chain

    def _supported_or_fallback(self, code: str) -> str:
        normalized = normalize_locale(code)
        if normalized in self._catalogs:
            return normalized
        logger.warning("Locale '%s' is not available; using '%s'", code, FALLBACK_LOCALE)
        return FALLBACK_LOCALE
