"""FastAPI entrypoint: internal API + webhooks + plain-HTML operator pages + APScheduler."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.clients.api import router as clients_router
from app.config import get_settings
from app.db import init_db
from app.escalation.api import router as escalations_router
from app.logging import configure_logging, get_logger
from app.otp.webhook import router as webhook_router
from app.reporting.admin import router as admin_router

log = get_logger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    init_db()
    scheduler = None
    if getattr(app.state, "enable_scheduler", True):
        from app.scheduler import build_scheduler

        scheduler = build_scheduler()
        scheduler.start()
        log.info("scheduler_started", jobs=[j.id for j in scheduler.get_jobs()])
    try:
        yield
    finally:
        if scheduler:
            scheduler.shutdown(wait=False)


def create_app(enable_scheduler: bool = True) -> FastAPI:
    app = FastAPI(title="JobAgent", version="0.1.0", lifespan=lifespan, docs_url="/docs")
    app.state.enable_scheduler = enable_scheduler
    app.include_router(clients_router)
    app.include_router(escalations_router)
    app.include_router(webhook_router)
    app.include_router(admin_router)

    @app.get("/health")
    def health():
        return {"ok": True, "llm_mode": get_settings().effective_llm_mode}

    return app


app = create_app()
