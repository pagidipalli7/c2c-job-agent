from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select, update

from app.db import session_scope
from app.db.base import utcnow
from app.db.models import Application

STALE_LOCK = timedelta(minutes=45)


class DBQueue:
    name = "db"

    def enqueue(self, app: Application) -> None:  # rows are already in the table
        return None

    def claim(self, worker_id: str, ats_types: list[str] | None = None, limit: int = 20) -> list[int]:
        now = utcnow()
        claimed: list[int] = []
        with session_scope() as s:
            # recover locks abandoned by a crashed worker
            s.execute(
                update(Application)
                .where(Application.status == "in_progress", Application.locked_at < now - STALE_LOCK)
                .values(status="queued", locked_by=None, locked_at=None)
            )
            q = select(Application.id).where(Application.status == "queued", Application.scheduled_at <= now).order_by(Application.scheduled_at).limit(limit)
            if ats_types:
                q = q.where(Application.ats_type.in_(ats_types))
            for (app_id,) in s.execute(q):
                res = s.execute(
                    update(Application)
                    .where(Application.id == app_id, Application.status == "queued")
                    .values(status="in_progress", locked_by=worker_id, locked_at=now)
                )
                if res.rowcount == 1:
                    claimed.append(app_id)
        return claimed

    def release(self, app_id: int, scheduled_at: datetime | None = None) -> None:
        with session_scope() as s:
            s.execute(
                update(Application)
                .where(Application.id == app_id, Application.status == "in_progress")
                .values(status="queued", locked_by=None, locked_at=None, scheduled_at=scheduled_at or utcnow())
            )
