"""Pacing rules: per-client daily cap, 8am-8pm client-local window, jitter between submissions."""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.base import utcnow
from app.db.models import Application, Client


def local_now(client: Client, now: datetime | None = None) -> datetime:
    now = (now or utcnow()).replace(tzinfo=ZoneInfo("UTC"))
    try:
        return now.astimezone(ZoneInfo(client.timezone or "America/Chicago"))
    except Exception:  # noqa: BLE001
        return now.astimezone(ZoneInfo("America/Chicago"))


def in_window(client: Client, now: datetime | None = None) -> bool:
    s = get_settings()
    return s.submit_window_start_hour <= local_now(client, now).hour < s.submit_window_end_hour


def next_window_start(client: Client, now: datetime | None = None) -> datetime:
    """UTC-naive datetime for the next 8am client-local (today if still ahead, else tomorrow)."""
    s = get_settings()
    ln = local_now(client, now)
    start = ln.replace(hour=s.submit_window_start_hour, minute=random.randint(0, 20), second=0, microsecond=0)
    if ln.hour >= s.submit_window_start_hour:
        start = start + timedelta(days=1)
    return start.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def submissions_today(session: Session, client: Client, now: datetime | None = None) -> int:
    ln = local_now(client, now)
    day_start_local = ln.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start = day_start_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return int(
        session.scalar(
            select(func.count(Application.id)).where(
                Application.client_id == client.id,
                Application.status.in_(("submitted", "in_progress", "needs_otp")),
                Application.updated_at >= day_start,
            )
        )
        or 0
    )


def daily_cap_reached(session: Session, client: Client, now: datetime | None = None) -> bool:
    return submissions_today(session, client, now) >= get_settings().daily_cap_per_client


def jitter() -> timedelta:
    s = get_settings()
    return timedelta(seconds=random.uniform(s.jitter_min_minutes * 60, s.jitter_max_minutes * 60))
