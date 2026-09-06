"""Deterministic answers derived from the client's profile (these ARE client-approved: the client gave us
the facts). Covers the standard fields every ATS asks for, and yes/no work-auth questions."""
from __future__ import annotations

import re

from app.clients.schemas import ProfileData
from app.db.models import Client

_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(first|given) name\b", re.I), "first_name"),
    (re.compile(r"\b(last|family) name|surname\b", re.I), "last_name"),
    (re.compile(r"\bfull name\b|^name$", re.I), "full_name"),
    (re.compile(r"\bpreferred name\b", re.I), "first_name"),
    (re.compile(r"\be-?mail\b", re.I), "email"),
    (re.compile(r"\bphone|mobile|cell\b", re.I), "phone"),
    (re.compile(r"\blinkedin\b", re.I), "linkedin"),
    (re.compile(r"\bgithub\b", re.I), "github"),
    (re.compile(r"\b(portfolio|website|personal site|url)\b", re.I), "portfolio"),
    (re.compile(r"\b(city|location|where are you (located|based)|current location)\b", re.I), "city"),
    (re.compile(r"\b(state|province)\b", re.I), "state"),
    (re.compile(r"\b(zip|postal)\b", re.I), "postal_code"),
    (re.compile(r"\bcountry\b", re.I), "country"),
    (re.compile(r"\b(current|most recent) (company|employer)\b", re.I), "current_company"),
    (re.compile(r"\b(current|most recent) (title|role|position)\b", re.I), "current_title"),
    (re.compile(r"\bauthori[sz]ed to work\b|\bwork authori[sz]ation\b|\blegally (able|eligible|permitted) to work\b|\bright to work\b", re.I), "authorized_us"),
    (re.compile(r"\bsponsor", re.I), "needs_sponsorship"),
    (re.compile(r"\b(gender|sex)\b", re.I), "eeo_gender"),
    (re.compile(r"\b(race|ethnicity|ethnic)\b", re.I), "eeo_race"),
    (re.compile(r"\bveteran\b", re.I), "eeo_veteran"),
    (re.compile(r"\bdisabilit", re.I), "eeo_disability"),
    (re.compile(r"\bhow did you hear\b|\bsource\b|\breferr", re.I), "source"),
    (re.compile(r"\b(18|eighteen) (years|or older)\b|\bat least 18\b|\bover 18\b|\bof legal age\b", re.I), "over_18"),
    (re.compile(r"\bremote\b.*\b(work|comfortable|ok|okay)\b|\bwork(ing)? remotely\b", re.I), "remote_ok"),
    (re.compile(r"\byears? of (experience|exp)\b", re.I), "years_experience"),
]

_YES_NO = re.compile(r"^(yes|no)$", re.I)


def _yes_no(value: bool) -> str:
    return "Yes" if value else "No"


def profile_answer(client: Client, profile: ProfileData, question: str, options: list[str] | None = None) -> tuple[str | None, str | None]:
    """Return (answer, key) or (None, None). Options, when given, are matched case-insensitively."""
    key = next((k for pat, k in _RULES if pat.search(question or "")), None)
    if key is None:
        return None, None
    current = next((w for w in profile.work_history if not w.end or w.end.lower() in ("present", "current", "now")), profile.work_history[0] if profile.work_history else None)
    values = {
        "first_name": profile.first_name,
        "last_name": profile.last_name,
        "full_name": f"{profile.first_name} {profile.last_name}",
        "email": client.alias_email or client.real_email,
        "phone": client.phone or "",
        "linkedin": profile.links.linkedin or "",
        "github": profile.links.github or "",
        "portfolio": profile.links.portfolio or profile.links.github or "",
        "city": ", ".join(x for x in (profile.address.city, profile.address.state) if x),
        "state": profile.address.state or "",
        "postal_code": profile.address.postal_code or "",
        "country": profile.address.country,
        "current_company": current.company if current else "",
        "current_title": current.title if current else "",
        "authorized_us": _yes_no(profile.work_auth.authorized_us),
        "needs_sponsorship": _yes_no(profile.work_auth.needs_sponsorship),
        "eeo_gender": profile.eeo.gender,
        "eeo_race": profile.eeo.race,
        "eeo_veteran": profile.eeo.veteran,
        "eeo_disability": profile.eeo.disability,
        "source": "Company careers site",
        "over_18": "Yes",
        "remote_ok": "Yes" if profile.preferences.remote != "exclude" else "No",
        "years_experience": str(_years(profile)),
    }
    answer = values.get(key)
    if not answer:
        return None, None
    if options:
        picked = _pick_option(answer, options, key)
        return (picked, key) if picked else (None, None)
    return answer, key


def _years(profile: ProfileData) -> int:
    years = set()
    for w in profile.work_history:
        try:
            s = int(str(w.start)[:4])
            e = 2026 if not w.end or w.end.lower() in ("present", "current", "now") else int(str(w.end)[:4])
            years.update(range(s, e + 1))
        except ValueError:
            continue
    return max(0, len(years) - 1)


def _pick_option(answer: str, options: list[str], key: str) -> str | None:
    a = answer.lower().strip()
    for o in options:
        if o.lower().strip() == a:
            return o
    if key.startswith("eeo_"):
        for o in options:
            ol = o.lower()
            if "decline" in a and ("decline" in ol or "not wish" in ol or "prefer not" in ol or "do not wish" in ol):
                return o
            if a in ol or ol in a:
                return o
        return None
    if a in ("yes", "no"):
        for o in options:
            if o.lower().strip().startswith(a):
                return o
    for o in options:
        if a and (a in o.lower() or o.lower() in a):
            return o
    if key == "source":
        for o in options:
            if any(t in o.lower() for t in ("career", "website", "company site", "direct")):
                return o
    return None
