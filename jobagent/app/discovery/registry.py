"""CompanyRegistry helpers: load companies.yaml, record crawl outcomes, deactivate after 5 fails."""
from __future__ import annotations

from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.models import CompanyRegistry
from app.logging import get_logger

from .detect import ForbiddenSource, detect_ats

log = get_logger("discovery.registry")
MAX_FAILS = 5


def load_companies_yaml(session: Session, path: str | Path) -> int:
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    n = 0
    for entry in doc.get("companies", []):
        if "url" in entry and "slug" not in entry:
            try:
                d = detect_ats(entry["url"])
            except ForbiddenSource as e:
                log.error("companies_yaml_forbidden", url=entry["url"], error=str(e))
                continue
            entry = {**entry, "slug": d.slug, "ats": d.ats_type, "careers_url": entry["url"]}
        slug, ats = entry["slug"], entry["ats"]
        row = session.scalar(select(CompanyRegistry).where(CompanyRegistry.slug == slug, CompanyRegistry.ats_type == ats))
        if row is None:
            row = CompanyRegistry(slug=slug, ats_type=ats, name=entry.get("name") or slug, careers_url=entry.get("careers_url") or entry.get("url"))
            session.add(row)
        else:
            row.name = entry.get("name") or row.name
            row.careers_url = entry.get("careers_url") or entry.get("url") or row.careers_url
        if "active" in entry:
            row.active = bool(entry["active"])
        n += 1
    session.flush()
    return n


def add_company(session: Session, url: str, name: str | None = None) -> CompanyRegistry:
    d = detect_ats(url)
    row = session.scalar(select(CompanyRegistry).where(CompanyRegistry.slug == d.slug, CompanyRegistry.ats_type == d.ats_type))
    if row is None:
        row = CompanyRegistry(slug=d.slug, ats_type=d.ats_type, name=name or d.slug, careers_url=url)
        session.add(row)
        session.flush()
    return row


def active_companies(session: Session) -> list[CompanyRegistry]:
    return list(session.scalars(select(CompanyRegistry).where(CompanyRegistry.active.is_(True)).order_by(CompanyRegistry.last_crawled.nulls_first(), CompanyRegistry.id)))


def record_success(session: Session, company: CompanyRegistry) -> None:
    company.fail_count = 0
    company.last_error = None
    company.last_crawled = utcnow()


def record_failure(session: Session, company: CompanyRegistry, error: str, permanent: bool = False) -> None:
    company.fail_count += 1
    company.last_error = error[:1000]
    company.last_crawled = utcnow()
    if permanent or company.fail_count >= MAX_FAILS:
        company.active = False
        log.warning("company_deactivated", slug=company.slug, ats=company.ats_type, fails=company.fail_count, error=error[:200])
