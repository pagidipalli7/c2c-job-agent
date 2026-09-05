from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from agent.models import Job


class Source(ABC):
    name: str = "base"

    def __init__(self, cfg: dict, http):
        self.cfg = cfg
        self.http = http
        self.keywords: list[str] = list(cfg.get("keywords", []))
        self.locations: list[str] = list(cfg.get("locations", []))
        self.log = logging.getLogger(f"source.{self.name}")

    @abstractmethod
    def fetch(self) -> list[Job]:
        """Return candidate jobs. Raise on fatal source errors; the runner logs and continues."""
