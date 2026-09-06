"""Wipe the local database and generated files (PDFs, screenshots). Backups are kept.

    python scripts/reset_db.py --yes
"""
import _bootstrap  # noqa: F401
import argparse
import shutil
from pathlib import Path

from app.config import get_settings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="really delete")
    args = ap.parse_args()
    s = get_settings()
    if not s.database_url.startswith("sqlite:///"):
        raise SystemExit("reset_db only handles SQLite; drop the Postgres schema by hand")
    db = Path(s.database_url[len("sqlite:///"):])
    targets = [db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm"), s.pdf_dir, s.screenshot_dir]
    if not args.yes:
        print("would delete:", *[str(t) for t in targets if t.exists()], sep="\n  ")
        print("re-run with --yes")
        return
    for t in targets:
        if t.is_dir():
            shutil.rmtree(t, ignore_errors=True)
        elif t.exists():
            t.unlink()
    for d in (s.pdf_dir, s.screenshot_dir):
        d.mkdir(parents=True, exist_ok=True)
    print("database and generated files removed; run scripts/seed.py to start again")


if __name__ == "__main__":
    main()
