"""Resume tailoring: base JSON + job description -> tailored JSON (Sonnet). Validated by the anti-fabrication
validator; rejected output is retried once with the violations fed back, then we fall back to the base resume."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from app.llm import LLMError, get_llm, register_mock
from app.logging import get_logger

from .validator import validate

log = get_logger("tailoring")

SYSTEM = """You tailor a candidate's resume to a specific job posting. You receive the candidate's BASE RESUME as JSON and a JOB DESCRIPTION.

ABSOLUTE RULES - violating any of these makes the output unusable and it will be discarded:
1. Do NOT add, remove, rename or re-date any employer, job title, start/end date, school, degree, field of study, or certification. Copy these fields verbatim.
2. Do NOT add skills that are not already listed in the base resume's skills. You may reorder skills and drop less relevant ones.
3. Do NOT invent accomplishments, metrics, numbers, tools or responsibilities that are not supported by the base resume. You may rephrase and reorder existing bullets to emphasise what the job asks for, and you may drop bullets.
4. Do NOT change the candidate's name or contact details.
5. Keep the same JSON structure as the base resume.

What you SHOULD do:
- Rewrite "summary" (2-4 sentences) to speak to this role using the candidate's real experience and the posting's keywords.
- Reorder bullets within each job so the most relevant come first; reword them to mirror the posting's terminology where the underlying fact is the same.
- Reorder "skills" so the ones the posting asks for come first.
- Optionally set "headline" to the target role's title ONLY if the candidate's experience genuinely matches it.

Respond with ONLY the tailored resume as a single JSON object. No prose, no code fences."""


@dataclass
class TailorResult:
    resume: dict
    fallback_to_base: bool
    attempts: int
    violations: list[str] = field(default_factory=list)


def _user_prompt(base: dict, job_description: str, job_title: str, feedback: list[str] | None = None) -> str:
    parts = [f"TARGET ROLE: {job_title}", "JOB DESCRIPTION:", job_description[:8000], "", "BASE RESUME (JSON):", json.dumps(base, ensure_ascii=False)]
    if feedback:
        parts += ["", "Your previous attempt was REJECTED for fabrication. Fix these and try again, copying the affected fields verbatim from the base resume:"] + [f"- {f}" for f in feedback]
    return "\n".join(parts)


def tailor_resume(base: dict, job_description: str, job_title: str = "") -> TailorResult:
    llm = get_llm()
    feedback: list[str] | None = None
    last_violations: list[str] = []
    for attempt in (1, 2):
        try:
            candidate = llm.json_call("sonnet", SYSTEM, _user_prompt(base, job_description, job_title, feedback), purpose="tailor_resume", max_tokens=8000)
        except LLMError as e:
            log.warning("tailor_llm_failed", attempt=attempt, error=str(e))
            last_violations = [f"llm error: {e}"]
            continue
        if isinstance(candidate, dict):
            # never trust the model with identity/contact fields: copy from base
            for k in ("name", "contact"):
                if k in base:
                    candidate[k] = base[k]
        violations = validate(base, candidate)
        if not violations:
            log.info("tailor_accepted", attempt=attempt)
            return TailorResult(resume=candidate, fallback_to_base=False, attempts=attempt)
        last_violations = [str(v) for v in violations]
        log.warning("tailor_rejected", attempt=attempt, violations=last_violations)
        feedback = last_violations
    log.warning("tailor_fallback_to_base", violations=last_violations)
    return TailorResult(resume=json.loads(json.dumps(base)), fallback_to_base=True, attempts=2, violations=last_violations)


# ---------------------------------------------------------------- deterministic mock
_WORD = re.compile(r"[a-zA-Z][a-zA-Z0-9+#.]{2,}")


def _mock_tailor(system: str, user: str) -> dict:
    """Keyword-driven reorder: never invents anything, so it always passes validation."""
    jd = user.split("JOB DESCRIPTION:", 1)[1].split("BASE RESUME (JSON):", 1)[0].lower()
    base = json.loads(user.split("BASE RESUME (JSON):", 1)[1].split("\nYour previous attempt", 1)[0])
    title = user.split("TARGET ROLE:", 1)[1].split("\n", 1)[0].strip()
    jd_words = set(_WORD.findall(jd))

    def relevance(text: str) -> int:
        return sum(1 for w in _WORD.findall(text.lower()) if w in jd_words)

    out = json.loads(json.dumps(base))
    out["skills"] = sorted(base.get("skills", []), key=lambda s: (-relevance(s), base["skills"].index(s)))
    for job in out.get("work_history", []):
        job["bullets"] = sorted(job.get("bullets", []), key=lambda b: -relevance(b))
    top = [s for s in out["skills"] if relevance(s)][:4]
    years = len(base.get("work_history", []))
    out["summary"] = (
        f"{base.get('headline') or 'Professional'} targeting the {title or 'role'}. "
        f"Hands-on with {', '.join(top) if top else 'the core stack'} across {years} roles, "
        f"with a track record of delivering the kind of work described in this posting."
    ).strip()
    return out


register_mock("tailor_resume", _mock_tailor)
