"""Queue interface. The Application table is the durable source of truth; a queue backend only decides
*which* queued application a worker picks next. DB backend = polling query; Redis backend = sorted set."""
from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.db.models import Application


class ApplicationQueue(Protocol):
    name: str

    def enqueue(self, app: Application) -> None: ...

    def claim(self, worker_id: str, ats_types: list[str] | None = None, limit: int = 20) -> list[int]:
        """Return ids of applications that are due (scheduled_at <= now, status=queued) and lock them."""
        ...

    def release(self, app_id: int, scheduled_at: datetime | None = None) -> None:
        """Put a claimed application back (graceful shutdown, pacing deferral)."""
        ...
