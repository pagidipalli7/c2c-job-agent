"""Create or update a client from a YAML/JSON intake file.

    python scripts/add_client.py seed/client_alex.yaml [--approve-resume]
"""
import _bootstrap  # noqa: F401
import argparse

from app.clients.intake import IntakeError, approve_base_resume, load_intake_file, upsert_client
from app.db import init_db, session_scope


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--approve-resume", action="store_true", help="mark the base resume approved")
    args = ap.parse_args()
    init_db()
    try:
        with session_scope() as s:
            client = upsert_client(s, load_intake_file(args.path))
            if args.approve_resume:
                approve_base_resume(s, client.id)
            print(f"client id={client.id} name={client.name!r} alias={client.alias_email} resume_approved={client.base_resume.approved}")
    except IntakeError as e:
        raise SystemExit(f"intake rejected: {e}")


if __name__ == "__main__":
    main()
