"""Anti-fabrication validator. NON-NEGOTIABLE product rule: a tailored resume may rephrase, reorder and
emphasise, but must never add or change employers, titles, dates, degrees, schools, certifications, or skills.

`validate(base, tailored)` returns a list of violations (empty == accepted).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_WS = re.compile(r"\s+")


def _norm(s) -> str:
    return _WS.sub(" ", str(s or "")).strip().lower()


def _date(s) -> str:
    t = _norm(s)
    return "present" if t in ("", "present", "current", "now", "none") else t


@dataclass(frozen=True)
class Violation:
    section: str
    kind: str  # added | removed | changed | extra_field
    detail: str

    def __str__(self) -> str:
        return f"{self.section}: {self.kind} - {self.detail}"


def _job_key(w: dict) -> tuple:
    return (_norm(w.get("company")), _norm(w.get("title")), _date(w.get("start")), _date(w.get("end")))


def _edu_key(e: dict) -> tuple:
    return (_norm(e.get("school")), _norm(e.get("degree")), _norm(e.get("field")), _date(e.get("start")), _date(e.get("end")))


def _cert_key(c: dict) -> tuple:
    if isinstance(c, str):
        return (_norm(c), "", "")
    return (_norm(c.get("name")), _norm(c.get("issuer")), _norm(c.get("year")))


def validate(base: dict, tailored: dict) -> list[Violation]:
    v: list[Violation] = []
    if not isinstance(tailored, dict):
        return [Violation("root", "changed", "tailored resume is not an object")]

    # --- identity -------------------------------------------------------
    if _norm(tailored.get("name", base.get("name"))) != _norm(base.get("name")):
        v.append(Violation("name", "changed", f"{base.get('name')!r} -> {tailored.get('name')!r}"))

    # --- work history: exact multiset of (company, title, start, end) ----
    base_jobs = [w for w in base.get("work_history", []) if isinstance(w, dict)]
    tail_jobs = [w for w in tailored.get("work_history", []) if isinstance(w, dict)]
    base_keys = {_job_key(w): w for w in base_jobs}
    tail_keys = {_job_key(w): w for w in tail_jobs}
    if len(tail_jobs) != len(tail_keys):
        v.append(Violation("work_history", "added", "duplicate work history entries"))
    for k in tail_keys.keys() - base_keys.keys():
        # try to explain what changed relative to the closest base entry (same company)
        same_company = [bk for bk in base_keys if bk[0] == k[0]]
        if same_company:
            bk = same_company[0]
            what = "title" if bk[1] != k[1] else "dates"
            v.append(Violation("work_history", "changed", f"{what} changed for {k[0]!r}: {bk[1:]!r} -> {k[1:]!r}"))
        else:
            v.append(Violation("work_history", "added", f"new employer {k[0]!r} / {k[1]!r}"))
    for k in base_keys.keys() - tail_keys.keys():
        if not any(tk[0] == k[0] for tk in tail_keys):
            v.append(Violation("work_history", "removed", f"employer {k[0]!r} dropped (omitting history changes the record)"))
    for k, tw in tail_keys.items():
        bw = base_keys.get(k)
        if bw is None:
            continue
        if _norm(tw.get("location")) not in ("", _norm(bw.get("location"))):
            v.append(Violation("work_history", "changed", f"location changed for {k[0]!r}"))
        tb, bb = tw.get("bullets") or [], bw.get("bullets") or []
        if len(tb) > len(bb) + 1:
            v.append(Violation("work_history", "added", f"{k[0]!r}: {len(tb)} bullets vs {len(bb)} in base (invented accomplishments)"))
        _check_numbers(v, k[0], bb, tb)

    # --- education ------------------------------------------------------
    base_edu = {_edu_key(e) for e in base.get("education", []) if isinstance(e, dict)}
    tail_edu = {_edu_key(e) for e in tailored.get("education", []) if isinstance(e, dict)}
    for k in tail_edu - base_edu:
        v.append(Violation("education", "added" if not any(b[0] == k[0] for b in base_edu) else "changed", f"{k!r}"))
    for k in base_edu - tail_edu:
        if not any(t[0] == k[0] for t in tail_edu):
            v.append(Violation("education", "removed", f"{k[0]!r}"))

    # --- certifications --------------------------------------------------
    base_cert = {_cert_key(c)[0] for c in base.get("certifications", [])}
    tail_cert = {_cert_key(c)[0] for c in tailored.get("certifications", [])}
    for c in tail_cert - base_cert:
        v.append(Violation("certifications", "added", f"{c!r}"))
    for c in tailored.get("certifications", []):
        bk = _cert_key(c)
        match = next((_cert_key(b) for b in base.get("certifications", []) if _cert_key(b)[0] == bk[0]), None)
        if match and bk[2] and match[2] and bk[2] != match[2]:
            v.append(Violation("certifications", "changed", f"year changed for {bk[0]!r}"))

    # --- skills: subset of base (reorder/drop ok, never add) ------------
    base_skills = {_norm(s) for s in base.get("skills", [])}
    for s in tailored.get("skills", []):
        if _norm(s) not in base_skills:
            v.append(Violation("skills", "added", f"{s!r} not in base resume"))

    # --- structural: no unknown top-level sections ----------------------
    allowed = set(base.keys()) | {"summary", "headline", "skills", "work_history", "education", "certifications", "contact", "name"}
    for k in tailored.keys() - allowed:
        v.append(Violation(k, "extra_field", "unexpected top-level section"))
    return v


_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:%|k|m|\+)?", re.I)


def _check_numbers(v: list[Violation], company: str, base_bullets: list[str], tail_bullets: list[str]) -> None:
    """Numbers in tailored bullets must already appear somewhere in the base bullets (no inflated metrics)."""
    base_nums = {n.replace(",", "").replace(" ", "").lower() for b in base_bullets for n in _NUM.findall(str(b))}
    for b in tail_bullets:
        for n in _NUM.findall(str(b)):
            key = n.replace(",", "").replace(" ", "").lower()
            if key not in base_nums and key.rstrip("+%km") not in {x.rstrip("+%km") for x in base_nums}:
                v.append(Violation("work_history", "added", f"{company!r}: metric {n.strip()!r} not present in base bullets"))
                return


def is_valid(base: dict, tailored: dict) -> bool:
    return not validate(base, tailored)
