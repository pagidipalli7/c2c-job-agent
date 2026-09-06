"""Plain-HTML escalation queue: list open items, answer or dismiss. Protected by the admin basic-auth."""
from __future__ import annotations

from html import escape

from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.db.models import Application, Client
from app.reporting.auth import require_admin

from .service import answer_escalation, dismiss_escalation, open_items

router = APIRouter(prefix="/escalations", tags=["escalations"], dependencies=[Depends(require_admin)])

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Escalations</title>
<style>body{{font-family:system-ui,sans-serif;max-width:1000px;margin:2rem auto;padding:0 1rem}}
.item{{border:1px solid #ccc;border-radius:6px;padding:1rem;margin-bottom:1rem}}
textarea{{width:100%;min-height:70px}} .meta{{color:#555;font-size:.9em}} .tag{{background:#fee;padding:2px 6px;border-radius:4px}}
nav a{{margin-right:1rem}}</style></head><body>
<nav><a href="/admin">Admin</a><a href="/escalations">Escalations</a></nav>
<h1>Open escalations ({n})</h1>{items}</body></html>"""

ITEM = """<div class="item"><div class="meta">#{id} · {client} · {reason} · {created}<br>{job}</div>
<p><strong>{question}</strong>{options}</p>
<form method="post" action="/escalations/{id}/answer">
<textarea name="answer" placeholder="Your answer (saved to the client's answer bank and the application resumes)">{draft}</textarea><br>
<label><input type="checkbox" name="save_to_bank" value="1" checked> save to answer bank</label>
<button type="submit">Answer &amp; resume</button></form>
<form method="post" action="/escalations/{id}/dismiss" style="margin-top:.5rem"><button type="submit">Dismiss (cancel application)</button></form></div>"""


@router.get("", response_class=HTMLResponse)
def list_escalations(db: Session = Depends(get_db)):
    items = open_items(db)
    blocks = []
    for it in items:
        client = db.get(Client, it.client_id)
        app = db.get(Application, it.application_id) if it.application_id else None
        job = (app.job_snapshot or {}) if app else {}
        jobline = f'{escape(job.get("company",""))} — {escape(job.get("title",""))} <a href="{escape(job.get("url","#"))}">posting</a>' if job else "(no application)"
        opts = (it.context or {}).get("options") or []
        blocks.append(
            ITEM.format(
                id=it.id,
                client=escape(client.name if client else str(it.client_id)),
                reason=f'<span class="tag">{escape(it.reason or "")}</span>',
                created=it.created_at.strftime("%Y-%m-%d %H:%M"),
                job=jobline,
                question=escape(it.question),
                options=("<br><em>Options: " + escape(" | ".join(opts)) + "</em>") if opts else "",
                draft=escape(it.llm_draft or ""),
            )
        )
    return PAGE.format(n=len(items), items="".join(blocks) or "<p>Nothing open. 🎉</p>")


@router.post("/{item_id}/answer")
def post_answer(item_id: int, answer: str = Form(...), save_to_bank: str | None = Form(None), db: Session = Depends(get_db)):
    answer_escalation(db, item_id, answer.strip(), save_to_bank=bool(save_to_bank))
    db.commit()
    return RedirectResponse("/escalations", status_code=303)


@router.post("/{item_id}/dismiss")
def post_dismiss(item_id: int, db: Session = Depends(get_db)):
    dismiss_escalation(db, item_id)
    db.commit()
    return RedirectResponse("/escalations", status_code=303)
