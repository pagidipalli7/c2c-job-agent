"""Escalation queue: create items, notify the operator, and resume applications when answered."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.schemas import normalize_question
from app.config import get_settings
from app.db.base import utcnow
from app.db.models import AnswerBank, Application, ApplicationEvent, Client, EscalationItem
from app.logging import get_logger

log = get_logger("escalation")


def create_escalation(session: Session, client_id: int, question: str, *, application_id: int | None, llm_draft: str | None, reason: str, context: dict | None = None) -> EscalationItem:
    key = normalize_question(question)
    existing = session.scalar(
        select(EscalationItem).where(EscalationItem.client_id == client_id, EscalationItem.normalized_key == key, EscalationItem.status == "open")
    )
    if existing:
        if application_id and existing.application_id != application_id:
            ctx = dict(existing.context or {})
            ctx.setdefault("other_applications", []).append(application_id)
            existing.context = ctx
        return existing
    item = EscalationItem(
        client_id=client_id,
        application_id=application_id,
        question=question,
        normalized_key=key,
        llm_draft=llm_draft,
        reason=reason,
        context=context or {},
    )
    session.add(item)
    session.flush()
    log.info("escalation_created", escalation_id=item.id, client_id=client_id, reason=reason, question=question[:120])
    notify_operator(session, item)
    return item


def notify_operator(session: Session, item: EscalationItem) -> None:
    from app.reporting.mail import send_mail

    settings = get_settings()
    client = session.get(Client, item.client_id)
    app = session.get(Application, item.application_id) if item.application_id else None
    job = (app.job_snapshot or {}) if app else {}
    body = (
        f"New escalation #{item.id} for {client.name if client else item.client_id}\n\n"
        f"Question: {item.question}\n"
        f"Reason: {item.reason}\n"
        f"Application: {job.get('company','')} - {job.get('title','')} ({job.get('url','')})\n"
        f"LLM draft: {item.llm_draft or '(none)'}\n\n"
        f"Answer it here: {settings.base_url}/escalations\n"
    )
    send_mail(settings.operator_email, f"[JobAgent] Escalation #{item.id}: {item.question[:60]}", body)


def open_items(session: Session, client_id: int | None = None) -> list[EscalationItem]:
    q = select(EscalationItem).where(EscalationItem.status == "open").order_by(EscalationItem.created_at)
    if client_id:
        q = q.where(EscalationItem.client_id == client_id)
    return list(session.scalars(q))


def answer_escalation(session: Session, item_id: int, answer: str, *, save_to_bank: bool = True) -> EscalationItem:
    item = session.get(EscalationItem, item_id)
    if item is None:
        raise KeyError(f"escalation {item_id} not found")
    item.answer = answer
    item.status = "answered"
    item.answered_at = utcnow()
    if save_to_bank:
        row = session.scalar(select(AnswerBank).where(AnswerBank.client_id == item.client_id, AnswerBank.normalized_key == item.normalized_key))
        if row:
            row.answer, row.source, row.confidence = answer, "client", 1.0
        else:
            session.add(AnswerBank(client_id=item.client_id, question_text=item.question, normalized_key=item.normalized_key, answer=answer, source="client", confidence=1.0))
    # requeue every application that was waiting on this question
    app_ids = [item.application_id] if item.application_id else []
    app_ids += list((item.context or {}).get("other_applications", []))
    for app_id in app_ids:
        app = session.get(Application, app_id)
        if app and app.status == "needs_human":
            others = session.scalars(
                select(EscalationItem).where(EscalationItem.application_id == app.id, EscalationItem.status == "open", EscalationItem.id != item.id)
            ).all()
            if not others:
                app.status = "queued"
                app.scheduled_at = utcnow()
                app.error = None
                app.events.append(ApplicationEvent(status="queued", note=f"escalation #{item.id} answered; requeued"))
                log.info("application_requeued", app_id=app.id, escalation_id=item.id)
    session.flush()
    return item


def dismiss_escalation(session: Session, item_id: int, note: str = "") -> EscalationItem:
    item = session.get(EscalationItem, item_id)
    if item is None:
        raise KeyError(f"escalation {item_id} not found")
    item.status = "dismissed"
    item.answer = note or None
    item.answered_at = utcnow()
    if item.application_id:
        app = session.get(Application, item.application_id)
        if app and app.status == "needs_human":
            app.status = "cancelled"
            app.events.append(ApplicationEvent(status="cancelled", note=f"escalation #{item.id} dismissed"))
    session.flush()
    return item
