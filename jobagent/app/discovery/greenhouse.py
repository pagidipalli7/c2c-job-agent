"""Greenhouse job board API: https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"""
from __future__ import annotations

from .base import CrawlError, RawJob, html_to_text, looks_remote, parse_dt
from .http import get_json, make_client

BASE = "https://boards-api.greenhouse.io/v1/boards"


class GreenhouseCrawler:
    ats_type = "greenhouse"

    async def fetch_jobs(self, company_slug: str) -> list[RawJob]:
        async with make_client() as client:
            data = await get_json(client, f"{BASE}/{company_slug}/jobs", params={"content": "true"})
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            raise CrawlError(f"greenhouse schema drift for {company_slug}: no 'jobs' list")
        out: list[RawJob] = []
        for j in jobs:
            if not isinstance(j, dict) or "id" not in j:
                continue
            location = ((j.get("location") or {}).get("name") or "") if isinstance(j.get("location"), dict) else str(j.get("location") or "")
            depts = j.get("departments") or []
            dept = depts[0].get("name") if depts and isinstance(depts[0], dict) else None
            offices = j.get("offices") or []
            office_names = " ".join(o.get("name", "") for o in offices if isinstance(o, dict))
            title = str(j.get("title") or "").strip()
            if not title:
                continue
            content = html_to_text(j.get("content"))
            out.append(
                RawJob(
                    ats_type=self.ats_type,
                    company_slug=company_slug,
                    company_name=str(j.get("company_name") or company_slug),
                    external_id=str(j["id"]),
                    title=title,
                    location=location,
                    url=str(j.get("absolute_url") or f"https://boards.greenhouse.io/{company_slug}/jobs/{j['id']}"),
                    apply_url=str(j.get("absolute_url") or ""),
                    remote=looks_remote(location, office_names, title),
                    department=dept,
                    description_text=content,
                    posted_at=parse_dt(j.get("first_published") or j.get("updated_at")),
                    raw={"id": j["id"], "internal_job_id": j.get("internal_job_id"), "requisition_id": j.get("requisition_id")},
                )
            )
        return out
