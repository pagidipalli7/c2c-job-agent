"""Offline ATS simulator: an httpx.MockTransport that answers the real Greenhouse/Lever/Ashby/
SmartRecruiters/Workday URLs with deterministic fake boards. Used when the network is unavailable
(`scripts/run_discovery.py --simulate`) and by tests. Boards are deterministic per slug so a rerun
produces zero duplicates; `drop` lets tests simulate a posting disappearing.
"""
from __future__ import annotations

import json
import random
import re
from datetime import datetime, timedelta, timezone

import httpx

TITLES = [
    "Senior Power Platform Developer",
    "Power Apps Developer",
    "Dynamics 365 Developer",
    "Power Platform Solution Architect",
    "Data Engineer",
    "Senior Data Engineer",
    "Analytics Engineer",
    "Staff Software Engineer, Backend",
    "Product Manager, Growth",
    "Account Executive",
    "Recruiting Coordinator",
    "Power BI Developer",
    "Machine Learning Engineer",
    "Customer Success Manager",
]
LOCATIONS = ["Austin, TX", "Dallas, TX", "Remote - US", "San Jose, CA", "San Francisco, CA", "New York, NY", "Chicago, IL", "Remote"]
DEPTS = ["Engineering", "Data", "IT", "Product", "Sales", "People"]

JD = {
    "power": (
        "We are looking for a {title} to build model-driven and canvas apps on Microsoft Power Platform. "
        "You will design Power Automate flows, Dataverse data models, and integrate with Dynamics 365 and Azure. "
        "Requirements: 5+ years Power Apps, Power Automate, Dataverse; PL-400 preferred; strong SQL; ALM with Azure DevOps. "
        "Must be authorized to work in the US. Salary range $125,000 - $160,000."
    ),
    "data": (
        "As a {title} you will own batch and streaming pipelines on Spark and Airflow, model data in Snowflake with dbt, "
        "and partner with analytics teams. Requirements: 4+ years Python and SQL, Airflow, Spark, cloud data warehouses (Snowflake/Redshift/BigQuery), "
        "Kafka a plus. Salary range $150,000 - $190,000. We sponsor visas for the right candidate."
    ),
    "other": (
        "Join us as a {title}. You will work cross-functionally with sales, product and engineering to grow the business. "
        "3+ years relevant experience required."
    ),
}


def _jd(title: str) -> str:
    t = title.lower()
    key = "power" if ("power" in t or "dynamics" in t) else "data" if ("data" in t or "analytics" in t) else "other"
    return JD[key].format(title=title)


def board_for(slug: str, n: int | None = None, generation: int = 0) -> list[dict]:
    rng = random.Random(f"{slug}:{generation}")
    count = n or rng.randint(6, 14)
    now = datetime.now(timezone.utc)
    jobs = []
    for i in range(count):
        title = rng.choice(TITLES)
        loc = rng.choice(LOCATIONS)
        # make titles unique-ish per board so fingerprints don't collide within a board
        if any(j["title"] == title and j["location"] == loc for j in jobs):
            title = f"{title} {['II','III','Lead','Principal'][i % 4]}"
        jobs.append(
            {
                "id": f"{abs(hash((slug, i, generation))) % 10_000_000}",
                "title": title,
                "location": loc,
                "department": rng.choice(DEPTS),
                "posted_at": now - timedelta(days=rng.randint(0, 30), hours=rng.randint(0, 23)),
                "remote": "remote" in loc.lower(),
                "description": _jd(title),
            }
        )
    return jobs


class Simulator:
    def __init__(self, generation: int = 0, drop: dict[str, set[str]] | None = None, fail_slugs: set[str] | None = None, rate_limit_once: set[str] | None = None):
        self.generation = generation
        self.drop = drop or {}  # slug -> set of titles to omit
        self.fail_slugs = fail_slugs or set()
        self.rate_limit_once = set(rate_limit_once or set())
        self.calls: list[str] = []

    def _jobs(self, slug: str) -> list[dict]:
        return [j for j in board_for(slug, generation=self.generation) if j["title"] not in self.drop.get(slug, set())]

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        host, path = request.url.host, request.url.path
        m = None
        if host == "boards-api.greenhouse.io":
            m = re.match(r"/v1/boards/([^/]+)/jobs", path)
            kind = "greenhouse"
        elif host == "api.lever.co":
            m = re.match(r"/v0/postings/([^/]+)", path)
            kind = "lever"
        elif host == "api.ashbyhq.com":
            m = re.match(r"/posting-api/job-board/([^/]+)", path)
            kind = "ashby"
        elif host == "api.smartrecruiters.com":
            m = re.match(r"/v1/companies/([^/]+)/postings(?:/([^/]+))?", path)
            kind = "smartrecruiters"
        elif host.endswith(".myworkdayjobs.com"):
            m = re.match(r"/wday/cxs/([^/]+)/([^/]+)(/jobs)?(/.*)?", path)
            kind = "workday"
        else:
            return httpx.Response(404, text="unknown host")
        if not m:
            return httpx.Response(404, text="bad path")
        slug = m.group(1)
        if slug in self.fail_slugs or slug.startswith("gone-"):
            return httpx.Response(404, text="Not found")
        if slug in self.rate_limit_once:
            self.rate_limit_once.discard(slug)
            return httpx.Response(429, headers={"Retry-After": "0"}, text="slow down")
        jobs = self._jobs(slug)
        name = slug.replace("-", " ").title()
        if kind == "greenhouse":
            body = {"jobs": [
                {"id": int(j["id"]), "title": j["title"], "absolute_url": f"https://boards.greenhouse.io/{slug}/jobs/{j['id']}",
                 "location": {"name": j["location"]}, "departments": [{"name": j["department"]}], "offices": [{"name": j["location"]}],
                 "first_published": j["posted_at"].isoformat(), "updated_at": j["posted_at"].isoformat(),
                 "content": f"<div><p>{j['description']}</p></div>", "company_name": name} for j in jobs]}
        elif kind == "lever":
            body = [
                {"id": f"lv-{j['id']}", "text": j["title"], "hostedUrl": f"https://jobs.lever.co/{slug}/lv-{j['id']}", "applyUrl": f"https://jobs.lever.co/{slug}/lv-{j['id']}/apply",
                 "categories": {"location": j["location"], "team": j["department"], "commitment": "Full-time"}, "workplaceType": "remote" if j["remote"] else "on-site",
                 "createdAt": int(j["posted_at"].timestamp() * 1000), "descriptionPlain": j["description"], "lists": []} for j in jobs]
        elif kind == "ashby":
            body = {"jobs": [
                {"id": f"as-{j['id']}", "title": j["title"], "location": j["location"], "isRemote": j["remote"], "department": j["department"],
                 "jobUrl": f"https://jobs.ashbyhq.com/{slug}/as-{j['id']}", "applyUrl": f"https://jobs.ashbyhq.com/{slug}/as-{j['id']}/application",
                 "publishedAt": j["posted_at"].isoformat(), "descriptionHtml": f"<p>{j['description']}</p>", "employmentType": "FullTime",
                 "compensation": {"compensationTierSummary": "$140K – $170K"}} for j in jobs]}
        elif kind == "smartrecruiters":
            posting_id = m.group(2)
            if posting_id:
                j = next((x for x in jobs if x["id"] == posting_id), None)
                if not j:
                    return httpx.Response(404)
                body = {"id": posting_id, "jobAd": {"sections": {"jobDescription": {"text": f"<p>{j['description']}</p>"}, "qualifications": {"text": ""}}}}
            else:
                offset = int(request.url.params.get("offset", 0))
                page = jobs[offset: offset + 100]
                body = {"totalFound": len(jobs), "content": [
                    {"id": j["id"], "name": j["title"], "releasedDate": j["posted_at"].isoformat(), "company": {"name": name},
                     "location": {"city": j["location"].split(",")[0], "region": j["location"].split(",")[-1].strip() if "," in j["location"] else "", "country": "us", "remote": j["remote"]},
                     "department": {"label": j["department"]}, "typeOfEmployment": {"label": "Full-time"}} for j in page]}
        else:  # workday
            site, is_list, ext = m.group(2), m.group(3), m.group(4)
            if is_list:
                payload = json.loads(request.content or b"{}")
                offset = int(payload.get("offset", 0))
                page = jobs[offset: offset + 20]
                body = {"total": len(jobs), "jobPostings": [
                    {"title": j["title"], "externalPath": f"/job/{j['location'].replace(' ', '-')}/{j['title'].replace(' ', '-')}_R{j['id']}",
                     "locationsText": j["location"], "postedOn": "Posted Today", "bulletFields": [f"R{j['id']}"]} for j in page]}
            else:
                jid = (ext or "").rsplit("_R", 1)[-1]
                j = next((x for x in jobs if x["id"] == jid), None)
                if not j:
                    return httpx.Response(404)
                body = {"jobPostingInfo": {"jobDescription": f"<p>{j['description']}</p>", "location": j["location"], "title": j["title"]}}
        return httpx.Response(200, json=body)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
