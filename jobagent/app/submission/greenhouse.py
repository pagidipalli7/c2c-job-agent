"""Greenhouse (HTTP, no browser).

1. GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{id}?questions=true  -> questions + field names
2. GET https://boards.greenhouse.io/{slug}/jobs/{id}  -> authenticity_token + cookies of the hosted form
3. multipart POST https://boards.greenhouse.io/{slug}/jobs/{id} with job_application[...] fields + resume
   (the hosted form and the Job Board API use identical field names, e.g.
    job_application[first_name], job_application[answers_attributes][0][text_value]).
Confirmation = response text containing the thank-you banner or a /confirmation redirect.
Quirk: boards protected by reCAPTCHA return the form again -> needs_human (see README).
"""
from __future__ import annotations

import re

import httpx
from bs4 import BeautifulSoup

from app.logging import get_logger

from .base import FormQuestion, NeedsHuman, SubmissionContext, SubmissionResult, TransientError
from .fields import resolve_question, standard_values
from .http import make_client, to_form

log = get_logger("submission.greenhouse")
API = "https://boards-api.greenhouse.io/v1/boards"
HOST = "https://boards.greenhouse.io"
_SUCCESS = re.compile(r"thank you for applying|application has been submitted|application-confirmation|/confirmation", re.I)
_CAPTCHA = re.compile(r"recaptcha|g-recaptcha|hcaptcha", re.I)


def parse_questions(data: dict) -> list[FormQuestion]:
    out: list[FormQuestion] = []
    for group in ("questions", "location_questions", "compliance"):
        items = data.get(group) or []
        for q in items:
            if group == "compliance":
                for cq in q.get("questions") or []:
                    out.extend(_fields_for(cq))
            else:
                out.extend(_fields_for(q))
    return out


def _fields_for(q: dict) -> list[FormQuestion]:
    out = []
    label = str(q.get("label") or "")
    required = bool(q.get("required"))
    for f in q.get("fields") or []:
        ftype = str(f.get("type") or "input_text")
        t = {"input_text": "text", "textarea": "textarea", "input_file": "file", "input_hidden": "hidden", "multi_value_single_select": "select", "multi_value_multi_select": "multiselect"}.get(ftype, "text")
        options = [(str(v.get("label")), str(v.get("value"))) for v in (f.get("values") or []) if isinstance(v, dict)]
        out.append(FormQuestion(name=str(f.get("name")), label=label, type=t, required=required, options=options))
    return out


class GreenhouseAdapter:
    ats_type = "greenhouse"

    async def submit(self, ctx: SubmissionContext) -> SubmissionResult:
        job = ctx.job
        slug, job_id = job.get("company_slug"), job.get("external_id")
        if not slug or not job_id:
            return SubmissionResult(status="failed", step_reached="init", error="missing slug/external_id in job snapshot")
        async with make_client() as client:
            try:
                r = await client.get(f"{API}/{slug}/jobs/{job_id}", params={"questions": "true"})
            except httpx.HTTPError as e:
                raise TransientError(str(e)) from e
            if r.status_code == 404:
                return SubmissionResult(status="failed", step_reached="fetch_questions", error="job no longer exists (404)")
            if r.status_code >= 500 or r.status_code == 429:
                raise TransientError(f"{r.status_code} fetching questions")
            questions = parse_questions(r.json())
            if not questions:
                return SubmissionResult(status="failed", step_reached="fetch_questions", error="no questions returned; board may not accept API applications")

            # build the multipart body
            std = standard_values(ctx)
            data: list[tuple[str, str]] = []
            files: dict = {}
            for q in questions:
                if q.type == "file":
                    if re.search(r"resume", q.name, re.I):
                        files[q.name] = ("resume.pdf", ctx.resume_pdf, "application/pdf")
                    continue  # cover letter optional
                if q.type == "hidden":
                    continue
                value = resolve_question(ctx, q)  # may raise NeedsHuman
                if value is None:
                    if q.required:
                        raise NeedsHuman(f"no value for required field {q.label!r}", step="questions")
                    continue
                if q.type == "multiselect":
                    data.append((q.name + "[]", value))
                else:
                    data.append((q.name, value))
            names = {n for n, _ in data}
            for key, fname in (("first_name", "job_application[first_name]"), ("last_name", "job_application[last_name]"), ("email", "job_application[email]"), ("phone", "job_application[phone]")):
                if fname not in names and std.get(key):
                    data.append((fname, std[key]))

            # hosted form token
            try:
                page = await client.get(f"{HOST}/{slug}/jobs/{job_id}")
            except httpx.HTTPError as e:
                raise TransientError(str(e)) from e
            if page.status_code >= 500:
                raise TransientError(f"{page.status_code} fetching hosted form")
            soup = BeautifulSoup(page.text, "html.parser")
            token = soup.find("input", {"name": "authenticity_token"})
            meta = soup.find("meta", {"name": "csrf-token"})
            token_val = (token.get("value") if token else None) or (meta.get("content") if meta else None)
            if token_val:
                data.append(("authenticity_token", token_val))
            if _CAPTCHA.search(page.text) and "g-recaptcha-response" in page.text:
                raise NeedsHuman("board requires reCAPTCHA; submit manually or use browser flow", step="captcha")

            try:
                resp = await client.post(f"{HOST}/{slug}/jobs/{job_id}", data=to_form(data), files=files, headers={"Referer": f"{HOST}/{slug}/jobs/{job_id}"})
            except httpx.HTTPError as e:
                raise TransientError(str(e)) from e
        if resp.status_code >= 500 or resp.status_code == 429:
            raise TransientError(f"{resp.status_code} on submit")
        text = resp.text
        if resp.status_code in (200, 201, 302) and (_SUCCESS.search(text) or _SUCCESS.search(str(resp.url)) or _looks_json_ok(resp)):
            conf = _confirmation_text(text) or f"HTTP {resp.status_code} {resp.url}"
            log.info("greenhouse_submitted", app_id=ctx.application.id, slug=slug, job_id=job_id)
            return SubmissionResult(status="success", step_reached="submitted", confirmation_text=conf)
        if _CAPTCHA.search(text):
            return SubmissionResult(status="needs_human", step_reached="captcha", error="captcha challenge on submit")
        errors = [e.get_text(" ", strip=True) for e in BeautifulSoup(text, "html.parser").select(".field_with_errors, .error, .errors, .alert")]
        return SubmissionResult(status="failed", step_reached="submit", error=f"HTTP {resp.status_code}: {'; '.join(errors)[:300] or text[:300]}")


def _looks_json_ok(resp: httpx.Response) -> bool:
    try:
        j = resp.json()
    except ValueError:
        return False
    return isinstance(j, dict) and (j.get("success") is True or j.get("status") in ("success", "ok") or "id" in j)


def _confirmation_text(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    for sel in ("#application_confirmation", ".application-confirmation", "h1", "h2"):
        el = soup.select_one(sel)
        if el and _SUCCESS.search(el.get_text(" ", strip=True)):
            return el.get_text(" ", strip=True)[:500]
    m = _SUCCESS.search(html)
    return html[max(0, m.start() - 60): m.end() + 120].strip() if m else None
