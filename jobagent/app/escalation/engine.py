"""Answer Engine for arbitrary form questions.

Order: AnswerBank hit -> profile-derived fact -> FORBIDDEN class? escalate -> Sonnet {answer, confidence}
-> confidence >= 0.8: use + save as llm_approved; else escalate with the draft.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.intake import get_profile_data
from app.clients.schemas import ProfileData, normalize_question
from app.db.models import AnswerBank, Client
from app.llm import LLMError, get_llm, register_mock
from app.logging import get_logger
from app.matching.scorer import profile_summary

from .forbidden import forbidden_class
from .profile_answers import profile_answer
from .service import create_escalation

log = get_logger("answer_engine")

CONFIDENCE_THRESHOLD = 0.8

SYSTEM = """You answer job-application form questions on behalf of a candidate, using ONLY the candidate profile provided.
Rules:
- Never invent facts. If the profile does not contain the information needed, set confidence low (< 0.5).
- Be concise and professional; answer in first person where natural. For yes/no questions answer "Yes" or "No" first.
- If a list of allowed options is given, the answer MUST be exactly one of the options, copied verbatim.
- Never answer questions about salary, relocation, criminal history, clearances, licenses, references, start dates, or legal matters - set confidence to 0 for those.

Respond with ONLY a JSON object: {"answer": <string>, "confidence": <number 0-1>, "rationale": <short string>}"""


@dataclass
class AnswerResult:
    status: str  # answered | escalated
    answer: str | None = None
    source: str | None = None  # bank_client | bank_llm | profile | llm
    confidence: float = 0.0
    escalation_id: int | None = None
    reason: str | None = None


def lookup_bank(session: Session, client_id: int, question: str) -> AnswerBank | None:
    key = normalize_question(question)
    row = session.scalar(select(AnswerBank).where(AnswerBank.client_id == client_id, AnswerBank.normalized_key == key))
    if row:
        return row
    # loose match: same key ignoring trailing "*"/"(required)" noise and 'please' wording
    loose = re.sub(r"\b(please|kindly|required|optional)\b", "", key).strip()
    if loose != key:
        return session.scalar(select(AnswerBank).where(AnswerBank.client_id == client_id, AnswerBank.normalized_key == loose))
    return None


def _fit_option(answer: str, options: list[str]) -> str | None:
    a = (answer or "").strip().lower()
    for o in options:
        if o.strip().lower() == a:
            return o
    for o in options:
        if a and (a in o.lower() or o.lower() in a):
            return o
    if a.startswith(("yes", "no")):
        for o in options:
            if o.lower().startswith(a[:2] if a.startswith("no") else "yes"):
                return o
    return None


def answer_question(
    session: Session,
    client: Client,
    question: str,
    *,
    options: list[str] | None = None,
    application_id: int | None = None,
    context: dict | None = None,
    profile: ProfileData | None = None,
) -> AnswerResult:
    profile = profile or get_profile_data(client)
    q = (question or "").strip()

    # 1. Answer bank (client-approved or previously llm-approved)
    row = lookup_bank(session, client.id, q)
    if row:
        ans = _fit_option(row.answer, options) if options else row.answer
        if ans:
            return AnswerResult(status="answered", answer=ans, source=f"bank_{row.source}", confidence=row.confidence)

    # 2. Profile-derived facts
    ans, key = profile_answer(client, profile, q, options)
    if ans:
        return AnswerResult(status="answered", answer=ans, source="profile", confidence=1.0)

    # 3. FORBIDDEN class -> always escalate (no LLM draft: humans decide these)
    fclass = forbidden_class(q)
    if fclass:
        esc = create_escalation(session, client.id, q, application_id=application_id, llm_draft=None, reason=f"forbidden_class:{fclass}", context={**(context or {}), "options": options})
        return AnswerResult(status="escalated", escalation_id=esc.id, reason=f"forbidden class: {fclass}")

    # 4. LLM
    user = f"CANDIDATE PROFILE:\n{profile_summary(profile)}\n\nQUESTION: {q}"
    if options:
        user += "\nALLOWED OPTIONS (answer must be one of these, verbatim):\n" + "\n".join(f"- {o}" for o in options)
    if context and context.get("job_title"):
        user += f"\n(Context: applying for {context['job_title']} at {context.get('company','')})"
    draft, conf = None, 0.0
    try:
        data = get_llm().json_call("sonnet", SYSTEM, user, purpose="answer_question", max_tokens=600)
        if isinstance(data, dict):
            draft = str(data.get("answer") or "").strip() or None
            try:
                conf = max(0.0, min(1.0, float(data.get("confidence", 0))))
            except (TypeError, ValueError):
                conf = 0.0
    except LLMError as e:
        log.warning("answer_llm_failed", error=str(e))
    if draft and options:
        fitted = _fit_option(draft, options)
        if not fitted:
            conf = min(conf, 0.5)
        else:
            draft = fitted
    if draft and conf >= CONFIDENCE_THRESHOLD:
        key = normalize_question(q)
        existing = session.scalar(select(AnswerBank).where(AnswerBank.client_id == client.id, AnswerBank.normalized_key == key))
        if existing is None:
            session.add(AnswerBank(client_id=client.id, question_text=q, normalized_key=key, answer=draft, source="llm_approved", confidence=conf))
            session.flush()
        return AnswerResult(status="answered", answer=draft, source="llm", confidence=conf)

    esc = create_escalation(session, client.id, q, application_id=application_id, llm_draft=draft, reason="low_confidence" if draft else "no_answer", context={**(context or {}), "options": options, "confidence": conf})
    return AnswerResult(status="escalated", escalation_id=esc.id, answer=None, confidence=conf, reason=f"confidence {conf:.2f} < {CONFIDENCE_THRESHOLD}")


# ---------------------------------------------------------------- deterministic mock
def _mock_answer(system: str, user: str) -> dict:
    q = user.split("QUESTION:", 1)[1].split("\n", 1)[0].strip().lower()
    options = []
    if "ALLOWED OPTIONS" in user:
        options = [l[2:].strip() for l in user.split("ALLOWED OPTIONS", 1)[1].splitlines() if l.startswith("- ")]
    prof = user.split("QUESTION:", 1)[0].lower()
    if options:
        # yes/no style -> prefer Yes; otherwise pick the first option confidently only if obvious
        for o in options:
            if o.lower().startswith("yes"):
                return {"answer": o, "confidence": 0.85, "rationale": "affirmative"}
        return {"answer": options[0], "confidence": 0.6, "rationale": "guess"}
    if "why" in q and ("interested" in q or "want" in q or "join" in q):
        return {"answer": "I'm drawn to this role because it matches the work I've been doing and where I want to grow next; the team's focus lines up with my experience.", "confidence": 0.85, "rationale": "generic motivation"}
    if "cover letter" in q or "tell us about yourself" in q or "describe your experience" in q:
        return {"answer": "I bring several years of hands-on experience directly relevant to this role, with a record of shipping production work and collaborating across teams.", "confidence": 0.82, "rationale": "from profile"}
    if "years" in q:
        return {"answer": "5", "confidence": 0.7, "rationale": "estimate"}
    if "favorite" in q or "hobby" in q or "fun fact" in q:
        return {"answer": "", "confidence": 0.2, "rationale": "not in profile"}
    return {"answer": "I'd be happy to discuss this further.", "confidence": 0.4, "rationale": "unclear"}


register_mock("answer_question", _mock_answer)
