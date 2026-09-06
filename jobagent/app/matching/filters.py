"""Hard filters: no LLM. Each returns None (pass) or a rejection reason string."""
from __future__ import annotations

import re
from datetime import timedelta

from app.clients.schemas import ProfileData
from app.config import get_settings
from app.db.base import utcnow
from app.db.models import JobPosting
from app.discovery.base import normalize, normalize_company

_STATE_ABBR = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar", "california": "ca", "colorado": "co", "connecticut": "ct",
    "delaware": "de", "florida": "fl", "georgia": "ga", "hawaii": "hi", "idaho": "id", "illinois": "il", "indiana": "in", "iowa": "ia",
    "kansas": "ks", "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md", "massachusetts": "ma", "michigan": "mi",
    "minnesota": "mn", "mississippi": "ms", "missouri": "mo", "montana": "mt", "nebraska": "ne", "nevada": "nv", "new hampshire": "nh",
    "new jersey": "nj", "new mexico": "nm", "new york": "ny", "north carolina": "nc", "north dakota": "nd", "ohio": "oh", "oklahoma": "ok",
    "oregon": "or", "pennsylvania": "pa", "rhode island": "ri", "south carolina": "sc", "south dakota": "sd", "tennessee": "tn", "texas": "tx",
    "utah": "ut", "vermont": "vt", "virginia": "va", "washington": "wa", "west virginia": "wv", "wisconsin": "wi", "wyoming": "wy",
}
_CONTRACT_WORDS = {
    "contract": re.compile(r"\b(contract|contractor|c2c|corp[- ]to[- ]corp|1099|temporary|temp)\b", re.I),
    "internship": re.compile(r"\b(intern|internship|co-op)\b", re.I),
    "part_time": re.compile(r"\bpart[- ]time\b", re.I),
}


def _loc_tokens(s: str) -> set[str]:
    t = normalize(s)
    for name, abbr in _STATE_ABBR.items():
        t = t.replace(name, abbr)
    return set(t.split())


def blacklist_reason(profile: ProfileData, job: JobPosting) -> str | None:
    company = normalize_company(job.company_name)
    slug = normalize_company(job.company_slug)
    for b in profile.blacklist.companies:
        nb = normalize_company(b)
        if nb and (nb == company or nb == slug or nb in company or company in nb and len(company) > 3):
            return f"blacklisted company: {b}"
    for d in profile.blacklist.domains:
        d = d.lower().strip()
        if d and d in (job.url or "").lower():
            return f"blacklisted domain: {d}"
    return None


def location_reason(profile: ProfileData, job: JobPosting) -> str | None:
    prefs = profile.preferences
    is_remote = bool(job.remote)
    if prefs.remote == "only" and not is_remote:
        return "not remote (client wants remote only)"
    if prefs.remote == "exclude" and is_remote:
        return "remote role (client excludes remote)"
    if is_remote and prefs.remote in ("include", "only"):
        return None
    if not prefs.locations:
        return None
    job_tokens = _loc_tokens(job.location)
    if not job_tokens:
        return None  # unknown location: let the LLM judge
    for loc in prefs.locations:
        if loc.lower().strip() == "remote":
            continue
        want = _loc_tokens(loc)
        if want and want <= job_tokens:
            return None
        # city-only preference ("Austin") vs "Austin, TX"
        if want and (want & job_tokens) and len(want & job_tokens) >= max(1, len(want) - 1):
            return None
    return f"location {job.location!r} not in preferences {prefs.locations}"


def contract_type_reason(profile: ProfileData, job: JobPosting) -> str | None:
    allowed = {c.lower() for c in profile.preferences.contract_types}
    blob = f"{job.title} {(job.raw or {}).get('commitment') or ''} {(job.raw or {}).get('employmentType') or ''} {(job.raw or {}).get('typeOfEmployment') or ''}"
    for kind, pat in _CONTRACT_WORDS.items():
        if pat.search(blob) and kind not in allowed:
            return f"{kind} role not in contract_types {sorted(allowed)}"
    return None


def title_reason(profile: ProfileData, job: JobPosting) -> str | None:
    title = normalize(job.title)
    for kw in profile.preferences.exclude_title_keywords:
        if normalize(kw) and normalize(kw) in title:
            return f"title contains excluded keyword {kw!r}"
    title_tokens = set(title.split())
    for target in profile.preferences.target_titles:
        t_tokens = [w for w in normalize(target).split() if w not in ("senior", "sr", "jr", "junior", "lead", "staff", "principal", "ii", "iii", "iv")]
        if not t_tokens:
            continue
        hits = sum(1 for w in t_tokens if w in title_tokens)
        if t_tokens[0] not in title_tokens:
            continue  # "Power Platform Developer" must not match "Platform Engineer"; "Data Engineer" not "Platform Engineer"
        if hits >= max(1, len(t_tokens) - 1) and hits / len(t_tokens) >= 0.6:
            return None
    return f"title {job.title!r} does not match target titles"


def freshness_reason(profile: ProfileData, job: JobPosting) -> str | None:
    max_days = get_settings().max_job_age_days
    posted = job.posted_at or job.first_seen
    if posted and posted < utcnow() - timedelta(days=max_days):
        return f"posted {posted.date()} is older than {max_days} days"
    return None


HARD_FILTERS = [
    ("blacklist", blacklist_reason),
    ("location", location_reason),
    ("contract_type", contract_type_reason),
    ("title", title_reason),
    ("freshness", freshness_reason),
]


def hard_filter(profile: ProfileData, job: JobPosting) -> tuple[bool, str | None, str | None]:
    """Returns (passed, failing_filter_name, reason)."""
    for name, fn in HARD_FILTERS:
        reason = fn(profile, job)
        if reason:
            return False, name, reason
    return True, None, None
