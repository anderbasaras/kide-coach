"""Local persistence: SQLite + SQLAlchemy 2, schema managed by Alembic."""

from kide.storage.db import make_engine, make_session_factory, upgrade_database
from kide.storage.models import (
    Base,
    DailyCheckin,
    DayType,
    LlmCall,
    SessionPhoto,
    TrainingSession,
    User,
)

__all__ = [
    "Base",
    "DailyCheckin",
    "DayType",
    "LlmCall",
    "SessionPhoto",
    "TrainingSession",
    "User",
    "make_engine",
    "make_session_factory",
    "upgrade_database",
]
