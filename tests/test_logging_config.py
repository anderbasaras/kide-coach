import logging

import pytest

from kide.logging_config import (
    PINNED_LOGGERS,
    REDACTED,
    RedactingFormatter,
    ShortNetworkErrors,
    configure_logging,
)

TOKEN = "123456:ABC-fake-token"


@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    saved = (list(root.handlers), root.level)
    levels = {name: logging.getLogger(name).level for name in ("kide", *PINNED_LOGGERS)}
    yield
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)


def format_line(message: str, *args: object) -> str:
    record = logging.LogRecord("httpx", logging.INFO, __file__, 1, message, args, None)
    return RedactingFormatter("%(message)s", [TOKEN]).format(record)


def test_token_is_redacted_from_messages_and_args():
    line = format_line("POST https://api.telegram.org/bot%s/getUpdates", TOKEN)
    assert TOKEN not in line
    assert f"bot{REDACTED}/getUpdates" in line


def test_token_is_redacted_from_tracebacks():
    try:
        raise RuntimeError(f"failed: https://api.telegram.org/bot{TOKEN}/getMe")
    except RuntimeError:
        import sys

        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "boom", (), sys.exc_info())
    assert TOKEN not in RedactingFormatter("%(message)s", [TOKEN]).format(record)


@pytest.mark.usefixtures("restore_logging")
def test_debug_level_does_not_unpin_third_party_loggers():
    configure_logging("DEBUG", secrets=[TOKEN])
    assert logging.getLogger("kide").level == logging.DEBUG
    for name in PINNED_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING


@pytest.mark.usefixtures("restore_logging")
def test_configuring_twice_does_not_duplicate_handlers():
    configure_logging("INFO")
    configure_logging("INFO")
    assert [h.get_name() for h in logging.getLogger().handlers].count("kide") == 1


def test_network_retries_are_one_line():
    try:
        raise OSError("connection reset")
    except OSError:
        import sys

        record = logging.LogRecord(
            "telegram.ext",
            logging.ERROR,
            __file__,
            1,
            "Network Retry Loop (Polling): fail",
            (),
            sys.exc_info(),
        )
    assert ShortNetworkErrors().filter(record)
    assert "Traceback" not in logging.Formatter().format(record)
