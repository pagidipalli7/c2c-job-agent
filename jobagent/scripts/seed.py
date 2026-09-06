"""Seed the database with 2 fake clients (seed/*.yaml) and the company registry (companies.yaml)."""
import _bootstrap  # noqa: F401
from pathlib import Path

from app.clients.intake import load_intake_file, upsert_client
from app.db import init_db, session_scope
from app.discovery.registry import load_companies_yaml


def main():
    init_db()
    with session_scope() as s:
        for f in sorted(Path("seed").glob("client_*.yaml")):
            c = upsert_client(s, load_intake_file(f))
            print(f"seeded client {c.id}: {c.name} <{c.alias_email}> resume_approved={c.base_resume.approved}")
        n = load_companies_yaml(s, Path("companies.yaml"))
        print(f"company registry: {n} companies loaded")


if __name__ == "__main__":
    main()
