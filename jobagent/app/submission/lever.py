"""Lever (HTTP). GET https://jobs.lever.co/{slug}/{posting}/apply -> parse the form (standard fields, custom
question "cards", EEO selects, consent checkboxes) -> multipart POST back to the same URL.
Quirk: some boards enable hCaptcha (`h-captcha-response`) -> needs_human."""
from __future__ import annotations

import re

import httpx
from bs4 import BeautifulSoup

from app.logging import get_logger

from .base import FormQuestion, NeedsHuman, SubmissionContext, SubmissionResult, TransientError
from .fields import resolve_question, standard_values
from .http import make_client, to_form

log = get_logger("submission.lever")
HOST = "https://jobs.lever.co"
_SUCCESS = re.compile(r"application has been submitted|thanks? for applying|application received|/thanks", re.I)


def parse_form(html: str) -> tuple[list[FormQuestion], dict[str, str], str | None]:
    """Returns (questions, hidden_fields, action_url)."""
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form", id="application-form") or soup.find("form")
    if form is None:
        return [], {}, None
    hidden: dict[str, str] = {}
    questions: list[FormQuestion] = []
    seen: set[str] = set()
    for el in form.find_all(["input", "textarea", "select"]):
        name = el.get("name")
        if not name or name in seen:
            continue
        itype = (el.get("type") or el.name).lower()
        if itype == "hidden":
            hidden[name] = el.get("value") or ""
            continue
        if itype == "submit" or itype == "button":
            continue
        label = _label_for(form, el)
        required = el.has_attr("required") or "required" in (el.get("class") or []) or "*" in label
        if el.name == "select":
            options = []
            for o in el.find_all("option"):
                text = o.get_text(strip=True)
                val = o.get("value")
                if val is None:
                    val = text
                if val == "" or o.has_attr("disabled") or not text:
                    continue  # placeholder "Select..." entries
                options.append((text, val))
            questions.append(FormQuestion(name=name, label=label, type="select", required=required, options=options))
        elif itype == "radio":
            existing = next((q for q in questions if q.name == name), None)
            opt = (_label_for(form, el, radio=True), el.get("value") or "")
            if existing:
                existing.options.append(opt)
                continue
            questions.append(FormQuestion(name=name, label=label, type="select", required=required, options=[opt]))
            continue  # radio groups share a name; allow more options
        elif itype == "checkbox":
            questions.append(FormQuestion(name=name, label=label, type="checkbox", required=required, value=el.get("value") or "on"))
        elif itype == "file":
            questions.append(FormQuestion(name=name, label=label or name, type="file", required=required))
        elif el.name == "textarea":
            questions.append(FormQuestion(name=name, label=label, type="textarea", required=required))
        else:
            questions.append(FormQuestion(name=name, label=label, type="text", required=required))
        seen.add(name)
    return questions, hidden, form.get("action")


def _label_for(form, el, radio: bool = False) -> str:
    if radio:
        parent = el.find_parent("label")
        if parent:
            return parent.get_text(" ", strip=True)
    if el.get("id"):
        lab = form.find("label", {"for": el.get("id")})
        if lab:
            return lab.get_text(" ", strip=True)
    parent = el.find_parent(["label", "li", "div"])
    if parent:
        lab = parent.find(["label", "div"], class_=re.compile(r"label|question|text", re.I))
        if lab:
            return lab.get_text(" ", strip=True)
    return el.get("placeholder") or el.get("aria-label") or el.get("name") or ""


class LeverAdapter:
    ats_type = "lever"

    async def submit(self, ctx: SubmissionContext) -> SubmissionResult:
        job = ctx.job
        slug, posting = job.get("company_slug"), job.get("external_id")
        if not slug or not posting:
            return SubmissionResult(status="failed", step_reached="init", error="missing slug/posting id")
        url = f"{HOST}/{slug}/{posting}/apply"
        async with make_client() as client:
            try:
                page = await client.get(url)
            except httpx.HTTPError as e:
                raise TransientError(str(e)) from e
            if page.status_code == 404:
                return SubmissionResult(status="failed", step_reached="fetch_form", error="posting gone (404)")
            if page.status_code >= 500 or page.status_code == 429:
                raise TransientError(f"{page.status_code} fetching apply form")
            if "h-captcha" in page.text or "hcaptcha" in page.text.lower():
                raise NeedsHuman("Lever board uses hCaptcha; apply manually", step="captcha")
            questions, hidden, action = parse_form(page.text)
            if not questions:
                return SubmissionResult(status="failed", step_reached="fetch_form", error="could not parse apply form")
            std = standard_values(ctx)
            data: list[tuple[str, str]] = list(hidden.items())
            files: dict = {}
            for q in questions:
                if q.type == "file":
                    if re.search(r"resume|cv", q.name + q.label, re.I):
                        files[q.name] = ("resume.pdf", ctx.resume_pdf, "application/pdf")
                    continue
                if q.name == "name":
                    data.append((q.name, std["full_name"]))
                    continue
                if q.name in ("email", "phone", "org"):
                    v = {"email": std["email"], "phone": std["phone"], "org": std["current_company"]}[q.name]
                    if v:
                        data.append((q.name, v))
                    continue
                m = re.match(r"urls\[(.+)\]", q.name)
                if m:
                    key = m.group(1).lower()
                    v = std["linkedin"] if "linkedin" in key else std["github"] if "github" in key else std["portfolio"] if key in ("portfolio", "other", "website") else ""
                    if v:
                        data.append((q.name, v))
                    continue
                if q.name == "comments":
                    continue  # "Additional information" - optional, never fabricate
                value = resolve_question(ctx, q)
                if value is None:
                    if q.required:
                        raise NeedsHuman(f"no value for required field {q.label!r}", step="questions")
                    continue
                data.append((q.name, q.value if q.type == "checkbox" else value))
            post_url = action if action and action.startswith("http") else url
            try:
                resp = await client.post(post_url, data=to_form(data), files=files, headers={"Referer": url})
            except httpx.HTTPError as e:
                raise TransientError(str(e)) from e
        if resp.status_code >= 500 or resp.status_code == 429:
            raise TransientError(f"{resp.status_code} on submit")
        if resp.status_code < 400 and (_SUCCESS.search(resp.text) or _SUCCESS.search(str(resp.url)) or "/thanks" in str(resp.url)):
            log.info("lever_submitted", app_id=ctx.application.id, slug=slug, posting=posting)
            m = _SUCCESS.search(resp.text)
            return SubmissionResult(status="success", step_reached="submitted", confirmation_text=(resp.text[max(0, m.start() - 40): m.end() + 100].strip() if m else f"redirected to {resp.url}"))
        return SubmissionResult(status="failed", step_reached="submit", error=f"HTTP {resp.status_code}: {resp.text[:300]}")
