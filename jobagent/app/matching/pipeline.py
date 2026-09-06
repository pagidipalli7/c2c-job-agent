"""Per (client, job): hard filters -> Haiku score -> threshold -> Application(status=queued)."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.clients.intake import get_profile_data
from app.clients.schemas import ProfileData
from app.config import get_settings
from app.db import session_scope
from app.db.models import Application, ApplicationEvent, Client, JobPosting
from app.logging import get_logger

from .dedup import already_applied, company_cooldown_hit, company_key, stagger_schedule
from .filters import hard_filter
from .scorer import ScoreResult, score_job

log = get_logger("matching")


@dataclass
class Decision:
    client_id: int
    client_name: str
    job_id: int
    company: str
    title: str
    location: str
    url: str
    stage: str  # hard_filter | dedup | llm | queued | error
    decision: str  # skip | apply
    reason: str
    score: int | None = None
    missing_keywords: list[str] = field(default_factory=list)
    scheduled_at: str | None = None


def job_snapshot(job: JobPosting) -> dict:
    return {
        "fingerprint": job.fingerprint,
        "company": job.company_name,
        "company_slug": job.company_slug,
        "ats_type": job.ats_type,
        "title": job.title,
        "location": job.location,
        "remote": job.remote,
        "url": job.url,
        "apply_url": job.apply_url,
        "external_id": job.external_id,
        "posted_at": job.posted_at.isoformat() if job.posted_at else None,
        "description_text": job.description_text,
        "raw": job.raw,
    }


def evaluate(session: Session, client: Client, profile: ProfileData, job: JobPosting, dry_run: bool = False, scorer=score_job) -> Decision:
    base = dict(client_id=client.id, client_name=client.name, job_id=job.id, company=job.company_name, title=job.title, location=job.location, url=job.url)
    settings = get_settings()

    if job.status != "open":
        return Decision(**base, stage="hard_filter", decision="skip", reason="job closed")
    ok, fname, reason = hard_filter(profile, job)
    if not ok:
        return Decision(**base, stage="hard_filter", decision="skip", reason=f"{fname}: {reason}")

    if already_applied(session, client.id, job.fingerprint):
        return Decision(**base, stage="dedup", decision="skip", reason="already applied to this job")
    key = company_key(job)
    prior = company_cooldown_hit(session, client.id, key)
    if prior:
        return Decision(**base, stage="dedup", decision="skip", reason=f"company cooldown: applied to {job.company_name} on {prior.created_at.date()} (app #{prior.id}, {prior.status})")

    result: ScoreResult = scorer(profile, job)
    if not (result.apply and result.score >= settings.match_threshold):
        return Decision(**base, stage="llm", decision="skip", reason="; ".join(result.reasons) or "below threshold", score=result.score, missing_keywords=result.missing_keywords)

    if dry_run:
        return Decision(**base, stage="llm", decision="apply", reason="; ".join(result.reasons), score=result.score, missing_keywords=result.missing_keywords)

    scheduled_at = stagger_schedule(session, job.fingerprint)
    app = Application(
        client_id=client.id,
        job_id=job.id,
        job_fingerprint=job.fingerprint,
        company_key=key,
        ats_type=job.ats_type,
        status="queued",
        scheduled_at=scheduled_at,
        trace_id=uuid.uuid4().hex[:16],
        match_score=result.score,
        match_reasons=result.reasons,
        missing_keywords=result.missing_keywords,
        job_snapshot=job_snapshot(job),
    )
    session.add(app)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        return Decision(**base, stage="dedup", decision="skip", reason="already applied (unique constraint)")
    app.events.append(ApplicationEvent(status="queued", note=f"score={result.score}; scheduled_at={scheduled_at.isoformat()}"))
    session.flush()
    from app.queue import get_queue

    get_queue().enqueue(app)
    log.info("application_queued", app_id=app.id, client_id=client.id, job_id=job.id, score=result.score, scheduled_at=scheduled_at.isoformat())
    return Decision(**base, stage="queued", decision="apply", reason="; ".join(result.reasons), score=result.score, missing_keywords=result.missing_keywords, scheduled_at=scheduled_at.isoformat())


def _client_ready(client: Client) -> str | None:
    if client.status != "active":
        return f"client status={client.status}"
    if client.current_profile is None:
        return "no profile"
    if client.base_resume is None or not client.base_resume.approved:
        return "base resume not approved"
    return None


def match_client(session: Session, client: Client, jobs: list[JobPosting], dry_run: bool = False, scorer=score_job) -> list[Decision]:
    why = _client_ready(client)
    if why and not dry_run:
        log.info("client_skipped", client_id=client.id, reason=why)
        return []
    profile = get_profile_data(client)
    decisions = []
    for job in jobs:
        try:
            decisions.append(evaluate(session, client, profile, job, dry_run=dry_run, scorer=scorer))
        except Exception as e:  # noqa: BLE001
            log.exception("match_error", client_id=client.id, job_id=job.id)
            decisions.append(Decision(client_id=client.id, client_name=client.name, job_id=job.id, company=job.company_name, title=job.title, location=job.location, url=job.url, stage="error", decision="skip", reason=str(e)))
    return decisions


def match_new_jobs(job_ids: list[int]) -> dict:
    """Scheduler entry point: match every active client against newly discovered jobs."""
    summary = {"jobs": len(job_ids), "clients": 0, "queued": 0, "skipped": 0}
    if not job_ids:
        return summary
    with session_scope() as s:
        clients = list(s.scalars(select(Client).where(Client.status == "active")))
        summary["clients"] = len(clients)
        for c in clients:
            jobs = list(s.scalars(select(JobPosting).where(JobPosting.id.in_(job_ids), JobPosting.status == "open")))
            for d in match_client(s, c, jobs):
                summary["queued" if d.stage == "queued" else "skipped"] += 1
    return summary


def match_all_open(client_id: int | None = None, dry_run: bool = False) -> list[Decision]:
    with session_scope() as s:
        q = select(Client).where(Client.status == "active")
        if client_id:
            q = q.where(Client.id == client_id)
        clients = list(s.scalars(q))
        jobs = list(s.scalars(select(JobPosting).where(JobPosting.status == "open").order_by(JobPosting.first_seen.desc())))
        out = []
        for c in clients:
            out.extend(match_client(s, c, jobs, dry_run=dry_run))
        return out
