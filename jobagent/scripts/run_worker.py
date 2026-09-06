"""Run the submission worker.

    python scripts/run_worker.py                 # all adapters
    python scripts/run_worker.py --ats greenhouse --ats lever
    python scripts/run_worker.py --once           # one pass, then exit
"""
import _bootstrap  # noqa: F401
import argparse
import asyncio

from app.db import init_db
from app.logging import configure_logging
from app.submission.worker import Worker


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ats", action="append", default=None)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--poll", type=float, default=15.0)
    ap.add_argument("--pretty", action="store_true")
    ap.add_argument("--force", action="store_true", help="ignore the 8am-8pm window, daily cap and jitter (testing only)")
    ap.add_argument("--simulate", action="store_true", help="submit to the fake ATS endpoints instead of the real ones")
    args = ap.parse_args()
    configure_logging(json_logs=not args.pretty)
    init_db()
    if args.simulate:
        from app.submission.http import set_transport
        from app.submission.simulators import SubmissionSimulator

        set_transport(SubmissionSimulator().transport())
    w = Worker(ats_types=args.ats, poll_seconds=args.poll, force=args.force)
    if args.once:
        n = asyncio.run(w.run_once())
        print(f"processed {n} application(s)")
    else:
        asyncio.run(w.run_forever())


if __name__ == "__main__":
    main()
