"""Workday (Playwright).

careers URL -> Apply -> Apply Manually -> create account (vault) or sign in -> [OTP / verify email] ->
My Information -> My Experience (work, education, resume, links) -> Application Questions -> Voluntary
Disclosures -> Self Identify -> Review -> Submit.

Selectors come from workday_fields.yaml (data-automation-id first). Every page transition is screenshotted;
the confirmation page is captured full-page. Unknown questions go through the Answer Engine; unresolved ->
needs_human (screenshot, requeue after the operator answers). OTP screens register a session and wait 120s.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import yaml

from app.accounts.vault import get_or_create_account, touch
from app.config import PACKAGE_ROOT, get_settings
from app.logging import get_logger
from app.otp.registry import register_session, wait_for_value

from .base import FormQuestion, NeedsHuman, NeedsOTP, SubmissionContext, SubmissionResult, TransientError
from .browser import browser_page, human_type, pause, snap
from .fields import resolve_question, standard_values

log = get_logger("submission.workday")
FIELDS: dict = yaml.safe_load((Path(__file__).parent / "workday_fields.yaml").read_text(encoding="utf-8"))
_MONTHS = {"01": "January", "02": "February", "03": "March", "04": "April", "05": "May", "06": "June", "07": "July", "08": "August", "09": "September", "10": "October", "11": "November", "12": "December"}


class WorkdayAdapter:
    ats_type = "workday"

    # ---------------------------------------------------------------- helpers
    async def _first(self, page, key: str, section: str, timeout: int = 2500):
        """First visible locator among the candidate selectors for FIELDS[section][key]."""
        for sel in FIELDS[section][key]:
            try:
                all_ = page.locator(sel)
                for i in range(min(await all_.count(), 12)):
                    loc = all_.nth(i)
                    if await loc.is_visible():
                        return loc
            except Exception as e:  # noqa: BLE001
                log.debug("selector_probe_failed", selector=sel, error=str(e)[:200])
                continue
        log.debug("selector_not_found", section=section, key=key, url=page.url)
        return None

    async def _fill(self, page, key: str, section: str, value: str | None, required: bool = False) -> bool:
        if not value:
            return False
        loc = await self._first(page, key, section)
        if loc is None:
            if required:
                raise NeedsHuman(f"workday field {section}.{key} not found", step=section)
            return False
        tag = await loc.evaluate("e => e.tagName.toLowerCase()")
        if tag == "select":
            await loc.select_option(label=value)
        elif tag == "button":
            await self._choose_dropdown(page, loc, value)
        else:
            await human_type(loc, value)
        await pause(0.2, 0.6)
        return True

    async def _choose_dropdown(self, page, button, value: str) -> bool:
        """Workday dropdowns are buttons that open a listbox of [role=option]; some are typeahead prompts."""
        await button.click()
        await pause(0.3, 0.7)
        listbox = page.locator('[role="listbox"], [data-automation-id="menuList"], ul[role="menu"]').last
        option = listbox.locator('[role="option"], li[data-automation-id="menuItem"], li').filter(has_text=re.compile(rf"^\s*{re.escape(value)}\s*$", re.I)).first
        if not await option.count():
            option = listbox.locator('[role="option"], li').filter(has_text=re.compile(re.escape(value), re.I)).first
        if await option.count():
            await option.click()
            return True
        # typeahead: type into the focused search box
        await page.keyboard.type(value[:40])
        await pause(0.4, 0.9)
        option = page.locator('[role="option"], li[data-automation-id="menuItem"]').filter(has_text=re.compile(re.escape(value), re.I)).first
        if await option.count():
            await option.click()
            return True
        await page.keyboard.press("Escape")
        return False

    async def _click(self, page, key: str, section: str, required: bool = True) -> bool:
        loc = await self._first(page, key, section)
        if loc is None:
            if required:
                raise NeedsHuman(f"workday control {section}.{key} not found", step=section)
            return False
        await loc.click()
        await pause(0.5, 1.2)
        return True

    async def _next(self, page, ctx: SubmissionContext, step: str) -> None:
        await snap(page, ctx.session, ctx.application.id, step)
        await self._click(page, "next", "nav")
        try:
            await page.wait_for_load_state("networkidle", timeout=20_000)
        except Exception:  # noqa: BLE001
            pass
        await pause(0.8, 1.6)
        err = await self._first(page, "page_error", "nav", timeout=800)
        if err is not None:
            text = (await err.inner_text()).strip()
            if text:
                shot = await snap(page, ctx.session, ctx.application.id, f"{step}_error")
                raise NeedsHuman(f"workday validation error on {step}: {text[:300]}", step=step)

    async def _page_text(self, page) -> str:
        try:
            return (await page.inner_text("body")).lower()
        except Exception:  # noqa: BLE001
            return ""

    async def _current_step(self, page) -> str:
        """Identify the wizard page from progress bar / headings / distinctive fields."""
        text = await self._page_text(page)
        for key, marker in (
            ("confirmation", FIELDS["confirmation"]["text"]),
        ):
            if any(m in text for m in marker):
                return key
        for sel in FIELDS["nav"]["progress"]:
            bars = page.locator(sel)
            for i in range(await bars.count()):
                loc = bars.nth(i)
                if not await loc.is_visible():
                    continue
                t = (await loc.inner_text()).lower()
                for name, key in (("my information", "my_information"), ("my experience", "my_experience"), ("application questions", "questions"), ("voluntary disclosures", "disclosures"), ("self identify", "self_identify"), ("review", "review")):
                    if re.search(rf"{name}[^\n·]*\((current|active)\)|(current|active)[^\n·]*{name}", t):
                        return key
        if await self._first(page, "first_name", "my_information", timeout=600):
            return "my_information"
        if await self._first(page, "resume_input", "my_experience", timeout=600) or await self._first(page, "work_add", "my_experience", timeout=600):
            return "my_experience"
        if await self._first(page, "gender", "disclosures", timeout=600) or await self._first(page, "terms", "disclosures", timeout=600):
            return "disclosures"
        if await self._first(page, "name", "self_identify", timeout=600):
            return "self_identify"
        if await self._first(page, "otp_input", "auth", timeout=600):
            return "otp"
        if await self._first(page, "email", "auth", timeout=600):
            return "auth"
        if "review" in text and await self._first(page, "next", "nav", timeout=600):
            return "review"
        if await page.locator(FIELDS["questions"]["container"][0]).count():
            return "questions"
        return "unknown"

    # ---------------------------------------------------------------- flow
    async def submit(self, ctx: SubmissionContext) -> SubmissionResult:
        job = ctx.job
        url = job.get("apply_url") or job.get("url")
        if not url:
            return SubmissionResult(status="failed", step_reached="init", error="no workday url")
        app_id = ctx.application.id
        try:
            async with browser_page(ctx.client.id) as page:
                await page.goto(url, wait_until="domcontentloaded")
                await pause(1.0, 2.0)
                await snap(page, ctx.session, app_id, "job_page")
                await self._start_application(page, ctx)
                await self._authenticate(page, ctx)
                visited: dict[str, int] = {}
                for _ in range(14):  # hard stop against loops
                    step = await self._current_step(page)
                    visited[step] = visited.get(step, 0) + 1
                    if visited[step] > 3:
                        shot = await snap(page, ctx.session, app_id, f"stuck_{step}")
                        raise NeedsHuman(f"stuck on workday step {step}", step=step)
                    log.info("workday_step", step=step)
                    if step == "confirmation":
                        shot = await snap(page, ctx.session, app_id, "confirmation", full_page=True)
                        text = await page.inner_text("body")
                        m = re.search("|".join(re.escape(t) for t in FIELDS["confirmation"]["text"]), text, re.I)
                        return SubmissionResult(status="success", step_reached="submitted", screenshot_path=shot, confirmation_text=text[max(0, (m.start() if m else 0) - 80):(m.end() if m else 0) + 300].strip())
                    handler = {
                        "auth": self._authenticate,
                        "otp": self._handle_otp,
                        "my_information": self._my_information,
                        "my_experience": self._my_experience,
                        "questions": self._questions,
                        "disclosures": self._disclosures,
                        "self_identify": self._self_identify,
                        "review": self._review,
                    }.get(step)
                    if handler is None:
                        # unknown page: try generic questions then Next
                        await self._questions(page, ctx, step_name="unknown")
                        continue
                    await handler(page, ctx)
                shot = await snap(page, ctx.session, app_id, "gave_up")
                return SubmissionResult(status="needs_human", step_reached="wizard", screenshot_path=shot, error="wizard did not reach confirmation")
        except (NeedsHuman, NeedsOTP):
            raise
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if any(t in msg.lower() for t in ("timeout", "net::", "navigation", "connection", "target closed")):
                raise TransientError(msg) from e
            raise

    async def _start_application(self, page, ctx: SubmissionContext) -> None:
        if await self._first(page, "email", "auth", timeout=800):
            return  # already on sign-in
        await self._click(page, "apply_button", "apply", required=False)
        await pause()
        await self._click(page, "apply_manually", "apply", required=False)
        await pause()
        await snap(page, ctx.session, ctx.application.id, "apply_clicked")

    async def _authenticate(self, page, ctx: SubmissionContext) -> None:
        if not await self._first(page, "email", "auth", timeout=3000):
            return
        slug = ctx.job.get("company_slug") or ctx.job.get("company") or "workday"
        acct, password = get_or_create_account(ctx.session, ctx.client, "workday", slug)
        email = acct.username
        text = await self._page_text(page)
        creating = "create account" in text and ("verify" in text or await self._first(page, "verify_password", "auth", timeout=600) is not None)
        if not creating and await self._first(page, "create_account_link", "auth", timeout=800) and acct.last_used is None:
            await self._click(page, "create_account_link", "auth")
            creating = True
        await self._fill(page, "email", "auth", email, required=True)
        await self._fill(page, "password", "auth", password, required=True)
        if await self._first(page, "verify_password", "auth", timeout=600):
            await self._fill(page, "verify_password", "auth", password)
            creating = True
        cb = await self._first(page, "create_account_checkbox", "auth", timeout=500)
        if cb is not None and not await cb.is_checked():
            await cb.check()
        await snap(page, ctx.session, ctx.application.id, "auth_filled")
        if creating:
            await self._click(page, "create_submit", "auth")
        else:
            await self._click(page, "sign_in_submit", "auth")
        await pause(1.0, 2.0)
        err = await self._first(page, "error", "auth", timeout=800)
        if err is not None:
            msg = (await err.inner_text()).strip().lower()
            if msg:
                if creating and ("already" in msg or "exists" in msg or "in use" in msg):
                    # account exists from a previous run: switch to sign in
                    await self._click(page, "sign_in_link", "auth", required=False)
                    await self._fill(page, "email", "auth", email, required=True)
                    await self._fill(page, "password", "auth", password, required=True)
                    await self._click(page, "sign_in_submit", "auth")
                    await pause(1.0, 2.0)
                    err2 = await self._first(page, "error", "auth", timeout=800)
                    if err2 is not None and (await err2.inner_text()).strip():
                        await snap(page, ctx.session, ctx.application.id, "auth_error")
                        raise NeedsHuman(f"workday sign-in failed: {(await err2.inner_text()).strip()[:200]}", step="auth")
                else:
                    await snap(page, ctx.session, ctx.application.id, "auth_error")
                    raise NeedsHuman(f"workday auth error: {msg[:200]}", step="auth")
        touch(ctx.session, acct)
        await snap(page, ctx.session, ctx.application.id, "auth_done")

    async def _handle_otp(self, page, ctx: SubmissionContext) -> None:
        settings = get_settings()
        domain = re.sub(r"^https?://", "", ctx.job.get("url") or "").split("/")[0] or "myworkday.com"
        otp = register_session(ctx.session, ctx.client.id, ctx.client.alias_email or ctx.client.real_email, domain, application_id=ctx.application.id, kind="any")
        ctx.session.commit()
        await snap(page, ctx.session, ctx.application.id, "otp_waiting")
        log.info("workday_otp_wait", session_id=otp.session_id)
        got = await wait_for_value(otp.session_id, settings.otp_wait_seconds)
        if got is None:
            await snap(page, ctx.session, ctx.application.id, "otp_timeout")
            raise NeedsOTP(otp.session_id, step="otp")
        kind, value = got
        if kind == "verification_link":
            await page.goto(value, wait_until="domcontentloaded")
            await pause(1.0, 2.0)
            await snap(page, ctx.session, ctx.application.id, "verification_link_opened")
            return
        await self._fill(page, "otp_input", "auth", value, required=True)
        await self._click(page, "otp_submit", "auth")
        await pause(1.0, 2.0)
        await snap(page, ctx.session, ctx.application.id, "otp_submitted")

    async def _my_information(self, page, ctx: SubmissionContext) -> None:
        std = standard_values(ctx)
        p = ctx.profile
        await self._fill(page, "source", "my_information", "Company Website")  # optional dropdown; best effort
        no = await self._first(page, "previous_worker_no", "my_information", timeout=600)
        if no is not None:
            await no.check()
        await self._fill(page, "country", "my_information", p.address.country)
        await self._fill(page, "first_name", "my_information", p.first_name, required=True)
        await self._fill(page, "last_name", "my_information", p.last_name, required=True)
        await self._fill(page, "address_line1", "my_information", p.address.line1 or "")
        await self._fill(page, "city", "my_information", p.address.city or "")
        await self._fill(page, "state", "my_information", _state_name(p.address.state))
        await self._fill(page, "postal_code", "my_information", p.address.postal_code or "")
        await self._fill(page, "phone_type", "my_information", "Mobile")
        await self._fill(page, "phone_number", "my_information", re.sub(r"[^\d]", "", std["phone"])[-10:])
        await self._questions(page, ctx, step_name="my_information", advance=False)
        await self._next(page, ctx, "my_information")

    async def _my_experience(self, page, ctx: SubmissionContext) -> None:
        p, std = ctx.profile, standard_values(ctx)
        # work history: newest first, as Workday expects
        if await self._first(page, "work_add", "my_experience", timeout=1500):
            for i, w in enumerate(p.work_history[:4]):
                await self._click(page, "work_add" if i == 0 else "work_add_another", "my_experience", required=(i == 0))
                blocks = page.locator('[data-automation-id="workExperienceSection"] [data-automation-id^="workExperience-"], [data-automation-id^="workExperience-"]')
                block = blocks.nth(i) if await blocks.count() > i else page
                await self._fill_in(block, "job_title", w.title)
                await self._fill_in(block, "company", w.company)
                await self._fill_in(block, "location", w.location or "")
                current = not w.end or w.end.lower() in ("present", "current", "now")
                cur = block.locator(FIELDS["my_experience"]["current"][0]).first
                if current and await cur.count():
                    await cur.check()
                await self._fill_date(block, "startDate", w.start)
                if not current:
                    await self._fill_date(block, "endDate", w.end)
                await self._fill_in(block, "description", "\n".join(f"• {b}" for b in w.bullets[:5]))
        if await self._first(page, "edu_add", "my_experience", timeout=1000) and p.education:
            for i, e in enumerate(p.education[:2]):
                await self._click(page, "edu_add", "my_experience", required=False)
                blocks = page.locator('[data-automation-id^="education-"]')
                block = blocks.nth(i) if await blocks.count() > i else page
                await self._fill_in(block, "school", e.school)
                await self._fill_in(block, "degree", _degree_label(e.degree))
                await self._fill_in(block, "field_of_study", e.field or "")
        resume = await self._first(page, "resume_input", "my_experience", timeout=1500)
        if resume is not None:
            await resume.set_input_files({"name": "resume.pdf", "mimeType": "application/pdf", "buffer": ctx.resume_pdf})
            await pause(1.0, 2.0)
        await self._fill(page, "linkedin", "my_experience", std["linkedin"])
        await self._fill(page, "website", "my_experience", std["portfolio"] or std["github"])
        await self._questions(page, ctx, step_name="my_experience", advance=False)
        await self._next(page, ctx, "my_experience")

    async def _fill_in(self, scope, key: str, value: str) -> None:
        if not value:
            return
        for sel in FIELDS["my_experience"][key]:
            loc = scope.locator(sel).last
            if await loc.count():
                tag = await loc.evaluate("e => e.tagName.toLowerCase()")
                if tag == "select":
                    try:
                        await loc.select_option(label=value)
                    except Exception:  # noqa: BLE001
                        pass
                elif tag == "button":
                    await loc.click()
                    opt = scope.page.locator('[role="option"], li').filter(has_text=re.compile(re.escape(value), re.I)).first if hasattr(scope, "page") else None
                    if opt is not None and await opt.count():
                        await opt.click()
                else:
                    await human_type(loc, value)
                await pause(0.1, 0.4)
                return

    async def _fill_date(self, scope, prefix: str, value: str | None) -> None:
        if not value:
            return
        m = re.match(r"(\d{4})(?:-(\d{2}))?", str(value))
        if not m:
            return
        year, month = m.group(1), m.group(2) or "01"
        for key, val in ((f'input[data-automation-id="{prefix}-dateSectionMonth-input"]', month), (f'input[data-automation-id="{prefix}-dateSectionYear-input"]', year)):
            loc = scope.locator(key).last
            if await loc.count():
                await loc.click()
                await loc.type(val, delay=80)
                await pause(0.1, 0.3)

    async def _questions(self, page, ctx: SubmissionContext, step_name: str = "questions", advance: bool = True) -> None:
        """Generic walker over [data-automation-id^=formField-] containers: label -> control -> value."""
        containers = page.locator(FIELDS["questions"]["container"][0])
        n = await containers.count()
        for i in range(n):
            c = containers.nth(i)
            try:
                if not await c.is_visible():
                    continue
                label = (await c.locator("label, legend, [data-automation-id='richText'], [data-automation-id='formLabel']").first.inner_text(timeout=800)).strip() if await c.locator("label, legend, [data-automation-id='richText'], [data-automation-id='formLabel']").count() else (await c.inner_text()).split("\n")[0].strip()
            except Exception:  # noqa: BLE001
                continue
            if not label or len(label) > 400:
                continue
            required = "*" in label or bool(await c.locator("[aria-required='true'], [required]").count())
            label_clean = label.replace("*", "").strip()
            text_input = c.locator("input[type=text], input:not([type]), input[type=tel], input[type=email], input[type=number]").first
            textarea = c.locator("textarea").first
            select = c.locator("select").first
            button = c.locator("button[aria-haspopup='listbox'], button[data-automation-id]:not([data-automation-id*='Add']):not([data-automation-id*='delete'])").first
            radios = c.locator("input[type=radio]")
            checkbox = c.locator("input[type=checkbox]").first
            try:
                if await text_input.count() and await text_input.is_visible() and not (await text_input.input_value()):
                    if await text_input.get_attribute("data-automation-id") and re.search(r"(firstName|lastName|phone|postalCode|city|addressLine|email|linkedin|website|jobTitle|company|school|dateSection)", await text_input.get_attribute("data-automation-id") or ""):
                        continue  # handled by the dedicated page handlers
                    q = FormQuestion(name=label_clean, label=label_clean, type="text", required=required)
                    v = resolve_question(ctx, q)
                    if v:
                        await human_type(text_input, v)
                elif await textarea.count() and await textarea.is_visible() and not (await textarea.input_value()):
                    q = FormQuestion(name=label_clean, label=label_clean, type="textarea", required=required)
                    v = resolve_question(ctx, q)
                    if v:
                        await human_type(textarea, v)
                elif await select.count() and await select.is_visible():
                    opts = [(o.strip(), o.strip()) for o in await select.locator("option").all_inner_texts() if o.strip() and not re.match(r"^(select|choose|--)", o.strip(), re.I)]
                    cur = (await select.input_value()).strip()
                    if cur and cur not in ("", "0") and not re.match(r"^(select|choose|--|please)", cur, re.I):
                        continue
                    q = FormQuestion(name=label_clean, label=label_clean, type="select", required=required, options=opts)
                    v = resolve_question(ctx, q)
                    if v:
                        await select.select_option(label=v)
                elif await radios.count():
                    labels = []
                    for j in range(await radios.count()):
                        r = radios.nth(j)
                        rid = await r.get_attribute("id")
                        lab = (await c.locator(f"label[for='{rid}']").inner_text()).strip() if rid and await c.locator(f"label[for='{rid}']").count() else (await r.get_attribute("value") or "")
                        labels.append((lab, await r.get_attribute("value") or lab))
                    if any([await radios.nth(j).is_checked() for j in range(await radios.count())]):
                        continue
                    q = FormQuestion(name=label_clean, label=label_clean, type="select", required=required, options=labels)
                    v = resolve_question(ctx, q)
                    if v:
                        idx = next((j for j, (lab, val) in enumerate(labels) if val == v or lab == v), None)
                        if idx is not None:
                            await radios.nth(idx).check()
                elif await button.count() and await button.is_visible():
                    current = (await button.inner_text()).strip()
                    if current and not re.match(r"^(select one|select|choose|--)", current, re.I):
                        continue
                    await button.click()
                    await pause(0.3, 0.6)
                    opts = [o.strip() for o in await page.locator("[role='listbox'] [role='option'], [data-automation-id='menuList'] li").all_inner_texts() if o.strip()]
                    await page.keyboard.press("Escape")
                    q = FormQuestion(name=label_clean, label=label_clean, type="select", required=required, options=[(o, o) for o in opts])
                    v = resolve_question(ctx, q)
                    if v:
                        await self._choose_dropdown(page, button, v)
                elif await checkbox.count() and await checkbox.is_visible() and not await checkbox.is_checked():
                    q = FormQuestion(name=label_clean, label=label_clean, type="checkbox", required=required)
                    v = resolve_question(ctx, q)
                    if v:
                        await checkbox.check()
            except NeedsHuman:
                await snap(page, ctx.session, ctx.application.id, f"{step_name}_needs_human")
                raise
        if advance:
            await self._next(page, ctx, step_name)

    async def _disclosures(self, page, ctx: SubmissionContext) -> None:
        eeo = ctx.profile.eeo
        await self._fill(page, "gender", "disclosures", _eeo_option(eeo.gender, ["Male", "Female", "I do not wish to answer", "Decline"]))
        await self._fill(page, "hispanic", "disclosures", "No" if "decline" not in (eeo.race or "").lower() and "hispanic" not in (eeo.race or "").lower() else "I do not wish to answer")
        await self._fill(page, "ethnicity", "disclosures", _eeo_option(eeo.race, ["I do not wish to answer", "Decline"]))
        await self._fill(page, "veteran", "disclosures", _eeo_option(eeo.veteran, ["I am not a protected veteran", "I do not wish to answer", "Decline"]))
        terms = await self._first(page, "terms", "disclosures", timeout=800)
        if terms is not None and not await terms.is_checked():
            await terms.check()
        await self._questions(page, ctx, step_name="disclosures", advance=False)
        await self._next(page, ctx, "disclosures")

    async def _self_identify(self, page, ctx: SubmissionContext) -> None:
        p = ctx.profile
        await self._fill(page, "name", "self_identify", f"{p.first_name} {p.last_name}")
        today = datetime.now()
        for key, val in (("date_month", f"{today.month:02d}"), ("date_day", f"{today.day:02d}"), ("date_year", str(today.year))):
            loc = await self._first(page, key, "self_identify", timeout=500)
            if loc is not None:
                await loc.click()
                await loc.type(val, delay=80)
        want = "decline" if "decline" in (p.eeo.disability or "").lower() else ("yes" if re.search(r"\byes\b|have a disability", p.eeo.disability or "", re.I) else "no")
        opts = page.locator(FIELDS["self_identify"]["disability_options"][0])
        for i in range(await opts.count()):
            o = opts.nth(i)
            oid = await o.get_attribute("id")
            lab = (await page.locator(f"label[for='{oid}']").inner_text()).lower() if oid and await page.locator(f"label[for='{oid}']").count() else (await o.get_attribute("value") or "").lower()
            if (want == "decline" and ("do not want" in lab or "decline" in lab or "don't wish" in lab)) or (want == "no" and lab.startswith("no")) or (want == "yes" and lab.startswith("yes")):
                await o.check()
                break
        await self._questions(page, ctx, step_name="self_identify", advance=False)
        await self._next(page, ctx, "self_identify")

    async def _review(self, page, ctx: SubmissionContext) -> None:
        await snap(page, ctx.session, ctx.application.id, "review", full_page=True)
        await self._click(page, "next", "nav")  # labelled "Submit" on the review page
        try:
            await page.wait_for_load_state("networkidle", timeout=30_000)
        except Exception:  # noqa: BLE001
            pass
        await pause(1.5, 3.0)


def _state_name(abbr: str | None) -> str:
    from app.matching.filters import _STATE_ABBR

    if not abbr:
        return ""
    a = abbr.strip().lower()
    for name, ab in _STATE_ABBR.items():
        if ab == a:
            return name.title()
    return abbr


def _degree_label(degree: str | None) -> str:
    d = (degree or "").lower().replace(".", "")
    if d.startswith("bs") or d.startswith("ba") or "bachelor" in d or d.startswith("be"):
        return "Bachelor"
    if d.startswith("ms") or d.startswith("ma") or "master" in d:
        return "Master"
    if "phd" in d or "doctor" in d:
        return "Doctorate"
    if "associate" in d:
        return "Associate"
    return degree or ""


def _eeo_option(value: str | None, candidates: list[str]) -> str:
    v = (value or "").lower()
    if "decline" in v or not v:
        return next((c for c in candidates if "not wish" in c.lower() or "decline" in c.lower()), candidates[-1])
    for c in candidates:
        if c.lower() in v or v in c.lower():
            return c
    return value or ""
