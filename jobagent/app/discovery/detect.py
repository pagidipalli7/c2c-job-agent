"""Detect ATS type + slug from a careers-page URL. Job boards we refuse to automate are rejected loudly."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

FORBIDDEN_HOSTS = ("linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com")


class ForbiddenSource(ValueError):
    """LinkedIn / Indeed / Glassdoor / ZipRecruiter are never automated (ToS + ban risk)."""


@dataclass
class Detected:
    ats_type: str
    slug: str
    tenant_host: str | None = None  # workday: e.g. acme.wd5.myworkdayjobs.com
    site: str | None = None  # workday career site name


_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("greenhouse", re.compile(r"(?:boards|job-boards|boards-api)\.greenhouse\.io/(?:v1/boards/)?(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)")),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board\?for=([A-Za-z0-9_-]+)")),
    ("lever", re.compile(r"jobs\.lever\.co/([A-Za-z0-9_-]+)")),
    ("lever", re.compile(r"api\.lever\.co/v0/postings/([A-Za-z0-9_-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_-]+)")),
    ("ashby", re.compile(r"api\.ashbyhq\.com/posting-api/job-board/([A-Za-z0-9_-]+)")),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)")),
    ("smartrecruiters", re.compile(r"api\.smartrecruiters\.com/v1/companies/([A-Za-z0-9_-]+)")),
]
_WORKDAY = re.compile(r"^([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com$", re.I)
_WORKDAY_SITE = re.compile(r"^/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)")


def detect_ats(url: str) -> Detected:
    u = url.strip()
    if not re.match(r"^https?://", u):
        u = "https://" + u
    parsed = urlparse(u)
    host = parsed.netloc.lower()
    if any(host == h or host.endswith("." + h) for h in FORBIDDEN_HOSTS):
        raise ForbiddenSource(f"{host} is a job board we do not automate (ToS). Use the company's own careers site.")
    m = _WORKDAY.match(host)
    if m:
        tenant = m.group(1)
        site_m = _WORKDAY_SITE.match(parsed.path or "/")
        site = site_m.group(1) if site_m else "External"
        return Detected(ats_type="workday", slug=tenant, tenant_host=host, site=site)
    for ats, pat in _PATTERNS:
        m = pat.search(u)
        if m:
            return Detected(ats_type=ats, slug=m.group(1))
    # Greenhouse/Lever embedded on a company domain: /careers?gh_jid=..., etc. cannot be resolved without HTML.
    raise ValueError(f"could not detect ATS from {url!r}; supported: greenhouse, lever, ashby, smartrecruiters, workday")


def detect_from_html(html: str) -> Detected | None:
    """Best-effort detection from a careers page's HTML (embedded boards)."""
    for ats, pat in _PATTERNS:
        m = pat.search(html)
        if m:
            return Detected(ats_type=ats, slug=m.group(1))
    m = re.search(r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)", html, re.I)
    if m:
        return Detected(ats_type="workday", slug=m.group(1), tenant_host=f"{m.group(1)}.{m.group(2)}.myworkdayjobs.com", site=m.group(3))
    return None
