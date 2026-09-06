"""Shared crawler interface + job normalisation/fingerprinting."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import unescape
from typing import Protocol

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_NONALNUM = re.compile(r"[^a-z0-9 ]")
_CORP = re.compile(r"\b(inc|llc|ltd|corp|corporation|co|company|plc|gmbh|holdings|group)\b\.?")


def html_to_text(markup: str | None) -> str:
    if not markup:
        return ""
    t = unescape(markup)
    t = re.sub(r"</?(p|div|br|li|h\d|tr|ul|ol)[^>]*>", "\n", t, flags=re.I)
    t = _TAG.sub(" ", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t)
    return t.strip()


def normalize(s: str | None) -> str:
    """Lower-case, strip punctuation/corporate suffixes/whitespace. Used for fingerprints and blacklist checks."""
    t = (s or "").lower().strip()
    t = _NONALNUM.sub(" ", t)
    t = _CORP.sub(" ", t)
    return _WS.sub(" ", t).strip()


def normalize_company(s: str | None) -> str:
    return normalize(s)


def normalize_location(s: str | None) -> str:
    t = normalize(s)
    t = re.sub(r"\b(united states|usa|us)\b", "", t)
    return _WS.sub(" ", t).strip()


def fingerprint(company: str, title: str, location: str) -> str:
    payload = "|".join((normalize_company(company), normalize(title), normalize_location(location)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_REMOTE = re.compile(r"\b(remote|anywhere|work from home|wfh|distributed)\b", re.I)


def looks_remote(*fields: str | None) -> bool:
    return any(f and _REMOTE.search(f) for f in fields)


def parse_dt(value) -> datetime | None:
    """ISO strings or epoch millis -> naive UTC datetime."""
    if value in (None, "", 0):
        return None
    try:
        if isinstance(value, (int, float)):
            ts = float(value)
            if ts > 1e11:
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
        s = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except (ValueError, TypeError, OverflowError):
        return None


@dataclass
class RawJob:
    ats_type: str
    company_slug: str
    company_name: str
    external_id: str
    title: str
    location: str
    url: str
    apply_url: str | None = None
    remote: bool = False
    department: str | None = None
    description_text: str = ""
    posted_at: datetime | None = None
    raw: dict = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.company_name, self.title, self.location)


class SlugGone(Exception):
    """404 from the ATS: the board/slug no longer exists."""


class RateLimited(Exception):
    """429 that persisted through retries."""


class CrawlError(Exception):
    """Any other crawl failure (schema drift, network, 5xx)."""


class Crawler(Protocol):
    ats_type: str

    async def fetch_jobs(self, company_slug: str) -> list[RawJob]: ...
