"""Job store: fingerprint dedup, first/last seen, close after 2 consecutive missing crawls."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.models import CompanyRegistry, JobPosting

from .base import RawJob
from . import http as dhttp

CLOSE_AFTER_MISSING = 2


@dataclass
class UpsertStats:
    seen: int = 0
    new: int = 0
    updated: int = 0
    closed: int = 0
    new_jobs: list[JobPosting] | None = None


def upsert_jobs(session: Session, company: CompanyRegistry, jobs: list[RawJob]) -> UpsertStats:
    """Merge a board crawl into the store. Jobs from this board not in `jobs` get missing_crawls += 1."""
    stats = UpsertStats(new_jobs=[])
    now = utcnow()
    seen_fps: set[str] = set()
    existing = {
        j.fingerprint: j
        for j in session.scalars(select(JobPosting).where(JobPosting.company_slug == company.slug, JobPosting.ats_type == company.ats_type))
    }
    for rj in jobs:
        fp = rj.fingerprint
        if fp in seen_fps:  # duplicate within the same board response
            continue
        seen_fps.add(fp)
        stats.seen += 1
        row = existing.get(fp) or session.scalar(select(JobPosting).where(JobPosting.fingerprint == fp))
        raw = dict(rj.raw or {})
        if dhttp._transport_override is not None:
            raw["simulated"] = True
        if row is None:
            row = JobPosting(
                fingerprint=fp,
                external_id=rj.external_id,
                url=rj.url,
                apply_url=rj.apply_url,
                ats_type=rj.ats_type,
                company_slug=rj.company_slug,
                company_name=rj.company_name,
                title=rj.title,
                location=rj.location,
                remote=rj.remote,
                department=rj.department,
                description_text=rj.description_text,
                posted_at=rj.posted_at,
                first_seen=now,
                last_seen=now,
                raw=raw,
            )
            session.add(row)
            stats.new += 1
            stats.new_jobs.append(row)
        else:
            row.last_seen = now
            row.missing_crawls = 0
            if row.status != "open":
                row.status = "open"
            # refresh mutable fields defensively (description edits, apply URL changes)
            row.url = rj.url or row.url
            row.apply_url = rj.apply_url or row.apply_url
            row.external_id = rj.external_id or row.external_id
            if rj.description_text and rj.description_text != row.description_text:
                row.description_text = rj.description_text
            row.remote = rj.remote
            row.department = rj.department or row.department
            row.posted_at = rj.posted_at or row.posted_at
            stats.updated += 1
    for fp, row in existing.items():
        if fp in seen_fps or row.status == "closed":
            continue
        row.missing_crawls += 1
        if row.missing_crawls >= CLOSE_AFTER_MISSING:
            row.status = "closed"
            stats.closed += 1
    session.flush()
    return stats


def open_jobs(session: Session) -> list[JobPosting]:
    return list(session.scalars(select(JobPosting).where(JobPosting.status == "open").order_by(JobPosting.first_seen.desc())))


def purge_simulated(session: Session) -> tuple[int, int]:
    """Close every job that came from the offline simulator and cancel applications that point at one.
    Called at the start of every real (non-simulated) discovery run."""
    from app.db.models import Application, ApplicationEvent

    closed = cancelled = 0
    for job in session.scalars(select(JobPosting).where(JobPosting.status == "open")):
        if (job.raw or {}).get("simulated"):
            job.status = "closed"
            closed += 1
    sim_ids = [j.id for j in session.scalars(select(JobPosting)) if (j.raw or {}).get("simulated")]
    if sim_ids:
        for app in session.scalars(select(Application).where(Application.job_id.in_(sim_ids), Application.status.in_(("queued", "scheduled", "in_progress", "needs_otp", "needs_human", "failed")))):
            app.status = "cancelled"
            app.events.append(ApplicationEvent(status="cancelled", note="simulated job purged by real discovery"))
            cancelled += 1
    session.flush()
    return closed, cancelled
