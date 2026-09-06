"""Adapter health: rolling 24h success rate per ATS; pause an adapter below the threshold."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.base import utcnow
from app.db.models import AdapterResult, AdapterState
from app.logging import get_logger

log = get_logger("submission.health")
MIN_SAMPLES = 5


def record_result(session: Session, ats_type: str, status: str, application_id: int | None = None) -> None:
    session.add(AdapterResult(ats_type=ats_type, success=(status == "success"), status=status, application_id=application_id))
    session.flush()


def success_rate(session: Session, ats_type: str, hours: int = 24) -> tuple[float | None, int]:
    since = utcnow() - timedelta(hours=hours)
    rows = session.execute(
        select(func.count(AdapterResult.id), func.sum(func.cast(AdapterResult.success, __import__("sqlalchemy").Integer)))
        .where(AdapterResult.ats_type == ats_type, AdapterResult.created_at >= since, AdapterResult.status.in_(("success", "failed")))
    ).one()
    total, ok = int(rows[0] or 0), int(rows[1] or 0)
    return (ok / total if total else None), total


def get_state(session: Session, ats_type: str) -> AdapterState:
    st = session.get(AdapterState, ats_type)
    if st is None:
        st = AdapterState(ats_type=ats_type, paused=False)
        session.add(st)
        session.flush()
    return st


def evaluate_health(session: Session, ats_type: str) -> AdapterState:
    """Called after each result. Pauses the adapter (with WARNING) when the 24h rate drops below threshold."""
    rate, n = success_rate(session, ats_type)
    st = get_state(session, ats_type)
    threshold = get_settings().adapter_health_min_success_rate
    if rate is not None and n >= MIN_SAMPLES and rate < threshold and not st.paused:
        st.paused = True
        st.paused_reason = f"24h success rate {rate:.0%} over {n} attempts < {threshold:.0%}"
        log.warning("adapter_paused", ats_type=ats_type, rate=rate, samples=n)
    session.flush()
    return st


def is_paused(session: Session, ats_type: str) -> bool:
    st = session.get(AdapterState, ats_type)
    return bool(st and st.paused)


def resume_adapter(session: Session, ats_type: str) -> None:
    st = get_state(session, ats_type)
    st.paused = False
    st.paused_reason = None
    session.flush()


def health_report(session: Session) -> list[dict]:
    out = []
    types = {r for (r,) in session.execute(select(AdapterResult.ats_type).distinct())} | {r for (r,) in session.execute(select(AdapterState.ats_type))} | {"greenhouse", "lever", "ashby", "workday"}
    for t in sorted(types):
        rate, n = success_rate(session, t)
        st = session.get(AdapterState, t)
        out.append({"ats_type": t, "success_rate_24h": rate, "attempts_24h": n, "paused": bool(st and st.paused), "reason": st.paused_reason if st else None})
    return out
