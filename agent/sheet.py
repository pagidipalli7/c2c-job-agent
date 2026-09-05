"""Google Sheets append-only writer (gspread + service account)."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from .models import Job

log = logging.getLogger("sheet")

DEFAULT_COLUMNS = [
    "date_found", "job_id", "title", "company", "location", "rate", "employment_type",
    "contact_email", "match_percent", "missing_skills", "visa_status", "source", "url",
]


def job_row(job: Job, columns: list[str], now: datetime | None = None) -> list:
    now = now or datetime.now(timezone.utc)
    values = {
        "date_found": now.strftime("%Y-%m-%d %H:%M UTC"),
        "job_id": job.job_id,
        "title": job.title,
        "company": job.company,
        "location": job.location,
        "rate": job.rate,
        "employment_type": job.employment_type,
        "contact_email": job.contact_email,
        "match_percent": job.match_percent if job.match_percent is not None else "",
        "missing_skills": job.missing_skills,
        "visa_status": job.visa_status,
        "source": job.source,
        "url": job.url,
    }
    return [values.get(c, "") for c in columns]


class SheetWriter:
    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        self.columns = list(cfg.get("columns") or DEFAULT_COLUMNS)
        self.worksheet_name = cfg.get("worksheet") or ""
        self.sheet_id = os.environ.get("SHEET_ID", "")
        self.sa_json = os.environ.get("GOOGLE_SA_JSON", "")
        self._ws = None

    @property
    def configured(self) -> bool:
        return bool(self.sheet_id and self.sa_json)

    def _worksheet(self):
        if self._ws is not None:
            return self._ws
        import gspread  # imported lazily so --dry-run works without creds

        if not self.configured:
            raise RuntimeError("SHEET_ID / GOOGLE_SA_JSON not set")
        try:
            info = json.loads(self.sa_json)
        except json.JSONDecodeError:
            # allow a file path in GOOGLE_SA_JSON too
            with open(self.sa_json, "r", encoding="utf-8") as fh:
                info = json.load(fh)
        gc = gspread.service_account_from_dict(info)
        sh = gc.open_by_key(self.sheet_id)
        self._ws = sh.worksheet(self.worksheet_name) if self.worksheet_name else sh.sheet1
        return self._ws

    def ensure_header(self) -> None:
        ws = self._worksheet()
        first = ws.row_values(1)
        if not any(c.strip() for c in first):
            ws.append_row(self.columns, value_input_option="RAW")
            log.info("created header row")

    def existing_job_ids(self) -> set[str]:
        ws = self._worksheet()
        header = ws.row_values(1)
        if not any(c.strip() for c in header):
            return set()
        try:
            col = header.index("job_id") + 1
        except ValueError:
            col = 2
        vals = ws.col_values(col)[1:]
        ids = {v.strip() for v in vals if v and v.strip()}
        log.info("loaded %d existing job_ids from sheet", len(ids))
        return ids

    def append(self, jobs: list[Job]) -> int:
        if not jobs:
            return 0
        ws = self._worksheet()
        self.ensure_header()
        rows = [job_row(j, self.columns) for j in jobs]
        ws.append_rows(rows, value_input_option="RAW", insert_data_option="INSERT_ROWS")
        log.info("appended %d rows", len(rows))
        return len(rows)
