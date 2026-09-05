"""requests.Session with retry/backoff, realistic User-Agent and default timeouts."""
from __future__ import annotations

import logging

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("http")

_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


class HttpClient:
    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        self.timeout = float(cfg.get("timeout_seconds", 25))
        retries = int(cfg.get("retries", 3))
        backoff = float(cfg.get("backoff_factor", 1.5))
        self.session = requests.Session()
        retry = Retry(
            total=retries,
            connect=retries,
            read=retries,
            status=retries,
            backoff_factor=backoff,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD", "POST"}),
            raise_on_status=False,
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update(
            {
                "User-Agent": cfg.get("user_agent") or _DEFAULT_UA,
                "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )

    def get(self, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        resp = self.session.get(url, **kwargs)
        log.debug("GET %s -> %s", resp.url, resp.status_code)
        return resp

    def get_json(self, url: str, **kwargs):
        resp = self.get(url, **kwargs)
        resp.raise_for_status()
        return resp.json()
