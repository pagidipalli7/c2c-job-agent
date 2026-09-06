"""Crawl every active company with bounded concurrency; called by the scheduler and scripts/run_discovery.py."""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field

from app.config import get_settings
from app.db import session_scope
from app.db.base import utcnow
from app.db.models import CompanyRegistry, DiscoveryRun
from app.logging import get_logger

from .base import CrawlError, RateLimited, SlugGone
from .crawlers import SUBMIT_ONLY, get_crawler
from .registry import active_companies, record_failure, record_success
from .store import upsert_jobs

log = get_logger("discovery.runner")


@dataclass
class RunReport:
    companies_crawled: int = 0
    companies_failed: int = 0
    jobs_seen: int = 0
    jobs_new: int = 0
    jobs_closed: int = 0
    new_job_ids: list[int] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


async def crawl_company(company_id: int, report: RunReport, sem: asyncio.Semaphore, stagger_max: float = 0.0) -> None:
    async with sem:
        if stagger_max:
            await asyncio.sleep(random.uniform(0, stagger_max))
        with session_scope() as s:
            company = s.get(CompanyRegistry, company_id)
            assert company is not None
            slug, ats, name = company.slug, company.ats_type, company.name
        if ats in SUBMIT_ONLY:
            from .workday_discovery import WorkdayDiscovery  # lazy: needs tenant host

            crawler = WorkdayDiscovery()
        else:
            try:
                crawler = get_crawler(ats)
            except KeyError as e:
                with session_scope() as s:
                    record_failure(s, s.get(CompanyRegistry, company_id), str(e), permanent=True)
                report.companies_failed += 1
                report.errors[f"{ats}:{slug}"] = str(e)
                return
        try:
            if ats in SUBMIT_ONLY:
                with session_scope() as s:
                    careers_url = s.get(CompanyRegistry, company_id).careers_url
                jobs = await crawler.fetch_jobs(slug, careers_url=careers_url, company_name=name)
            else:
                jobs = await crawler.fetch_jobs(slug)
        except SlugGone as e:
            with session_scope() as s:
                record_failure(s, s.get(CompanyRegistry, company_id), f"404: {e}", permanent=True)
            report.companies_failed += 1
            report.errors[f"{ats}:{slug}"] = "slug gone (404)"
            log.warning("slug_gone", ats=ats, slug=slug)
            return
        except (RateLimited, CrawlError, Exception) as e:  # noqa: BLE001 - crawl must not crash the run
            with session_scope() as s:
                record_failure(s, s.get(CompanyRegistry, company_id), f"{type(e).__name__}: {e}")
            report.companies_failed += 1
            report.errors[f"{ats}:{slug}"] = f"{type(e).__name__}: {e}"
            log.warning("crawl_failed", ats=ats, slug=slug, error=str(e))
            return
        for j in jobs:
            if not j.company_name or j.company_name == slug:
                j.company_name = name
        with session_scope() as s:
            company = s.get(CompanyRegistry, company_id)
            stats = upsert_jobs(s, company, jobs)
            record_success(s, company)
            s.flush()
            report.new_job_ids.extend(j.id for j in stats.new_jobs)
        report.companies_crawled += 1
        report.jobs_seen += stats.seen
        report.jobs_new += stats.new
        report.jobs_closed += stats.closed
        log.info("crawled", ats=ats, slug=slug, seen=stats.seen, new=stats.new, closed=stats.closed)


async def run_discovery(concurrency: int | None = None, stagger_max: float = 0.0, only_slugs: list[str] | None = None) -> RunReport:
    settings = get_settings()
    report = RunReport()
    with session_scope() as s:
        run = DiscoveryRun()
        s.add(run)
        s.flush()
        run_id = run.id
        ids = [c.id for c in active_companies(s) if not only_slugs or c.slug in only_slugs]
    sem = asyncio.Semaphore(concurrency or settings.discovery_concurrency)
    await asyncio.gather(*(crawl_company(cid, report, sem, stagger_max) for cid in ids))
    with session_scope() as s:
        run = s.get(DiscoveryRun, run_id)
        run.finished_at = utcnow()
        run.companies_crawled = report.companies_crawled
        run.companies_failed = report.companies_failed
        run.jobs_seen = report.jobs_seen
        run.jobs_new = report.jobs_new
        run.jobs_closed = report.jobs_closed
    log.info("discovery_done", **{k: v for k, v in report.__dict__.items() if k not in ("new_job_ids", "errors")})
    return report
