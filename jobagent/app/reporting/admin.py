"""/admin: basic-auth plain-HTML operator pages (clients, applications with filters, escalations link,
adapter health, discovery stats, inbound mail)."""
from __future__ import annotations

from html import escape

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.db.models import Application, Client, CompanyRegistry, DiscoveryRun, EscalationItem, InboundEmail, JobPosting
from app.submission.health import health_report, resume_adapter

from .auth import require_admin

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])

STYLE = """<style>body{font-family:system-ui,sans-serif;max-width:1200px;margin:1.5rem auto;padding:0 1rem;color:#222}
table{border-collapse:collapse;width:100%;font-size:.92em}th,td{border:1px solid #ddd;padding:.35rem .5rem;text-align:left;vertical-align:top}
th{background:#f4f4f4}nav a{margin-right:1rem}.ok{color:#080}.bad{color:#b00}.warn{color:#c60}form.inline{display:inline}
.pill{padding:1px 6px;border-radius:4px;background:#eee;font-size:.85em}</style>"""
NAV = '<nav><a href="/admin">Overview</a><a href="/admin/clients">Clients</a><a href="/admin/applications">Applications</a><a href="/escalations">Escalations</a><a href="/admin/health">Adapter health</a><a href="/admin/inbound">Inbound mail</a></nav>'


def page(title: str, body: str) -> str:
    return f"<!doctype html><html><head><meta charset='utf-8'><title>{escape(title)}</title>{STYLE}</head><body>{NAV}<h1>{escape(title)}</h1>{body}</body></html>"


def table(headers: list[str], rows: list[list[str]]) -> str:
    h = "".join(f"<th>{escape(x)}</th>" for x in headers)
    r = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{h}</tr></thead><tbody>{r or '<tr><td colspan=99>none</td></tr>'}</tbody></table>"


@router.get("", response_class=HTMLResponse)
def overview(db: Session = Depends(get_db)):
    by_status = dict(db.execute(select(Application.status, func.count()).group_by(Application.status)).all())
    open_esc = db.scalar(select(func.count(EscalationItem.id)).where(EscalationItem.status == "open")) or 0
    jobs_open = db.scalar(select(func.count(JobPosting.id)).where(JobPosting.status == "open")) or 0
    companies = db.scalar(select(func.count(CompanyRegistry.id)).where(CompanyRegistry.active.is_(True))) or 0
    last_run = db.scalar(select(DiscoveryRun).order_by(desc(DiscoveryRun.id)))
    flagged = db.scalar(select(func.count(InboundEmail.id)).where(InboundEmail.flagged.is_(True))) or 0
    body = "<h2>Applications by status</h2>" + table(["status", "count"], [[escape(k), str(v)] for k, v in sorted(by_status.items())])
    body += f"<h2>Now</h2><ul><li>Open escalations: <a href='/escalations'>{open_esc}</a></li><li>Open jobs: {jobs_open} across {companies} active companies</li>"
    if last_run:
        body += f"<li>Last discovery: {last_run.started_at:%Y-%m-%d %H:%M} UTC — crawled {last_run.companies_crawled}, failed {last_run.companies_failed}, new jobs {last_run.jobs_new}, closed {last_run.jobs_closed}</li>"
    body += f"<li>Flagged inbound mail (replies/interviews): {flagged}</li></ul>"
    body += "<h2>Adapter health (24h)</h2>" + _health_table(health_report(db))
    return page("JobAgent admin", body)


def _health_table(rows: list[dict]) -> str:
    out = []
    for r in rows:
        rate = "n/a" if r["success_rate_24h"] is None else f"{r['success_rate_24h']:.0%}"
        status = f"<span class='bad'>PAUSED</span> {escape(r['reason'] or '')} <form class='inline' method='post' action='/admin/health/{r['ats_type']}/resume'><button>resume</button></form>" if r["paused"] else "<span class='ok'>active</span>"
        out.append([escape(r["ats_type"]), rate, str(r["attempts_24h"]), status])
    return table(["adapter", "success rate", "attempts", "state"], out)


@router.get("/health", response_class=HTMLResponse)
def health_page(db: Session = Depends(get_db)):
    return page("Adapter health", _health_table(health_report(db)))


@router.post("/health/{ats_type}/resume")
def health_resume(ats_type: str, db: Session = Depends(get_db)):
    resume_adapter(db, ats_type)
    db.commit()
    return RedirectResponse("/admin/health", status_code=303)


@router.get("/clients", response_class=HTMLResponse)
def clients_page(db: Session = Depends(get_db)):
    rows = []
    for c in db.scalars(select(Client).order_by(Client.id)):
        counts = dict(db.execute(select(Application.status, func.count()).where(Application.client_id == c.id).group_by(Application.status)).all())
        rows.append([str(c.id), escape(c.name), escape(c.alias_email or ""), escape(c.real_email), escape(c.status), "yes" if (c.base_resume and c.base_resume.approved) else "<span class='bad'>NO</span>",
                     escape(c.resume_template), escape(", ".join(f"{k}={v}" for k, v in sorted(counts.items()))), f"<a href='/admin/applications?client_id={c.id}'>apps</a>"])
    return page("Clients", table(["id", "name", "alias", "real email", "status", "resume approved", "template", "applications", ""], rows))


@router.get("/applications", response_class=HTMLResponse)
def applications_page(db: Session = Depends(get_db), status: str | None = Query(None), client_id: int | None = Query(None), ats: str | None = Query(None), limit: int = Query(200, le=1000)):
    q = select(Application).order_by(desc(Application.updated_at)).limit(limit)
    if status:
        q = q.where(Application.status == status)
    if client_id:
        q = q.where(Application.client_id == client_id)
    if ats:
        q = q.where(Application.ats_type == ats)
    rows = []
    for a in db.scalars(q):
        snap = a.job_snapshot or {}
        rows.append([str(a.id), escape(a.client.name), f"<span class='pill'>{escape(a.status)}</span>", escape(a.ats_type), escape(snap.get("company", "")), f"<a href='{escape(snap.get('url', '#'))}'>{escape(snap.get('title', ''))}</a>",
                     str(a.match_score or ""), a.scheduled_at.strftime("%m-%d %H:%M") if a.scheduled_at else "", a.submitted_at.strftime("%m-%d %H:%M") if a.submitted_at else "", str(a.attempts), escape((a.error or "")[:120]), f"<a href='/admin/applications/{a.id}'>proof</a>"])
    filters = "<form method='get'>status <input name='status' value='%s'> client_id <input name='client_id' value='%s' size=4> ats <input name='ats' value='%s' size=12> <button>filter</button></form>" % (escape(status or ""), client_id or "", escape(ats or ""))
    return page("Applications", filters + table(["id", "client", "status", "ats", "company", "title", "score", "scheduled", "submitted", "tries", "error", ""], rows))


@router.get("/applications/{app_id}", response_class=HTMLResponse)
def application_detail(app_id: int, db: Session = Depends(get_db)):
    from .proof import proof_of_work

    a = db.get(Application, app_id)
    if a is None:
        return HTMLResponse(page("Not found", "no such application"), status_code=404)
    p = proof_of_work(db, a)
    body = f"<p><b>{escape(p['client'])}</b> → {escape(p['job'].get('company',''))} — {escape(p['job'].get('title',''))} (<a href='{escape(p['job'].get('url','#'))}'>posting</a>)</p>"
    body += f"<p>status <span class='pill'>{escape(p['status'])}</span> · ats {escape(p['ats_type'])} · trace {escape(p['trace_id'])} · score {p['match_score']}</p>"
    body += "<h2>Status history</h2>" + table(["when (UTC)", "status", "note"], [[e["at"], escape(e["status"]), escape(e["note"] or "")] for e in p["events"]])
    body += "<h2>Resume sent</h2>" + (f"<p>{escape(p['resume']['path'])}<br>sha256 {escape(p['resume']['hash'])}</p>" if p["resume"] else "<p>none yet</p>")
    body += "<h2>Screenshots</h2>" + table(["step", "path"], [[escape(s["step"]), escape(s["path"])] for s in p["screenshots"]])
    body += "<h2>Confirmation</h2><pre>" + escape(p["confirmation_text"] or "") + "</pre>"
    body += "<h2>Escalations</h2>" + table(["id", "status", "question", "answer"], [[str(e["id"]), escape(e["status"]), escape(e["question"]), escape(e["answer"] or "")] for e in p["escalations"]])
    body += "<h2>Inbound mail</h2>" + table(["when", "type", "from", "subject"], [[m["at"], escape(m["classification"] or ""), escape(m["sender"]), escape(m["subject"])] for m in p["emails"]])
    return page(f"Application #{app_id}", body)


@router.get("/inbound", response_class=HTMLResponse)
def inbound_page(db: Session = Depends(get_db), limit: int = Query(200, le=1000)):
    rows = []
    for m in db.scalars(select(InboundEmail).order_by(desc(InboundEmail.id)).limit(limit)):
        rows.append([m.received_at.strftime("%m-%d %H:%M"), escape(m.recipient), escape(m.sender), escape(m.subject[:80]), escape(m.classification or ""), escape((m.extracted_value or "")[:40]), "yes" if m.forwarded else "", str(m.application_id or "")])
    return page("Inbound mail", table(["when", "to", "from", "subject", "class", "value", "forwarded", "app"], rows))
