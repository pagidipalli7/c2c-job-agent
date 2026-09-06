"""Map profile -> standard ATS fields, and resolve arbitrary questions through the Answer Engine."""
from __future__ import annotations

import re

from app.escalation.engine import answer_question
from app.logging import get_logger

from .base import FormQuestion, NeedsHuman, SubmissionContext

log = get_logger("submission.fields")

_STANDARD = [
    (re.compile(r"^(first[_ ]?name|given[_ ]?name|fname)$|first name", re.I), "first_name"),
    (re.compile(r"^(last[_ ]?name|family[_ ]?name|surname|lname)$|last name", re.I), "last_name"),
    (re.compile(r"^(full[_ ]?name|name|candidate_name)$", re.I), "full_name"),
    (re.compile(r"e-?mail", re.I), "email"),
    (re.compile(r"phone|mobile|tel", re.I), "phone"),
    (re.compile(r"linkedin", re.I), "linkedin"),
    (re.compile(r"github", re.I), "github"),
    (re.compile(r"portfolio|website|personal.?site|^urls?\[?(other|portfolio)", re.I), "portfolio"),
    (re.compile(r"^(org|organization|company|current[_ ]?company|employer)$|current company|current employer", re.I), "current_company"),
    (re.compile(r"^(title|current[_ ]?title|job[_ ]?title)$|current title", re.I), "current_title"),
    (re.compile(r"^(location|city|candidate_location|current_location)$|^location|current location", re.I), "location"),
    (re.compile(r"resume|cv\b", re.I), "resume"),
    (re.compile(r"cover[_ ]?letter", re.I), "cover_letter"),
]


def standard_values(ctx: SubmissionContext) -> dict[str, str]:
    p, c = ctx.profile, ctx.client
    current = next((w for w in p.work_history if not w.end or w.end.lower() in ("present", "current", "now")), p.work_history[0] if p.work_history else None)
    return {
        "first_name": p.first_name,
        "last_name": p.last_name,
        "full_name": f"{p.first_name} {p.last_name}",
        "email": c.alias_email or c.real_email,
        "phone": c.phone or "",
        "linkedin": p.links.linkedin or "",
        "github": p.links.github or "",
        "portfolio": p.links.portfolio or "",
        "current_company": current.company if current else "",
        "current_title": current.title if current else "",
        "location": ", ".join(x for x in (p.address.city, p.address.state) if x),
        "cover_letter": "",
    }


def standard_key(name_or_label: str) -> str | None:
    for pat, key in _STANDARD:
        if pat.search(name_or_label or ""):
            return key
    return None


def resolve_question(ctx: SubmissionContext, q: FormQuestion) -> str | None:
    """Value for a form question: standard field, else Answer Engine. Raises NeedsHuman when escalated."""
    if q.type in ("hidden",):
        return q.value
    std = standard_values(ctx)
    key = standard_key(q.name) or standard_key(q.label)
    if key and key not in ("resume", "cover_letter") and std.get(key):
        val = std[key]
        if q.options:
            return _fit(val, q.options) or _answer_via_engine(ctx, q)
        return val
    if key == "cover_letter":
        return None if not q.required else _answer_via_engine(ctx, q)
    if key == "resume":
        return None  # handled as a file by the adapter
    if q.type == "checkbox" and not q.options:
        # consent/acknowledgement checkboxes: only tick if it's plainly an acknowledgement, else ask
        if re.search(r"\b(agree|acknowledge|consent|confirm|certify|i understand|privacy|terms)\b", q.label, re.I) and not re.search(r"\b(background|drug|non-?compete|relocat|salary)\b", q.label, re.I):
            return "1"
        return _answer_via_engine(ctx, q)
    return _answer_via_engine(ctx, q)


def _fit(value: str, options: list[tuple[str, str]]) -> str | None:
    v = value.lower().strip()
    for label, val in options:
        if label.lower().strip() == v or str(val).lower() == v:
            return str(val)
    for label, val in options:
        if v and (v in label.lower() or label.lower() in v):
            return str(val)
    return None


def _answer_via_engine(ctx: SubmissionContext, q: FormQuestion) -> str | None:
    labels = [l for l, _ in q.options] if q.options else None
    if not q.required and not labels and q.type in ("textarea",) and re.search(r"\b(additional|anything else|other information|comments?)\b", q.label, re.I):
        return None  # optional free-text: skip rather than fabricate
    r = answer_question(
        ctx.session,
        ctx.client,
        q.label,
        options=labels,
        application_id=ctx.application.id,
        context={"job_title": ctx.job.get("title"), "company": ctx.job.get("company"), "field": q.name},
        profile=ctx.profile,
    )
    if r.status == "escalated":
        if not q.required:
            log.info("optional_question_skipped_after_escalation", field=q.name, escalation_id=r.escalation_id)
            return None
        raise NeedsHuman(f"question needs a human answer: {q.label!r}", escalation_ids=[r.escalation_id] if r.escalation_id else [], step="questions")
    if q.options:
        for label, val in q.options:
            if label == r.answer or str(val) == r.answer:
                return str(val)
        fitted = _fit(r.answer or "", q.options)
        if fitted is None:
            raise NeedsHuman(f"answer {r.answer!r} does not match options for {q.label!r}", step="questions")
        return fitted
    return r.answer
