import asyncio

import httpx
import pytest
from sqlalchemy import select

from app.db.models import CompanyRegistry, JobPosting
from app.discovery import http as dhttp
from app.discovery.base import SlugGone, fingerprint, normalize, normalize_company
from app.discovery.crawlers import get_crawler
from app.discovery.detect import ForbiddenSource, detect_ats
from app.discovery.registry import MAX_FAILS, load_companies_yaml, record_failure
from app.discovery.runner import run_discovery
from app.discovery.simulator import Simulator
from app.discovery.store import upsert_jobs
from tests.conftest import ROOT


@pytest.fixture()
def sim():
    s = Simulator()
    dhttp.set_transport(s.transport())
    yield s
    dhttp.set_transport(None)


@pytest.fixture()
def registry(db):
    n = load_companies_yaml(db, ROOT / "companies.yaml")
    db.commit()
    assert n >= 20
    return db


# ------------------------------------------------------------------ crawlers
@pytest.mark.parametrize("ats,slug", [("greenhouse", "stripe"), ("lever", "netflix"), ("ashby", "ramp"), ("smartrecruiters", "bosch")])
def test_each_crawler_parses_simulated_board(sim, ats, slug):
    jobs = asyncio.run(get_crawler(ats).fetch_jobs(slug))
    assert jobs and all(j.ats_type == ats and j.title and j.url and j.external_id for j in jobs)
    assert any(j.description_text for j in jobs)
    assert any(j.posted_at for j in jobs)


def test_workday_discovery_parses_tenant_endpoint(sim):
    from app.discovery.workday_discovery import WorkdayDiscovery

    jobs = asyncio.run(WorkdayDiscovery().fetch_jobs("kyndryl", careers_url="https://kyndryl.wd5.myworkdayjobs.com/KyndrylProfessionalCareers", company_name="Kyndryl"))
    assert jobs and all(j.ats_type == "workday" and "myworkdayjobs.com" in j.url for j in jobs)
    assert jobs[0].company_name == "Kyndryl" and jobs[0].description_text


def test_404_raises_slug_gone(sim):
    with pytest.raises(SlugGone):
        asyncio.run(get_crawler("greenhouse").fetch_jobs("gone-company"))


def test_429_is_retried_with_backoff():
    s = Simulator(rate_limit_once={"stripe"})
    dhttp.set_transport(s.transport())
    try:
        jobs = asyncio.run(get_crawler("greenhouse").fetch_jobs("stripe"))
    finally:
        dhttp.set_transport(None)
    assert jobs
    assert sum("stripe" in c for c in s.calls) == 2


def test_schema_drift_is_a_crawl_error():
    from app.discovery.base import CrawlError

    dhttp.set_transport(httpx.MockTransport(lambda r: httpx.Response(200, json={"unexpected": 1})))
    try:
        with pytest.raises(CrawlError):
            asyncio.run(get_crawler("greenhouse").fetch_jobs("x"))
        with pytest.raises(CrawlError):
            asyncio.run(get_crawler("lever").fetch_jobs("x"))
    finally:
        dhttp.set_transport(None)


# ------------------------------------------------------------------ detection
@pytest.mark.parametrize(
    "url,ats,slug",
    [
        ("https://boards.greenhouse.io/stripe", "greenhouse", "stripe"),
        ("https://job-boards.greenhouse.io/figma/jobs/123", "greenhouse", "figma"),
        ("https://jobs.lever.co/netflix/abc", "lever", "netflix"),
        ("https://jobs.ashbyhq.com/ramp", "ashby", "ramp"),
        ("jobs.smartrecruiters.com/Bosch/12345", "smartrecruiters", "Bosch"),
        ("https://kyndryl.wd5.myworkdayjobs.com/en-US/KyndrylProfessionalCareers/job/x", "workday", "kyndryl"),
    ],
)
def test_detect_ats(url, ats, slug):
    d = detect_ats(url)
    assert (d.ats_type, d.slug) == (ats, slug)
    if ats == "workday":
        assert d.site == "KyndrylProfessionalCareers" and d.tenant_host == "kyndryl.wd5.myworkdayjobs.com"


@pytest.mark.parametrize("url", ["https://www.linkedin.com/jobs/view/123", "https://indeed.com/viewjob?jk=1", "https://www.glassdoor.com/job/x", "https://ziprecruiter.com/c/x"])
def test_forbidden_boards_are_rejected(url):
    with pytest.raises(ForbiddenSource):
        detect_ats(url)


# ------------------------------------------------------------------ fingerprint / dedup
def test_fingerprint_normalisation():
    assert normalize_company("Stripe, Inc.") == normalize_company("stripe") == "stripe"
    assert normalize("Sr.  Data   Engineer!") == "sr data engineer"
    assert fingerprint("Acme Inc", "Data Engineer", "Austin, TX, US") == fingerprint("ACME", "data engineer", "austin tx")
    assert fingerprint("Acme", "Data Engineer", "Austin") != fingerprint("Acme", "Data Engineer", "Dallas")


def test_run_discovery_populates_and_rerun_has_zero_duplicates(sim, registry):
    active = len([c for c in registry.scalars(select(CompanyRegistry)) if c.active])
    r1 = asyncio.run(run_discovery(concurrency=5))
    assert r1.companies_crawled == active and r1.companies_failed == 0
    assert r1.jobs_new > 50
    total = registry.scalars(select(JobPosting)).all()
    assert len(total) == r1.jobs_new
    assert len({j.fingerprint for j in total}) == len(total)
    r2 = asyncio.run(run_discovery(concurrency=5))
    assert r2.jobs_new == 0 and r2.jobs_closed == 0
    assert len(registry.scalars(select(JobPosting)).all()) == len(total)
    for c in registry.scalars(select(CompanyRegistry)):
        assert c.last_crawled is not None and c.fail_count == 0


def test_job_closed_after_two_missing_crawls(db, sim):
    company = CompanyRegistry(slug="stripe", ats_type="greenhouse", name="Stripe")
    db.add(company)
    db.commit()
    jobs = asyncio.run(get_crawler("greenhouse").fetch_jobs("stripe"))
    upsert_jobs(db, company, jobs)
    db.commit()
    victim = jobs[0]
    rest = jobs[1:]
    s1 = upsert_jobs(db, company, rest)
    db.commit()
    row = db.scalar(select(JobPosting).where(JobPosting.fingerprint == victim.fingerprint))
    assert row.status == "open" and row.missing_crawls == 1 and s1.closed == 0
    s2 = upsert_jobs(db, company, rest)
    db.commit()
    db.refresh(row)
    assert row.status == "closed" and s2.closed == 1
    # reappears -> reopened
    upsert_jobs(db, company, jobs)
    db.commit()
    db.refresh(row)
    assert row.status == "open" and row.missing_crawls == 0


def test_registry_deactivates_after_five_failures(db):
    c = CompanyRegistry(slug="flaky", ats_type="lever", name="Flaky")
    db.add(c)
    db.commit()
    for i in range(MAX_FAILS - 1):
        record_failure(db, c, "boom")
        assert c.active
    record_failure(db, c, "boom")
    assert not c.active and c.fail_count == MAX_FAILS


def test_runner_handles_gone_slug_and_failures(db):
    db.add_all([CompanyRegistry(slug="gone-co", ats_type="greenhouse", name="Gone"), CompanyRegistry(slug="stripe", ats_type="greenhouse", name="Stripe")])
    db.commit()
    dhttp.set_transport(Simulator().transport())
    try:
        r = asyncio.run(run_discovery(concurrency=2))
    finally:
        dhttp.set_transport(None)
    assert r.companies_failed == 1 and r.companies_crawled == 1
    gone = db.scalar(select(CompanyRegistry).where(CompanyRegistry.slug == "gone-co"))
    db.refresh(gone)
    assert gone.active is False and "404" in gone.last_error


def test_workday_discovery_runs_every_search_term_without_duplicates(sim, monkeypatch):
    from app.config import get_settings
    from app.discovery.workday_discovery import WorkdayDiscovery

    monkeypatch.setattr(get_settings(), "workday_search_terms", "Power Platform,Data Engineer")
    jobs = asyncio.run(WorkdayDiscovery().fetch_jobs("kyndryl", careers_url="https://kyndryl.wd5.myworkdayjobs.com/KyndrylProfessionalCareers", company_name="Kyndryl"))
    assert jobs and len({j.raw["externalPath"] for j in jobs}) == len(jobs)
    assert {j.raw["search_term"] for j in jobs} <= {"Power Platform", "Data Engineer"}


def test_real_discovery_purges_simulated_jobs_and_cancels_their_applications(db, registry):
    from app.db.models import Application
    from app.discovery.store import purge_simulated

    dhttp.set_transport(Simulator().transport())
    try:
        asyncio.run(run_discovery(concurrency=5))
    finally:
        dhttp.set_transport(None)
    jobs = db.scalars(select(JobPosting)).all()
    assert jobs and all(j.raw.get("simulated") for j in jobs)
    job = jobs[0]
    db.add(Application(client_id=1, job_id=job.id, job_fingerprint=job.fingerprint, company_key="x", ats_type=job.ats_type, status="queued", trace_id="t"))
    db.commit()
    closed, cancelled = purge_simulated(db)
    db.commit()
    assert closed == len(jobs) and cancelled == 1
    db.expire_all()
    assert db.get(JobPosting, job.id).status == "closed"
    assert db.scalar(select(Application)).status == "cancelled"
