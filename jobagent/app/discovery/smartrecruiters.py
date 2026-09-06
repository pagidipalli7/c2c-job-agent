"""SmartRecruiters postings API: https://api.smartrecruiters.com/v1/companies/{slug}/postings (paged, 100/page)."""
from __future__ import annotations

from .base import CrawlError, RawJob, html_to_text, looks_remote, parse_dt
from .http import get_json, make_client

BASE = "https://api.smartrecruiters.com/v1/companies"


class SmartRecruitersCrawler:
    ats_type = "smartrecruiters"
    fetch_details = True  # each posting's description needs a second call; cap it
    max_detail_fetches = 60

    async def fetch_jobs(self, company_slug: str) -> list[RawJob]:
        out: list[RawJob] = []
        async with make_client() as client:
            offset = 0
            content: list[dict] = []
            while True:
                data = await get_json(client, f"{BASE}/{company_slug}/postings", params={"limit": 100, "offset": offset})
                page = data.get("content") if isinstance(data, dict) else None
                if not isinstance(page, list):
                    raise CrawlError(f"smartrecruiters schema drift for {company_slug}: no 'content' list")
                content.extend(p for p in page if isinstance(p, dict))
                total = int(data.get("totalFound") or 0)
                offset += len(page)
                if not page or offset >= total or offset >= 1000:
                    break
            details = 0
            for p in content:
                pid = p.get("id")
                if not pid or not p.get("name"):
                    continue
                loc = p.get("location") or {}
                location = ", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country")) if x) if isinstance(loc, dict) else ""
                remote = bool(loc.get("remote")) if isinstance(loc, dict) else False
                desc = ""
                if self.fetch_details and details < self.max_detail_fetches:
                    try:
                        d = await get_json(client, f"{BASE}/{company_slug}/postings/{pid}")
                        sections = (d.get("jobAd") or {}).get("sections") or {}
                        desc = "\n".join(html_to_text((sections.get(k) or {}).get("text")) for k in ("jobDescription", "qualifications", "additionalInformation"))
                        details += 1
                    except Exception:  # detail failures must not kill the board
                        desc = ""
                dept = p.get("department") or {}
                out.append(
                    RawJob(
                        ats_type=self.ats_type,
                        company_slug=company_slug,
                        company_name=str((p.get("company") or {}).get("name") or company_slug),
                        external_id=str(pid),
                        title=str(p["name"]).strip(),
                        location=location,
                        url=f"https://jobs.smartrecruiters.com/{company_slug}/{pid}",
                        apply_url=f"https://jobs.smartrecruiters.com/{company_slug}/{pid}",
                        remote=remote or looks_remote(location, str(p["name"])),
                        department=str(dept.get("label") or "") or None if isinstance(dept, dict) else None,
                        description_text=desc.strip(),
                        posted_at=parse_dt(p.get("releasedDate")),
                        raw={"id": pid, "ref": p.get("refNumber"), "typeOfEmployment": (p.get("typeOfEmployment") or {}).get("label") if isinstance(p.get("typeOfEmployment"), dict) else None},
                    )
                )
        return out
