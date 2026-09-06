"""Inbound-mail classification: {type: otp_code | verification_link | recruiter_reply | interview_request |
rejection | spam, value}. Regex first; Haiku only when regex can't decide."""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.llm import LLMError, get_llm, register_mock
from app.logging import get_logger

from .extractor import extract

log = get_logger("otp.classifier")

TYPES = ("otp_code", "verification_link", "recruiter_reply", "interview_request", "rejection", "spam")

_INTERVIEW = re.compile(r"\b(interview|schedule (a|some) time|phone screen|screening call|next steps?|meet (with )?(the|our) team|availability (for|to) (a|an)? ?(call|chat|conversation)|book a time|calendly|would love to (chat|talk|speak)|set up a (call|time)|coding (challenge|assessment)|take-?home|hackerrank|codility)\b", re.I)
_REJECTION = re.compile(r"\b(not (be )?moving forward|decided to (move forward|proceed) with other|other candidates|unfortunately|not selected|no longer (being )?considered|will not be (moving|proceeding)|regret to inform|position has been filled|pursue other (candidates|applicants)|not a (match|fit) at this time|declined)\b", re.I)
_RECRUITER = re.compile(r"\b(recruit\w*|talent|hiring (manager|team)|thank you for (your )?(interest|applying)|your application|reviewing your (application|resume|profile)|application (was|has been) received|we('ve| have) received your application)\b", re.I)
_SPAM = re.compile(r"\b(unsubscribe|newsletter|webinar|special offer|limited time|% off|discount|promo|marketing|job alert|recommended jobs|jobs for you|daily digest)\b", re.I)
_ACK = re.compile(r"\b(application (was|has been) (received|submitted)|thank you for applying|we('ve| have) received your application|confirmation of your application)\b", re.I)

SYSTEM = """Classify a job-application related email for a candidate. Categories:
- otp_code: contains a one-time code for logging in / verifying an account (value = the code)
- verification_link: asks to click a link to verify/activate an account (value = the URL)
- interview_request: recruiter/hiring team wants to schedule a call, interview, assessment, or asks for availability
- recruiter_reply: any other human/personal reply from a recruiter or hiring team that needs the candidate's attention (questions, requests for documents, offers)
- rejection: the candidate is not moving forward
- spam: automated acknowledgements, job alerts, newsletters, marketing, anything not needing attention
Respond with ONLY JSON: {"type": <category>, "value": <code/url or null>, "confidence": <0-1>}"""


@dataclass
class Classification:
    type: str
    value: str | None = None
    by: str = "regex"
    confidence: float = 1.0


def classify_regex(subject: str, body: str) -> Classification | None:
    ex = extract(subject, body)
    text = f"{subject}\n{body}"
    if ex.code and re.search(r"(code|otp|passcode|pin|verif|one[- ]time|sign[- ]?in|log[- ]?in|security)", text, re.I):
        return Classification("otp_code", ex.code)
    if ex.link and re.search(r"(verify|confirm|activate|validate) (your )?(email|account|address)|click (the|this) link|magic link|sign[- ]?in link", text, re.I):
        return Classification("verification_link", ex.link)
    if _INTERVIEW.search(text) and not _REJECTION.search(text):
        return Classification("interview_request")
    if _REJECTION.search(text):
        return Classification("rejection")
    if _ACK.search(text) and not re.search(r"\?", body or ""):
        return Classification("spam")  # automated "we received your application" -> no action
    if _SPAM.search(text) and not _RECRUITER.search(text):
        return Classification("spam")
    return None


def classify(subject: str, body: str, sender: str = "") -> Classification:
    c = classify_regex(subject or "", body or "")
    if c:
        return c
    user = f"FROM: {sender}\nSUBJECT: {subject}\n\n{(body or '')[:4000]}"
    try:
        data = get_llm().json_call("haiku", SYSTEM, user, purpose="classify_email", max_tokens=200)
        t = str(data.get("type", "spam")).strip().lower() if isinstance(data, dict) else "spam"
        if t not in TYPES:
            t = "recruiter_reply" if _RECRUITER.search(subject + body) else "spam"
        value = data.get("value") if isinstance(data, dict) else None
        try:
            conf = float(data.get("confidence", 0.7)) if isinstance(data, dict) else 0.5
        except (TypeError, ValueError):
            conf = 0.5
        if t == "otp_code" and (not value or not re.fullmatch(r"[A-Za-z0-9-]{4,10}", str(value))):
            value = extract(subject, body).code
            if not value:
                t = "spam"
        return Classification(t, str(value) if value else None, by="llm", confidence=conf)
    except LLMError as e:
        log.warning("classify_llm_failed", error=str(e))
        # fail safe: a human wrote it? -> treat as recruiter reply so it gets forwarded rather than lost
        return Classification("recruiter_reply" if _RECRUITER.search(subject + body) else "spam", by="fallback", confidence=0.3)


def _mock_classify(system: str, user: str) -> dict:
    text = user.lower()
    if "interview" in text or "availability" in text:
        return {"type": "interview_request", "value": None, "confidence": 0.9}
    if "unfortunately" in text or "other candidates" in text:
        return {"type": "rejection", "value": None, "confidence": 0.9}
    if "?" in text and ("recruit" in text or "hiring" in text or "regards" in text):
        return {"type": "recruiter_reply", "value": None, "confidence": 0.8}
    return {"type": "spam", "value": None, "confidence": 0.6}


register_mock("classify_email", _mock_classify)
