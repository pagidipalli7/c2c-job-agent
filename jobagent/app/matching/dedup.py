"""Application dedup rules.

1. (client, job_fingerprint) unique  -> DB unique constraint uq_app_client_job (+ pre-check).
2. (client, company) max 1 per 90 days -> query on company_key within the cooldown window.
3. Cross-client stagger: 2+ clients matching the same job are scheduled >= 3h apart.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.base import utcnow
from app.db.models import Application, JobPosting
from app.discovery.base import normalize_company

ACTIVE_STATUSES = ("queued", "scheduled", "in_progress", "needs_otp", "needs_human", "submitted")


def company_key(job: JobPosting) -> str:
    return normalize_company(job.company_name) or normalize_company(job.company_slug)


def already_applied(session: Session, client_id: int, fingerprint: str) -> Application | None:
    return session.scalar(select(Application).where(Application.client_id == client_id, Application.job_fingerprint == fingerprint))


def company_cooldown_hit(session: Session, client_id: int, key: str, now: datetime | None = None) -> Application | None:
    """An application to this company within the last N days (any non-cancelled status)."""
    now = now or utcnow()
    since = now - timedelta(days=get_settings().company_cooldown_days)
    return session.scalar(
        select(Application)
        .where(
            Application.client_id == client_id,
            Application.company_key == key,
            Application.created_at >= since,
            Application.status.in_(ACTIVE_STATUSES + ("failed", "rejected")),
        )
        .order_by(Application.created_at.desc())
    )


def stagger_schedule(session: Session, job_fingerprint: str, earliest: datetime | None = None) -> datetime:
    """Return a scheduled_at >= max(existing scheduled_at for this job) + stagger hours."""
    earliest = earliest or utcnow()
    latest = session.scalar(select(func.max(Application.scheduled_at)).where(Application.job_fingerprint == job_fingerprint))
    if latest is None:
        return earliest
    return max(earliest, latest + timedelta(hours=get_settings().cross_client_stagger_hours))
