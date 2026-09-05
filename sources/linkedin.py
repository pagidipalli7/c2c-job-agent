"""LinkedIn — public guest job search (no login) + guest job-posting detail endpoint."""
from __future__ import annotations

import re
import time

from bs4 import BeautifulSoup

from agent.models import Job
from agent.textsig import employment_hint, find_rate, visa_hint, years_required_hint

from .base import Source

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
_URN = re.compile(r"(\d{6,})")


def parse_search_cards(page_html: str) -> list[dict]:
    """Parse the guest search fragment into dicts: id, title, company, location, posted, url."""
    soup = BeautifulSoup(page_html, "html.parser")
    out = []
    for li in soup.select("li"):
        ent = li.select_one("[data-entity-urn]")
        a = li.select_one("a.base-card__full-link, a[href*='/jobs/view/']")
        title = li.select_one("h3")
        if not (title and (ent or a)):
            continue
        jid = ""
        if ent:
            m = _URN.search(ent.get("data-entity-urn", ""))
            jid = m.group(1) if m else ""
        if not jid and a:
            m = re.search(r"-(\d{6,})\?|/view/(\d{6,})", a.get("href", ""))
            jid = next((g for g in (m.groups() if m else ()) if g), "")
        if not jid:
            continue
        company = li.select_one("h4")
        loc = li.select_one(".job-search-card__location")
        tm = li.select_one("time")
        out.append(
            {
                "id": jid,
                "title": title.get_text(" ", strip=True),
                "company": company.get_text(" ", strip=True) if company else "",
                "location": loc.get_text(" ", strip=True) if loc else "",
                "posted": (tm.get("datetime") if tm else "") or "",
                "url": f"https://www.linkedin.com/jobs/view/{jid}",
            }
        )
    return out


def parse_detail(page_html: str) -> dict:
    soup = BeautifulSoup(page_html, "html.parser")
    node = soup.select_one(".show-more-less-html__markup, .description__text")
    desc = node.get_text("\n", strip=True) if node else ""
    criteria = {}
    for item in soup.select(".description__job-criteria-item"):
        h, v = item.select_one("h3"), item.select_one("span")
        if h and v:
            criteria[h.get_text(strip=True).lower()] = v.get_text(strip=True)
    return {"description": desc, "criteria": criteria}


class LinkedInSource(Source):
    name = "linkedin"

    def __init__(self, cfg, http):
        super().__init__(cfg, http)
        l = cfg.get("linkedin", {}) or {}
        self.location = l.get("location") or (self.locations[0] if self.locations else "United States")
        self.posted_within = int(l.get("posted_within_seconds", 86400))
        self.job_types = list(l.get("job_types") or ["C"])
        self.max_pages = int(l.get("max_pages", 2))
        self.fetch_details = bool(l.get("fetch_details", True))
        self.max_detail_fetches = int(l.get("max_detail_fetches", 60))
        self.delay = float(l.get("request_delay_seconds", 0.7))

    def _search(self, keyword: str) -> list[dict]:
        rows, start = [], 0
        for _ in range(self.max_pages):
            params = {
                "keywords": keyword,
                "location": self.location,
                "f_TPR": f"r{self.posted_within}",
                "start": str(start),
            }
            if self.job_types:
                params["f_JT"] = ",".join(self.job_types)
            resp = self.http.get(SEARCH_URL, params=params, headers={"Accept": "text/html"})
            if resp.status_code == 400 and start > 0:
                break  # LinkedIn returns 400 when paging past the end
            if resp.status_code == 429:
                self.log.warning("rate limited by LinkedIn on %r; stopping this keyword", keyword)
                break
            resp.raise_for_status()
            cards = parse_search_cards(resp.text)
            if not cards:
                break
            rows.extend(cards)
            start += len(cards)
            time.sleep(self.delay)
        return rows

    def fetch(self) -> list[Job]:
        raw: dict[str, dict] = {}
        for kw in self.keywords:
            try:
                cards = self._search(kw)
                self.log.info("keyword=%r results=%d", kw, len(cards))
            except Exception as exc:
                self.log.error("search failed for %r: %s", kw, exc)
                continue
            for c in cards:
                raw.setdefault(c["id"], c)

        jobs: list[Job] = []
        budget = self.max_detail_fetches if self.fetch_details else 0
        for jid, c in raw.items():
            desc, criteria = "", {}
            if budget > 0:
                try:
                    resp = self.http.get(DETAIL_URL.format(job_id=jid), headers={"Accept": "text/html"})
                    budget -= 1
                    if resp.status_code == 200:
                        det = parse_detail(resp.text)
                        desc, criteria = det["description"], det["criteria"]
                    time.sleep(self.delay)
                except Exception as exc:
                    self.log.debug("detail fetch failed for %s: %s", jid, exc)
            emp_line = criteria.get("employment type", "")
            blob = f"{c['title']}\n{emp_line}\n{desc}"
            hint = employment_hint(blob)
            if not hint and emp_line:
                hint = "Contract (type unclear)" if "contract" in emp_line.lower() else ("Full-time" if "full" in emp_line.lower() else "")
            crit_txt = "\n".join(f"{k.title()}: {v}" for k, v in criteria.items())
            jobs.append(
                Job(
                    title=c["title"],
                    company=c["company"],
                    location=c["location"],
                    rate=find_rate(blob),
                    description=(crit_txt + "\n\n" if crit_txt else "") + desc,
                    url=c["url"],
                    source=self.name,
                    employment_hint=hint,
                    visa_hint=visa_hint(blob),
                    years_hint=years_required_hint(desc),
                    posted_at=c["posted"],
                    extra={"linkedin_id": jid, "seniority": criteria.get("seniority level", "")},
                ).clean()
            )
        return jobs
