"""Keyless remote-job JSON feeds: Remotive, RemoteOK, Jobicy. Strict title/tag matching because
their own search is fuzzy (a 'power platform' query returns copywriters)."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup

from agent.models import Job
from agent.textsig import employment_hint, find_rate, visa_hint, years_required_hint

from .base import Source

_US_OK = re.compile(r"\b(usa|u\.s\.|united states|us only|americas|north america|worldwide|anywhere|remote)\b", re.I)


def _text(html_: str) -> str:
    return BeautifulSoup(html_ or "", "html.parser").get_text("\n", strip=True)


class RemoteBoardsSource(Source):
    name = "remoteboards"

    def __init__(self, cfg, http):
        super().__init__(cfg, http)
        r = cfg.get("remoteboards", {}) or {}
        self.terms = [t.lower() for t in (r.get("title_terms") or [k.lower() for k in self.keywords])]
        self.us_only = bool(r.get("us_locations_only", True))
        self.since = datetime.now(timezone.utc) - timedelta(days=2)

    def _match(self, *fields: str) -> bool:
        blob = " ".join(f or "" for f in fields).lower()
        return any(t in blob for t in self.terms)

    def _loc_ok(self, loc: str) -> bool:
        return (not self.us_only) or (not loc) or bool(_US_OK.search(loc))

    def _mk(self, title, company, location, desc, url, rate, posted, board) -> Job:
        blob = f"{title}\n{desc}"
        return Job(
            title=title, company=company, location=location or "Remote", rate=rate or find_rate(blob),
            description=desc, url=url, source=self.name,
            employment_hint=employment_hint(blob), visa_hint=visa_hint(blob),
            years_hint=years_required_hint(desc), posted_at=posted, extra={"board": board},
        ).clean()

    def _remotive(self) -> list[Job]:
        out = []
        for term in sorted({t for t in self.terms if " " in t or len(t) > 6}):
            data = self.http.get_json("https://remotive.com/api/remote-jobs", params={"search": term, "limit": "50"})
            for j in data.get("jobs", []):
                tags = " ".join(j.get("tags") or [])
                if not self._match(j.get("title", ""), tags):
                    continue
                if not self._loc_ok(j.get("candidate_required_location", "")):
                    continue
                out.append(self._mk(j.get("title", ""), j.get("company_name", ""),
                                    f"Remote ({j.get('candidate_required_location','')})", _text(j.get("description", "")),
                                    j.get("url", ""), j.get("salary", ""), j.get("publication_date", ""), "remotive"))
        return out

    def _remoteok(self) -> list[Job]:
        out = []
        data = self.http.get_json("https://remoteok.com/api")
        for j in data if isinstance(data, list) else []:
            if not isinstance(j, dict) or not j.get("id"):
                continue
            if not self._match(j.get("position", ""), " ".join(j.get("tags") or [])):
                continue
            if not self._loc_ok(j.get("location", "")):
                continue
            sal = ""
            if j.get("salary_min") or j.get("salary_max"):
                sal = f"${int(j.get('salary_min') or 0):,} - ${int(j.get('salary_max') or 0):,}/yr"
            out.append(self._mk(j.get("position", ""), j.get("company", ""), f"Remote ({j.get('location','')})".replace(" ()", ""),
                                _text(j.get("description", "")), j.get("url", ""), sal, j.get("date", ""), "remoteok"))
        return out

    def _jobicy(self) -> list[Job]:
        out = []
        data = self.http.get_json("https://jobicy.com/api/v2/remote-jobs", params={"count": "100", "geo": "usa"})
        for j in data.get("jobs", []):
            if not self._match(j.get("jobTitle", ""), " ".join(j.get("jobIndustry") or [])):
                continue
            sal = ""
            if j.get("salaryMin") or j.get("salaryMax"):
                sal = f"${int(j.get('salaryMin') or 0):,} - ${int(j.get('salaryMax') or 0):,}/{j.get('salaryPeriod','yr')}"
            out.append(self._mk(j.get("jobTitle", ""), j.get("companyName", ""), f"Remote ({j.get('jobGeo','')})",
                                _text(j.get("jobDescription", "")), j.get("url", ""), sal, j.get("pubDate", ""), "jobicy"))
        return out

    def fetch(self) -> list[Job]:
        jobs: list[Job] = []
        for name, fn in (("remotive", self._remotive), ("remoteok", self._remoteok), ("jobicy", self._jobicy)):
            try:
                got = fn()
                self.log.info("board=%s matches=%d", name, len(got))
                jobs.extend(got)
            except Exception as exc:  # one board failing must not sink the others
                self.log.warning("board=%s failed: %s", name, exc)
        return jobs
