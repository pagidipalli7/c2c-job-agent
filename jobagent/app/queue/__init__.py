from __future__ import annotations

from app.config import get_settings
from app.logging import get_logger

from .base import ApplicationQueue
from .db_queue import DBQueue

log = get_logger("queue")
_queue: ApplicationQueue | None = None


def get_queue() -> ApplicationQueue:
    global _queue
    if _queue is None:
        url = get_settings().redis_url
        if url:
            try:
                from .redis_queue import RedisQueue

                q = RedisQueue(url)
                q.r.ping()
                q.resync()
                _queue = q
                log.info("queue_backend", backend="redis")
            except Exception as e:  # noqa: BLE001
                log.warning("redis_unavailable_fallback_db", error=str(e))
                _queue = DBQueue()
        else:
            _queue = DBQueue()
    return _queue


def reset_queue() -> None:
    global _queue
    _queue = None


__all__ = ["ApplicationQueue", "DBQueue", "get_queue", "reset_queue"]
