"""Data access for the bot. Handlers use these classes and never touch SQLAlchemy directly.

Use :func:`unit_of_work` for one transaction: it commits when the block ends and
rolls back if it raises::

    with unit_of_work(session_factory) as repos:
        user = repos.users.get_or_create("Alex")
        checkin = repos.checkins.get_or_create(user.id, today)

Repositories only ``flush``; committing is the unit of work's job.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, sessionmaker

from kide.storage.models import (
    DailyCheckin,
    DayType,
    LlmCall,
    SessionPhoto,
    TrainingSession,
    User,
)

TEXT_SEPARATOR = "\n\n"


class UserRepository:
    """The single user of this instance (D-011)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_or_create(self, name: str) -> User:
        """Return the user, creating it on first run; keeps the name in sync with the profile."""
        user = self._session.scalars(select(User).order_by(User.id).limit(1)).first()
        if user is None:
            user = User(name=name)
            self._session.add(user)
        elif user.name != name:
            user.name = name
        self._session.flush()
        return user

    def set_language(self, user: User, language: str | None) -> None:
        """Store the /language choice; ``None`` goes back to the profile language."""
        user.language = language
        self._session.flush()


class CheckinRepository:
    """One ``daily_checkins`` row per user and local date."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, user_id: int, day: dt.date) -> DailyCheckin | None:
        return self._session.scalars(
            select(DailyCheckin).where(DailyCheckin.user_id == user_id, DailyCheckin.date == day)
        ).first()

    def get_by_id(self, checkin_id: int) -> DailyCheckin | None:
        return self._session.get(DailyCheckin, checkin_id)

    def get_or_create(self, user_id: int, day: dt.date) -> DailyCheckin:
        checkin = self.get(user_id, day)
        if checkin is None:
            checkin = DailyCheckin(user_id=user_id, date=day)
            self._session.add(checkin)
            self._session.flush()
        return checkin

    def mark_sent(self, checkin: DailyCheckin, chat_id: int, at: dt.datetime) -> None:
        """Record the first time the question was sent; later sends keep the original time."""
        if checkin.sent_at is None:
            checkin.sent_at = at
            checkin.chat_id = chat_id
        self._session.flush()

    def mark_reminded(self, checkin: DailyCheckin, at: dt.datetime) -> None:
        checkin.reminder_sent_at = at
        self._session.flush()

    def refresh_choice(self, checkin: DailyCheckin, at: dt.datetime) -> None:
        """Mirror the first session of the day in ``choice``; clear it if there is none."""
        self._session.flush()
        self._session.refresh(checkin, ["sessions"])
        first = checkin.sessions[0] if checkin.sessions else None
        checkin.choice = first.type if first else None
        if first is None:
            checkin.answered_at = None
        elif checkin.answered_at is None:
            checkin.answered_at = at
        self._session.flush()


class SessionRepository:
    """Training sessions of a day and their photos."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self, checkin: DailyCheckin, day_type: DayType, raw_input: str | None = None
    ) -> TrainingSession:
        training = TrainingSession(
            user_id=checkin.user_id,
            checkin=checkin,
            date=checkin.date,
            type=day_type,
            raw_input=raw_input,
        )
        self._session.add(training)
        self._session.flush()
        return training

    def get(self, session_id: int) -> TrainingSession | None:
        return self._session.get(TrainingSession, session_id)

    def for_day(self, user_id: int, day: dt.date) -> list[TrainingSession]:
        return list(
            self._session.scalars(
                select(TrainingSession)
                .where(TrainingSession.user_id == user_id, TrainingSession.date == day)
                .order_by(TrainingSession.id)
            )
        )

    def between(self, user_id: int, start: dt.date, end: dt.date) -> list[TrainingSession]:
        """Sessions from ``start`` to ``end`` (both included), oldest first."""
        return list(
            self._session.scalars(
                select(TrainingSession)
                .where(
                    TrainingSession.user_id == user_id,
                    TrainingSession.date >= start,
                    TrainingSession.date <= end,
                )
                .order_by(TrainingSession.date, TrainingSession.id)
            )
        )

    def append_text(self, training: TrainingSession, text: str) -> None:
        """Add a message to ``raw_input`` (several WOD messages end up in one text)."""
        text = text.strip()
        if not text:
            return
        training.raw_input = (
            f"{training.raw_input}{TEXT_SEPARATOR}{text}" if training.raw_input else text
        )
        self._session.flush()

    def set_note(self, training: TrainingSession, note: str | None) -> None:
        training.notes = note.strip() if note and note.strip() else None
        self._session.flush()

    def add_photo(
        self,
        training: TrainingSession,
        path: str,
        file_unique_id: str,
        media_group_id: str | None = None,
    ) -> SessionPhoto | None:
        """Attach a stored photo. Returns ``None`` if this file was already attached."""
        if self.has_photo(training, file_unique_id):
            return None
        photo = SessionPhoto(
            path=path, file_unique_id=file_unique_id, media_group_id=media_group_id
        )
        training.photos.append(photo)
        self._session.flush()
        return photo

    def has_photo(self, training: TrainingSession, file_unique_id: str) -> bool:
        return any(photo.file_unique_id == file_unique_id for photo in training.photos)

    def set_parsed(self, training: TrainingSession, record: dict[str, Any] | None) -> None:
        """Store Claude's reading (a ``ParsedRecord`` as JSON), or clear it with ``None``."""
        training.parsed_json = record
        self._session.flush()

    def reset(self, training: TrainingSession, day_type: DayType) -> list[str]:
        """Start the session over with a new type (correction).

        Clears the text, the note and the photos. Returns the paths of the removed
        photos so the caller can delete the files.
        """
        removed = [photo.path for photo in training.photos]
        training.type = day_type
        training.raw_input = None
        training.notes = None
        training.parsed_json = None
        training.photos.clear()
        self._session.flush()
        return removed

    def delete(self, training: TrainingSession) -> list[str]:
        """Delete the session. Returns the paths of its photos so the files can be deleted."""
        removed = [photo.path for photo in training.photos]
        self._session.delete(training)
        self._session.flush()
        return removed


@dataclass(frozen=True)
class LlmUsageSummary:
    """Calls of one purpose and model, for ``kide llm costs``."""

    purpose: str
    model: str
    calls: int
    failed: int
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    cost_usd: Decimal | None
    mean_latency_ms: float | None

    @property
    def mean_cost_usd(self) -> Decimal | None:
        succeeded = self.calls - self.failed
        if self.cost_usd is None or succeeded <= 0:
            return None
        return self.cost_usd / succeeded


class LlmCallRepository:
    """One row per Anthropic API call: tokens and cost, never the content (D-037)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(
        self,
        *,
        user_id: int | None,
        session_id: int | None,
        purpose: str,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
        estimated_cost_usd: Decimal | None = None,
        latency_ms: int | None = None,
        error: str | None = None,
    ) -> LlmCall:
        call = LlmCall(
            user_id=user_id,
            session_id=session_id,
            purpose=purpose,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
            estimated_cost_usd=estimated_cost_usd,
            latency_ms=latency_ms,
            success=error is None,
            error=error,
        )
        self._session.add(call)
        self._session.flush()
        return call

    def for_session(self, session_id: int) -> list[LlmCall]:
        return list(
            self._session.scalars(
                select(LlmCall).where(LlmCall.session_id == session_id).order_by(LlmCall.id)
            )
        )

    def summary(self, since: dt.datetime | None = None) -> list[LlmUsageSummary]:
        """Totals per purpose and model, optionally only for calls made since ``since``."""
        failed = func.sum(case((LlmCall.success, 0), else_=1))
        query = select(
            LlmCall.purpose,
            LlmCall.model,
            func.count(LlmCall.id),
            failed,
            func.sum(LlmCall.input_tokens),
            func.sum(LlmCall.output_tokens),
            func.sum(LlmCall.cache_creation_input_tokens),
            func.sum(LlmCall.cache_read_input_tokens),
            func.sum(LlmCall.estimated_cost_usd),
            func.avg(LlmCall.latency_ms),
        ).group_by(LlmCall.purpose, LlmCall.model)
        if since is not None:
            query = query.where(LlmCall.created_at >= since)
        rows = self._session.execute(query.order_by(LlmCall.purpose, LlmCall.model)).all()
        return [
            LlmUsageSummary(
                purpose=row[0],
                model=row[1],
                calls=int(row[2]),
                failed=int(row[3] or 0),
                input_tokens=int(row[4] or 0),
                output_tokens=int(row[5] or 0),
                cache_creation_input_tokens=int(row[6] or 0),
                cache_read_input_tokens=int(row[7] or 0),
                cost_usd=Decimal(str(row[8])) if row[8] is not None else None,
                mean_latency_ms=float(row[9]) if row[9] is not None else None,
            )
            for row in rows
        ]


@dataclass(frozen=True)
class Repositories:
    session: Session

    @property
    def users(self) -> UserRepository:
        return UserRepository(self.session)

    @property
    def checkins(self) -> CheckinRepository:
        return CheckinRepository(self.session)

    @property
    def sessions(self) -> SessionRepository:
        return SessionRepository(self.session)

    @property
    def llm_calls(self) -> LlmCallRepository:
        return LlmCallRepository(self.session)


@contextmanager
def unit_of_work(factory: sessionmaker[Session]) -> Iterator[Repositories]:
    """One transaction: commit on success, roll back on error."""
    with factory.begin() as session:
        yield Repositories(session)
