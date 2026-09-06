"""Lever postings API: https://api.lever.co/v0/postings/{slug}?mode=json"""
from __future__ import annotations

from .base import CrawlError, RawJob, html_to_text, looks_remote, parse_dt
from .http import get_json, make_client

BASE = "https://api.lever.co/v0/postings"


class LeverCrawler:
    ats_type = "lever"

    async def fetch_jobs(self, company_slug: str) -> list[RawJob]:
        async with make_client() as client:
            data = await get_json(client, f"{BASE}/{company_slug}", params={"mode": "json"})
        if not isinstance(data, list):
            raise CrawlError(f"lever schema drift for {company_slug}: expected list")
        out: list[RawJob] = []
        for p in data:
            if not isinstance(p, dict) or not p.get("id"):
                continue
            cats = p.get("categories") or {}
            location = str(cats.get("location") or "")
            workplace = str(p.get("workplaceType") or "")
            title = str(p.get("text") or "").strip()
            if not title:
                continue
            desc = html_to_text(p.get("descriptionPlain") or p.get("description"))
            for lst in p.get("lists") or []:
                if isinstance(lst, dict):
                    desc += f"\n{lst.get('text','')}\n{html_to_text(lst.get('content'))}"
            out.append(
                RawJob(
                    ats_type=self.ats_type,
                    company_slug=company_slug,
                    company_name=company_slug.replace("-", " ").title(),
                    external_id=str(p["id"]),
                    title=title,
                    location=location,
                    url=str(p.get("hostedUrl") or f"https://jobs.lever.co/{company_slug}/{p['id']}"),
                    apply_url=str(p.get("applyUrl") or f"https://jobs.lever.co/{company_slug}/{p['id']}/apply"),
                    remote=workplace.lower() == "remote" or looks_remote(location, title),
                    department=str(cats.get("team") or cats.get("department") or "") or None,
                    description_text=desc.strip(),
                    posted_at=parse_dt(p.get("createdAt")),
                    raw={"id": p["id"], "workplaceType": workplace, "commitment": cats.get("commitment")},
                )
            )
        return out
