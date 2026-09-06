"""Drives the Workday adapter against a local fake Workday wizard (same data-automation-ids) in headless
Chromium, including the OTP round trip through the session registry."""
import asyncio
import json
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from sqlalchemy import select

from app.clients.intake import get_profile_data
from app.db.base import utcnow
from app.db.models import Application, ATSAccount, JobPosting, OTPSession, Screenshot
from app.discovery.base import fingerprint
from app.otp.processor import InboundPayload, process_inbound
from app.submission.base import NeedsHuman, NeedsOTP, SubmissionContext
from app.submission.workday import WorkdayAdapter

FIXTURE = Path(__file__).parent / "fixtures" / "fake_workday" / "index.html"
PDF = b"%PDF-1.4 fake resume for workday"


class FakeServer:
    def __init__(self):
        self.submitted: list[dict] = []
        html = FIXTURE.read_bytes()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("content-type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html)

            def do_POST(self):
                n = int(self.headers.get("content-length", 0))
                outer.submitted.append(json.loads(self.rfile.read(n) or b"{}"))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, qs: str = "") -> str:
        return f"http://127.0.0.1:{self.port}/job{qs}"

    def stop(self):
        self.httpd.shutdown()


@pytest.fixture()
def server():
    s = FakeServer()
    yield s
    s.stop()


@pytest.fixture()
def alex(seed_clients):
    return next(c for c in seed_clients if c.name == "Alex Rivera")


def make_app(db, client, url, ext="wd1"):
    job = JobPosting(fingerprint=fingerprint("Acme", "Data Engineer", ext), url=url, apply_url=url, ats_type="workday", company_slug="acme", company_name="Acme", title="Data Engineer", location="Remote", remote=True, description_text="Spark", posted_at=utcnow())
    db.add(job)
    db.flush()
    app = Application(client_id=client.id, job_id=job.id, job_fingerprint=job.fingerprint, company_key="acme", ats_type="workday", status="in_progress", trace_id="wd", job_snapshot={"company": "Acme", "company_slug": "acme", "title": "Data Engineer", "url": url, "apply_url": url})
    db.add(app)
    db.commit()
    return app


async def deliver_otp_when_registered(db, alex, code="135790", timeout=60.0):
    """Simulates the mail webhook: wait for a waiting OTP session, then process an inbound code email."""
    deadline = utcnow() + timedelta(seconds=timeout)
    while utcnow() < deadline:
        db.expire_all()
        row = db.scalar(select(OTPSession).where(OTPSession.client_id == alex.id, OTPSession.status == "waiting"))
        if row:
            process_inbound(db, InboundPayload(recipient=alex.alias_email, sender="Workday <noreply@acme.myworkday.com>", subject="Verify your email", body_text=f"Your verification code is {code}."))
            db.commit()
            return row
        await asyncio.sleep(0.3)
    return None


def test_workday_full_flow_with_otp(db, server, alex):
    app = make_app(db, alex, server.url())
    ctx = SubmissionContext(session=db, application=app, client=alex, profile=get_profile_data(alex), resume_pdf=PDF, trace_id="wd")

    async def run():
        otp_task = asyncio.create_task(deliver_otp_when_registered(db, alex))
        result = await WorkdayAdapter().submit(ctx)
        return result, await otp_task

    result, otp_row = asyncio.run(run())
    assert result.status == "success", result
    assert "application has been submitted" in result.confirmation_text.lower()
    assert result.screenshot_path and Path(result.screenshot_path).exists()
    assert otp_row is not None and db.get(OTPSession, otp_row.id).status == "fulfilled"

    # the fake tenant received a coherent application
    sub = server.submitted[-1]
    assert sub["email"] == alex.alias_email and sub["first"] == "Alex" and sub["last"] == "Rivera"
    assert sub["phone"].endswith("5550142") and sub["country"] == "United States" and sub["region"] == "Texas" and sub["prev"] == "false"
    assert sub["source"] == "Company Website"
    assert [w["company"] for w in sub["work"]] == ["Contoso Consulting", "Fabrikam Inc", "Northwind Traders"]
    assert sub["work"][0]["current"] is True and sub["work"][0]["startYear"] == "2021" and sub["work"][0]["startMonth"] == "03"
    assert sub["edu"][0]["school"].startswith("University of Texas") and sub["edu"][0]["degree"] == "Bachelor"
    assert sub["resume"] == "resume.pdf" and sub["resumeSize"] == len(PDF)
    assert sub["authorized"] == "Yes" and sub["sponsorship"] == "no" and sub["why"]
    assert sub["gender"] == "I do not wish to answer" and sub["veteran"] == "I am not a protected veteran" and sub["ethnicity"] == "I do not wish to answer"
    assert sub["disability"] == "decline"

    # vault account was created for this tenant and marked used
    acct = db.scalar(select(ATSAccount).where(ATSAccount.client_id == alex.id, ATSAccount.ats_type == "workday", ATSAccount.company_slug == "acme"))
    assert acct is not None and acct.username == alex.alias_email and acct.last_used is not None
    assert sub["password"] and len(sub["password"]) == 20

    # screenshots on every page transition + full-page confirmation
    steps = [s.step for s in db.scalars(select(Screenshot).where(Screenshot.application_id == app.id))]
    for expected in ("job_page", "auth_filled", "otp_waiting", "otp_submitted", "my_information", "my_experience", "questions", "disclosures", "self_identify", "review", "confirmation"):
        assert expected in steps, (expected, steps)


def test_workday_forbidden_question_escalates_with_screenshot(db, server, alex):
    app = make_app(db, alex, server.url("?salary=1"), ext="wd2")
    # Alex's stored salary answer must not be used: strip it so the forbidden path triggers
    for a in list(alex.answers):
        if "salary" in a.question_text.lower():
            db.delete(a)
    db.commit()
    ctx = SubmissionContext(session=db, application=app, client=alex, profile=get_profile_data(alex), resume_pdf=PDF, trace_id="wd")

    async def run():
        otp_task = asyncio.create_task(deliver_otp_when_registered(db, alex))
        try:
            await WorkdayAdapter().submit(ctx)
        finally:
            otp_task.cancel()

    with pytest.raises(NeedsHuman) as ei:
        asyncio.run(run())
    assert ei.value.escalation_ids and ei.value.step == "questions"
    steps = [s.step for s in db.scalars(select(Screenshot).where(Screenshot.application_id == app.id))]
    assert "questions_needs_human" in steps
    assert not server.submitted


def test_workday_otp_timeout_raises_needs_otp(db, server, alex, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "otp_wait_seconds", 2)
    app = make_app(db, alex, server.url(), ext="wd3")
    ctx = SubmissionContext(session=db, application=app, client=alex, profile=get_profile_data(alex), resume_pdf=PDF, trace_id="wd")
    with pytest.raises(NeedsOTP):
        asyncio.run(WorkdayAdapter().submit(ctx))
    db.expire_all()
    assert db.scalar(select(OTPSession).where(OTPSession.application_id == app.id)).status == "expired"
