"""What the coach chat knows: the last days in plain text, and a short memory.

Pure functions and a small class, no Telegram or API calls, so they are fully tested.

- :func:`recent_days` describes each of the last ``days`` days: the confirmed (or draft)
  reading of WODs and activities, the user's own text when nothing was read, and notes.
  Photos and screenshots are never included, only their count (D-040).
- :class:`ChatHistory` keeps the last few exchanges in memory (never in the database):
  enough to follow a conversation, forgotten after a pause or a restart.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from kide.bot.summaries import summarize_activity, summarize_wod
from kide.catalog import MovementCatalog
from kide.llm.schemas import ParsedRecord, RecordStatus, Wod
from kide.profile import Profile
from kide.storage.models import DayType, TrainingSession

Translate = Callable[..., str]

RECENT_DAYS = 7
MAX_RAW_CHARS = 600  # the user's unread text per session, truncated
HISTORY_TURNS = 6  # question + answer pairs kept
HISTORY_IDLE = dt.timedelta(minutes=60)  # after this pause the chat starts afresh
HISTORY_MAX_CHARS = 12_000  # ~4k tokens of past messages at most

_LABELS = {
    DayType.GYM: "Gym session",
    DayType.BOX: "Box class",
    DayType.REST: "Rest day",
    DayType.OTHER: "Other activity",
}


def _truncate(text: str, limit: int = MAX_RAW_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def describe_session(
    training: TrainingSession, t_en: Translate, catalog: MovementCatalog
) -> list[str]:
    """Lines about one session, in English (the prompt language)."""
    lines = [f"- {_LABELS[training.type]}"]
    record = ParsedRecord.model_validate(training.parsed_json) if training.parsed_json else None
    if record is not None:
        item = record.item()
        state = "confirmed" if record.status is RecordStatus.CONFIRMED else "not confirmed yet"
        summary = (
            summarize_wod(item, t_en, "en", catalog)
            if isinstance(item, Wod)
            else summarize_activity(item, t_en)
        )
        lines[0] += f" ({state}):"
        lines.extend(f"  {line}" for line in summary.splitlines())
    elif training.type is DayType.BOX and training.raw_input:
        lines[0] += ", WOD as written by the athlete (not read):"
        lines.append(f"  {_truncate(training.raw_input)}")
    if training.notes:
        lines.append(f"  Athlete's note: {_truncate(training.notes)}")
    if training.photos:
        lines.append(f"  ({len(training.photos)} photo(s), not shared)")
    return lines


def recent_days(
    sessions: Iterable[TrainingSession],
    today: dt.date,
    t_en: Translate,
    catalog: MovementCatalog,
    days: int = RECENT_DAYS,
) -> str:
    """The last ``days`` days, today included, oldest first; days without a log say so."""
    by_day: dict[dt.date, list[TrainingSession]] = {}
    for training in sessions:
        by_day.setdefault(training.date, []).append(training)
    lines = [f"Training log, last {days} days (oldest first):"]
    for offset in range(days - 1, -1, -1):
        day = today - dt.timedelta(days=offset)
        suffix = " (today)" if offset == 0 else ""
        lines.append(f"{day.isoformat()} {day.strftime('%a')}{suffix}:")
        day_sessions = by_day.get(day, [])
        if not day_sessions:
            lines.append("- nothing logged")
        for training in day_sessions:
            lines.extend(describe_session(training, t_en, catalog))
    return "\n".join(lines)


def today_header(profile: Profile, today: dt.date) -> str:
    """Today's date and the days left to each upcoming competition (deterministic)."""
    lines = [f"Today is {today.strftime('%A')} {today.isoformat()} (athlete's time zone)."]
    for competition in profile.competitions:
        days_left = (competition.date - today).days
        if days_left >= 0:
            lines.append(f"{competition.name} ({competition.type}) is in {days_left} days.")
    return "\n".join(lines)


@dataclass
class ChatHistory:
    """The last exchanges of the coach chat, in memory only."""

    turns: list[tuple[str, str]] = field(default_factory=list)
    last_at: dt.datetime | None = None

    def messages(self, now: dt.datetime) -> list[dict[str, str]]:
        """Past messages for the API, or none if the chat has been idle too long."""
        if self.last_at is None or now - self.last_at > HISTORY_IDLE:
            self.turns.clear()
            return []
        result: list[dict[str, str]] = []
        for question, answer in self.turns:
            result += [
                {"role": "user", "content": question},
                {"role": "assistant", "content": answer},
            ]
        return result

    def add(self, now: dt.datetime, question: str, answer: str) -> None:
        if self.last_at is None or now - self.last_at > HISTORY_IDLE:
            self.turns.clear()
        self.turns.append((question, answer))
        del self.turns[:-HISTORY_TURNS]
        while (
            len(self.turns) > 1 and sum(len(q) + len(a) for q, a in self.turns) > HISTORY_MAX_CHARS
        ):
            self.turns.pop(0)
        self.last_at = now


def split_message(text: str, limit: int = 4000) -> list[str]:
    """Telegram messages hold 4096 characters: split at paragraph or line breaks."""
    text = text.strip()
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut <= 0:
            cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks
