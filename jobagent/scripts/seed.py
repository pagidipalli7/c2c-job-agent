"""Load the company registry (companies.yaml) and real client intake files (clients/*.yaml).

    python scripts/seed.py           # companies + clients/*.yaml
    python scripts/seed.py --demo    # also load the 2 fake demo clients from seed/ (tests use these)
"""
import _bootstrap  # noqa: F401
import argparse
from pathlib import Path

from app.clients.intake import load_intake_file, upsert_client
from app.db import init_db, session_scope
from app.discovery.registry import load_companies_yaml


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="also load the fake demo clients from seed/")
    args = ap.parse_args()
    init_db()
    with session_scope() as s:
        files = sorted(Path("clients").glob("*.yaml")) if Path("clients").exists() else []
        if args.demo:
            files += sorted(Path("seed").glob("client_*.yaml"))
        for f in files:
            c = upsert_client(s, load_intake_file(f))
            print(f"client {c.id}: {c.name} <{c.alias_email}> resume_approved={c.base_resume.approved}  ({f})")
        if not files:
            print("no client files found in clients/ (add one, see clients/tarun.yaml); use --demo for the fake clients")
        n = load_companies_yaml(s, Path("companies.yaml"))
        print(f"company registry: {n} companies loaded")


if __name__ == "__main__":
    main()
