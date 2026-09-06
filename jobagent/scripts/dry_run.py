"""Print match decisions (with reasons) for a client against every stored open job. Nothing is queued.

    python scripts/dry_run.py --client 1 [--all] [--markdown report.md]
"""
import _bootstrap  # noqa: F401
import argparse
from collections import Counter

from app.db import init_db
from app.logging import configure_logging
from app.matching.pipeline import match_all_open


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", type=int, default=None, help="client id (default: all active clients)")
    ap.add_argument("--all", action="store_true", help="show skipped jobs too (default: only LLM-stage and applies)")
    ap.add_argument("--commit", action="store_true", help="actually queue applications instead of dry run")
    ap.add_argument("--markdown", default=None, help="also write a markdown report to this path")
    args = ap.parse_args()
    configure_logging(json_logs=False)
    import logging

    logging.getLogger().setLevel(logging.WARNING)
    init_db()
    decisions = match_all_open(client_id=args.client, dry_run=not args.commit)
    lines = []
    by_client: dict[str, list] = {}
    for d in decisions:
        by_client.setdefault(d.client_name, []).append(d)
    for name, ds in by_client.items():
        stages = Counter(d.stage for d in ds)
        applies = [d for d in ds if d.decision == "apply"]
        lines.append(f"\n## {name}: {len(ds)} jobs evaluated, {len(applies)} to apply")
        lines.append(f"stages: {dict(stages)}")
        lines.append("\n| decision | score | company | title | location | reason |\n|---|---|---|---|---|---|")
        for d in sorted(ds, key=lambda d: (d.decision != "apply", -(d.score or 0))):
            if not args.all and d.stage == "hard_filter":
                continue
            reason = d.reason.replace("|", "/")[:120]
            if d.missing_keywords:
                reason += f" (missing: {', '.join(d.missing_keywords[:5])})"
            lines.append(f"| {d.decision.upper() if d.decision == 'apply' else d.decision} | {d.score if d.score is not None else '-'} | {d.company} | {d.title} | {d.location} | {reason} |")
        if not args.all:
            hf = Counter(d.reason.split(":")[0] for d in ds if d.stage == "hard_filter")
            lines.append(f"\nhard-filter skips (hidden, use --all): {dict(hf)}")
    text = "\n".join(lines)
    print(text)
    if args.markdown:
        with open(args.markdown, "w", encoding="utf-8") as f:
            f.write("# Dry-run match report\n" + text + "\n")
        print(f"\nwrote {args.markdown}")


if __name__ == "__main__":
    main()
