"""Send the weekly client digests and/or the operator daily summary now.

    python scripts/send_digests.py --weekly --operator [--print]
"""
import _bootstrap  # noqa: F401
import argparse

from sqlalchemy import select

from app.db import init_db, session_scope
from app.db.models import Client
from app.reporting.digest import client_digest, operator_summary, render_client_digest, render_operator_summary, send_all_weekly_digests, send_operator_daily_summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weekly", action="store_true")
    ap.add_argument("--operator", action="store_true")
    ap.add_argument("--print", action="store_true", help="print instead of sending")
    args = ap.parse_args()
    init_db()
    if args.print:
        with session_scope() as s:
            if args.weekly:
                for c in s.scalars(select(Client)):
                    subject, text = render_client_digest(client_digest(s, c))
                    print(f"=== {c.real_email}: {subject}\n{text}\n")
            if args.operator:
                subject, text = render_operator_summary(operator_summary(s))
                print(f"=== operator: {subject}\n{text}")
        return
    if args.weekly:
        print("weekly digests sent:", send_all_weekly_digests())
    if args.operator:
        print("operator summary sent:", send_operator_daily_summary())


if __name__ == "__main__":
    main()
