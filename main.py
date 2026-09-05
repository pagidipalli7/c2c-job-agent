#!/usr/bin/env python3
"""C2C job aggregator: fetch -> dedupe -> Claude analysis -> filter -> Google Sheet."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

# Load .env for local runs (GitHub Actions injects secrets as env vars directly).
_env_file = Path(__file__).resolve().parent / ".env"
if _env_file.exists():
    for line in _env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from agent.analyzer import AnalysisError, ClaudeAnalyzer  # noqa: E402
from agent.config import load_config, load_profile  # noqa: E402
from agent.dedupe import Deduper  # noqa: E402
from agent.http import HttpClient  # noqa: E402
from agent.models import Job  # noqa: E402
from agent.sheet import SheetWriter  # noqa: E402
from sources import build_sources  # noqa: E402

log = logging.getLogger("run")


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-16s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )
    for noisy in ("urllib3", "httpx", "httpcore", "google", "anthropic._base_client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="C2C job aggregator")
    p.add_argument("--dry-run", action="store_true", help="print rows instead of writing to the sheet")
    p.add_argument("--config", default=None, help="path to config.yaml")
    p.add_argument("--profile", default=None, help="path to profile.md")
    p.add_argument("--sources", default=None, help="comma-separated subset of sources to run (dice,gmail,serpapi)")
    p.add_argument("--skip-analysis", action="store_true", help="skip Claude; print raw candidates (implies --dry-run)")
    p.add_argument("--limit", type=int, default=0, help="cap candidates sent to Claude (debugging)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.skip_analysis:
        args.dry_run = True
    setup_logging(args.verbose)
    t0 = time.time()
    cfg = load_config(args.config)
    profile = load_profile(args.profile)
    http = HttpClient(cfg.get("http"))
    a_cfg = cfg.get("analysis", {})
    min_match = int(a_cfg.get("min_match_percent", 80))
    max_years = int(a_cfg.get("max_years_required", 0) or 0)   # 0 disables the experience cap
    stats: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    source_errors: dict[str, str] = {}

    # ---- 1. existing ids from the sheet ---------------------------------------
    sheet = SheetWriter(cfg.get("sheet"))
    existing: set[str] = set()
    if sheet.configured:
        try:
            existing = sheet.existing_job_ids()
        except Exception as exc:
            if args.dry_run:
                log.warning("could not read sheet (%s); continuing dry-run without existing ids", exc)
            else:
                log.error("cannot read sheet: %s", exc)
                return 2
    else:
        msg = "SHEET_ID / GOOGLE_SA_JSON not set"
        if args.dry_run:
            log.warning("%s - dry-run continues without sheet dedupe", msg)
        else:
            log.error("%s - refusing to run without a sheet (use --dry-run)", msg)
            return 2
    deduper = Deduper(existing)

    # ---- 2. fetch from each source (fail-soft) --------------------------------
    only = [s.strip() for s in args.sources.split(",")] if args.sources else None
    sources = build_sources(cfg, http, only)
    candidates: list[Job] = []
    for src in sources:
        name = src.name
        try:
            found = src.fetch()
        except Exception as exc:
            log.error("source=%s FAILED: %s", name, exc, exc_info=args.verbose)
            source_errors[name] = str(exc)
            found = []
        stats[name]["found"] = len(found)
        new = deduper.filter_new(found)
        stats[name]["new"] = len(new)
        log.info("source=%s found=%d new=%d", name, len(found), len(new))
        candidates.extend(new)

    if args.limit and len(candidates) > args.limit:
        log.info("--limit: trimming candidates %d -> %d", len(candidates), args.limit)
        candidates = candidates[: args.limit]

    # ---- 3. Claude analysis (one batched call) --------------------------------
    kept: list[Job] = []
    if args.skip_analysis:
        kept = candidates
        log.info("--skip-analysis: %d raw candidates", len(kept))
    elif candidates:
        try:
            analyzer = ClaudeAnalyzer(profile, a_cfg)
            analyzer.analyze(candidates)
        except AnalysisError as exc:
            log.error("analysis failed: %s", exc)
            return 3
        except Exception as exc:
            log.error("analysis call failed: %s", exc, exc_info=args.verbose)
            return 3
        # ---- 4. hard filters -------------------------------------------------
        for job in candidates:
            s = stats[job.source]
            if job.match_percent is None:
                s["dropped_unanalyzed"] += 1
                continue
            if job.match_percent < min_match:
                s["dropped_low_match"] += 1
                log.debug("drop low match %d%% %r", job.match_percent, job.title)
                continue
            if job.visa_status == "Restricted":
                s["dropped_visa"] += 1
                log.debug("drop visa %r", job.title)
                continue
            if max_years and job.years_required > max_years:
                s["dropped_experience"] += 1
                log.debug("drop experience %d yrs %r", job.years_required, job.title)
                continue
            kept.append(job)
    else:
        log.info("no new candidates - skipping analysis")

    # ---- 5. write / print --------------------------------------------------------
    columns = sheet.columns
    if args.dry_run:
        tab = sheet.today_tab_name() if sheet.daily_tabs else (sheet.worksheet_name or "sheet1")
        print("\n=== DRY RUN: %d row(s) that would be written to tab %r ===" % (len(kept), tab))
        print("\t".join(columns))
        now = sheet.now()
        for job in kept:
            print("\t".join(str(v) for v in sheet.row(job, now)))
        print("=== end ===\n")
        for job in kept:
            stats[job.source]["would_write"] += 1
    else:
        written = sheet.append(kept)
        by_src = defaultdict(int)
        for job in kept:
            by_src[job.source] += 1
        for name, n in by_src.items():
            stats[name]["written"] = n
        log.info("wrote %d row(s) to sheet", written)

    # ---- 6. structured summary -------------------------------------------------
    for src in sources:
        s = stats[src.name]
        log.info(
            "SUMMARY source=%s found=%d new=%d dropped_low_match=%d dropped_visa=%d dropped_experience=%d dropped_unanalyzed=%d written=%d%s",
            src.name, s["found"], s["new"], s["dropped_low_match"], s["dropped_visa"], s["dropped_experience"],
            s["dropped_unanalyzed"], s["written"], f" error={source_errors[src.name]!r}" if src.name in source_errors else "",
        )
    summary = {
        "dry_run": args.dry_run,
        "elapsed_s": round(time.time() - t0, 1),
        "candidates": len(candidates),
        "kept": len(kept),
        "sources": {k: dict(v) for k, v in stats.items()},
        "source_errors": source_errors,
    }
    print("RUN_SUMMARY " + json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
