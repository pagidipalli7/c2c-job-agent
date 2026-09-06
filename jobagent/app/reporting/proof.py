"""Proof-of-work bundle for one application: job snapshot, resume hash/file, screenshots, confirmation,
timestamps, status history, escalations, inbound mail."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Application, EscalationItem, InboundEmail


def proof_of_work(session: Session, app: Application) -> dict:
    esc = session.scalars(select(EscalationItem).where(EscalationItem.application_id == app.id)).all()
    mails = session.scalars(select(InboundEmail).where(InboundEmail.application_id == app.id).order_by(InboundEmail.received_at)).all()
    return {
        "application_id": app.id,
        "client": app.client.name,
        "status": app.status,
        "ats_type": app.ats_type,
        "trace_id": app.trace_id,
        "match_score": app.match_score,
        "match_reasons": app.match_reasons,
        "job": app.job_snapshot or {},
        "resume": {"path": app.resume_pdf_path, "hash": app.resume_pdf_hash, "tailored": app.tailored_resume} if app.resume_pdf_path else None,
        "screenshots": [{"step": s.step, "path": s.path, "at": s.created_at.isoformat()} for s in app.screenshots],
        "confirmation_text": app.confirmation_text,
        "created_at": app.created_at.isoformat(),
        "submitted_at": app.submitted_at.isoformat() if app.submitted_at else None,
        "events": [{"at": e.created_at.strftime("%Y-%m-%d %H:%M:%S"), "status": e.status, "note": e.note} for e in app.events],
        "escalations": [{"id": e.id, "status": e.status, "question": e.question, "answer": e.answer} for e in esc],
        "emails": [{"at": m.received_at.strftime("%Y-%m-%d %H:%M"), "classification": m.classification, "sender": m.sender, "subject": m.subject} for m in mails],
    }
