"""SerpAPI Google Jobs — optional; skipped gracefully when SERPAPI_KEY is missing."""
from __future__ import annotations

import os

from agent.models import Job
from agent.textsig import employment_hint, find_rate, visa_hint, years_required_hint

from .base import Source

SERP_URL = "https://serpapi.com/search.json"


class SerpApiSource(Source):
    name = "serpapi"

    def __init__(self, cfg, http):
        super().__init__(cfg, http)
        s = cfg.get("serpapi", {}) or {}
        self.api_key = os.environ.get("SERPAPI_KEY", "")
        self.engine = s.get("engine", "google_jobs")
        self.location = s.get("location", "United States")
        self.remote_only = bool(s.get("remote_only", True))
        self.max_per_keyword = int(s.get("max_per_keyword", 20))

    def fetch(self) -> list[Job]:
        if not self.api_key:
            self.log.info("SERPAPI_KEY not set - skipping")
            return []
        seen: set[str] = set()
        jobs: list[Job] = []
        for kw in self.keywords:
            params = {
                "engine": self.engine,
                "q": f"{kw} contract",
                "hl": "en",
                "gl": "us",
                "location": self.location,
                "api_key": self.api_key,
            }
            if self.remote_only:
                params["ltype"] = "1"
            try:
                data = self.http.get_json(SERP_URL, params=params)
            except Exception as exc:
                self.log.warning("query failed for %r: %s", kw, exc)
                continue
            if data.get("error"):
                self.log.warning("serpapi error for %r: %s", kw, data["error"])
                continue
            results = (data.get("jobs_results") or [])[: self.max_per_keyword]
            self.log.info("keyword=%r results=%d", kw, len(results))
            for r in results:
                url = r.get("share_link") or ""
                if not url:
                    for opt in r.get("apply_options") or []:
                        if opt.get("link"):
                            url = opt["link"]
                            break
                key = url or (r.get("job_id") or f"{r.get('company_name')}|{r.get('title')}|{r.get('location')}")
                if key in seen:
                    continue
                seen.add(key)
                ext = r.get("detected_extensions") or {}
                desc = r.get("description") or ""
                blob = f"{r.get('title','')}\n{ext.get('schedule_type','')}\n{desc}"
                highlights = []
                for h in r.get("job_highlights") or []:
                    items = h.get("items") or []
                    if items:
                        highlights.append(f"{h.get('title','')}: " + " | ".join(items))
                jobs.append(
                    Job(
                        title=r.get("title", ""),
                        company=r.get("company_name", ""),
                        location=r.get("location", "") or ("Remote" if ext.get("work_from_home") else ""),
                        rate=ext.get("salary") or find_rate(blob),
                        description=(f"Schedule type: {ext.get('schedule_type','')}\nVia: {r.get('via','')}\n\n"
                                     + desc + ("\n\n" + "\n".join(highlights) if highlights else "")),
                        url=url,
                        source=self.name,
                        employment_hint=employment_hint(blob) or (
                            "Contract (type unclear)" if "contract" in str(ext.get("schedule_type", "")).lower() else ""),
                        visa_hint=visa_hint(blob),
                        years_hint=years_required_hint(desc),
                        posted_at=str(ext.get("posted_at") or ""),
                        extra={"google_job_id": r.get("job_id", ""), "via": r.get("via", "")},
                    ).clean()
                )
        return jobs
