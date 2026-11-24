import datetime as dt
from decimal import Decimal

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError, StatementError

from kide.cli import main
from kide.storage import (
    Base,
    DailyCheckin,
    DayType,
    LlmCall,
    TrainingSession,
    User,
    make_engine,
    make_session_factory,
    upgrade_database,
)
from kide.storage.db import downgrade_database

TABLES = {"users", "daily_checkins", "training_sessions", "llm_calls", "session_photos"}


@pytest.fixture
def db_url(tmp_path):
    url = f"sqlite:///{(tmp_path / 'data' / 'kide.db').as_posix()}"
    upgrade_database(url)
    return url


@pytest.fixture
def session(db_url):
    engine = make_engine(db_url)
    with make_session_factory(engine)() as db_session:
        yield db_session
    engine.dispose()


def table_names(url: str) -> set[str]:
    engine = make_engine(url)
    names = set(inspect(engine).get_table_names()) - {"alembic_version"}
    engine.dispose()
    return names


# --- Schema and migrations ------------------------------------------------------


def test_upgrade_creates_database_and_folder(tmp_path, db_url):
    assert (tmp_path / "data" / "kide.db").is_file()
    assert table_names(db_url) == TABLES


def test_upgrade_is_idempotent(db_url):
    upgrade_database(db_url)
    assert table_names(db_url) == TABLES


def test_migrations_match_models(db_url):
    """Fails if a model changes without a new migration."""
    engine = make_engine(db_url)
    with engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    engine.dispose()
    assert diff == []


def test_upgrade_from_0001_keeps_data(tmp_path):
    """A database created in phase 1 upgrades to the current schema without losing rows."""
    from sqlalchemy import text

    url = f"sqlite:///{(tmp_path / 'old.db').as_posix()}"
    upgrade_database(url, "0001")
    engine = make_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO users (id, name, created_at) VALUES (1, 'Alex', '2026-11-22')")
        )
    upgrade_database(url)
    with make_session_factory(engine)() as db_session:
        user = db_session.get(User, 1)
        assert user is not None
        assert user.name == "Alex"
        assert user.language is None
    engine.dispose()
    assert table_names(url) == TABLES


def test_downgrade_to_base_and_back(db_url):
    downgrade_database(db_url, "base")
    assert table_names(db_url) == set()
    upgrade_database(db_url)
    assert table_names(db_url) == TABLES


# --- Data -----------------------------------------------------------------------


def test_full_day_round_trip(session):
    user = User(name="Alex")
    checkin = DailyCheckin(
        user=user,
        date=dt.date(2026, 11, 23),
        chat_id=-1001234567890,
        sent_at=dt.datetime(2026, 11, 23, 8, 0, tzinfo=dt.UTC),
        choice=DayType.BOX,
    )
    wod = {"format": "amrap", "minutes": 12, "movements": ["wall_ball", "burpee"]}
    training = TrainingSession(
        user=user, checkin=checkin, date=checkin.date, type=DayType.BOX, parsed_json=wod
    )
    session.add_all([user, checkin, training])
    session.flush()
    session.add(
        LlmCall(
            user_id=user.id,
            session_id=training.id,
            purpose="wod_parse",
            model="test-model",
            input_tokens=1200,
            output_tokens=300,
            cache_read_input_tokens=1000,
            estimated_cost_usd=Decimal("0.004321"),
        )
    )
    session.commit()
    session.expire_all()

    stored = session.scalars(select(TrainingSession)).one()
    assert stored.parsed_json == wod
    assert stored.type is DayType.BOX
    assert stored.checkin is not None
    assert stored.checkin.chat_id == -1001234567890
    assert stored.created_at.tzinfo is dt.UTC
    call = session.scalars(select(LlmCall)).one()
    assert call.estimated_cost_usd == Decimal("0.004321")
    assert call.success is True


def test_datetimes_are_stored_as_utc(session):
    madrid = dt.timezone(dt.timedelta(hours=1))
    user = User(name="Alex", created_at=dt.datetime(2026, 11, 23, 9, 0, tzinfo=madrid))
    session.add(user)
    session.commit()
    session.expire_all()
    assert session.get(User, user.id).created_at == dt.datetime(2026, 11, 23, 8, 0, tzinfo=dt.UTC)


def test_naive_datetimes_are_rejected(session):
    session.add(User(name="Alex", created_at=dt.datetime(2026, 11, 23, 9, 0)))
    with pytest.raises(StatementError, match="naive datetimes"):
        session.flush()


def test_one_checkin_per_day(session):
    user = User(name="Alex")
    session.add_all(
        [
            DailyCheckin(user=user, date=dt.date(2026, 11, 23)),
            DailyCheckin(user=user, date=dt.date(2026, 11, 23)),
        ]
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_foreign_keys_are_enforced(session):
    session.add(TrainingSession(user_id=999, date=dt.date(2026, 11, 23), type=DayType.GYM))
    with pytest.raises(IntegrityError):
        session.flush()


def test_unknown_day_type_is_rejected(session):
    user = User(name="Alex")
    session.add(TrainingSession(user=user, date=dt.date(2026, 11, 23), type="swim"))
    with pytest.raises(StatementError):
        session.flush()


# --- CLI --------------------------------------------------------------------------


def test_cli_db_upgrade_from_scratch(tmp_path, capsys):
    url = f"sqlite:///{(tmp_path / 'fresh' / 'kide.db').as_posix()}"
    assert main(["db", "upgrade", "--url", url]) == 0
    assert "is up to date" in capsys.readouterr().out
    assert table_names(url) == TABLES


def test_cli_db_upgrade_uses_data_dir(tmp_path, monkeypatch):
    from kide.config import get_settings

    monkeypatch.chdir(tmp_path)  # ignore any real .env in the repo
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "var"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    get_settings.cache_clear()
    try:
        assert main(["db", "upgrade"]) == 0
    finally:
        get_settings.cache_clear()
    assert (tmp_path / "var" / "kide.db").is_file()
