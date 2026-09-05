"""Google Sheets append-only writer (gspread + service account).

Modes:
  * daily_tabs: true  -> one worksheet per day, named with the local date (e.g. 2026-09-05), created
                         on first write with a header row. Dedupe reads job_id from EVERY tab.
  * daily_tabs: false -> a single fixed worksheet (config sheet.worksheet, or the first sheet).
Timestamps use sheet.timezone (default America/Chicago -> CST/CDT).
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .models import Job

log = logging.getLogger("sheet")

DEFAULT_COLUMNS = [
    "date_found", "job_id", "title", "company", "location", "rate", "employment_type",
    "contact_email", "match_percent", "missing_skills", "visa_status", "source", "url",
]


def _col_letter(idx1: int) -> str:
    """1 -> A, 27 -> AA."""
    s = ""
    while idx1 > 0:
        idx1, rem = divmod(idx1 - 1, 26)
        s = chr(65 + rem) + s
    return s


def job_row(job: Job, columns: list[str], now: datetime | None = None, time_format: str = "%Y-%m-%d %H:%M %Z") -> list:
    now = now or datetime.now(timezone.utc)
    values = {
        "date_found": now.strftime(time_format),
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
        self.daily_tabs = bool(cfg.get("daily_tabs", True))
        self.tab_name_format = cfg.get("tab_name_format") or "%Y-%m-%d"
        self.time_format = cfg.get("time_format") or "%Y-%m-%d %H:%M %Z"
        tz_name = cfg.get("timezone") or "America/Chicago"
        try:
            self.tz = ZoneInfo(tz_name)
        except Exception:
            log.warning("unknown timezone %r; falling back to UTC", tz_name)
            self.tz = timezone.utc
        self.sheet_id = os.environ.get("SHEET_ID", "")
        self.sa_json = os.environ.get("GOOGLE_SA_JSON", "")
        self._sh = None

    # ------------------------------------------------------------------ basics
    @property
    def configured(self) -> bool:
        return bool(self.sheet_id and self.sa_json)

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def today_tab_name(self) -> str:
        return self.now().strftime(self.tab_name_format)

    def row(self, job: Job, now: datetime | None = None) -> list:
        return job_row(job, self.columns, now or self.now(), self.time_format)

    def _spreadsheet(self):
        if self._sh is not None:
            return self._sh
        import gspread  # imported lazily so --dry-run works without creds

        if not self.configured:
            raise RuntimeError("SHEET_ID / GOOGLE_SA_JSON not set")
        try:
            info = json.loads(self.sa_json)
        except json.JSONDecodeError:
            with open(self.sa_json, "r", encoding="utf-8") as fh:  # allow a file path too
                info = json.load(fh)
        gc = gspread.service_account_from_dict(info)
        self._sh = gc.open_by_key(self.sheet_id)
        return self._sh

    def _target_worksheet(self, create: bool):
        """Today's tab (daily mode) or the fixed tab. Creates + writes the header when `create`."""
        import gspread

        sh = self._spreadsheet()
        if not self.daily_tabs:
            ws = sh.worksheet(self.worksheet_name) if self.worksheet_name else sh.sheet1
            if create:
                self._ensure_header(ws)
            return ws
        title = self.today_tab_name()
        try:
            ws = sh.worksheet(title)
        except gspread.WorksheetNotFound:
            if not create:
                return None
            ws = sh.add_worksheet(title=title, rows=1000, cols=max(len(self.columns), 13), index=0)
            log.info("created worksheet %r", title)
        if create:
            self._ensure_header(ws)
        return ws

    def _ensure_header(self, ws) -> None:
        first = ws.row_values(1)
        if not any(c.strip() for c in first):
            ws.append_row(self.columns, value_input_option="RAW")
            ws.format("1:1", {"textFormat": {"bold": True}})
            ws.freeze(rows=1)
            log.info("created header row in %r", ws.title)

    # ------------------------------------------------------------------ dedupe
    def existing_job_ids(self) -> set[str]:
        """job_id values from every worksheet that has a job_id header (2 API calls total)."""
        sh = self._spreadsheet()
        worksheets = sh.worksheets()
        if not worksheets:
            return set()
        header_ranges = [f"'{ws.title}'!1:1" for ws in worksheets]
        headers = sh.values_batch_get(header_ranges).get("valueRanges", [])
        id_ranges: list[str] = []
        for ws, hdr in zip(worksheets, headers):
            row = (hdr.get("values") or [[]])[0]
            if "job_id" in row:
                col = _col_letter(row.index("job_id") + 1)
                id_ranges.append(f"'{ws.title}'!{col}2:{col}")
        ids: set[str] = set()
        if id_ranges:
            for vr in sh.values_batch_get(id_ranges).get("valueRanges", []):
                for r in vr.get("values") or []:
                    if r and str(r[0]).strip():
                        ids.add(str(r[0]).strip())
        log.info("loaded %d existing job_ids from %d tab(s)", len(ids), len(id_ranges))
        return ids

    # ------------------------------------------------------------------ write
    def append(self, jobs: list[Job]) -> int:
        if not jobs:
            return 0
        ws = self._target_worksheet(create=True)
        now = self.now()
        rows = [self.row(j, now) for j in jobs]
        ws.append_rows(rows, value_input_option="RAW", insert_data_option="INSERT_ROWS")
        log.info("appended %d rows to %r", len(rows), ws.title)
        return len(rows)
