"""Adzuna — official free API (https://developer.adzuna.com). Skipped when keys are missing."""
from __future__ import annotations

import os

from agent.models import Job
from agent.textsig import employment_hint, find_rate, visa_hint, years_required_hint

from .base import Source


class AdzunaSource(Source):
    name = "adzuna"

    def __init__(self, cfg, http):
        super().__init__(cfg, http)
        a = cfg.get("adzuna", {}) or {}
        self.app_id = os.environ.get("ADZUNA_APP_ID", "")
        self.app_key = os.environ.get("ADZUNA_APP_KEY", "")
        self.country = a.get("country", "us")
        self.per_page = int(a.get("results_per_page", 50))
        self.max_days_old = int(a.get("max_days_old", 1))
        self.contract_only = bool(a.get("contract_only", True))

    def fetch(self) -> list[Job]:
        if not self.app_id or not self.app_key:
            self.log.info("ADZUNA_APP_ID / ADZUNA_APP_KEY not set - skipping")
            return []
        url = f"https://api.adzuna.com/v1/api/jobs/{self.country}/search/1"
        seen: set[str] = set()
        jobs: list[Job] = []
        for kw in self.keywords:
            params = {
                "app_id": self.app_id,
                "app_key": self.app_key,
                "what": kw,
                "results_per_page": str(self.per_page),
                "max_days_old": str(self.max_days_old),
                "content-type": "application/json",
            }
            if self.contract_only:
                params["contract_type"] = "contract"
            try:
                data = self.http.get_json(url, params=params)
            except Exception as exc:
                self.log.warning("query failed for %r: %s", kw, exc)
                continue
            results = data.get("results") or []
            self.log.info("keyword=%r results=%d", kw, len(results))
            for r in results:
                url_ = r.get("redirect_url") or ""
                key = str(r.get("id") or url_)
                if not key or key in seen:
                    continue
                seen.add(key)
                desc = r.get("description") or ""
                sal = ""
                if r.get("salary_min") or r.get("salary_max"):
                    sal = f"${int(r.get('salary_min') or 0):,} - ${int(r.get('salary_max') or 0):,}/yr"
                ct = " ".join(x for x in [r.get("contract_type", ""), r.get("contract_time", "")] if x)
                blob = f"{r.get('title','')}\n{ct}\n{desc}"
                jobs.append(
                    Job(
                        title=r.get("title", ""),
                        company=(r.get("company") or {}).get("display_name", ""),
                        location=(r.get("location") or {}).get("display_name", ""),
                        rate=sal or find_rate(blob),
                        description=f"Contract: {ct}\n\n{desc}",
                        url=url_,
                        source=self.name,
                        employment_hint=employment_hint(blob) or ("Contract (type unclear)" if "contract" in ct else ""),
                        visa_hint=visa_hint(blob),
                        years_hint=years_required_hint(desc),
                        posted_at=r.get("created", ""),
                        extra={"adzuna_id": r.get("id", "")},
                    ).clean()
                )
        return jobs
