"""Weekly per-client digest (Monday 8am) and operator daily summary."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import session_scope
from app.db.base import utcnow
from app.db.models import AdapterResult, Application, Client, DiscoveryRun, EscalationItem, InboundEmail, JobPosting
from app.logging import get_logger
from app.submission.health import health_report

from .mail import send_mail

log = get_logger("reporting.digest")


def client_digest(session: Session, client: Client, days: int = 7) -> dict:
    since = utcnow() - timedelta(days=days)
    applied = session.scalars(
        select(Application).where(Application.client_id == client.id, Application.status.in_(("submitted", "rejected")), Application.submitted_at >= since).order_by(desc(Application.submitted_at))
    ).all()
    replies = session.scalars(
        select(InboundEmail).where(InboundEmail.client_id == client.id, InboundEmail.flagged.is_(True), InboundEmail.received_at >= since).order_by(desc(InboundEmail.received_at))
    ).all()
    rejections = [a for a in applied if a.status == "rejected"]
    pending = session.scalars(select(EscalationItem).where(EscalationItem.client_id == client.id, EscalationItem.status == "open").order_by(EscalationItem.created_at)).all()
    queued = session.scalar(select(func.count(Application.id)).where(Application.client_id == client.id, Application.status.in_(("queued", "scheduled")))) or 0
    return {
        "client": client,
        "period_days": days,
        "applied": applied,
        "applied_count": len(applied),
        "replies": replies,
        "interviews": [r for r in replies if r.classification == "interview_request"],
        "rejections": rejections,
        "pending_escalations": pending,
        "queued": queued,
    }


def render_client_digest(d: dict) -> tuple[str, str]:
    c = d["client"]
    lines = [f"Hi {c.name.split()[0]},", "", f"Here is your JobAgent summary for the last {d['period_days']} days.", "", f"Applications submitted: {d['applied_count']}   (queued for this week: {d['queued']})"]
    for a in d["applied"]:
        j = a.job_snapshot or {}
        when = a.submitted_at.strftime("%b %d") if a.submitted_at else ""
        flag = "  [rejected]" if a.status == "rejected" else ""
        lines.append(f"  - {j.get('company','')} — {j.get('title','')} — {when} — {j.get('url','')}{flag}")
    lines += ["", f"Interview requests / recruiter replies: {len(d['replies'])}"]
    for r in d["replies"]:
        lines.append(f"  - {r.received_at:%b %d} — {r.classification.replace('_',' ')} from {r.sender}: {r.subject[:80]} (forwarded to you)")
    lines += ["", f"Rejections: {len(d['rejections'])}"]
    if d["pending_escalations"]:
        lines += ["", "Questions we need your answer on (we hold those applications until you reply):"]
        for e in d["pending_escalations"][:10]:
            lines.append(f"  - {e.question}")
        lines.append("Reply to this email with your answers and we'll add them to your answer bank.")
    lines += ["", "— JobAgent"]
    text = "\n".join(lines)
    subject = f"Your week in applications: {d['applied_count']} submitted, {len(d['interviews'])} interview request(s)"
    return subject, text


def send_all_weekly_digests() -> int:
    n = 0
    with session_scope() as s:
        for c in s.scalars(select(Client).where(Client.status == "active")):
            d = client_digest(s, c)
            subject, text = render_client_digest(d)
            if send_mail(c.real_email, subject, text):
                n += 1
    log.info("weekly_digests_sent", count=n)
    return n


def operator_summary(session: Session, hours: int = 24) -> dict:
    since = utcnow() - timedelta(hours=hours)
    by_status = dict(session.execute(select(Application.status, func.count()).where(Application.updated_at >= since).group_by(Application.status)).all())
    totals = dict(session.execute(select(Application.status, func.count()).group_by(Application.status)).all())
    esc_open = session.scalar(select(func.count(EscalationItem.id)).where(EscalationItem.status == "open")) or 0
    esc_new = session.scalar(select(func.count(EscalationItem.id)).where(EscalationItem.created_at >= since)) or 0
    runs = session.scalars(select(DiscoveryRun).where(DiscoveryRun.started_at >= since).order_by(desc(DiscoveryRun.started_at))).all()
    jobs_open = session.scalar(select(func.count(JobPosting.id)).where(JobPosting.status == "open")) or 0
    replies = session.scalar(select(func.count(InboundEmail.id)).where(InboundEmail.flagged.is_(True), InboundEmail.received_at >= since)) or 0
    per_client = session.execute(
        select(Client.name, func.count(Application.id)).join(Application, Application.client_id == Client.id).where(Application.status == "submitted", Application.submitted_at >= since).group_by(Client.name)
    ).all()
    return {
        "hours": hours,
        "by_status_recent": by_status,
        "totals": totals,
        "escalations_open": esc_open,
        "escalations_new": esc_new,
        "discovery_runs": runs,
        "jobs_open": jobs_open,
        "replies_forwarded": replies,
        "per_client": per_client,
        "adapter_health": health_report(session),
    }


def render_operator_summary(d: dict) -> tuple[str, str]:
    lines = [f"JobAgent operator summary — last {d['hours']}h", ""]
    lines.append("Submissions by status (updated in window): " + ", ".join(f"{k}={v}" for k, v in sorted(d["by_status_recent"].items())) or "none")
    lines.append("Pipeline totals: " + ", ".join(f"{k}={v}" for k, v in sorted(d["totals"].items())))
    lines.append("Submitted per client: " + (", ".join(f"{n}={c}" for n, c in d["per_client"]) or "none"))
    lines += ["", f"Escalations: {d['escalations_open']} open ({d['escalations_new']} new)", f"Recruiter replies / interviews forwarded: {d['replies_forwarded']}", "", "Adapter health (24h):"]
    for h in d["adapter_health"]:
        rate = "n/a" if h["success_rate_24h"] is None else f"{h['success_rate_24h']:.0%}"
        lines.append(f"  - {h['ats_type']}: {rate} over {h['attempts_24h']} attempts" + (f"  PAUSED: {h['reason']}" if h["paused"] else ""))
    lines += ["", f"Discovery: {len(d['discovery_runs'])} run(s), {d['jobs_open']} open jobs"]
    for r in d["discovery_runs"][:6]:
        lines.append(f"  - {r.started_at:%m-%d %H:%M} crawled={r.companies_crawled} failed={r.companies_failed} new={r.jobs_new} closed={r.jobs_closed}")
    settings = get_settings()
    lines += ["", f"Admin: {settings.base_url}/admin   Escalations: {settings.base_url}/escalations"]
    submitted = d["by_status_recent"].get("submitted", 0)
    return f"[JobAgent] Daily: {submitted} submitted, {d['escalations_open']} escalations open", "\n".join(lines)


def send_operator_daily_summary() -> bool:
    with session_scope() as s:
        subject, text = render_operator_summary(operator_summary(s))
    return send_mail(get_settings().operator_email, subject, text)
