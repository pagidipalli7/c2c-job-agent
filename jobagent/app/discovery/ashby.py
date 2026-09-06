"""Ashby job board API: https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"""
from __future__ import annotations

from .base import CrawlError, RawJob, html_to_text, looks_remote, parse_dt
from .http import get_json, make_client

BASE = "https://api.ashbyhq.com/posting-api/job-board"


class AshbyCrawler:
    ats_type = "ashby"

    async def fetch_jobs(self, company_slug: str) -> list[RawJob]:
        async with make_client() as client:
            data = await get_json(client, f"{BASE}/{company_slug}", params={"includeCompensation": "true"})
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            raise CrawlError(f"ashby schema drift for {company_slug}: no 'jobs' list")
        out: list[RawJob] = []
        for j in jobs:
            if not isinstance(j, dict) or not j.get("id"):
                continue
            title = str(j.get("title") or "").strip()
            if not title:
                continue
            location = str(j.get("location") or "")
            secondary = j.get("secondaryLocations") or []
            if secondary and isinstance(secondary, list):
                location = "; ".join([location] + [str(s.get("location", "")) for s in secondary if isinstance(s, dict)]).strip("; ")
            comp = j.get("compensation") or {}
            comp_summary = comp.get("compensationTierSummary") if isinstance(comp, dict) else None
            desc = html_to_text(j.get("descriptionHtml")) or str(j.get("descriptionPlain") or "")
            if comp_summary:
                desc += f"\nCompensation: {comp_summary}"
            out.append(
                RawJob(
                    ats_type=self.ats_type,
                    company_slug=company_slug,
                    company_name=company_slug.replace("-", " ").title(),
                    external_id=str(j["id"]),
                    title=title,
                    location=location,
                    url=str(j.get("jobUrl") or f"https://jobs.ashbyhq.com/{company_slug}/{j['id']}"),
                    apply_url=str(j.get("applyUrl") or f"https://jobs.ashbyhq.com/{company_slug}/{j['id']}/application"),
                    remote=bool(j.get("isRemote")) or looks_remote(location, title),
                    department=str(j.get("department") or j.get("team") or "") or None,
                    description_text=desc.strip(),
                    posted_at=parse_dt(j.get("publishedAt")),
                    raw={"id": j["id"], "employmentType": j.get("employmentType"), "compensation": comp_summary},
                )
            )
        return out
