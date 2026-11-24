"""ORM models (SQLAlchemy 2, SQLite).

One instance serves one person (D-011): ``users`` holds a single row, but every
table keeps a ``user_id`` so the schema does not need to change if that ever does.

Free-form payloads (parsed WODs, planned and actual sessions) are JSON columns:
their shape is defined by Pydantic schemas in later phases, not by the database.
"""

from __future__ import annotations

import datetime as dt
import enum
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Date,
    DateTime,
    Dialect,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Explicit constraint names keep Alembic batch migrations on SQLite predictable.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_N_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class UTCDateTime(TypeDecorator[dt.datetime]):
    """Stores naive UTC (SQLite has no time zones) and always returns aware UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: dt.datetime | None, dialect: Dialect) -> Any:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetimes are ambiguous; pass an aware datetime")
        return value.astimezone(dt.UTC).replace(tzinfo=None)

    def process_result_value(self, value: Any, dialect: Dialect) -> dt.datetime | None:
        return None if value is None else value.replace(tzinfo=dt.UTC)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class DayType(enum.StrEnum):
    """What the user does on a day (answer to the daily check-in)."""

    GYM = "gym"
    BOX = "box"
    REST = "rest"
    OTHER = "other"


def _day_type() -> Enum:
    return Enum(
        DayType,
        name="day_type",
        native_enum=False,
        create_constraint=True,
        length=10,
        values_callable=lambda members: [member.value for member in members],
        validate_strings=True,
    )


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    # Chosen with /language; overrides profile.identity.language when set (D-027).
    language: Mapped[str | None] = mapped_column(String(10))
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)

    checkins: Mapped[list[DailyCheckin]] = relationship(back_populates="user")
    sessions: Mapped[list[TrainingSession]] = relationship(back_populates="user")


class DailyCheckin(Base):
    """The daily "What's on today?" question and its answer. One per user and day.

    ``date`` is the local date in the profile's time zone. ``choice`` mirrors the type of
    the first training session of the day; the sessions hold what was actually done.
    """

    __tablename__ = "daily_checkins"
    __table_args__ = (UniqueConstraint("user_id", "date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    date: Mapped[dt.date] = mapped_column(Date)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    sent_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    answered_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    reminder_sent_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    choice: Mapped[DayType | None] = mapped_column(_day_type())
    note: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(back_populates="checkins")
    sessions: Mapped[list[TrainingSession]] = relationship(
        back_populates="checkin", order_by="TrainingSession.id"
    )


class TrainingSession(Base):
    """What was planned and done on a day: a box WOD, a gym session, rest or other."""

    __tablename__ = "training_sessions"
    __table_args__ = (Index("ix_training_sessions_user_date", "user_id", "date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    checkin_id: Mapped[int | None] = mapped_column(
        ForeignKey("daily_checkins.id", ondelete="SET NULL")
    )
    date: Mapped[dt.date] = mapped_column(Date)
    type: Mapped[DayType] = mapped_column(_day_type())
    raw_input: Mapped[str | None] = mapped_column(Text, doc="What the user wrote, as is.")
    parsed_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, doc="Structured WOD.")
    planned_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, doc="Engine plan.")
    actual_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, doc="What was done.")
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(back_populates="sessions")
    checkin: Mapped[DailyCheckin | None] = relationship(back_populates="sessions")
    photos: Mapped[list[SessionPhoto]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="SessionPhoto.id",
    )


class SessionPhoto(Base):
    """A photo sent for a session (e.g. the box whiteboard), stored under DATA_DIR.

    ``path`` is relative to DATA_DIR (POSIX separators) so the data folder can be moved
    or mounted elsewhere (D-010). ``file_unique_id`` is Telegram's stable id for the
    file: it avoids storing the same photo twice if an update is delivered again.
    """

    __tablename__ = "session_photos"
    __table_args__ = (UniqueConstraint("session_id", "file_unique_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("training_sessions.id", ondelete="CASCADE"), index=True
    )
    path: Mapped[str] = mapped_column(String(500))
    file_unique_id: Mapped[str] = mapped_column(String(100))
    media_group_id: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow)

    session: Mapped[TrainingSession] = relationship(back_populates="photos")


class LlmCall(Base):
    """One call to the Anthropic API, for cost tracking.

    Cache token counts are kept apart: with prompt caching they are billed at
    different rates, so the cost cannot be estimated from input tokens alone.
    """

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("training_sessions.id", ondelete="SET NULL")
    )
    purpose: Mapped[str] = mapped_column(String(40), doc="e.g. wod_parse, log_parse, chat.")
    model: Mapped[str] = mapped_column(String(100))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_creation_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    success: Mapped[bool] = mapped_column(default=True)
    error: Mapped[str | None] = mapped_column(Text)
