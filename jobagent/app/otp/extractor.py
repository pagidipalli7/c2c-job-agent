"""Regex extraction of OTP codes and verification links from email text. LLM is only used on a miss."""
from __future__ import annotations

import re
from dataclasses import dataclass

_KEYWORD = r"(?:code|otp|passcode|pin|verification|verify|confirm|security|one[- ]time|token|access)"
# 4-8 digit code near a keyword, either side, within ~60 chars
_CODE_NEAR = re.compile(rf"{_KEYWORD}[^0-9]{{0,60}}?\b(\d{{4,8}})\b|\b(\d{{4,8}})\b[^0-9]{{0,40}}?{_KEYWORD}", re.I | re.S)
_CODE_ALNUM_NEAR = re.compile(rf"{_KEYWORD}\b.{{0,40}}?\b([A-Z0-9]{{6,8}})\b", re.I | re.S)
_CODE_LINE = re.compile(r"^\s*(\d{6})\s*$", re.M)  # a lone 6-digit line
_CODE_SPACED = re.compile(r"\b(\d{3})[ -](\d{3})\b")
_URL = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
_VERIFY_URL_HINT = re.compile(r"(verif|confirm|activate|validate|token=|otp|magic|signin|login|auth)", re.I)
_UNSUB = re.compile(r"(unsubscribe|preferences|privacy|terms|opt-?out|mailto:)", re.I)


@dataclass
class Extracted:
    code: str | None = None
    link: str | None = None

    @property
    def kind(self) -> str | None:
        if self.code:
            return "otp_code"
        if self.link:
            return "verification_link"
        return None


def extract_code(text: str) -> str | None:
    t = text or ""
    m = _CODE_NEAR.search(t)
    if m:
        return m.group(1) or m.group(2)
    m = _CODE_LINE.search(t)
    if m:
        return m.group(1)
    m = _CODE_SPACED.search(t)
    if m and re.search(_KEYWORD, t, re.I):
        return m.group(1) + m.group(2)
    for m in _CODE_ALNUM_NEAR.finditer(t):
        cand = m.group(1)
        if re.search(r"\d", cand) and re.search(r"[A-Za-z]", cand):
            return cand
    return None


def extract_link(text: str) -> str | None:
    urls = [u.rstrip(".,;") for u in _URL.findall(text or "")]
    verify = [u for u in urls if _VERIFY_URL_HINT.search(u) and not _UNSUB.search(u)]
    if verify:
        return max(verify, key=len)  # tokenised links are long
    return None


def extract(subject: str, body: str) -> Extracted:
    text = f"{subject or ''}\n{body or ''}"
    return Extracted(code=extract_code(text), link=extract_link(body or ""))
