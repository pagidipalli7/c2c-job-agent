"""Generic label-driven Playwright flow for simple single-page application forms (Ashby fallback and any
other hosted form). Fills inputs by their visible label, uploads the resume, answers custom questions through
the Answer Engine, screenshots every step, and confirms by looking for a thank-you message."""
from __future__ import annotations

import re

from app.logging import get_logger

from .base import FormQuestion, NeedsHuman, SubmissionContext, SubmissionResult, TransientError
from .browser import browser_page, human_type, pause, snap
from .fields import resolve_question, standard_values

log = get_logger("submission.generic")
_SUCCESS = re.compile(r"thank you|thanks for applying|application (has been )?(submitted|received)|we('ve| have) received your application", re.I)


class GenericBrowserAdapter:
    def __init__(self, ats_type: str = "generic"):
        self.ats_type = ats_type

    async def submit(self, ctx: SubmissionContext, url: str) -> SubmissionResult:
        app_id = ctx.application.id
        std = standard_values(ctx)
        try:
            async with browser_page(ctx.client.id) as page:
                await page.goto(url, wait_until="domcontentloaded")
                await pause()
                await snap(page, ctx.session, app_id, "form")
                # some boards show the form behind an "Apply" button
                apply_btn = page.get_by_role("button", name=re.compile(r"^apply", re.I)).first
                if await apply_btn.count() and not await page.locator("input[type=file]").count():
                    await apply_btn.click()
                    await pause()
                controls = await page.evaluate(_COLLECT_JS)
                for c in controls:
                    label = c["label"] or c["name"] or c["placeholder"] or ""
                    sel = f"[data-ja-idx='{c['idx']}']"
                    loc = page.locator(sel)
                    if c["type"] == "file":
                        if re.search(r"resume|cv", label + c["name"], re.I) or not c["name"]:
                            await loc.set_input_files({"name": "resume.pdf", "mimeType": "application/pdf", "buffer": ctx.resume_pdf})
                        continue
                    q = FormQuestion(name=c["name"] or label, label=label, type=c["type"], required=c["required"], options=[(o, o) for o in c["options"]])
                    if c["type"] in ("select",):
                        value = resolve_question(ctx, q)
                        if value:
                            await loc.select_option(label=value) if value in c["options"] else await loc.select_option(value)
                        continue
                    if c["type"] == "checkbox":
                        value = resolve_question(ctx, q)
                        if value:
                            await loc.check()
                        continue
                    if c["type"] == "radio":
                        value = resolve_question(ctx, q)
                        if value:
                            await page.locator(f"[data-ja-idx='{c['idx']}'][value='{value}']").check()
                        continue
                    key_value = None
                    for key in ("full_name", "first_name", "last_name", "email", "phone", "linkedin", "github", "portfolio", "location"):
                        if re.search(key.replace("_", " ?"), label, re.I) or (c["name"] and re.search(key.replace("_", ""), c["name"].replace("_", ""), re.I)):
                            key_value = std.get(key)
                            break
                    value = key_value or resolve_question(ctx, q)
                    if value:
                        await human_type(loc, value)
                        await pause(0.1, 0.4)
                await snap(page, ctx.session, app_id, "filled")
                submit = page.get_by_role("button", name=re.compile(r"submit|apply|send application", re.I)).first
                if not await submit.count():
                    submit = page.locator("button[type=submit], input[type=submit]").first
                await submit.click()
                await page.wait_for_load_state("networkidle", timeout=30_000)
                await pause(1.0, 2.0)
                body = await page.inner_text("body")
                shot = await snap(page, ctx.session, app_id, "confirmation", full_page=True)
                if _SUCCESS.search(body):
                    m = _SUCCESS.search(body)
                    return SubmissionResult(status="success", step_reached="submitted", screenshot_path=shot, confirmation_text=body[max(0, m.start() - 80): m.end() + 200].strip())
                errors = await page.locator("[aria-invalid='true'], .error, [class*=error]").all_inner_texts()
                return SubmissionResult(status="needs_human", step_reached="submit", screenshot_path=shot, error=f"no confirmation detected; errors={errors[:5]}")
        except NeedsHuman:
            raise
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if any(t in msg.lower() for t in ("timeout", "net::", "navigation", "connection")):
                raise TransientError(msg) from e
            raise


_COLLECT_JS = """
() => {
  const out = [];
  const els = Array.from(document.querySelectorAll('input, textarea, select'));
  let idx = 0;
  const seenRadio = new Set();
  for (const el of els) {
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (['hidden','submit','button','image','reset'].includes(type)) continue;
    if (el.disabled || el.offsetParent === null && type !== 'file') continue;
    if (type === 'radio' && seenRadio.has(el.name)) { el.setAttribute('data-ja-idx', String([...out].find(o => o.name===el.name && o.type==='radio').idx)); continue; }
    let label = '';
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) label = l.innerText.trim(); }
    if (!label) { const p = el.closest('label'); if (p) label = p.innerText.trim(); }
    if (!label) { const fs = el.closest('fieldset, div'); const lg = fs && fs.querySelector('legend, label, [class*=label]'); if (lg) label = lg.innerText.trim(); }
    el.setAttribute('data-ja-idx', String(idx));
    let options = [];
    if (el.tagName === 'SELECT') options = Array.from(el.options).filter(o => o.value).map(o => o.text.trim());
    if (type === 'radio') { seenRadio.add(el.name); options = Array.from(document.querySelectorAll(`input[type=radio][name="${CSS.escape(el.name)}"]`)).map(r => r.value); }
    out.push({idx, name: el.name || '', type: el.tagName === 'SELECT' ? 'select' : el.tagName === 'TEXTAREA' ? 'textarea' : type, label, placeholder: el.placeholder || '', required: el.required, options});
    idx++;
  }
  return out;
}
"""
