"""Glue used by the submission worker: tailor + render + store for an Application."""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import Application, Client
from app.logging import get_logger

from .render import render_resume, store_resume
from .tailor import tailor_resume

log = get_logger("tailoring.service")


def prepare_resume_for_application(session: Session, app: Application, client: Client) -> bytes:
    """Returns PDF bytes; persists tailored JSON, artifact row, pdf path/hash on the application."""
    if app.resume_pdf_path and app.resume_pdf_hash:
        from pathlib import Path

        p = Path(app.resume_pdf_path)
        if p.exists():
            return p.read_bytes()
    base = client.base_resume
    if base is None or not base.approved:
        raise RuntimeError(f"client {client.id} base resume not approved")
    snap = app.job_snapshot or {}
    result = tailor_resume(base.data, snap.get("description_text") or "", snap.get("title") or "")
    rendered = render_resume(result.resume, client.resume_template, contact_email=client.alias_email, contact_phone=client.phone)
    if rendered.page_count > 2:
        log.warning("resume_over_two_pages", app_id=app.id, pages=rendered.page_count)
    art = store_resume(session, client.id, rendered, client.resume_template, result.resume, application_id=app.id, fallback_to_base=result.fallback_to_base)
    app.tailored_resume = result.resume
    app.resume_pdf_path = art.path
    app.resume_pdf_hash = art.content_hash
    session.flush()
    return rendered.pdf
