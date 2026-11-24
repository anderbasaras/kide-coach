"""Daily check-in jobs with a fake JobQueue, a mocked bot and a real temporary database."""

import datetime as dt
from types import SimpleNamespace

import pytest
from telegram.error import NetworkError

from kide.bot import scheduler
from kide.bot.handlers import checkin as flow
from kide.storage import DayType
from kide.storage.repositories import unit_of_work
from tests.bot_helpers import bind, make_bot, make_context, make_services
from tests.telegram_factories import ALLOWED_CHAT, message_update

# Profile time zone in the shared test data: Europe/Berlin (UTC+1 in November).
BEFORE = dt.datetime(2026, 11, 24, 7, 0, tzinfo=dt.UTC)  # 08:00 local
DUE = dt.datetime(2026, 11, 24, 8, 0, tzinfo=dt.UTC)  # 09:00 local
LATE = dt.datetime(2026, 11, 24, 20, 0, tzinfo=dt.UTC)  # 21:00 local
TODAY = dt.date(2026, 11, 24)


class FakeJob:
    def __init__(self, name):
        self.name = name
        self.removed = False

    def schedule_removal(self):
        self.removed = True


class FakeJobQueue:
    """Records jobs like PTB's JobQueue; ``get_jobs_by_name`` sees non-removed ones."""

    def __init__(self):
        self.jobs: list[tuple[FakeJob, dict]] = []

    def _add(self, name, **details):
        job = FakeJob(name)
        self.jobs.append((job, details))
        return job

    def run_once(self, callback, when, name=None, job_kwargs=None, **kwargs):
        return self._add(name, callback=callback, when=when, job_kwargs=job_kwargs)

    def run_repeating(self, callback, interval, first=None, name=None, **kwargs):
        return self._add(name, callback=callback, interval=interval, first=first)

    def get_jobs_by_name(self, name):
        return [job for job, _ in self.jobs if job.name == name and not job.removed]

    def active(self, name):
        return [details for job, details in self.jobs if job.name == name and not job.removed]


@pytest.fixture
def clock():
    return SimpleNamespace(now=BEFORE)


@pytest.fixture
def services(tmp_path, profile_data, clock):
    profile_data["identity"]["language"] = "es"
    return make_services(tmp_path, profile_data, clock=lambda: clock.now)


@pytest.fixture
def bot():
    return make_bot()


def checkin_row(services):
    with unit_of_work(services.session_factory) as repos:
        return repos.checkins.get(services.user_id, TODAY)


# --- One check-in per day ------------------------------------------------------------


@pytest.mark.anyio
async def test_nothing_before_the_time(services, bot):
    assert not await scheduler.ensure_today_checkin(bot, services)
    bot.send_message.assert_not_awaited()


@pytest.mark.anyio
async def test_sent_once_to_the_main_chat(services, bot, clock):
    clock.now = DUE
    assert await scheduler.ensure_today_checkin(bot, services)
    assert bot.send_message.await_args.kwargs["chat_id"] == ALLOWED_CHAT
    assert bot.send_message.await_args.kwargs["text"] == "¿Qué toca hoy?"
    assert checkin_row(services).sent_at == DUE

    clock.now = DUE + dt.timedelta(hours=2)  # a restart later in the day
    assert not await scheduler.ensure_today_checkin(bot, services)
    assert bot.send_message.await_count == 1


@pytest.mark.anyio
async def test_not_sent_if_already_asked_with_today(services, bot, clock):
    context = make_context(services, bot)
    await flow.today(bind(message_update(text="/hoy"), bot), context)  # 08:00, by hand
    clock.now = DUE
    assert not await scheduler.ensure_today_checkin(bot, services)
    assert bot.send_message.await_count == 1


@pytest.mark.anyio
async def test_not_sent_if_already_answered(services, bot, clock):
    with unit_of_work(services.session_factory) as repos:
        checkin = repos.checkins.get_or_create(services.user_id, TODAY)
        repos.sessions.create(checkin, DayType.REST)
    clock.now = DUE
    assert not await scheduler.ensure_today_checkin(bot, services)
    bot.send_message.assert_not_awaited()


@pytest.mark.anyio
async def test_a_failed_send_is_retried_later(services, bot, clock):
    clock.now = DUE
    bot.send_message.side_effect = NetworkError("offline")
    assert not await scheduler.ensure_today_checkin(bot, services)
    assert checkin_row(services).sent_at is None

    bot.send_message.side_effect = None
    clock.now = DUE + dt.timedelta(minutes=15)
    assert await scheduler.ensure_today_checkin(bot, services)


# --- Jobs ------------------------------------------------------------------------------


def test_next_checkin_is_scheduled_even_if_late(services):
    queue = FakeJobQueue()
    when = scheduler.schedule_next_checkin(queue, services)
    assert when == DUE
    (job,) = queue.active(scheduler.DAILY_JOB)
    assert job["when"] == DUE
    assert job["job_kwargs"]["misfire_grace_time"] is None  # runs after a suspend


def test_rescheduling_replaces_the_previous_job(services, clock):
    queue = FakeJobQueue()
    scheduler.schedule_next_checkin(queue, services)
    clock.now = DUE + dt.timedelta(minutes=1)
    scheduler.schedule_next_checkin(queue, services)
    (job,) = queue.active(scheduler.DAILY_JOB)
    assert job["when"] == DUE + dt.timedelta(days=1)


@pytest.mark.anyio
async def test_startup_after_the_time_asks_once_and_schedules_tomorrow(services, bot, clock):
    clock.now = LATE
    queue = FakeJobQueue()
    await scheduler.start_checkin_jobs(bot, queue, services)
    assert bot.send_message.await_count == 1
    (daily,) = queue.active(scheduler.DAILY_JOB)
    assert daily["when"] == DUE + dt.timedelta(days=1)
    (backup,) = queue.active(scheduler.BACKUP_JOB)
    assert backup["interval"] == scheduler.BACKUP_INTERVAL

    # Restart: no duplicate question, still exactly one job of each kind.
    await scheduler.start_checkin_jobs(bot, queue, services)
    assert bot.send_message.await_count == 1
    assert len(queue.active(scheduler.DAILY_JOB)) == 1
    assert len(queue.active(scheduler.BACKUP_JOB)) == 1


@pytest.mark.anyio
async def test_daily_job_asks_and_schedules_the_next_day(services, bot, clock):
    clock.now = DUE
    queue = FakeJobQueue()
    context = make_context(services, bot, job_queue=queue)
    await scheduler._daily_job(context)
    assert bot.send_message.await_count == 1
    (daily,) = queue.active(scheduler.DAILY_JOB)
    assert daily["when"] == DUE + dt.timedelta(days=1)


@pytest.mark.anyio
async def test_daily_job_reschedules_even_if_something_fails(services, bot, clock, monkeypatch):
    clock.now = DUE

    async def boom(*args):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(scheduler, "ensure_today_checkin", boom)
    queue = FakeJobQueue()
    with pytest.raises(RuntimeError):
        await scheduler._daily_job(make_context(services, bot, job_queue=queue))
    assert len(queue.active(scheduler.DAILY_JOB)) == 1


@pytest.mark.anyio
async def test_backup_recreates_a_missing_daily_job(services, bot):
    queue = FakeJobQueue()
    await scheduler._backup_job(make_context(services, bot, job_queue=queue))
    assert len(queue.active(scheduler.DAILY_JOB)) == 1


# --- Optional reminder ----------------------------------------------------------------


@pytest.fixture
def reminding(tmp_path, profile_data, clock):
    profile_data["identity"]["language"] = "es"
    profile_data["schedule"] = {"checkin_reminder": {"enabled": True, "after_hours": 3}}
    return make_services(tmp_path, profile_data, clock=lambda: clock.now)


async def ask_at_nine(services, bot, clock, queue):
    clock.now = DUE
    assert await scheduler.ensure_today_checkin(bot, services, queue)


@pytest.mark.anyio
async def test_reminder_is_scheduled_when_the_question_is_sent(reminding, bot, clock):
    queue = FakeJobQueue()
    await ask_at_nine(reminding, bot, clock, queue)
    (job,) = queue.active(scheduler.REMINDER_JOB)
    assert job["when"] == DUE + dt.timedelta(hours=3)
    assert job["job_kwargs"]["misfire_grace_time"] is None


@pytest.mark.anyio
async def test_reminder_is_sent_once_with_the_buttons(reminding, bot, clock):
    await ask_at_nine(reminding, bot, clock, FakeJobQueue())
    clock.now = DUE + dt.timedelta(hours=1)
    assert not await scheduler.ensure_reminder(bot, reminding)

    clock.now = DUE + dt.timedelta(hours=3)
    assert await scheduler.ensure_reminder(bot, reminding)
    call = bot.send_message.await_args
    assert call.kwargs["text"] == "Recordatorio: todavía no me has dicho qué toca hoy."
    buttons = [b.callback_data for row in call.kwargs["reply_markup"].inline_keyboard for b in row]
    assert f"ci:{TODAY}:box" in buttons
    assert checkin_row(reminding).reminder_sent_at == clock.now

    clock.now = DUE + dt.timedelta(hours=4)
    assert not await scheduler.ensure_reminder(bot, reminding)  # only one


@pytest.mark.anyio
async def test_no_reminder_once_answered(reminding, bot, clock):
    await ask_at_nine(reminding, bot, clock, FakeJobQueue())
    with unit_of_work(reminding.session_factory) as repos:
        checkin = repos.checkins.get(reminding.user_id, TODAY)
        repos.sessions.create(checkin, DayType.GYM)
    clock.now = DUE + dt.timedelta(hours=3)
    assert not await scheduler.ensure_reminder(bot, reminding)
    assert scheduler.schedule_reminder(FakeJobQueue(), reminding) is None


@pytest.mark.anyio
async def test_reminder_survives_a_restart(reminding, bot, clock):
    await ask_at_nine(reminding, bot, clock, FakeJobQueue())
    clock.now = DUE + dt.timedelta(hours=1)  # restart: nothing to ask, reminder pending
    queue = FakeJobQueue()
    await scheduler.start_checkin_jobs(bot, queue, reminding)
    assert bot.send_message.await_count == 1  # no second question
    (job,) = queue.active(scheduler.REMINDER_JOB)
    assert job["when"] == DUE + dt.timedelta(hours=3)


@pytest.mark.anyio
async def test_backup_check_sends_a_missed_reminder(reminding, bot, clock):
    await ask_at_nine(reminding, bot, clock, FakeJobQueue())
    clock.now = DUE + dt.timedelta(hours=3, minutes=10)
    queue = FakeJobQueue()
    await scheduler._backup_job(make_context(reminding, bot, job_queue=queue))
    assert bot.send_message.await_args.kwargs["text"].startswith("Recordatorio")


@pytest.mark.anyio
async def test_no_reminder_when_disabled(services, bot, clock):
    queue = FakeJobQueue()
    await ask_at_nine(services, bot, clock, queue)
    assert queue.active(scheduler.REMINDER_JOB) == []
    clock.now = DUE + dt.timedelta(hours=5)
    assert not await scheduler.ensure_reminder(bot, services)
