"""OTP session registry. A browser adapter that hits an OTP screen registers
{session_id, alias, expected_sender_domain, expires_at=now+180s} and then awaits `wait_for_value`.
The inbound-mail processor matches (alias + sender domain + waiting window) and fulfils the session."""
from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import session_scope
from app.db.base import utcnow
from app.db.models import OTPSession
from app.logging import get_logger

log = get_logger("otp.registry")


def register_session(session: Session, client_id: int, alias: str, expected_sender_domain: str, application_id: int | None = None, kind: str = "any", ttl_seconds: int | None = None) -> OTPSession:
    ttl = ttl_seconds or get_settings().otp_session_ttl_seconds
    row = OTPSession(
        session_id=uuid.uuid4().hex,
        client_id=client_id,
        application_id=application_id,
        alias=alias.lower(),
        expected_sender_domain=_root_domain(expected_sender_domain),
        kind=kind,
        expires_at=utcnow() + timedelta(seconds=ttl),
    )
    session.add(row)
    session.flush()
    log.info("otp_session_registered", session_id=row.session_id, alias=row.alias, domain=row.expected_sender_domain, expires_at=row.expires_at.isoformat())
    return row


def _root_domain(domain: str) -> str:
    parts = (domain or "").lower().strip().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else (domain or "").lower()


def _domain_matches(expected: str, actual: str) -> bool:
    e, a = _root_domain(expected), _root_domain(actual)
    if not e or e == a:
        return True
    # Workday and other ATSs send from their own domains on behalf of the company
    ats_senders = {"myworkday.com", "workday.com", "myworkdayjobs.com", "greenhouse.io", "greenhouse-mail.io", "lever.co", "hire.lever.co", "ashbyhq.com", "smartrecruiters.com", "icims.com", "successfactors.com", "taleo.net", "oraclecloud.com"}
    return a in ats_senders


def match_waiting(session: Session, alias: str, sender_domain: str, kind: str) -> OTPSession | None:
    now = utcnow()
    rows = session.scalars(
        select(OTPSession).where(OTPSession.alias == alias.lower(), OTPSession.status == "waiting", OTPSession.expires_at >= now).order_by(OTPSession.created_at.desc())
    ).all()
    for r in rows:
        if r.kind not in ("any", kind):
            continue
        if _domain_matches(r.expected_sender_domain, sender_domain):
            return r
    return None


def fulfil(session: Session, row: OTPSession, value: str, kind: str) -> None:
    row.status = "fulfilled"
    row.value = value
    row.kind = kind
    row.fulfilled_at = utcnow()
    session.flush()
    log.info("otp_session_fulfilled", session_id=row.session_id, kind=kind)


def expire_stale() -> int:
    n = 0
    with session_scope() as s:
        for r in s.scalars(select(OTPSession).where(OTPSession.status == "waiting", OTPSession.expires_at < utcnow())):
            r.status = "expired"
            n += 1
    if n:
        log.info("otp_sessions_expired", count=n)
    return n


async def wait_for_value(session_id: str, timeout_seconds: int | None = None, poll: float = 2.0) -> tuple[str, str] | None:
    """Poll the DB (cross-process safe) until the session is fulfilled. Returns (kind, value) or None on timeout."""
    timeout = timeout_seconds or get_settings().otp_wait_seconds
    deadline = utcnow() + timedelta(seconds=timeout)
    while utcnow() < deadline:
        with session_scope() as s:
            row = s.scalar(select(OTPSession).where(OTPSession.session_id == session_id))
            if row is None:
                return None
            if row.status == "fulfilled" and row.value:
                return row.kind, row.value
            if row.status in ("expired", "cancelled"):
                return None
        await asyncio.sleep(poll)
    with session_scope() as s:
        row = s.scalar(select(OTPSession).where(OTPSession.session_id == session_id))
        if row and row.status == "waiting":
            row.status = "expired"
    log.warning("otp_wait_timeout", session_id=session_id, timeout=timeout)
    return None
