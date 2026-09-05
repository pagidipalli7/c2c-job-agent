"""Company career pages via their applicant-tracking-system public JSON APIs.

Adapters (one per ATS; companies are listed in config.yaml -> careers):
  * workday     POST {tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs  (+ detail GET)
  * greenhouse  GET  boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
  * lever       GET  api.lever.co/v0/postings/{company}?mode=json
  * amazon      GET  amazon.jobs/en/search.json
Every adapter is fail-soft per company; a title/description must contain one of careers.title_terms.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup
from dateutil import parser as dtparse

from agent.models import Job
from agent.textsig import employment_hint, find_rate, visa_hint, years_required_hint

from .base import Source

WORKDAY_US_COUNTRY_ID = "bc33aa3152ec42d4995f4791a106ed09"
_US_LOC = re.compile(r"\b(US|USA|U\.S\.|United States|Remote)\b|,\s*[A-Z]{2}$", re.I)
_STATE = re.compile(r",\s*(A[LKZR]|C[AOT]|D[EC]|FL|GA|HI|I[DLNA]|K[SY]|LA|M[EDAINSOT]|N[EVHJMYC]|O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[TA]|W[AVIY])\b")


def _text(markup: str) -> str:
    return BeautifulSoup(html.unescape(markup or ""), "html.parser").get_text("\n", strip=True)


def _workday_days(posted_on: str) -> int | None:
    """'Posted Today' -> 0, 'Posted Yesterday' -> 1, 'Posted 6 Days Ago' -> 6, 'Posted 30+ Days Ago' -> 30."""
    s = (posted_on or "").lower()
    if "today" in s:
        return 0
    if "yesterday" in s:
        return 1
    m = re.search(r"(\d+)\+?\s*days?", s)
    return int(m.group(1)) if m else None


class CareersSource(Source):
    name = "careers"

    def __init__(self, cfg, http):
        super().__init__(cfg, http)
        c = cfg.get("careers", {}) or {}
        self.queries = list(c.get("queries") or ["Power Platform", "Power Apps", "Power Automate", "Power BI", "Dynamics 365"])
        self.terms = [t.lower() for t in (c.get("title_terms") or [])]
        self.max_days_old = int(c.get("max_days_old", 1))
        self.us_only = bool(c.get("us_only", True))
        self.max_detail_fetches = int(c.get("max_detail_fetches", 40))
        self.workday = list(c.get("workday") or [])
        self.greenhouse = list(c.get("greenhouse") or [])
        self.lever = list(c.get("lever") or [])
        self.amazon = bool(c.get("amazon", True))
        self.since = datetime.now(timezone.utc) - timedelta(days=self.max_days_old, hours=6)

    # ------------------------------------------------------------------ helpers
    def _match(self, *fields: str) -> bool:
        blob = " ".join(f or "" for f in fields).lower()
        return any(t in blob for t in self.terms) if self.terms else True

    def _recent(self, when: str | None) -> bool:
        if not when:
            return True  # unknown date -> keep, dedupe protects us
        try:
            dt = dtparse.parse(str(when))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt >= self.since
        except Exception:
            return True

    def _us(self, *locs: str) -> bool:
        if not self.us_only:
            return True
        blob = " ".join(l or "" for l in locs)
        return not blob or bool(_US_LOC.search(blob) or _STATE.search(blob))

    def _job(self, title, company, location, desc, url, posted, extra) -> Job:
        blob = f"{title}\n{desc}"
        return Job(
            title=title, company=company, location=location, rate=find_rate(blob), description=desc, url=url,
            source=self.name, employment_hint=employment_hint(blob) or "Full-time (career page)",
            visa_hint=visa_hint(blob), years_hint=years_required_hint(desc), posted_at=str(posted or ""), extra=extra,
        ).clean()

    # ------------------------------------------------------------------ workday
    def _workday_company(self, entry: dict, detail_budget: list[int]) -> list[Job]:
        name, tenant, wd, site = entry["name"], entry["tenant"], entry.get("wd", "wd5"), entry["site"]
        base = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
        hdrs = {"Accept": "application/json", "Content-Type": "application/json"}
        # The US "locationCountry" facet id is tenant-specific; try the common one, fall back to no facet.
        use_facet = self.us_only
        seen, out = set(), []
        for q in self.queries:
            body = {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": q}
            if use_facet:
                body["appliedFacets"] = {"locationCountry": [WORKDAY_US_COUNTRY_ID]}
            resp = self.http.session.post(base + "/jobs", json=body, timeout=self.http.timeout, headers=hdrs)
            if resp.status_code == 400 and use_facet:
                use_facet = False
                body["appliedFacets"] = {}
                resp = self.http.session.post(base + "/jobs", json=body, timeout=self.http.timeout, headers=hdrs)
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code} for {name}")
            for jp in resp.json().get("jobPostings", []):
                path = jp.get("externalPath") or ""
                if not path or path in seen:
                    continue
                days = _workday_days(jp.get("postedOn", ""))
                if days is not None and days > self.max_days_old:
                    continue
                if not self._match(jp.get("title", ""), " ".join(jp.get("bulletFields") or [])):
                    continue
                seen.add(path)
                desc, loc, url, time_type, country = "", jp.get("locationsText") or "", "", "", ""
                if detail_budget[0] > 0:
                    try:
                        d = self.http.get(base + path, headers={"Accept": "application/json"})
                        detail_budget[0] -= 1
                        info = d.json().get("jobPostingInfo", {}) if d.status_code == 200 else {}
                        desc = _text(info.get("jobDescription", ""))
                        loc = info.get("location") or loc
                        url = info.get("externalUrl") or ""
                        time_type = info.get("timeType") or ""
                        country = (info.get("country") or {}).get("descriptor", "") if isinstance(info.get("country"), dict) else str(info.get("country") or "")
                    except Exception as exc:
                        self.log.debug("workday detail failed %s: %s", path, exc)
                # Always enforce US-only from the posting itself (facets are unreliable across tenants).
                if self.us_only:
                    if country and "united states" not in country.lower():
                        continue
                    if not country and not self._us(loc):
                        continue
                if not url:
                    url = f"https://{tenant}.{wd}.myworkdayjobs.com/{site}{path}"
                out.append(self._job(jp.get("title", ""), name, loc,
                                     (f"Time type: {time_type}\n\n" if time_type else "") + desc, url,
                                     jp.get("postedOn", ""), {"ats": "workday", "posted_on": jp.get("postedOn", ""), "country": country}))
        return out

    # ------------------------------------------------------------------ greenhouse
    def _greenhouse_company(self, entry: dict) -> list[Job]:
        name, token = entry["name"], entry["token"]
        data = self.http.get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs", params={"content": "true"})
        out = []
        for j in data.get("jobs", []):
            content = _text(j.get("content", ""))
            if not self._match(j.get("title", ""), content[:600]):
                continue
            if not self._recent(j.get("first_published") or j.get("updated_at")):
                continue
            loc = (j.get("location") or {}).get("name", "")
            if not self._us(loc):
                continue
            out.append(self._job(j.get("title", ""), name, loc, content, j.get("absolute_url", ""),
                                 j.get("first_published") or j.get("updated_at"), {"ats": "greenhouse", "gh_id": j.get("id")}))
        return out

    # ------------------------------------------------------------------ lever
    def _lever_company(self, entry: dict) -> list[Job]:
        name, token = entry["name"], entry["token"]
        data = self.http.get_json(f"https://api.lever.co/v0/postings/{token}", params={"mode": "json"})
        out = []
        for p in data if isinstance(data, list) else []:
            cats = p.get("categories") or {}
            desc = p.get("descriptionPlain") or _text(p.get("description", ""))
            if not self._match(p.get("text", ""), desc[:600]):
                continue
            created = p.get("createdAt")
            if created and not self._recent(datetime.fromtimestamp(int(created) / 1000, tz=timezone.utc).isoformat()):
                continue
            loc = cats.get("location", "") or ", ".join(cats.get("allLocations") or [])
            if (p.get("country") or "").upper() not in ("", "US") and self.us_only:
                continue
            if not self._us(loc, p.get("country", "")):
                continue
            commitment = cats.get("commitment", "")
            out.append(self._job(p.get("text", ""), name, f"{loc} ({p.get('workplaceType','')})".replace(" ()", ""),
                                 (f"Commitment: {commitment}\n\n" if commitment else "") + desc, p.get("hostedUrl", ""),
                                 datetime.fromtimestamp(int(created) / 1000, tz=timezone.utc).isoformat() if created else "",
                                 {"ats": "lever", "lever_id": p.get("id")}))
        return out

    # ------------------------------------------------------------------ amazon
    def _amazon(self) -> list[Job]:
        seen, out = set(), []
        for q in self.queries:
            data = self.http.get_json("https://www.amazon.jobs/en/search.json",
                                      params={"base_query": q, "country": "USA", "result_limit": "50", "sort": "recent", "offset": "0"})
            for j in data.get("jobs", []):
                path = j.get("job_path") or ""
                if not path or path in seen:
                    continue
                desc = _text(j.get("description", "")) + "\n\nBasic qualifications:\n" + _text(j.get("basic_qualifications", "")) \
                    + "\n\nPreferred qualifications:\n" + _text(j.get("preferred_qualifications", ""))
                if not self._match(j.get("title", ""), desc[:800]):
                    continue
                if not self._recent(j.get("posted_date") or j.get("updated_time")):
                    continue
                seen.add(path)
                out.append(self._job(j.get("title", ""), j.get("company_name") or "Amazon", j.get("location", ""),
                                     f"Schedule: {j.get('job_schedule_type','')}\n\n{desc}", "https://www.amazon.jobs" + path,
                                     j.get("posted_date", ""), {"ats": "amazon", "amazon_id": j.get("id_icims", "")}))
        return out

    # ------------------------------------------------------------------ fetch
    def fetch(self) -> list[Job]:
        jobs: list[Job] = []
        budget = [self.max_detail_fetches]

        def run(label: str, fn, *args):
            try:
                got = fn(*args)
                self.log.info("company=%s matches=%d", label, len(got))
                jobs.extend(got)
            except Exception as exc:  # one company failing must not sink the source
                self.log.warning("company=%s failed: %s", label, exc)

        for e in self.workday:
            run(e["name"], self._workday_company, e, budget)
        for e in self.greenhouse:
            run(e["name"], self._greenhouse_company, e)
        for e in self.lever:
            run(e["name"], self._lever_company, e)
        if self.amazon:
            run("Amazon", self._amazon)
        return jobs
