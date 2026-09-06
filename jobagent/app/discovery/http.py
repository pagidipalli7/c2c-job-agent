"""Thin httpx wrapper: retries on 429/5xx with backoff, 404 -> SlugGone, optional MockTransport."""
from __future__ import annotations

import asyncio
import random

import httpx

from app.config import get_settings
from app.logging import get_logger

from .base import CrawlError, RateLimited, SlugGone

log = get_logger("discovery.http")

_transport_override: httpx.AsyncBaseTransport | None = None


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """Tests and the offline simulator inject an httpx.MockTransport here."""
    global _transport_override
    _transport_override = transport


def make_client(timeout: float = 30.0) -> httpx.AsyncClient:
    kwargs: dict = {
        "timeout": timeout,
        "headers": {"User-Agent": "Mozilla/5.0 (compatible; JobAgent/1.0)", "Accept": "application/json"},
        "follow_redirects": True,
    }
    if _transport_override is not None:
        kwargs["transport"] = _transport_override
    else:
        proxy = get_settings().proxy_url
        if proxy:
            kwargs["proxy"] = proxy
    return httpx.AsyncClient(**kwargs)


async def get_json(client: httpx.AsyncClient, url: str, *, params: dict | None = None, max_attempts: int = 4, method: str = "GET", json_body=None):
    delay = 1.0
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = await client.request(method, url, params=params, json=json_body)
        except httpx.HTTPError as e:
            last_exc = e
            log.warning("http_error", url=url, attempt=attempt, error=str(e))
            await asyncio.sleep(delay + random.random())
            delay *= 2
            continue
        if resp.status_code == 404:
            raise SlugGone(url)
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            retry_after = resp.headers.get("Retry-After")
            wait = float(retry_after) if retry_after and retry_after.isdigit() else delay
            log.warning("http_retry", url=url, status=resp.status_code, wait=wait, attempt=attempt)
            if attempt == max_attempts:
                raise RateLimited(url) if resp.status_code == 429 else CrawlError(f"{resp.status_code} from {url}")
            await asyncio.sleep(min(wait, 60) + random.random())
            delay *= 2
            continue
        if resp.status_code >= 400:
            raise CrawlError(f"{resp.status_code} from {url}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as e:
            raise CrawlError(f"non-JSON response from {url}: {e}") from e
    raise CrawlError(f"giving up on {url}: {last_exc}")
