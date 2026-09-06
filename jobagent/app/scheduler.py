"""APScheduler jobs: discovery polling every 2h (staggered), matching after discovery, weekly digest,
operator daily summary, nightly backup, OTP-session expiry."""
from __future__ import annotations

import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings
from app.logging import get_logger

log = get_logger("scheduler")


async def discovery_then_match() -> None:
    from app.discovery.runner import run_discovery
    from app.matching.pipeline import match_new_jobs

    settings = get_settings()
    report = await run_discovery(stagger_max=300.0)  # spread requests over 5 minutes
    if report.new_job_ids:
        try:
            summary = await asyncio.to_thread(match_new_jobs, report.new_job_ids)
            log.info("match_after_discovery", **summary)
        except Exception as e:  # noqa: BLE001 - keep the scheduler alive; a bad key is logged loudly
            log.error("match_after_discovery_failed", error=str(e))


async def weekly_digests() -> None:
    from app.reporting.digest import send_all_weekly_digests

    await asyncio.to_thread(send_all_weekly_digests)


async def operator_daily() -> None:
    from app.reporting.digest import send_operator_daily_summary

    await asyncio.to_thread(send_operator_daily_summary)


async def nightly_backup() -> None:
    from app.reporting.backup import run_backup

    await asyncio.to_thread(run_backup)


async def expire_otp_sessions() -> None:
    from app.otp.registry import expire_stale

    await asyncio.to_thread(expire_stale)


def build_scheduler() -> AsyncIOScheduler:
    settings = get_settings()
    sched = AsyncIOScheduler(timezone="UTC")
    sched.add_job(discovery_then_match, IntervalTrigger(hours=settings.discovery_interval_hours), id="discovery", max_instances=1, coalesce=True)
    sched.add_job(weekly_digests, CronTrigger(day_of_week="mon", hour=8, minute=0, timezone="America/Chicago"), id="weekly_digest")
    sched.add_job(operator_daily, CronTrigger(hour=7, minute=30, timezone="America/Chicago"), id="operator_daily")
    sched.add_job(nightly_backup, CronTrigger(hour=3, minute=15, timezone="America/Chicago"), id="backup")
    sched.add_job(expire_otp_sessions, IntervalTrigger(minutes=1), id="otp_expiry")
    return sched
