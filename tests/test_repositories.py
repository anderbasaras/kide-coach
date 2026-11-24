import datetime as dt

import pytest
from sqlalchemy import func, select

from kide.storage import DayType, SessionPhoto, User, make_engine, make_session_factory
from kide.storage.db import upgrade_database
from kide.storage.repositories import unit_of_work

DAY = dt.date(2026, 11, 23)
T0 = dt.datetime(2026, 11, 23, 8, 0, tzinfo=dt.UTC)
T1 = T0 + dt.timedelta(minutes=5)


@pytest.fixture
def factory(tmp_path):
    url = f"sqlite:///{(tmp_path / 'kide.db').as_posix()}"
    upgrade_database(url)
    engine = make_engine(url)
    yield make_session_factory(engine)
    engine.dispose()


def make_checkin(factory, day=DAY):
    with unit_of_work(factory) as repos:
        user = repos.users.get_or_create("Alex")
        return user.id, repos.checkins.get_or_create(user.id, day).id


# --- Unit of work -----------------------------------------------------------------


def test_unit_of_work_commits(factory):
    with unit_of_work(factory) as repos:
        repos.users.get_or_create("Alex")
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(User)) == 1


def test_unit_of_work_rolls_back_on_error(factory):
    with pytest.raises(RuntimeError), unit_of_work(factory) as repos:
        repos.users.get_or_create("Alex")
        raise RuntimeError("boom")
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(User)) == 0


# --- Users ------------------------------------------------------------------------


def test_single_user_is_reused_and_renamed(factory):
    with unit_of_work(factory) as repos:
        first = repos.users.get_or_create("Alex").id
    with unit_of_work(factory) as repos:
        user = repos.users.get_or_create("Alexandra")
        assert user.id == first
        assert user.name == "Alexandra"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(User)) == 1


def test_language_choice_persists(factory):
    with unit_of_work(factory) as repos:
        repos.users.set_language(repos.users.get_or_create("Alex"), "es")
    with unit_of_work(factory) as repos:
        user = repos.users.get_or_create("Alex")
        assert user.language == "es"
        repos.users.set_language(user, None)
    with unit_of_work(factory) as repos:
        assert repos.users.get_or_create("Alex").language is None


# --- Check-ins --------------------------------------------------------------------


def test_checkin_get_or_create_is_one_per_day(factory):
    user_id, first = make_checkin(factory)
    _, again = make_checkin(factory)
    _, next_day = make_checkin(factory, DAY + dt.timedelta(days=1))
    assert first == again
    assert next_day != first
    with unit_of_work(factory) as repos:
        assert repos.checkins.get(user_id, DAY).id == first
        assert repos.checkins.get(user_id, DAY - dt.timedelta(days=1)) is None


def test_mark_sent_keeps_the_first_time(factory):
    _, checkin_id = make_checkin(factory)
    with unit_of_work(factory) as repos:
        checkin = repos.checkins.get_by_id(checkin_id)
        repos.checkins.mark_sent(checkin, chat_id=111, at=T0)
        repos.checkins.mark_sent(checkin, chat_id=222, at=T1)
    with unit_of_work(factory) as repos:
        checkin = repos.checkins.get_by_id(checkin_id)
        assert checkin.sent_at == T0
        assert checkin.chat_id == 111


def test_mark_reminded(factory):
    _, checkin_id = make_checkin(factory)
    with unit_of_work(factory) as repos:
        repos.checkins.mark_reminded(repos.checkins.get_by_id(checkin_id), T1)
    with unit_of_work(factory) as repos:
        assert repos.checkins.get_by_id(checkin_id).reminder_sent_at == T1


def test_choice_follows_the_first_session(factory):
    _, checkin_id = make_checkin(factory)
    with unit_of_work(factory) as repos:
        checkin = repos.checkins.get_by_id(checkin_id)
        box = repos.sessions.create(checkin, DayType.BOX)
        repos.sessions.create(checkin, DayType.GYM)
        repos.checkins.refresh_choice(checkin, T0)
        assert checkin.choice is DayType.BOX
        assert checkin.answered_at == T0

        repos.sessions.reset(box, DayType.REST)
        repos.checkins.refresh_choice(checkin, T1)
        assert checkin.choice is DayType.REST
        assert checkin.answered_at == T0  # first answer time is kept


def test_choice_is_cleared_when_no_session_is_left(factory):
    _, checkin_id = make_checkin(factory)
    with unit_of_work(factory) as repos:
        checkin = repos.checkins.get_by_id(checkin_id)
        only = repos.sessions.create(checkin, DayType.GYM)
        repos.checkins.refresh_choice(checkin, T0)
        repos.sessions.delete(only)
        repos.checkins.refresh_choice(checkin, T1)
        assert checkin.choice is None
        assert checkin.answered_at is None


# --- Sessions ---------------------------------------------------------------------


def test_sessions_of_a_day_in_creation_order(factory):
    user_id, checkin_id = make_checkin(factory)
    with unit_of_work(factory) as repos:
        checkin = repos.checkins.get_by_id(checkin_id)
        repos.sessions.create(checkin, DayType.BOX)
        repos.sessions.create(checkin, DayType.GYM)
    with unit_of_work(factory) as repos:
        sessions = repos.sessions.for_day(user_id, DAY)
        assert [s.type for s in sessions] == [DayType.BOX, DayType.GYM]
        assert all(s.date == DAY and s.checkin_id == checkin_id for s in sessions)
        assert repos.sessions.for_day(user_id, DAY + dt.timedelta(days=1)) == []


def test_wod_text_accumulates(factory):
    _, checkin_id = make_checkin(factory)
    with unit_of_work(factory) as repos:
        training = repos.sessions.create(repos.checkins.get_by_id(checkin_id), DayType.BOX)
        repos.sessions.append_text(training, "AMRAP 12'")
        repos.sessions.append_text(training, "   ")
        repos.sessions.append_text(training, " 10 wall balls\n10 burpees ")
        session_id = training.id
    with unit_of_work(factory) as repos:
        raw = repos.sessions.get(session_id).raw_input
        assert raw == "AMRAP 12'\n\n10 wall balls\n10 burpees"


def test_note_is_trimmed_and_empty_means_none(factory):
    _, checkin_id = make_checkin(factory)
    with unit_of_work(factory) as repos:
        training = repos.sessions.create(repos.checkins.get_by_id(checkin_id), DayType.REST)
        repos.sessions.set_note(training, "  legs sore  ")
        assert training.notes == "legs sore"
        repos.sessions.set_note(training, "  ")
        assert training.notes is None


def test_photos_are_kept_in_order_and_not_duplicated(factory):
    _, checkin_id = make_checkin(factory)
    with unit_of_work(factory) as repos:
        training = repos.sessions.create(repos.checkins.get_by_id(checkin_id), DayType.BOX)
        assert repos.sessions.add_photo(training, "photos/2026/11/a.jpg", "AAA", "album1")
        assert repos.sessions.add_photo(training, "photos/2026/11/b.jpg", "BBB", "album1")
        assert repos.sessions.add_photo(training, "photos/2026/11/a2.jpg", "AAA") is None
        session_id = training.id
    with unit_of_work(factory) as repos:
        photos = repos.sessions.get(session_id).photos
        assert [p.path for p in photos] == ["photos/2026/11/a.jpg", "photos/2026/11/b.jpg"]
        assert photos[0].media_group_id == "album1"


def test_reset_clears_content_and_returns_photo_paths(factory):
    _, checkin_id = make_checkin(factory)
    with unit_of_work(factory) as repos:
        training = repos.sessions.create(repos.checkins.get_by_id(checkin_id), DayType.BOX, "WOD")
        repos.sessions.add_photo(training, "photos/x.jpg", "X")
        repos.sessions.set_note(training, "hard")
        assert repos.sessions.reset(training, DayType.GYM) == ["photos/x.jpg"]
        session_id = training.id
    with unit_of_work(factory) as repos:
        training = repos.sessions.get(session_id)
        assert training.type is DayType.GYM
        assert (training.raw_input, training.notes, training.photos) == (None, None, [])
        assert repos.session.scalar(select(func.count()).select_from(SessionPhoto)) == 0


def test_delete_session_removes_its_photos(factory):
    user_id, checkin_id = make_checkin(factory)
    with unit_of_work(factory) as repos:
        training = repos.sessions.create(repos.checkins.get_by_id(checkin_id), DayType.BOX)
        repos.sessions.add_photo(training, "photos/x.jpg", "X")
        assert repos.sessions.delete(training) == ["photos/x.jpg"]
    with unit_of_work(factory) as repos:
        assert repos.sessions.for_day(user_id, DAY) == []
        assert repos.session.scalar(select(func.count()).select_from(SessionPhoto)) == 0
