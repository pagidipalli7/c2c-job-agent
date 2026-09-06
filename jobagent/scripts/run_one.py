"""Run a single application through its adapter right now, optionally headed (for Workday acceptance on real
tenants). Bypasses pacing. Prints the result and where the screenshots went.

    python scripts/run_one.py --app 12 --headed
    python scripts/run_one.py --app 12 --simulate   # HTTP ATSs against the fake endpoints
"""
import _bootstrap  # noqa: F401
import argparse
import asyncio

from sqlalchemy import update

from app.config import get_settings
from app.db import init_db, session_scope
from app.db.base import utcnow
from app.db.models import Application
from app.logging import configure_logging
from app.submission.worker import Worker


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", type=int, required=True, help="application id")
    ap.add_argument("--headed", action="store_true", help="show the browser (Workday/generic adapters)")
    ap.add_argument("--simulate", action="store_true")
    args = ap.parse_args()
    configure_logging(json_logs=False)
    init_db()
    if args.headed:
        get_settings().headless = False
    if args.simulate:
        from app.submission.http import set_transport
        from app.submission.simulators import SubmissionSimulator

        set_transport(SubmissionSimulator().transport())
    with session_scope() as s:
        s.execute(update(Application).where(Application.id == args.app, Application.status.in_(("queued", "needs_human", "failed", "scheduled"))).values(status="queued", scheduled_at=utcnow(), locked_by=None))
    w = Worker(worker_id="manual", force=True)
    ids = w.queue.claim("manual", None, limit=1000)
    if args.app not in ids:
        for other in ids:
            w.queue.release(other)
        raise SystemExit(f"application {args.app} is not claimable (status must be queued/needs_human/failed)")
    for other in ids:
        if other != args.app:
            w.queue.release(other)
    result = asyncio.run(w.process(args.app))
    with session_scope() as s:
        a = s.get(Application, args.app)
        print(f"result: {result.status if result else None} step={a.step_reached} status={a.status} error={a.error}")
        print(f"screenshots: {get_settings().screenshot_dir / str(a.id)}")
        if a.resume_pdf_path:
            print(f"resume: {a.resume_pdf_path} sha256={a.resume_pdf_hash}")


if __name__ == "__main__":
    main()
