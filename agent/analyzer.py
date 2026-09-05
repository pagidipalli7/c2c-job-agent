"""Claude analysis: ONE batched JSON call over all new candidate jobs, with defensive parsing."""
from __future__ import annotations

import json
import logging
import re

import anthropic

from .models import Job
from .textsig import visa_restricted_hard

log = logging.getLogger("analyzer")

EMPLOYMENT_VALUES = {"C2C", "W2", "Full-time", "Unclear"}
VISA_VALUES = {"H1B-OK", "Restricted", "Unclear"}

SYSTEM_PROMPT = """You are a meticulous technical recruiter screening contract job postings for ONE specific candidate.
You will receive the candidate's profile and a JSON array of job postings. For EVERY posting return one JSON object.

Scoring rules (match_percent, integer 0-100):
- Compare the posting's REQUIRED skills/experience to the candidate's profile. 90-100 = near-perfect fit; 80-89 = strong fit with at most one minor gap; 60-79 = partial; below 60 = poor.
- Primary-stack mismatches are heavy penalties: e.g. a role centred on Snowflake/AWS Glue/GCP/Java/Scala/Informatica/Salesforce/.NET when the candidate is Azure + Databricks + Power Platform.
- Seniority: candidate has 6+ years. Roles asking 10+ years or staff/principal/architect-only titles lose points; junior roles also lose points.
- Domain experience (utilities, healthcare/FHIR/HIPAA, financial services, SAP PM) is a bonus, never a requirement.

missing_skills: comma-separated REQUIRED skills the candidate lacks (per the profile's "Not in my toolkit" list and anything else clearly absent). Empty string if none. Do not list nice-to-haves.

employment_type: exactly one of "C2C", "W2", "Full-time", "Unclear".
- Any mention of corp-to-corp / C2C / 1099 / "all tax terms" / vendor-style emails describing a client requirement => "C2C".
- "W2 only", "W2 contract", "no C2C" => "W2".
- Permanent / FTE / direct hire / salaried => "Full-time".
- Otherwise "Unclear". Use the provided employment_hint as a strong signal but verify against the text.

visa_status: exactly one of "H1B-OK", "Restricted", "Unclear".
- "Restricted" if the posting says US Citizen only, Green Card only, USC/GC, GC/USC, no sponsorship, cannot sponsor, W2 only for citizens, must be US citizen, security clearance / public trust required, or similar exclusion of visa holders.
- "H1B-OK" if it explicitly welcomes H1B / visa transfer / sponsorship, or is a C2C vendor requirement with no citizenship restriction (C2C vendors routinely place H1B consultants).
- Otherwise "Unclear".

contact_email: for C2C jobs, the recruiter/vendor email taken from the contact_email_candidate field or from the posting text. Empty string if none is found or if the job is not C2C.

Return ONLY JSON matching the schema. Include every input index exactly once."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "jobs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "match_percent": {"type": "integer"},
                    "missing_skills": {"type": "string"},
                    "employment_type": {"type": "string", "enum": sorted(EMPLOYMENT_VALUES)},
                    "visa_status": {"type": "string", "enum": sorted(VISA_VALUES)},
                    "contact_email": {"type": "string"},
                },
                "required": ["index", "match_percent", "missing_skills", "employment_type", "visa_status", "contact_email"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["jobs"],
    "additionalProperties": False,
}


class AnalysisError(RuntimeError):
    pass


class ClaudeAnalyzer:
    def __init__(self, profile_md: str, cfg: dict | None = None, client: anthropic.Anthropic | None = None):
        cfg = cfg or {}
        self.profile = profile_md
        self.model = cfg.get("model", "claude-haiku-4-5")
        self.max_tokens = int(cfg.get("max_tokens", 16000))
        self.description_chars = int(cfg.get("description_chars", 2500))
        self.max_jobs_per_call = int(cfg.get("max_jobs_per_call", 150))
        self.client = client or anthropic.Anthropic(max_retries=3, timeout=180.0)

    # --------------------------------------------------------------- prompt
    def _payload(self, jobs: list[Job], offset: int = 0) -> list[dict]:
        out = []
        for i, j in enumerate(jobs):
            out.append(
                {
                    "index": offset + i,
                    "source": j.source,
                    "title": j.title,
                    "company": j.company,
                    "location": j.location,
                    "rate": j.rate,
                    "employment_hint": j.employment_hint,
                    "visa_hint": j.visa_hint,
                    "contact_email_candidate": j.contact_email,
                    "url": j.url,
                    "description": (j.description or "")[: self.description_chars],
                }
            )
        return out

    def _messages(self, payload: list[dict]) -> list[dict]:
        user = (
            "<candidate_profile>\n" + self.profile.strip() + "\n</candidate_profile>\n\n"
            "<jobs>\n" + json.dumps(payload, ensure_ascii=False) + "\n</jobs>\n\n"
            f"Analyze all {len(payload)} jobs and return the JSON object with a 'jobs' array."
        )
        return [{"role": "user", "content": user}]

    # --------------------------------------------------------------- call
    def _call(self, payload: list[dict]) -> str:
        messages = self._messages(payload)
        kwargs = dict(model=self.model, max_tokens=self.max_tokens, system=SYSTEM_PROMPT, messages=messages)
        try:
            resp = self.client.messages.create(
                **kwargs, output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}}
            )
        except anthropic.BadRequestError as exc:
            # Structured outputs unavailable for this model/account -> plain call, rely on defensive parsing.
            log.warning("structured output rejected (%s); retrying without output_config", exc.message)
            resp = self.client.messages.create(**kwargs)
        if resp.stop_reason == "refusal":
            raise AnalysisError("model refused the request")
        if resp.stop_reason == "max_tokens":
            log.warning("response hit max_tokens; JSON may be truncated")
        text = "".join(b.text for b in resp.content if b.type == "text")
        u = resp.usage
        log.info("model=%s input_tokens=%s output_tokens=%s stop=%s", resp.model, u.input_tokens, u.output_tokens, resp.stop_reason)
        return text

    # --------------------------------------------------------------- parsing
    @staticmethod
    def parse_response(text: str) -> list[dict]:
        """Defensive: strict JSON -> fenced block -> first balanced {...} or [...] -> per-object regex salvage."""
        text = (text or "").strip()
        candidates = [text]
        fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
        if fence:
            candidates.append(fence.group(1).strip())
        for opener, closer in (("{", "}"), ("[", "]")):
            start = text.find(opener)
            end = text.rfind(closer)
            if start != -1 and end > start:
                candidates.append(text[start : end + 1])
        for cand in candidates:
            try:
                data = json.loads(cand)
            except Exception:
                continue
            if isinstance(data, dict):
                data = data.get("jobs") or data.get("results") or data.get("data") or []
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict)]
        # Salvage individual objects (e.g. truncated output)
        salvaged = []
        for m in re.finditer(r"\{[^{}]*\"index\"\s*:\s*\d+[^{}]*\}", text, re.S):
            try:
                salvaged.append(json.loads(m.group(0)))
            except Exception:
                pass
        if salvaged:
            log.warning("salvaged %d partial job objects from malformed JSON", len(salvaged))
            return salvaged
        raise AnalysisError("could not parse any JSON from model response: " + text[:300].replace("\n", " "))

    @staticmethod
    def _coerce(item: dict) -> dict | None:
        try:
            idx = int(item.get("index"))
        except Exception:
            return None
        try:
            mp = int(round(float(str(item.get("match_percent", 0)).rstrip("%"))))
        except Exception:
            mp = 0
        mp = max(0, min(100, mp))
        emp = str(item.get("employment_type", "Unclear")).strip()
        emp_map = {e.lower().replace("-", "").replace(" ", ""): e for e in EMPLOYMENT_VALUES}
        emp = emp_map.get(emp.lower().replace("-", "").replace(" ", ""), "Unclear")
        visa = str(item.get("visa_status", "Unclear")).strip()
        visa_map = {v.lower().replace("-", ""): v for v in VISA_VALUES}
        visa = visa_map.get(visa.lower().replace("-", "").replace(" ", ""), "Unclear")
        ms = item.get("missing_skills", "")
        if isinstance(ms, list):
            ms = ", ".join(str(x) for x in ms)
        return {
            "index": idx,
            "match_percent": mp,
            "missing_skills": str(ms or "").strip(),
            "employment_type": emp,
            "visa_status": visa,
            "contact_email": str(item.get("contact_email") or "").strip().lower(),
        }

    # --------------------------------------------------------------- public
    def analyze(self, jobs: list[Job]) -> list[Job]:
        """Annotate jobs in place. Jobs the model did not return stay match_percent=None."""
        if not jobs:
            return jobs
        chunks = [jobs[i : i + self.max_jobs_per_call] for i in range(0, len(jobs), self.max_jobs_per_call)]
        if len(chunks) > 1:
            log.warning("%d candidates exceed max_jobs_per_call=%d; making %d calls", len(jobs), self.max_jobs_per_call, len(chunks))
        results: dict[int, dict] = {}
        offset = 0
        for chunk in chunks:
            text = self._call(self._payload(chunk, offset))
            for item in self.parse_response(text):
                c = self._coerce(item)
                if c is not None:
                    results[c["index"]] = c
            offset += len(chunk)

        missing = 0
        for i, job in enumerate(jobs):
            r = results.get(i)
            if r is None:
                missing += 1
                continue
            job.match_percent = r["match_percent"]
            job.missing_skills = r["missing_skills"]
            job.employment_type = r["employment_type"]
            job.visa_status = r["visa_status"]
            # Prefer the model's pick, fall back to the source-derived address for C2C jobs.
            if r["contact_email"]:
                job.contact_email = r["contact_email"]
            elif job.employment_type != "C2C":
                job.contact_email = job.contact_email if job.source == "gmail" else ""
            # Hard guard: explicit citizenship-only language always wins.
            if job.visa_status != "Restricted" and visa_restricted_hard(job.description):
                log.info("visa override -> Restricted for %r (explicit exclusion text)", job.title)
                job.visa_status = "Restricted"
        if missing:
            log.warning("%d job(s) missing from model output; they will be dropped as unanalyzed", missing)
        return jobs
