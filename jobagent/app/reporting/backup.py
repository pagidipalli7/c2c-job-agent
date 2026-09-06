"""Nightly backup: consistent SQLite copy (via the backup API) + PDFs/screenshots into data/backups/<date>/.
Keeps the newest `keep` backups."""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from app.config import get_settings
from app.logging import get_logger

log = get_logger("backup")


def run_backup(keep: int = 14) -> Path | None:
    settings = get_settings()
    dest = settings.backup_dir / datetime.utcnow().strftime("%Y%m%d-%H%M%S-%f")
    dest.mkdir(parents=True, exist_ok=True)
    url = settings.database_url
    if url.startswith("sqlite:///"):
        src_path = url[len("sqlite:///"):]
        if src_path and src_path != ":memory:" and Path(src_path).exists():
            src = sqlite3.connect(src_path)
            dst = sqlite3.connect(str(dest / "jobagent.db"))
            with dst:
                src.backup(dst)
            src.close()
            dst.close()
    else:
        (dest / "README.txt").write_text(f"database is {url.split('@')[-1]}; use pg_dump for the DB backup\n")
    for name in ("pdfs", "screenshots"):
        src_dir = settings.data_dir / name
        if src_dir.exists():
            shutil.copytree(src_dir, dest / name, dirs_exist_ok=True)
    # rotate
    backups = sorted([p for p in settings.backup_dir.iterdir() if p.is_dir()])
    for old in backups[:-keep]:
        shutil.rmtree(old, ignore_errors=True)
    log.info("backup_done", path=str(dest), kept=min(len(backups), keep))
    return dest
