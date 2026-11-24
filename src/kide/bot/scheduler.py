"""Daily check-in jobs (JobQueue / APScheduler).

- ``daily-checkin``: one ``run_once`` at the next due time (:func:`next_checkin_at`).
  When it runs it asks if needed and always schedules the next one, so the chain does
  not break on an error. ``misfire_grace_time=None``: if the computer was asleep at the
  due time, the job runs when it wakes up instead of being dropped (APScheduler's
  default would drop a job that is more than one second late).
- ``checkin-backup``: every 15 minutes, apply the same one-check-in-per-day rule and
  re-create ``daily-checkin`` if it is missing. Cheap insurance against timers that do
  not survive a suspend.
- ``checkin-reminder`` (optional, ``schedule.checkin_reminder``): one ``run_once`` after
  the question was sent; it only sends if the day is still unanswered and it is still
  that day. The backup check applies the same rule, so a restart never loses it.
- At startup :func:`start_checkin_jobs` applies the rule once (the bot started after
  the check-in time and today was not asked yet -> ask now) and schedules both jobs.

The scheduled question goes to the main chat: the first id in ALLOWED_CHAT_IDS.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from telegram import Bot
from telegram.error import TelegramError
from telegram.ext import ContextTypes, JobQueue

from kide.bot.handlers.checkin import send_checkin_question
from kide.bot.handlers.common import today_for
from kide.bot.keyboards import checkin_keyboard
from kide.bot.rules import local_date
from kide.bot.services import Services, get_services
from kide.bot.timing import (
    next_checkin_at,
    reminder_due_at,
    should_send_checkin,
    should_send_reminder,
)
from kide.storage.repositories import unit_of_work

logger = logging.getLogger(__name__)

DAILY_JOB = "daily-checkin"
BACKUP_JOB = "checkin-backup"
REMINDER_JOB = "checkin-reminder"
BACKUP_INTERVAL = dt.timedelta(minutes=15)


def main_chat_id(services: Services) -> int:
    return services.settings.allowed_chat_ids[0]


def _today_state(services: Services) -> tuple[bool, bool]:
    """``(already_sent, already_answered)`` for today's check-in."""
    with unit_of_work(services.session_factory) as repos:
        checkin = repos.checkins.get(services.user_id, today_for(services))
        if checkin is None:
            return False, False
        return checkin.sent_at is not None, bool(checkin.sessions)


async def ensure_today_checkin(
    bot: Bot, services: Services, job_queue: JobQueue | None = None
) -> bool:
    """Ask today's question if it is due and was neither sent nor answered. True if sent."""
    sent, answered = _today_state(services)
    if not should_send_checkin(
        services.clock(),
        services.profile.schedule.checkin_time,
        services.tz,
        already_sent=sent,
        already_answered=answered,
    ):
        return False
    try:
        delivered = await send_checkin_question(
            bot, services, main_chat_id(services), today_for(services)
        )
    except TelegramError as exc:  # not marked as sent: the backup check retries
        logger.warning("Could not send the daily check-in: %s", exc)
        return False
    if delivered:
        logger.info("Daily check-in sent for %s", today_for(services))
        if job_queue is not None:
            schedule_reminder(job_queue, services)
    return delivered


def schedule_next_checkin(job_queue: JobQueue, services: Services) -> dt.datetime:
    """(Re)schedule ``daily-checkin`` at the next due time and return that time (UTC)."""
    for job in job_queue.get_jobs_by_name(DAILY_JOB):
        job.schedule_removal()
    when = next_checkin_at(services.clock(), services.profile.schedule.checkin_time, services.tz)
    job_queue.run_once(
        _daily_job,
        when,
        name=DAILY_JOB,
        job_kwargs={"misfire_grace_time": None, "coalesce": True},
    )
    logger.info("Next check-in scheduled for %s", when.astimezone(services.tz).isoformat())
    return when


async def _daily_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    services = get_services(context)
    try:
        await ensure_today_checkin(context.bot, services, context.job_queue)
    finally:
        if context.job_queue is not None:
            schedule_next_checkin(context.job_queue, services)


async def _backup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    services = get_services(context)
    await ensure_today_checkin(context.bot, services, context.job_queue)
    await ensure_reminder(context.bot, services)
    job_queue = context.job_queue
    if job_queue is not None and not job_queue.get_jobs_by_name(DAILY_JOB):
        logger.warning("The daily check-in job was missing; scheduling it again")
        schedule_next_checkin(job_queue, services)


async def start_checkin_jobs(bot: Bot, job_queue: JobQueue, services: Services) -> None:
    """Startup: catch up on today if needed, then schedule the daily and backup jobs."""
    await ensure_today_checkin(bot, services, job_queue)
    schedule_reminder(job_queue, services)  # a question sent before a restart
    schedule_next_checkin(job_queue, services)
    for job in job_queue.get_jobs_by_name(BACKUP_JOB):
        job.schedule_removal()
    job_queue.run_repeating(
        _backup_job, interval=BACKUP_INTERVAL, first=BACKUP_INTERVAL, name=BACKUP_JOB
    )


# --- Optional reminder -------------------------------------------------------------------


@dataclass(frozen=True)
class _CheckinInfo:
    day: dt.date
    chat_id: int | None
    sent_at: dt.datetime | None
    reminded: bool
    answered: bool


def _today_info(services: Services) -> _CheckinInfo:
    day = today_for(services)
    with unit_of_work(services.session_factory) as repos:
        checkin = repos.checkins.get(services.user_id, day)
        if checkin is None:
            return _CheckinInfo(day, None, None, False, False)
        return _CheckinInfo(
            day,
            checkin.chat_id,
            checkin.sent_at,
            checkin.reminder_sent_at is not None,
            bool(checkin.sessions),
        )


def _reminder_wanted(services: Services, info: _CheckinInfo, now: dt.datetime) -> bool:
    settings = services.profile.schedule.checkin_reminder
    return should_send_reminder(
        now,
        services.tz,
        enabled=settings.enabled,
        after_hours=settings.after_hours,
        sent_at=info.sent_at,
        reminded=info.reminded,
        answered=info.answered,
    )


async def ensure_reminder(bot: Bot, services: Services) -> bool:
    """Send today's reminder if it is due. True if sent."""
    info = _today_info(services)
    if not _reminder_wanted(services, info, services.clock()):
        return False
    chat_id = info.chat_id or main_chat_id(services)
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=services.t("checkin.reminder"),
            reply_markup=checkin_keyboard(services.t, info.day),
        )
    except TelegramError as exc:
        logger.warning("Could not send the check-in reminder: %s", exc)
        return False
    with unit_of_work(services.session_factory) as repos:
        checkin = repos.checkins.get(services.user_id, info.day)
        if checkin is not None:
            repos.checkins.mark_reminded(checkin, services.clock())
    logger.info("Check-in reminder sent for %s", info.day)
    return True


def schedule_reminder(job_queue: JobQueue, services: Services) -> dt.datetime | None:
    """(Re)schedule today's reminder; None if it is off or not needed."""
    for job in job_queue.get_jobs_by_name(REMINDER_JOB):
        job.schedule_removal()
    settings = services.profile.schedule.checkin_reminder
    info = _today_info(services)
    if not settings.enabled or info.sent_at is None or info.reminded or info.answered:
        return None
    due = reminder_due_at(info.sent_at, settings.after_hours)
    if local_date(due, services.tz) != info.day:
        return None
    when = max(due, services.clock())
    job_queue.run_once(
        _reminder_job, when, name=REMINDER_JOB, job_kwargs={"misfire_grace_time": None}
    )
    logger.info("Check-in reminder scheduled for %s", when.astimezone(services.tz).isoformat())
    return when


async def _reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await ensure_reminder(context.bot, get_services(context))
