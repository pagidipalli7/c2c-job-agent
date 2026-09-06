"""httpx client for HTTP submission adapters (mock transport injectable, proxy-aware)."""
from __future__ import annotations

import httpx

from app.config import get_settings

_transport_override: httpx.AsyncBaseTransport | None = None


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    global _transport_override
    _transport_override = transport


def make_client(timeout: float = 60.0) -> httpx.AsyncClient:
    kwargs: dict = {
        "timeout": timeout,
        "follow_redirects": True,
        "headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    if _transport_override is not None:
        kwargs["transport"] = _transport_override
    else:
        proxy = get_settings().proxy_url
        if proxy:
            kwargs["proxy"] = proxy
    return httpx.AsyncClient(**kwargs)


def to_form(pairs: list[tuple[str, str]]) -> dict[str, str | list[str]]:
    """(name, value) pairs -> httpx form dict; repeated names become lists (multipart/urlencoded both accept)."""
    out: dict[str, str | list[str]] = {}
    for k, v in pairs:
        if k in out:
            cur = out[k]
            out[k] = (cur if isinstance(cur, list) else [cur]) + [v]
        else:
            out[k] = v
    return out
