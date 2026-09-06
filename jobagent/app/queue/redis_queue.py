"""Redis-backed queue: ZSET of application ids scored by scheduled_at epoch. Claiming still flips the DB
row (source of truth) so a Redis loss only costs ordering, never applications."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update

from app.db import session_scope
from app.db.base import utcnow
from app.db.models import Application

KEY = "jobagent:applications:due"


def _epoch(dt: datetime) -> float:
    return dt.replace(tzinfo=timezone.utc).timestamp()


class RedisQueue:
    name = "redis"

    def __init__(self, url: str):
        import redis

        self.r = redis.Redis.from_url(url)

    def enqueue(self, app: Application) -> None:
        self.r.zadd(KEY, {str(app.id): _epoch(app.scheduled_at)})

    def resync(self) -> int:
        """Rebuild the ZSET from the DB (startup)."""
        with session_scope() as s:
            rows = s.execute(select(Application.id, Application.scheduled_at).where(Application.status == "queued")).all()
        self.r.delete(KEY)
        if rows:
            self.r.zadd(KEY, {str(i): _epoch(t) for i, t in rows})
        return len(rows)

    def claim(self, worker_id: str, ats_types: list[str] | None = None, limit: int = 20) -> list[int]:
        now = utcnow()
        ids = [int(x) for x in self.r.zrangebyscore(KEY, 0, _epoch(now), start=0, num=limit * 3)]
        claimed: list[int] = []
        with session_scope() as s:
            for app_id in ids:
                if ats_types:
                    row = s.get(Application, app_id)
                    if row is None or row.ats_type not in ats_types:
                        if row is None:
                            self.r.zrem(KEY, str(app_id))
                        continue
                res = s.execute(update(Application).where(Application.id == app_id, Application.status == "queued").values(status="in_progress", locked_by=worker_id, locked_at=now))
                if res.rowcount == 1:
                    claimed.append(app_id)
                self.r.zrem(KEY, str(app_id))
                if len(claimed) >= limit:
                    break
        return claimed

    def release(self, app_id: int, scheduled_at: datetime | None = None) -> None:
        when = scheduled_at or utcnow()
        with session_scope() as s:
            s.execute(update(Application).where(Application.id == app_id, Application.status == "in_progress").values(status="queued", locked_by=None, locked_at=None, scheduled_at=when))
        self.r.zadd(KEY, {str(app_id): _epoch(when)})
