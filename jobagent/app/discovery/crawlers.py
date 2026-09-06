from __future__ import annotations

from .ashby import AshbyCrawler
from .base import Crawler
from .greenhouse import GreenhouseCrawler
from .lever import LeverCrawler
from .smartrecruiters import SmartRecruitersCrawler

CRAWLERS: dict[str, Crawler] = {
    c.ats_type: c for c in (GreenhouseCrawler(), LeverCrawler(), AshbyCrawler(), SmartRecruitersCrawler())
}

# ATS types we can *submit* to but not crawl through a public JSON API (Workday jobs are
# discovered via the tenant search endpoint in workday_discovery.py).
SUBMIT_ONLY = {"workday"}


def get_crawler(ats_type: str) -> Crawler:
    try:
        return CRAWLERS[ats_type]
    except KeyError:
        raise KeyError(f"no crawler for ats_type={ats_type!r}; known: {sorted(CRAWLERS)}")
