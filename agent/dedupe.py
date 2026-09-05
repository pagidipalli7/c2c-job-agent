"""Deduplication against the sheet and within the current run."""
from __future__ import annotations

import logging

from .models import Job

log = logging.getLogger("dedupe")


class Deduper:
    def __init__(self, existing_ids: set[str]):
        self.existing = set(existing_ids)
        self.seen_this_run: set[str] = set()

    def is_new(self, job: Job) -> bool:
        jid = job.job_id
        if jid in self.existing or jid in self.seen_this_run:
            return False
        self.seen_this_run.add(jid)
        return True

    def filter_new(self, jobs: list[Job]) -> list[Job]:
        return [j for j in jobs if self.is_new(j)]
