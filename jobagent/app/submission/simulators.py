"""Fake ATS application endpoints (httpx.MockTransport) mirroring the real request/response shapes the
HTTP adapters use. Used by tests and `scripts/run_worker.py --simulate`. Records every submission."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from email.parser import BytesParser
from email.policy import default as email_policy

import httpx

GH_QUESTIONS = {
    "questions": [
        {"label": "First Name", "required": True, "fields": [{"name": "job_application[first_name]", "type": "input_text"}]},
        {"label": "Last Name", "required": True, "fields": [{"name": "job_application[last_name]", "type": "input_text"}]},
        {"label": "Email", "required": True, "fields": [{"name": "job_application[email]", "type": "input_text"}]},
        {"label": "Phone", "required": False, "fields": [{"name": "job_application[phone]", "type": "input_text"}]},
        {"label": "Resume/CV", "required": True, "fields": [{"name": "job_application[resume]", "type": "input_file"}]},
        {"label": "LinkedIn Profile", "required": False, "fields": [{"name": "job_application[answers_attributes][0][text_value]", "type": "input_text"}]},
        {"label": "How did you hear about this job?", "required": True, "fields": [{"name": "job_application[answers_attributes][1][answer_selected_options_attributes][0][question_option_id]", "type": "multi_value_single_select", "values": [{"label": "LinkedIn", "value": 1}, {"label": "Company careers site", "value": 2}, {"label": "Referral", "value": 3}]}]},
        {"label": "Why do you want to work here?", "required": True, "fields": [{"name": "job_application[answers_attributes][2][text_value]", "type": "textarea"}]},
        {"label": "Are you legally authorized to work in the United States?", "required": True, "fields": [{"name": "job_application[answers_attributes][3][boolean_value]", "type": "multi_value_single_select", "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}]}]},
    ],
    "compliance": [
        {"type": "eeoc", "questions": [
            {"label": "Gender", "required": False, "fields": [{"name": "job_application[gender]", "type": "multi_value_single_select", "values": [{"label": "Male", "value": 1}, {"label": "Female", "value": 2}, {"label": "Decline To Self Identify", "value": 3}]}]},
            {"label": "Veteran Status", "required": False, "fields": [{"name": "job_application[veteran_status]", "type": "multi_value_single_select", "values": [{"label": "I am not a protected veteran", "value": 1}, {"label": "I identify as one or more of the classifications of a protected veteran", "value": 2}, {"label": "I don't wish to answer", "value": 3}]}]},
        ]},
    ],
}
GH_SALARY_Q = {"label": "What are your salary expectations?", "required": True, "fields": [{"name": "job_application[answers_attributes][9][text_value]", "type": "input_text"}]}

LEVER_FORM = """<html><body><form id="application-form" method="post" action="https://jobs.lever.co/{slug}/{pid}/apply" enctype="multipart/form-data">
<input type="hidden" name="csrf" value="tok-{pid}">
<label for="resume-upload-input">Resume/CV</label><input id="resume-upload-input" type="file" name="resume" required>
<label for="name-input">Full name</label><input id="name-input" name="name" required>
<label for="email-input">Email</label><input id="email-input" name="email" type="email" required>
<label for="phone-input">Phone</label><input id="phone-input" name="phone">
<label for="org-input">Current company</label><input id="org-input" name="org">
<label for="li">LinkedIn URL</label><input id="li" name="urls[LinkedIn]">
<label for="gh">GitHub URL</label><input id="gh" name="urls[GitHub]">
<label for="comments">Additional information</label><textarea id="comments" name="comments"></textarea>
<div class="application-question"><div class="application-label">Why are you interested in this role?</div><textarea name="cards[abc][field0]" required></textarea></div>
<div class="application-question"><div class="application-label">Will you now or in the future require sponsorship?</div>
<label><input type="radio" name="cards[abc][field1]" value="Yes">Yes</label><label><input type="radio" name="cards[abc][field1]" value="No">No</label></div>
{extra}
<label for="eeo-gender">Gender</label><select id="eeo-gender" name="eeo[gender]"><option value="">Select</option><option value="Male">Male</option><option value="Female">Female</option><option value="Decline to self identify">Decline to self identify</option></select>
<label><input type="checkbox" name="consent[marketing]" value="true"> I agree to the privacy policy</label>
<button type="submit">Submit application</button></form></body></html>"""
LEVER_CLEARANCE = '<div class="application-question"><div class="application-label">Do you hold an active security clearance?</div><input name="cards[abc][field2]" required></div>'

ASHBY_FORM = {"applicationForm": {"sections": [{"fieldEntries": [
    {"field": {"path": "_systemfield_name", "type": "String", "title": "Name"}, "isRequired": True},
    {"field": {"path": "_systemfield_email", "type": "Email", "title": "Email"}, "isRequired": True},
    {"field": {"path": "_systemfield_resume", "type": "File", "title": "Resume"}, "isRequired": True},
    {"field": {"path": "_systemfield_phone", "type": "Phone", "title": "Phone"}, "isRequired": False},
    {"field": {"path": "custom_linkedin", "type": "String", "title": "LinkedIn Profile"}, "isRequired": False},
    {"field": {"path": "custom_remote", "type": "Boolean", "title": "Are you comfortable working remotely?"}, "isRequired": True},
    {"field": {"path": "custom_source", "type": "ValueSelect", "title": "How did you hear about us?", "selectableValues": [{"label": "LinkedIn", "value": "li"}, {"label": "Company website", "value": "web"}]}, "isRequired": True},
]}]}}


@dataclass
class Submission:
    ats: str
    slug: str
    job_id: str
    fields: dict[str, list[str]]
    files: dict[str, bytes]


@dataclass
class SubmissionSimulator:
    salary_question_slugs: set[str] = field(default_factory=lambda: {"salaryco"})
    clearance_slugs: set[str] = field(default_factory=lambda: {"clearanceco"})
    captcha_slugs: set[str] = field(default_factory=lambda: {"captchaco"})
    flaky_slugs: set[str] = field(default_factory=lambda: {"flaky"})
    no_api_slugs: set[str] = field(default_factory=lambda: {"noapi"})
    always_fail_slugs: set[str] = field(default_factory=lambda: {"brokenco"})
    submissions: list[Submission] = field(default_factory=list)
    flaky_seen: set[str] = field(default_factory=set)

    def _parse_multipart(self, request: httpx.Request) -> tuple[dict[str, list[str]], dict[str, bytes]]:
        ctype = request.headers.get("content-type", "")
        fields: dict[str, list[str]] = {}
        files: dict[str, bytes] = {}
        if ctype.startswith("multipart/form-data"):
            raw = b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + request.content
            msg = BytesParser(policy=email_policy).parsebytes(raw)
            for part in msg.iter_parts():
                name = part.get_param("name", header="content-disposition")
                fname = part.get_param("filename", header="content-disposition")
                payload = part.get_payload(decode=True) or b""
                if fname:
                    files[name] = payload
                else:
                    fields.setdefault(name, []).append(payload.decode("utf-8", "replace"))
        else:
            for k, v in httpx.QueryParams(request.content.decode()).multi_items():
                fields.setdefault(k, []).append(v)
        return fields, files

    def handler(self, request: httpx.Request) -> httpx.Response:
        host, path, method = request.url.host, request.url.path, request.method
        # ---------------- greenhouse
        if host == "boards-api.greenhouse.io":
            m = re.match(r"/v1/boards/([^/]+)/jobs/([^/]+)$", path)
            if not m:
                return httpx.Response(404)
            slug = m.group(1)
            if slug in self.flaky_slugs and slug not in self.flaky_seen:
                self.flaky_seen.add(slug)
                return httpx.Response(503, text="upstream unavailable")
            if slug in self.always_fail_slugs:
                return httpx.Response(503, text="down")
            if slug.startswith("gone"):
                return httpx.Response(404)
            q = json.loads(json.dumps(GH_QUESTIONS))
            if slug in self.salary_question_slugs:
                q["questions"].append(GH_SALARY_Q)
            return httpx.Response(200, json=q)
        if host == "boards.greenhouse.io":
            m = re.match(r"/([^/]+)/jobs/([^/]+)$", path)
            if not m:
                return httpx.Response(404)
            slug, jid = m.groups()
            if method == "GET":
                extra = '<div class="g-recaptcha"></div><input name="g-recaptcha-response">' if slug in self.captcha_slugs else ""
                return httpx.Response(200, text=f'<html><form><input type="hidden" name="authenticity_token" value="csrf-{jid}">{extra}</form></html>')
            fields, files = self._parse_multipart(request)
            missing = [k for k in ("job_application[first_name]", "job_application[last_name]", "job_application[email]", "job_application[answers_attributes][2][text_value]") if not fields.get(k)]
            if "job_application[resume]" not in files or not files["job_application[resume]"].startswith(b"%PDF"):
                missing.append("resume")
            if fields.get("authenticity_token") != [f"csrf-{jid}"]:
                missing.append("authenticity_token")
            if missing:
                return httpx.Response(422, text=f'<html><div class="errors">missing: {", ".join(missing)}</div></html>')
            self.submissions.append(Submission("greenhouse", slug, jid, fields, files))
            return httpx.Response(200, text='<html><div id="application_confirmation"><h1>Thank you for applying</h1></div></html>')
        # ---------------- lever
        if host == "jobs.lever.co":
            m = re.match(r"/([^/]+)/([^/]+)/apply$", path)
            if not m:
                return httpx.Response(404)
            slug, pid = m.groups()
            if method == "GET":
                if slug.startswith("gone"):
                    return httpx.Response(404)
                extra = LEVER_CLEARANCE if slug in self.clearance_slugs else ""
                if slug in self.captcha_slugs:
                    extra += '<div class="h-captcha"></div>'
                return httpx.Response(200, text=LEVER_FORM.format(slug=slug, pid=pid, extra=extra))
            fields, files = self._parse_multipart(request)
            missing = [k for k in ("name", "email", "cards[abc][field0]", "cards[abc][field1]") if not fields.get(k)]
            if "resume" not in files or not files["resume"].startswith(b"%PDF"):
                missing.append("resume")
            if fields.get("csrf") != [f"tok-{pid}"]:
                missing.append("csrf")
            if missing:
                return httpx.Response(400, text=f"missing: {', '.join(missing)}")
            self.submissions.append(Submission("lever", slug, pid, fields, files))
            return httpx.Response(200, text="<html><h2>Your application has been submitted!</h2></html>")
        # ---------------- ashby
        if host == "api.ashbyhq.com":
            m = re.match(r"/posting-api/job-posting/([^/]+)(/application)?$", path)
            if not m:
                return httpx.Response(404)
            pid = m.group(1)
            if pid.startswith("noapi"):
                return httpx.Response(404, json={"error": "not enabled"})
            if not m.group(2):
                return httpx.Response(200, json=ASHBY_FORM, headers={"content-type": "application/json"})
            fields, files = self._parse_multipart(request)
            form = json.loads(fields.get("applicationForm", ["{}"])[0])
            subs = {s["path"]: s["value"] for s in form.get("fieldSubmissions", [])}
            errors = [p for p in ("_systemfield_name", "_systemfield_email", "custom_remote", "custom_source") if p not in subs]
            if subs.get("_systemfield_resume") not in files:
                errors.append("resume")
            if errors:
                return httpx.Response(400, json={"success": False, "errors": errors})
            self.submissions.append(Submission("ashby", "ashby", pid, {k: [str(v)] for k, v in subs.items()}, files))
            return httpx.Response(200, json={"success": True, "applicationId": f"app-{pid}"})
        return httpx.Response(404, text=f"unhandled {host}{path}")

    async def async_handler(self, request: httpx.Request) -> httpx.Response:
        await request.aread()  # multipart bodies are streams; load before the sync parser runs
        return self.handler(request)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.async_handler)
