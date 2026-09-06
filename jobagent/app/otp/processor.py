"""Route an inbound email: identify client by alias -> classify -> act."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Application, ApplicationEvent, Client, InboundEmail
from app.discovery.base import normalize_company
from app.logging import get_logger
from app.reporting.mail import send_mail

from .classifier import classify
from .registry import fulfil, match_waiting

log = get_logger("otp.processor")


@dataclass
class InboundPayload:
    recipient: str
    sender: str
    subject: str = ""
    body_text: str = ""
    body_html: str = ""
    message_id: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def sender_domain(self) -> str:
        m = re.search(r"@([A-Za-z0-9.-]+)", self.sender or "")
        return m.group(1).lower() if m else ""

    @property
    def sender_email(self) -> str:
        m = re.search(r"[\w.+-]+@[\w.-]+", self.sender or "")
        return m.group(0).lower() if m else (self.sender or "").lower()


def find_client_by_alias(session: Session, recipient: str) -> Client | None:
    m = re.search(r"[\w.+-]+@[\w.-]+", recipient or "")
    addr = (m.group(0) if m else recipient or "").lower()
    client = session.scalar(select(Client).where(Client.alias_email == addr))
    if client:
        return client
    m = re.match(r"client(\d+)(?:\+[^@]*)?@", addr)  # tolerate plus-addressing
    return session.get(Client, int(m.group(1))) if m else None


def _strip_html(html: str) -> str:
    from app.discovery.base import html_to_text

    return html_to_text(html)


def process_inbound(session: Session, payload: InboundPayload) -> InboundEmail:
    settings = get_settings()
    body = payload.body_text or _strip_html(payload.body_html)
    if payload.message_id:
        dup = session.scalar(select(InboundEmail).where(InboundEmail.message_id == payload.message_id))
        if dup:
            log.info("inbound_duplicate", message_id=payload.message_id)
            return dup
    client = find_client_by_alias(session, payload.recipient)
    email = InboundEmail(
        client_id=client.id if client else None,
        recipient=payload.recipient[:320],
        sender=payload.sender_email[:320],
        sender_domain=payload.sender_domain[:200],
        subject=payload.subject or "",
        body_text=body,
        message_id=payload.message_id,
    )
    session.add(email)
    session.flush()
    if client is None:
        email.classification = "unknown_recipient"
        log.warning("inbound_unknown_recipient", recipient=payload.recipient)
        return email

    c = classify(payload.subject or "", body, payload.sender)
    email.classification, email.extracted_value, email.classified_by = c.type, c.value, c.by
    log.info("inbound_classified", email_id=email.id, client_id=client.id, type=c.type, by=c.by, sender_domain=payload.sender_domain)

    if c.type in ("otp_code", "verification_link") and c.value:
        waiting = match_waiting(session, client.alias_email or "", payload.sender_domain, c.type)
        if waiting:
            fulfil(session, waiting, c.value, c.type)
            email.application_id = waiting.application_id
        else:
            log.warning("otp_no_waiting_session", client_id=client.id, sender_domain=payload.sender_domain, type=c.type)
    elif c.type in ("recruiter_reply", "interview_request"):
        app = _match_application(session, client, payload.sender_domain, body)
        email.application_id = app.id if app else None
        email.flagged = True
        email.forwarded = _forward(client, payload, body, c.type, app)
        if app:
            app.events.append(ApplicationEvent(status=app.status, note=f"{c.type} received from {payload.sender_email} (email #{email.id})"))
    elif c.type == "rejection":
        app = _match_application(session, client, payload.sender_domain, body)
        if app:
            email.application_id = app.id
            if app.status in ("submitted", "needs_human", "queued", "scheduled"):
                app.status = "rejected"
                app.events.append(ApplicationEvent(status="rejected", note=f"rejection email from {payload.sender_email} (email #{email.id})"))
        else:
            log.info("rejection_unmatched", client_id=client.id, sender_domain=payload.sender_domain)
    session.flush()
    return email


def _match_application(session: Session, client: Client, sender_domain: str, body: str) -> Application | None:
    """Best-effort: sender domain vs company name/slug, else company name mentioned in the body."""
    apps = session.scalars(
        select(Application).where(Application.client_id == client.id, Application.status.in_(("submitted", "needs_human", "queued", "in_progress", "rejected", "scheduled"))).order_by(Application.created_at.desc())
    ).all()
    root = ".".join(sender_domain.split(".")[-2:]) if sender_domain else ""
    base = root.split(".")[0] if root else ""
    for a in apps:
        snap = a.job_snapshot or {}
        names = {normalize_company(snap.get("company")), normalize_company(snap.get("company_slug")), a.company_key}
        names.discard("")
        if base and any(base == n.replace(" ", "") or base in n.replace(" ", "") or n.replace(" ", "") in base for n in names if len(n) > 2):
            return a
    low = (body or "").lower()
    for a in apps:
        name = (a.job_snapshot or {}).get("company") or ""
        if len(name) > 3 and name.lower() in low:
            return a
    return None


def _forward(client: Client, payload: InboundPayload, body: str, kind: str, app: Application | None) -> bool:
    snap = (app.job_snapshot or {}) if app else {}
    label = "Interview request" if kind == "interview_request" else "Recruiter reply"
    intro = (
        f"{label} received on your application alias ({client.alias_email}).\n"
        + (f"Application: {snap.get('company')} - {snap.get('title')} ({snap.get('url')})\n" if snap else "")
        + f"From: {payload.sender}\nSubject: {payload.subject}\n\nReply directly to the sender (Reply-To is set).\n"
        + "-" * 60 + "\n\n"
    )
    ok = send_mail(
        client.real_email,
        f"[{label}] {payload.subject or '(no subject)'}",
        intro + body,
        html=None,
        reply_to=payload.sender_email,
        headers={"X-JobAgent-Kind": kind},
    )
    log.info("inbound_forwarded", client_id=client.id, kind=kind, ok=ok, to=client.real_email)
    return ok
