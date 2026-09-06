"""Crawl all active companies once.

    python scripts/run_discovery.py            # real ATS APIs
    python scripts/run_discovery.py --simulate # offline simulator (deterministic fake boards)
    python scripts/run_discovery.py --slug stripe --slug netflix
"""
import _bootstrap  # noqa: F401
import argparse
import asyncio

from app.db import init_db, session_scope
from app.discovery.registry import load_companies_yaml
from app.discovery.runner import run_discovery
from app.logging import configure_logging


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--simulate", action="store_true", help="use the offline ATS simulator")
    ap.add_argument("--generation", type=int, default=0, help="simulator board generation (change to simulate churn)")
    ap.add_argument("--slug", action="append", help="limit to these slugs")
    ap.add_argument("--concurrency", type=int, default=None)
    ap.add_argument("--pretty", action="store_true", help="human-readable logs")
    args = ap.parse_args()
    configure_logging(json_logs=not args.pretty)
    init_db()
    with session_scope() as s:
        load_companies_yaml(s, "companies.yaml")
    if args.simulate:
        from app.discovery.http import set_transport
        from app.discovery.simulator import Simulator

        set_transport(Simulator(generation=args.generation).transport())
    report = asyncio.run(run_discovery(concurrency=args.concurrency, only_slugs=args.slug))
    print(
        f"companies crawled={report.companies_crawled} failed={report.companies_failed} "
        f"jobs seen={report.jobs_seen} new={report.jobs_new} closed={report.jobs_closed}"
    )
    for k, v in report.errors.items():
        print(f"  ! {k}: {v}")


if __name__ == "__main__":
    main()
