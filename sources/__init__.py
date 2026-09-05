"""Pluggable job sources. Every source exposes fetch() -> list[Job]."""
from __future__ import annotations

from typing import Type

from .base import Source
from .dice import DiceSource
from .gmail_imap import GmailSource
from .serpapi_jobs import SerpApiSource

REGISTRY: dict[str, Type[Source]] = {
    DiceSource.name: DiceSource,
    GmailSource.name: GmailSource,
    SerpApiSource.name: SerpApiSource,
}


def build_sources(cfg: dict, http, only: list[str] | None = None) -> list[Source]:
    enabled = only or cfg.get("sources", {}).get("enabled", list(REGISTRY))
    out: list[Source] = []
    for name in enabled:
        cls = REGISTRY.get(name)
        if cls is None:
            raise KeyError(f"unknown source '{name}' (known: {sorted(REGISTRY)})")
        out.append(cls(cfg, http))
    return out
