"""Detect ATS type + slug from a careers URL and optionally add it to the registry.

    python scripts/detect_ats.py https://boards.greenhouse.io/stripe
    python scripts/detect_ats.py https://acme.wd5.myworkdayjobs.com/External --add --name "Acme"
"""
import _bootstrap  # noqa: F401
import argparse

from app.db import init_db, session_scope
from app.discovery.detect import ForbiddenSource, detect_ats
from app.discovery.registry import add_company


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--add", action="store_true")
    ap.add_argument("--name", default=None)
    args = ap.parse_args()
    try:
        d = detect_ats(args.url)
    except ForbiddenSource as e:
        raise SystemExit(f"REFUSED: {e}")
    except ValueError as e:
        raise SystemExit(f"could not detect: {e}\nTip: open the careers page, find the embedded board URL (boards.greenhouse.io/..., jobs.lever.co/..., *.myworkdayjobs.com/...) and pass that.")
    print(f"ats={d.ats_type} slug={d.slug}" + (f" tenant_host={d.tenant_host} site={d.site}" if d.tenant_host else ""))
    if args.add:
        init_db()
        with session_scope() as s:
            row = add_company(s, args.url, args.name)
            print(f"registry id={row.id} active={row.active}")


if __name__ == "__main__":
    main()
