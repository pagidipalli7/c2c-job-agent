"""LLM job scoring (Haiku). Strict JSON {score, apply, missing_keywords, reasons}; defensive parsing."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.clients.schemas import ProfileData
from app.db.models import JobPosting
from app.llm import LLMAuthError, LLMError, get_llm, register_mock
from app.logging import get_logger

log = get_logger("matching.scorer")

SYSTEM = """You are a job-fit evaluator for a job-application service. You receive a candidate profile summary and a job posting.
Score how well the candidate fits the role for the purpose of deciding whether to submit an application.

Scoring guidance:
- 85-100: title and core skills match, experience level matches, no dealbreakers.
- 65-84: strong overlap, minor gaps that a tailored resume can address.
- 40-64: partial overlap; would likely be screened out.
- 0-39: different discipline, seniority far off, hard requirement missing (clearance, license, on-site in an unacceptable city, sponsorship not offered when needed).
Set apply=true only when score >= 65 AND there is no hard dealbreaker.
List keywords/skills from the posting that the profile lacks in missing_keywords (max 10).

Respond with ONLY a JSON object, no prose and no code fences:
{"score": <int 0-100>, "apply": <true|false>, "missing_keywords": [<string>...], "reasons": [<string>...]}"""


@dataclass
class ScoreResult:
    score: int
    apply: bool
    missing_keywords: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    raw: dict | None = None


def profile_summary(profile: ProfileData) -> str:
    lines = [f"Name: {profile.first_name} {profile.last_name}", f"Headline: {profile.headline or ''}"]
    if profile.summary:
        lines.append(f"Summary: {profile.summary.strip()}")
    lines.append("Work history:")
    for w in profile.work_history:
        lines.append(f"- {w.title} at {w.company} ({w.start} - {w.end or 'Present'})")
        for b in w.bullets[:4]:
            lines.append(f"    * {b}")
    if profile.education:
        lines.append("Education: " + "; ".join(f"{e.degree or ''} {e.field or ''} {e.school}".strip() for e in profile.education))
    lines.append("Skills: " + ", ".join(profile.skills))
    if profile.certifications:
        lines.append("Certifications: " + ", ".join(c.name for c in profile.certifications))
    wa = profile.work_auth
    lines.append(f"Work authorization: authorized_us={wa.authorized_us}, needs_sponsorship={wa.needs_sponsorship}, visa={wa.visa_type or 'n/a'}")
    p = profile.preferences
    lines.append(f"Preferences: titles={p.target_titles}; locations={p.locations}; remote={p.remote}; min_salary=${p.min_salary:,}; contract_types={p.contract_types}")
    return "\n".join(lines)


def job_text(job: JobPosting, max_chars: int = 6000) -> str:
    desc = (job.description_text or "")[:max_chars]
    return f"Company: {job.company_name}\nTitle: {job.title}\nLocation: {job.location} (remote={job.remote})\nDepartment: {job.department or ''}\n\n{desc}"


def _coerce(data) -> ScoreResult:
    if not isinstance(data, dict):
        raise LLMError("score response is not an object")
    try:
        score = int(round(float(data.get("score", 0))))
    except (TypeError, ValueError):
        raise LLMError(f"score is not numeric: {data.get('score')!r}")
    score = max(0, min(100, score))
    apply = data.get("apply")
    if isinstance(apply, str):
        apply = apply.strip().lower() in ("true", "yes", "1")
    mk = data.get("missing_keywords") or []
    reasons = data.get("reasons") or []
    return ScoreResult(
        score=score,
        apply=bool(apply),
        missing_keywords=[str(x) for x in mk if x][:10] if isinstance(mk, list) else [],
        reasons=[str(x) for x in reasons if x][:10] if isinstance(reasons, list) else [str(reasons)],
        raw=data,
    )


def score_job(profile: ProfileData, job: JobPosting) -> ScoreResult:
    user = f"CANDIDATE PROFILE:\n{profile_summary(profile)}\n\nJOB POSTING:\n{job_text(job)}"
    llm = get_llm()
    try:
        data = llm.json_call("haiku", SYSTEM, user, purpose="score_job", max_tokens=600)
        return _coerce(data)
    except LLMAuthError:
        raise  # a bad key fails every call: abort the run instead of scoring everything 0
    except LLMError as e:
        log.warning("score_failed", job_id=job.id, error=str(e))
        return ScoreResult(score=0, apply=False, reasons=[f"scoring failed: {e}"])


# ---------------------------------------------------------------- deterministic mock
_WORD = re.compile(r"[a-z0-9+#.]+")


def _mock_score(system: str, user: str) -> dict:
    """Keyword-overlap heuristic standing in for Haiku. Deterministic, roughly sensible."""
    prof, _, job = user.partition("\n\nJOB POSTING:\n")
    skills_line = next((l for l in prof.splitlines() if l.startswith("Skills:")), "")
    skills = [re.sub(r"\s*\(.*?\)", "", s).strip().lower() for s in re.split(r",(?![^()]*\))", skills_line[len("Skills:"):]) if s.strip()]
    titles_m = re.search(r"titles=\[(.*?)\]", prof)
    targets = [t.strip(" '\"").lower() for t in (titles_m.group(1).split(",") if titles_m else [])]
    job_l = job.lower()
    title_l = job_l.split("title:", 1)[1].split("\n", 1)[0].strip() if "title:" in job_l else ""
    title_hit = any(all(w in title_l for w in t.split() if w not in ("senior", "developer", "engineer")) for t in targets if t)
    hits = [s for s in skills if any(part.strip() and part.strip() in job_l for part in s.split("/"))]
    ratio = len(hits) / max(1, min(len(skills), 15))  # long skill lists are not penalised
    score = int(35 * title_hit + 55 * min(1.0, ratio * 2) + (10 if "authorized" in job_l else 5))
    needs_sponsor = "needs_sponsorship=true" in prof.lower()
    dealbreaker = needs_sponsor and ("no sponsorship" in job_l or "not sponsor" in job_l)
    if dealbreaker:
        score = min(score, 30)
    wanted = ["power apps", "power automate", "dataverse", "dynamics 365", "spark", "airflow", "snowflake", "kafka", "dbt", "python", "sql", "kubernetes", "react", "go"]
    missing = [w for w in wanted if w in job_l and w not in " ".join(skills)]
    reasons = [f"title match={title_hit}", f"{len(hits)}/{len(skills)} skills mentioned in JD"]
    if dealbreaker:
        reasons.append("posting excludes sponsorship but candidate needs it")
    return {"score": score, "apply": score >= 65 and not dealbreaker, "missing_keywords": missing[:10], "reasons": reasons}


register_mock("score_job", _mock_score)
