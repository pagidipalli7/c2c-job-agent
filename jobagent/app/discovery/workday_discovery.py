"""Workday has no public board API, but every tenant exposes the same JSON search endpoint the
careers UI uses: POST https://{tenant}.{wdN}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
Job detail: GET .../wday/cxs/{tenant}/{site}/{externalPath}. This is discovery only; submission is Playwright.
"""
from __future__ import annotations

import re
from datetime import timedelta

from app.db.base import utcnow

from .base import CrawlError, RawJob, html_to_text, looks_remote
from .detect import detect_ats
from .http import get_json, make_client


def _days_ago(posted_on: str | None) -> int | None:
    s = (posted_on or "").lower()
    if "today" in s:
        return 0
    if "yesterday" in s:
        return 1
    m = re.search(r"(\d+)\+?\s*days?", s)
    return int(m.group(1)) if m else None


class WorkdayDiscovery:
    ats_type = "workday"
    max_detail_fetches = 40
    page_size = 20
    max_pages = 5

    async def fetch_jobs(self, company_slug: str, careers_url: str | None = None, company_name: str | None = None, search_text: str = "") -> list[RawJob]:
        if not careers_url:
            raise CrawlError(f"workday tenant {company_slug} needs careers_url in companies.yaml")
        d = detect_ats(careers_url)
        base = f"https://{d.tenant_host}/wday/cxs/{d.slug}/{d.site}"
        out: list[RawJob] = []
        async with make_client() as client:
            details = 0
            for page in range(self.max_pages):
                body = {"appliedFacets": {}, "limit": self.page_size, "offset": page * self.page_size, "searchText": search_text}
                data = await get_json(client, f"{base}/jobs", method="POST", json_body=body)
                postings = data.get("jobPostings") if isinstance(data, dict) else None
                if not isinstance(postings, list):
                    raise CrawlError(f"workday schema drift for {company_slug}")
                for p in postings:
                    if not isinstance(p, dict) or not p.get("externalPath") or not p.get("title"):
                        continue
                    ext = p["externalPath"]
                    desc, location = "", str(p.get("locationsText") or "")
                    if details < self.max_detail_fetches:
                        try:
                            dj = await get_json(client, f"{base}{ext}")
                            info = dj.get("jobPostingInfo") or {}
                            desc = html_to_text(info.get("jobDescription"))
                            location = str(info.get("location") or location)
                            details += 1
                        except Exception:
                            pass
                    days = _days_ago(p.get("postedOn"))
                    out.append(
                        RawJob(
                            ats_type="workday",
                            company_slug=company_slug,
                            company_name=company_name or company_slug,
                            external_id=str(p.get("bulletFields", [ext])[0] if p.get("bulletFields") else ext),
                            title=str(p["title"]).strip(),
                            location=location,
                            url=f"https://{d.tenant_host}/{d.site}{ext}",
                            apply_url=f"https://{d.tenant_host}/{d.site}{ext}/apply",
                            remote=looks_remote(location, str(p["title"])),
                            description_text=desc,
                            posted_at=(utcnow() - timedelta(days=days)) if days is not None else None,
                            raw={"externalPath": ext, "postedOn": p.get("postedOn"), "tenant_host": d.tenant_host, "site": d.site},
                        )
                    )
                if len(postings) < self.page_size:
                    break
        return out
