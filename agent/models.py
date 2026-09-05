"""Job model and job_id derivation."""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query params that never identify a job and only make URLs look different.
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "gclid", "fbclid", "mc_cid", "mc_eid", "ref", "refid", "src", "source", "trk",
    "trackingid", "tracking_id", "recommendation", "rx_job", "rx_src", "rx_ts",
    "rx_paid", "rx_medium", "rx_campaign", "rx_group", "rx_viewer", "sid", "s",
}


def normalize_url(url: str) -> str:
    """Lower-case scheme/host, drop fragment + tracking params, strip trailing slash."""
    url = (url or "").strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if k.lower() not in _TRACKING_PARAMS and not k.lower().startswith("utm_")
    ]
    query.sort()
    path = re.sub(r"/+$", "", parts.path) or "/"
    return urlunsplit((parts.scheme.lower(), host, path, urlencode(query), ""))


def make_job_id(url: str, company: str, title: str, location: str) -> str:
    """job_id = normalized URL if present, else sha1(lower(company+title+location))."""
    norm = normalize_url(url)
    if norm:
        return norm
    key = (company or "").lower().strip() + (title or "").lower().strip() + (location or "").lower().strip()
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


@dataclass
class Job:
    title: str
    company: str = ""
    location: str = ""
    rate: str = ""
    description: str = ""
    url: str = ""
    source: str = ""
    contact_email: str = ""
    employment_hint: str = ""      # regex-derived hint passed to the analyzer
    visa_hint: str = ""            # regex-derived hint passed to the analyzer
    years_hint: int = 0            # regex-derived minimum years of experience (0 = not stated)
    posted_at: str = ""
    extra: dict = field(default_factory=dict)

    # filled in by the analyzer
    match_percent: int | None = None
    missing_skills: str = ""
    employment_type: str = ""
    visa_status: str = ""
    years_required: int = 0        # minimum years stated in the posting per the analyzer (0 = not stated)

    @property
    def job_id(self) -> str:
        return make_job_id(self.url, self.company, self.title, self.location)

    def clean(self) -> "Job":
        """Collapse whitespace on the short text fields."""
        for name in ("title", "company", "location", "rate", "contact_email"):
            setattr(self, name, re.sub(r"\s+", " ", (getattr(self, name) or "")).strip())
        self.description = (self.description or "").strip()
        self.url = (self.url or "").strip()
        return self

    def to_dict(self) -> dict:
        d = asdict(self)
        d["job_id"] = self.job_id
        return d
