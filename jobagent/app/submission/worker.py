"""Async submission worker.

- claims due applications from the queue backend
- enforces pacing (daily cap 40, 8am-8pm client-local, 3-12 min jitter per client), per-ATS concurrency 3,
  adapter health pause
- tailors + renders the resume, runs the adapter, records proof-of-work
- retry policy: transient failure -> retry x2 with backoff, then needs_human
- graceful shutdown: SIGTERM/SIGINT finish in-flight submissions, requeue the rest
"""
from __future__ import annotations

import asyncio
import signal
import traceback
import uuid
from datetime import datetime, timedelta

from app.clients.intake import get_profile_data
from app.config import get_settings
from app.db import session_scope
from app.db.base import utcnow
from app.db.models import Application, ApplicationEvent, Client, EscalationItem
from app.logging import bind_trace, clear_trace, get_logger
from app.queue import get_queue
from app.tailoring.service import prepare_resume_for_application

from . import health
from .base import NeedsHuman, NeedsOTP, SubmissionContext, SubmissionResult, TransientError
from .pacing import daily_cap_reached, in_window, jitter, next_window_start
from .registry import get_adapter

log = get_logger("worker")
MAX_ATTEMPTS = 3


def _event(app: Application, status: str, note: str | None = None) -> None:
    app.status = status
    app.events.append(ApplicationEvent(status=status, note=(note or "")[:2000]))


class Worker:
    def __init__(self, worker_id: str | None = None, ats_types: list[str] | None = None, poll_seconds: float = 15.0, force: bool = False):
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self.force = force  # bypass window/cap/jitter (manual testing only)
        self.ats_types = ats_types
        self.poll_seconds = poll_seconds
        self.stop = asyncio.Event()
        self.inflight: set[asyncio.Task] = set()
        self.next_ok: dict[int, datetime] = {}  # client_id -> earliest next submission (jitter)
        self.semaphores: dict[str, asyncio.Semaphore] = {}
        self.queue = get_queue()

    # ------------------------------------------------------------- lifecycle
    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_stop)
            except (NotImplementedError, RuntimeError):  # pragma: no cover - windows/threads
                pass

    def request_stop(self) -> None:
        log.warning("shutdown_requested", inflight=len(self.inflight))
        self.stop.set()

    async def run_forever(self) -> None:
        self.install_signal_handlers()
        log.info("worker_started", worker_id=self.worker_id, queue=self.queue.name, ats_types=self.ats_types)
        while not self.stop.is_set():
            n = await self.run_once()
            if n == 0:
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=self.poll_seconds)
                except asyncio.TimeoutError:
                    pass
        if self.inflight:
            log.info("draining_inflight", count=len(self.inflight))
            await asyncio.gather(*self.inflight, return_exceptions=True)
        log.info("worker_stopped", worker_id=self.worker_id)

    # ------------------------------------------------------------- one pass
    async def run_once(self) -> int:
        """Claim due applications and process them (bounded by per-ATS concurrency). Returns count started."""
        ids = self.queue.claim(self.worker_id, self.ats_types)
        started = 0
        for app_id in ids:
            if self.stop.is_set():
                self.queue.release(app_id)
                continue
            deferred = self._defer_if_needed(app_id)
            if deferred:
                continue
            task = asyncio.create_task(self._guarded(app_id))
            self.inflight.add(task)
            task.add_done_callback(self.inflight.discard)
            started += 1
        if self.inflight:
            await asyncio.gather(*list(self.inflight), return_exceptions=True)
        return started

    def _defer_if_needed(self, app_id: int) -> bool:
        """Apply pacing rules; returns True if the app was pushed back to the queue."""
        now = utcnow()
        with session_scope() as s:
            app = s.get(Application, app_id)
            client = s.get(Client, app.client_id)
            if client.status != "active":
                _event(app, "cancelled", f"client status={client.status}")
                return True
            if health.is_paused(s, app.ats_type):
                later = now + timedelta(hours=1)
                log.warning("adapter_paused_defer", ats_type=app.ats_type, app_id=app_id, until=later.isoformat())
                self.queue.release(app_id, later)
                return True
            if self.force:
                return False
            if not in_window(client, now):
                later = next_window_start(client, now)
                self.queue.release(app_id, later)
                return True
            if daily_cap_reached(s, client, now):
                later = next_window_start(client, now)
                log.info("daily_cap_defer", client_id=client.id, app_id=app_id, until=later.isoformat())
                self.queue.release(app_id, later)
                return True
            nxt = self.next_ok.get(client.id)
            if nxt and nxt > now:
                self.queue.release(app_id, nxt)
                return True
            self.next_ok[client.id] = now + jitter()
        return False

    async def _guarded(self, app_id: int) -> None:
        with session_scope() as s:
            ats = s.get(Application, app_id).ats_type
        sem = self.semaphores.setdefault(ats, asyncio.Semaphore(get_settings().per_ats_concurrency))
        async with sem:
            try:
                await self.process(app_id)
            except Exception:  # noqa: BLE001
                log.error("worker_unhandled", app_id=app_id, tb=traceback.format_exc())
                with session_scope() as s:
                    app = s.get(Application, app_id)
                    if app and app.status == "in_progress":
                        _event(app, "needs_human", f"unhandled error: {traceback.format_exc()[-800:]}")

    # ------------------------------------------------------------- submission
    async def process(self, app_id: int) -> SubmissionResult | None:
        with session_scope() as s:
            app = s.get(Application, app_id)
            if app is None or app.status != "in_progress":
                return None
            client = s.get(Client, app.client_id)
            bind_trace(trace_id=app.trace_id, app_id=app.id, client_id=client.id, ats=app.ats_type)
            app.attempts += 1
            app.events.append(ApplicationEvent(status="in_progress", note=f"attempt {app.attempts} by {self.worker_id}"))
            try:
                if client.base_resume is None or not client.base_resume.approved:
                    _event(app, "needs_human", "base resume not approved")
                    return None
                profile = get_profile_data(client)
                pdf = prepare_resume_for_application(s, app, client)
                adapter = get_adapter(app.ats_type)
                ctx = SubmissionContext(session=s, application=app, client=client, profile=profile, resume_pdf=pdf, trace_id=app.trace_id)
                log.info("submission_start", company=(app.job_snapshot or {}).get("company"), title=(app.job_snapshot or {}).get("title"))
                result = await adapter.submit(ctx)
            except NeedsHuman as e:
                result = SubmissionResult(status="needs_human", step_reached=e.step, error=e.reason, escalation_ids=e.escalation_ids)
            except NeedsOTP as e:
                result = SubmissionResult(status="needs_otp", step_reached=e.step, error=str(e))
            except TransientError as e:
                result = SubmissionResult(status="failed", step_reached="transient", error=str(e), transient=True)
            except Exception as e:  # noqa: BLE001
                result = SubmissionResult(status="failed", step_reached="exception", error=f"{type(e).__name__}: {e}", transient=False)
            finally:
                clear_trace()
            self._apply_result(s, app, result)
            return result

    def _apply_result(self, s, app: Application, r: SubmissionResult) -> None:
        app.step_reached = r.step_reached or app.step_reached
        app.error = r.error
        app.locked_by = None
        app.locked_at = None
        if r.status == "success":
            app.confirmation_text = r.confirmation_text
            app.submitted_at = utcnow()
            _event(app, "submitted", r.confirmation_text)
            health.record_result(s, app.ats_type, "success", app.id)
        elif r.status == "needs_otp":
            # OTP never arrived: retry later (the account may need manual verification)
            if app.attempts < MAX_ATTEMPTS:
                app.scheduled_at = utcnow() + timedelta(minutes=30)
                _event(app, "queued", f"otp timeout at {r.step_reached}; retry #{app.attempts}")
            else:
                _event(app, "needs_human", f"otp never arrived after {app.attempts} attempts")
            health.record_result(s, app.ats_type, "needs_otp", app.id)
        elif r.status == "needs_human":
            _event(app, "needs_human", r.error)
            if r.escalation_ids:
                for eid in r.escalation_ids:
                    item = s.get(EscalationItem, eid)
                    if item and item.application_id is None:
                        item.application_id = app.id
            health.record_result(s, app.ats_type, "needs_human", app.id)
        else:  # failed
            if r.transient and app.attempts < MAX_ATTEMPTS:
                backoff = timedelta(minutes=5 * (2 ** (app.attempts - 1)))
                app.scheduled_at = utcnow() + backoff
                _event(app, "queued", f"transient failure, retry in {backoff}: {r.error}")
            elif r.transient:
                app.error = f"failed after {app.attempts} attempts: {r.error}"
                _event(app, "needs_human", app.error)
                health.record_result(s, app.ats_type, "failed", app.id)
            else:
                _event(app, "failed", r.error)
                health.record_result(s, app.ats_type, "failed", app.id)
        health.evaluate_health(s, app.ats_type)
        log.info("submission_result", app_id=app.id, status=app.status, step=r.step_reached, error=(r.error or "")[:200])
