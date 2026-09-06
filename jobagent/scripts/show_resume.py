"""Render a client's *base* resume to PDF (and print the JSON) so it can be reviewed before approval.

    python scripts/show_resume.py --client 3 --pdf tarun_base_resume.pdf
    python scripts/show_resume.py --client 3 --template modern --pdf out.pdf
"""
import _bootstrap  # noqa: F401
import argparse
import json

from app.db import init_db, session_scope
from app.db.models import Client
from app.tailoring.render import render_resume


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client", type=int, required=True)
    ap.add_argument("--pdf", default=None, help="write the rendered PDF here")
    ap.add_argument("--template", default=None, help="override the client's template (classic|modern)")
    ap.add_argument("--json", action="store_true", help="print the base resume JSON")
    args = ap.parse_args()
    init_db()
    with session_scope() as s:
        c = s.get(Client, args.client)
        if c is None or c.base_resume is None:
            raise SystemExit(f"client {args.client} not found or has no base resume")
        print(f"{c.name} <{c.alias_email}>  template={c.resume_template}  approved={c.base_resume.approved}  version={c.base_resume.version}")
        if args.json:
            print(json.dumps(c.base_resume.data, indent=2))
        if args.pdf:
            r = render_resume(c.base_resume.data, args.template or c.resume_template, contact_email=c.alias_email, contact_phone=c.phone)
            with open(args.pdf, "wb") as f:
                f.write(r.pdf)
            print(f"wrote {args.pdf} ({r.page_count} page(s), sha256 {r.content_hash[:12]})")
        if not c.base_resume.approved:
            print(f"not approved yet: review, edit the intake YAML if needed, then `python scripts/add_client.py <file> --approve-resume`")


if __name__ == "__main__":
    main()
