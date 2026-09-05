"""Regex heuristics shared by sources and the analyzer (employment type, visa, rate, email)."""
from __future__ import annotations

import re

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)

_C2C = re.compile(r"\b(c2c|corp\s*[-–]?\s*to\s*[-–]?\s*corp|corp2corp|1099)\b", re.I)
_W2 = re.compile(r"\bw[-\s]?2\b", re.I)
_FTE = re.compile(r"\b(full[-\s]?time|fte|permanent|direct[-\s]hire)\b", re.I)
_CONTRACT = re.compile(r"\b(contract|contractor|consultant|consulting)\b", re.I)

# Strong "no visa" language. Deliberately conservative: matches explicit exclusion phrases only.
_VISA_RESTRICTED = re.compile(
    r"("
    r"\bus\s*citizens?\s*(only|required)\b"
    r"|\b(u\.?s\.?c\.?|usc)\s*(/|or|and)\s*(gc|g\.?c\.?|green\s*card)\b"
    r"|\bgc\s*(/|or)\s*usc\b"
    r"|\b(green\s*card|gc)\s*(holders?\s*)?(only|required)\b"
    r"|\bno\s*(visa\s*)?sponsorship\b"
    r"|\b(cannot|can\s*not|unable\s*to|will\s*not|won'?t|do(es)?\s*not)\s*(provide|offer|support)\s*(visa\s*)?sponsorship\b"
    r"|\bwithout\s*(visa\s*)?sponsorship\b"
    r"|\b(only\s*)?(us\s*citizens?|green\s*card\s*holders?|permanent\s*residents?)\s*(and|or|/)\s*(green\s*card\s*holders?|us\s*citizens?|permanent\s*residents?)\b"
    r"|\bw2\s*(only\s*)?(for\s*)?(us\s*)?citizens?\b"
    r"|\b(must\s*be\s*(a\s*)?)?(us\s*citizen|u\.s\.\s*citizen)\b"
    r"|\bsecurity\s*clearance\s*(required|is\s*required)\b"
    r"|\bpublic\s*trust\b"
    r"|\bno\s*(h1b|h-1b|opt|cpt|ead)\b"
    r"|\b(h1b|h-1b)\s*(not\s*accepted|not\s*allowed|cannot)\b"
    r")",
    re.I,
)
_VISA_OK = re.compile(r"\b(h[-\s]?1b\s*(ok|welcome|accepted|transfer)|visa\s*sponsorship\s*(available|provided)|sponsor(ship)?\s*(available|provided)|all\s*visas?)\b", re.I)

_RATE = re.compile(
    r"(\$\s?\d{2,3}(?:\.\d{1,2})?\s*(?:-|–|to)\s*\$?\s?\d{2,3}(?:\.\d{1,2})?\s*(?:/|per)?\s*(?:hr|hour|h)\b"
    r"|\$\s?\d{2,3}(?:\.\d{1,2})?\s*(?:/|per)\s*(?:hr|hour|h)\b"
    r"|\$\s?\d{2,3}(?:k|,\d{3})\s*(?:-|–|to)\s*\$?\s?\d{2,3}(?:k|,\d{3})(?:\s*(?:/|per)\s*(?:yr|year|annum))?"
    r"|\$\s?\d{2,3}\s*(?:-|–|to)\s*\d{2,3}\s*(?:/|per)?\s*(?:hr|hour)\b)",
    re.I,
)


def employment_hint(text: str) -> str:
    t = text or ""
    if _C2C.search(t):
        return "C2C"
    if _W2.search(t) and not _FTE.search(t):
        return "W2"
    if _FTE.search(t) and not _CONTRACT.search(t):
        return "Full-time"
    if _CONTRACT.search(t):
        return "Contract (type unclear)"
    return ""


def visa_hint(text: str) -> str:
    t = text or ""
    if _VISA_RESTRICTED.search(t):
        return "Restricted"
    if _VISA_OK.search(t):
        return "H1B-OK"
    return ""


def visa_restricted_hard(text: str) -> bool:
    """Belt-and-braces hard check used after the model's answer."""
    return bool(_VISA_RESTRICTED.search(text or ""))


def find_rate(text: str) -> str:
    m = _RATE.search(text or "")
    return re.sub(r"\s+", " ", m.group(0)).strip() if m else ""


def find_emails(text: str) -> list[str]:
    seen, out = set(), []
    for e in EMAIL_RE.findall(text or ""):
        e = e.strip(".").lower()
        if e not in seen and not e.endswith((".png", ".jpg", ".gif")):
            seen.add(e)
            out.append(e)
    return out
