"""SmartRecruiters. Their candidate-creation API needs a company API key, so submission goes through the
hosted apply page with the generic Playwright flow (label-driven fill + resume upload)."""
from __future__ import annotations

from .base import SubmissionContext, SubmissionResult
from .generic_browser import GenericBrowserAdapter


class SmartRecruitersAdapter:
    ats_type = "smartrecruiters"

    async def submit(self, ctx: SubmissionContext) -> SubmissionResult:
        url = ctx.job.get("apply_url") or ctx.job.get("url")
        if not url:
            return SubmissionResult(status="failed", step_reached="init", error="no apply url")
        return await GenericBrowserAdapter(ats_type=self.ats_type).submit(ctx, url=url)
