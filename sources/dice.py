"""Dice — parses the server-rendered search results embedded in dice.com/jobs (Next.js RSC payload),
then enriches each job with the full description from the job-detail page's JSON-LD.

dice.com's old public JSON endpoint (job-search-api.svc.dice.com) no longer resolves, but the search
page still ships the exact same job objects inside `self.__next_f.push([1, "..."])` script chunks.
"""
from __future__ import annotations

import html
import json
import re

from bs4 import BeautifulSoup

from agent.models import Job
from agent.textsig import employment_hint, find_rate, visa_hint

from .base import Source

SEARCH_PAGE = "https://www.dice.com/jobs"
_RSC_CHUNK = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', re.S)
_JOBLIST_KEY = '"jobList":{"data":'


def _clean_salary(val) -> str:
    """Dice salary strings arrive as '$$65', 'USD 47.00 - 67.00 per hour', '60 - 80', 'Depends on Experience'."""
    s = re.sub(r"\s+", " ", str(val or "")).strip()
    s = re.sub(r"\${2,}", "$", s)
    if re.fullmatch(r"\d{2,3}(\.\d+)? ?- ?\d{2,3}(\.\d+)?", s):
        s = "$" + s.replace(" ", "") + "/hr"
    return s


def _decode_rsc(page_html: str) -> str:
    """Concatenate the JS-string-escaped RSC chunks into one decoded string."""
    out = []
    for m in _RSC_CHUNK.finditer(page_html):
        try:
            out.append(json.loads('"' + m.group(1) + '"'))
        except json.JSONDecodeError:
            out.append(m.group(1).encode("utf-8").decode("unicode_escape", errors="ignore"))
    return "".join(out)


def parse_search_page(page_html: str) -> tuple[list[dict], dict]:
    """Return (jobs, meta) from a dice.com/jobs HTML page. Empty list if the payload is absent."""
    blob = _decode_rsc(page_html)
    idx = blob.find(_JOBLIST_KEY)
    if idx == -1:
        return [], {}
    dec = json.JSONDecoder()
    start = idx + len(_JOBLIST_KEY)
    jobs, end = dec.raw_decode(blob, start)
    meta: dict = {}
    m_idx = blob.find('"meta":', end)
    if m_idx != -1 and m_idx - end < 5:
        try:
            meta, _ = dec.raw_decode(blob, m_idx + len('"meta":'))
        except json.JSONDecodeError:
            meta = {}
    return [j for j in jobs if isinstance(j, dict)], meta


class DiceSource(Source):
    name = "dice"

    def __init__(self, cfg, http):
        super().__init__(cfg, http)
        d = cfg.get("dice", {}) or {}
        self.posted_within = d.get("posted_within", "ONE")
        self.employment_types = d.get("employment_types") or ["CONTRACTS", "THIRD_PARTY"]
        self.max_pages = int(d.get("max_pages", 2))
        self.extra_locations = list(d.get("extra_locations") or [])   # on-site/hybrid markets worth logging
        self.us_only = bool(d.get("us_only", True))
        self.fetch_details = bool(d.get("fetch_details", True))
        self.max_detail_fetches = int(d.get("max_detail_fetches", 60))

    # ------------------------------------------------------------------ search
    def _search(self, keyword: str, remote_only: bool, location: str = "") -> list[dict]:
        """One keyword, one location mode. remote_only uses Dice's workplaceTypes=Remote facet (the
        only server-side filter that actually restricts to remote); `location` runs a radius search."""
        rows: list[dict] = []
        for page in range(1, self.max_pages + 1):
            params = {
                "q": keyword,
                "countryCode": "US",
                "filters.postedDate": self.posted_within,
                "filters.employmentType": "|".join(self.employment_types),
                "page": str(page),
            }
            if remote_only:
                params["filters.workplaceTypes"] = "Remote"
            if location:
                params["location"] = location
                params["radius"] = "50"
                params["radiusUnit"] = "mi"
            resp = self.http.get(SEARCH_PAGE, params=params, headers={"Accept": "text/html"})
            resp.raise_for_status()
            jobs, meta = parse_search_page(resp.text)
            if not jobs and page == 1:
                # Payload missing -> last-ditch anchor scrape so a markup change degrades, not breaks.
                jobs = self._anchor_fallback(resp.text)
            rows.extend(jobs)
            total = meta.get("totalResults") if isinstance(meta, dict) else None
            page_size = meta.get("pageSize") if isinstance(meta, dict) else None
            if not jobs or (total is not None and page_size and page * page_size >= int(total)):
                break
        return rows

    @staticmethod
    def _anchor_fallback(page_html: str) -> list[dict]:
        soup = BeautifulSoup(page_html, "html.parser")
        out, seen = [], set()
        for a in soup.select('a[href*="/job-detail/"]'):
            m = re.search(r"/job-detail/([0-9a-fA-F-]{20,})", a.get("href", ""))
            title = a.get_text(" ", strip=True)
            if not m or m.group(1) in seen or not title:
                continue
            seen.add(m.group(1))
            out.append({"guid": m.group(1), "title": title,
                        "detailsPageUrl": f"https://www.dice.com/job-detail/{m.group(1)}"})
        return out

    # ------------------------------------------------------------------ detail
    def _fetch_detail(self, url: str) -> dict:
        """Full description (+ company/location fallbacks) from the job page's JSON-LD JobPosting."""
        resp = self.http.get(url, headers={"Accept": "text/html"})
        if resp.status_code != 200:
            return {}
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(tag.string or "")
            except Exception:
                continue
            for item in data if isinstance(data, list) else [data]:
                if not (isinstance(item, dict) and item.get("@type") == "JobPosting"):
                    continue
                desc = BeautifulSoup(html.unescape(item.get("description") or ""), "html.parser").get_text("\n", strip=True)
                org = item.get("hiringOrganization") or {}
                loc = item.get("jobLocation") or {}
                if isinstance(loc, list):
                    loc = loc[0] if loc else {}
                addr = (loc.get("address") or {}) if isinstance(loc, dict) else {}
                loc_str = ", ".join(x for x in [addr.get("addressLocality"), addr.get("addressRegion")] if x)
                return {
                    "description": desc,
                    "company": org.get("name", "") if isinstance(org, dict) else "",
                    "location": loc_str,
                    "employment": item.get("employmentType", ""),
                }
        node = soup.select_one('[data-testid="jobDescriptionHtml"], #jobDescription, .job-description')
        return {"description": node.get_text("\n", strip=True)} if node else {}

    # ------------------------------------------------------------------ fetch
    def fetch(self) -> list[Job]:
        remote_only = any(l.strip().lower() == "remote" for l in self.locations)
        raw_by_id: dict[str, dict] = {}
        queries = [(kw, remote_only, "") for kw in self.keywords]
        queries += [(kw, False, loc) for kw in self.keywords for loc in self.extra_locations]
        for kw, remote, loc in queries:
            try:
                rows = self._search(kw, remote_only=remote, location=loc)
                self.log.info("keyword=%r %s results=%d", kw, f"location={loc!r}" if loc else "remote", len(rows))
            except Exception as exc:  # per-query fail-soft
                self.log.error("search failed for %r (%s): %s", kw, loc or "remote", exc)
                continue
            for r in rows:
                jid = str(r.get("guid") or r.get("id") or r.get("detailsPageUrl") or "")
                if not jid or jid in raw_by_id:
                    continue
                country = ((r.get("jobLocation") or {}).get("country") or "") if isinstance(r.get("jobLocation"), dict) else ""
                if self.us_only and country and country.upper() not in ("USA", "US", "UNITED STATES"):
                    continue
                raw_by_id[jid] = r

        jobs: list[Job] = []
        detail_budget = self.max_detail_fetches if self.fetch_details else 0
        for jid, r in raw_by_id.items():
            url = r.get("detailsPageUrl") or f"https://www.dice.com/job-detail/{jid}"
            jl = r.get("jobLocation")
            loc = (jl.get("displayName", "") if isinstance(jl, dict) else (jl or "")) or ""
            wpt = r.get("workplaceTypes") or []
            if (r.get("isRemote") or "Remote" in wpt) and "remote" not in loc.lower():
                loc = f"Remote{' / ' + loc if loc else ''}"
            etypes = r.get("employmentType") or ""
            if isinstance(etypes, list):
                etypes = ", ".join(etypes)
            desc = r.get("summary") or ""
            if detail_budget > 0:
                try:
                    det = self._fetch_detail(url)
                    detail_budget -= 1
                    if det.get("description"):
                        desc = det["description"]
                    if not r.get("companyName") and det.get("company"):
                        r["companyName"] = det["company"]
                    if not loc and det.get("location"):
                        loc = det["location"]
                    if not etypes and det.get("employment"):
                        etypes = str(det["employment"])
                except Exception as exc:
                    self.log.debug("detail fetch failed for %s: %s", url, exc)
            blob = f"{r.get('title','')}\n{etypes}\n{r.get('employerType','')}\n{r.get('salary','')}\n{desc}"
            jobs.append(
                Job(
                    title=r.get("title", ""),
                    company=r.get("companyName", "") or "",
                    location=loc,
                    rate=_clean_salary(r.get("salary")) or find_rate(blob),
                    description=(f"Employment type (Dice): {etypes}\nEmployer type: {r.get('employerType','')}\n"
                                 f"Workplace: {', '.join(wpt) if isinstance(wpt, list) else wpt}\n\n{desc}"),
                    url=url,
                    source=self.name,
                    employment_hint=employment_hint(blob) or ("Contract (type unclear)" if "CONTRACT" in etypes.upper() else ""),
                    visa_hint=visa_hint(blob),
                    posted_at=str(r.get("postedDate") or r.get("modifiedDate") or ""),
                    extra={"dice_id": jid, "employerType": r.get("employerType", "")},
                ).clean()
            )
        return jobs
