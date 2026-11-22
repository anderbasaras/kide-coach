"""Engine, sessions and schema migrations.

The schema is managed by Alembic (migrations live in ``storage/migrations``), so
an existing database can be upgraded without losing data. ``upgrade_database``
is what ``kide db upgrade`` runs; it also creates a brand-new database.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event, make_url
from sqlalchemy.orm import Session, sessionmaker

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def make_engine(url: str, **kwargs: Any) -> Engine:
    """Create an engine. For SQLite: create the parent folder and enforce foreign keys."""
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite":
        database = parsed.database
        if database and database != ":memory:" and not database.startswith("file:"):
            Path(database).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, **kwargs)
        event.listen(engine, "connect", _enable_sqlite_foreign_keys)
        return engine
    return create_engine(url, **kwargs)


def _enable_sqlite_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def alembic_config(url: str) -> Config:
    """Alembic configuration built in code, so the installed package needs no alembic.ini."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def upgrade_database(url: str, revision: str = "head") -> None:
    """Create the database if needed and apply every pending migration."""
    make_engine(url).dispose()  # creates the SQLite parent folder
    command.upgrade(alembic_config(url), revision)


def downgrade_database(url: str, revision: str) -> None:
    command.downgrade(alembic_config(url), revision)
