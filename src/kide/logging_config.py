"""Logging for the bot process.

``LOG_LEVEL`` applies to Kide's own loggers (``kide.*``). Third-party loggers are
pinned to WARNING whatever ``LOG_LEVEL`` says, because:

- httpx logs every request URL at INFO, and Telegram request URLs contain the bot token;
- python-telegram-bot logs request and update payloads at DEBUG, which would include
  the user's messages (principle 5: their texts and photos never go to the log);
- the Anthropic SDK (and its HTTP client, httpx2) logs request options at DEBUG, which
  include prompts and images (D-037: only ids, types and counters are logged).

As a second line of defence, every formatted log line has the secrets (bot token,
API key) redacted.
Network retries from python-telegram-bot are logged as one line, without the long
traceback: they are expected when the connection drops and say nothing about Kide.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
REDACTED = "<redacted>"

PINNED_LOGGERS = ("httpx", "httpcore", "httpx2", "anthropic", "telegram", "apscheduler")
_HANDLER_NAME = "kide"


class RedactingFormatter(logging.Formatter):
    """Formatter that replaces secrets anywhere in the final line, tracebacks included."""

    def __init__(self, fmt: str, secrets: Iterable[str]) -> None:
        super().__init__(fmt)
        self._secrets = [secret for secret in secrets if secret]

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text


class ShortNetworkErrors(logging.Filter):
    """Drop the traceback of python-telegram-bot's network retry messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.startswith("telegram") and "Network Retry Loop" in record.getMessage():
            record.exc_info = None
            record.exc_text = None
        return True


def configure_logging(level: str, secrets: Iterable[str] = ()) -> None:
    """Send logs to stderr; Kide at ``level``, third-party libraries at WARNING."""
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter(LOG_FORMAT, secrets))
    handler.set_name(_HANDLER_NAME)
    handler.addFilter(ShortNetworkErrors())
    root = logging.getLogger()
    for old in [h for h in root.handlers if h.get_name() == _HANDLER_NAME]:
        root.removeHandler(old)  # calling twice must not duplicate every line
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    logging.getLogger("kide").setLevel(level)
    for name in PINNED_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
