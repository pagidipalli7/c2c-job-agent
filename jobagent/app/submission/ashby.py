"""Ashby. Tries the posting-api application submission first:
  GET  https://api.ashbyhq.com/posting-api/job-posting/{id}            -> applicationForm.sections[].fieldEntries[]
  POST https://api.ashbyhq.com/posting-api/job-posting/{id}/application (multipart: applicationForm JSON + files)
Boards that do not enable API applications (401/403/404) fall back to the generic Playwright flow."""
from __future__ import annotations

import json
import re

import httpx

from app.logging import get_logger

from .base import FormQuestion, NeedsHuman, SubmissionContext, SubmissionResult, TransientError
from .fields import resolve_question, standard_values
from .http import make_client

log = get_logger("submission.ashby")
API = "https://api.ashbyhq.com/posting-api/job-posting"


def parse_form(data: dict) -> list[FormQuestion]:
    out = []
    form = data.get("applicationForm") or {}
    for section in form.get("sections") or []:
        for entry in section.get("fieldEntries") or []:
            f = entry.get("field") or {}
            path, ftype = str(f.get("path") or ""), str(f.get("type") or "String")
            if not path:
                continue
            t = {"String": "text", "LongText": "textarea", "Email": "text", "Phone": "text", "File": "file", "ValueSelect": "select", "MultiValueSelect": "multiselect", "Boolean": "select", "Number": "text", "Date": "text"}.get(ftype, "text")
            options = [(str(v.get("label")), str(v.get("value"))) for v in (f.get("selectableValues") or []) if isinstance(v, dict)]
            if ftype == "Boolean":
                options = [("Yes", "true"), ("No", "false")]
            out.append(FormQuestion(name=path, label=str(f.get("title") or f.get("humanReadablePath") or path), type=t, required=bool(entry.get("isRequired") or f.get("isRequired"))))
            out[-1].options = options
    return out


class AshbyAdapter:
    ats_type = "ashby"

    async def submit(self, ctx: SubmissionContext) -> SubmissionResult:
        job = ctx.job
        posting_id = job.get("external_id")
        if not posting_id:
            return SubmissionResult(status="failed", step_reached="init", error="missing posting id")
        async with make_client() as client:
            try:
                r = await client.get(f"{API}/{posting_id}")
            except httpx.HTTPError as e:
                raise TransientError(str(e)) from e
            if r.status_code in (401, 403, 404, 405) or not r.headers.get("content-type", "").startswith("application/json"):
                log.info("ashby_api_unavailable", status=r.status_code, posting=posting_id)
                return await self._browser_fallback(ctx)
            if r.status_code >= 500 or r.status_code == 429:
                raise TransientError(f"{r.status_code} fetching form")
            questions = parse_form(r.json())
            if not questions:
                return await self._browser_fallback(ctx)
            std = standard_values(ctx)
            submissions: list[dict] = []
            files: dict = {}
            for q in questions:
                if q.type == "file":
                    if re.search(r"resume|cv", q.name + q.label, re.I):
                        files["resume"] = ("resume.pdf", ctx.resume_pdf, "application/pdf")
                        submissions.append({"path": q.name, "value": "resume"})
                    continue
                sysfield = {"_systemfield_name": std["full_name"], "_systemfield_email": std["email"], "_systemfield_phone": std["phone"], "_systemfield_location": std["location"]}
                if q.name in sysfield and sysfield[q.name]:
                    submissions.append({"path": q.name, "value": sysfield[q.name]})
                    continue
                value = resolve_question(ctx, q)
                if value is None:
                    if q.required:
                        raise NeedsHuman(f"no value for required field {q.label!r}", step="questions")
                    continue
                if q.type == "multiselect":
                    value = [value]
                elif q.options and value in ("true", "false"):
                    value = value == "true"
                submissions.append({"path": q.name, "value": value})
            try:
                resp = await client.post(f"{API}/{posting_id}/application", data={"applicationForm": json.dumps({"fieldSubmissions": submissions})}, files=files)
            except httpx.HTTPError as e:
                raise TransientError(str(e)) from e
        if resp.status_code >= 500 or resp.status_code == 429:
            raise TransientError(f"{resp.status_code} on submit")
        if resp.status_code in (401, 403, 404, 405):
            return await self._browser_fallback(ctx)
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code < 300 and body.get("success", True) and not body.get("errors"):
            log.info("ashby_submitted", app_id=ctx.application.id, posting=posting_id)
            return SubmissionResult(status="success", step_reached="submitted", confirmation_text=json.dumps(body)[:500] or f"HTTP {resp.status_code}")
        return SubmissionResult(status="failed", step_reached="submit", error=f"HTTP {resp.status_code}: {json.dumps(body)[:300] or resp.text[:300]}")

    async def _browser_fallback(self, ctx: SubmissionContext) -> SubmissionResult:
        from .generic_browser import GenericBrowserAdapter

        url = ctx.job.get("apply_url") or ctx.job.get("url")
        if not url:
            return SubmissionResult(status="failed", step_reached="fallback", error="no apply url for browser fallback")
        return await GenericBrowserAdapter(ats_type="ashby").submit(ctx, url=url)
