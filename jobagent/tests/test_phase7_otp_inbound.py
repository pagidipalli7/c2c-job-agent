import asyncio
import hashlib
import hmac
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.base import utcnow
from app.db.models import Application, InboundEmail, JobPosting, OTPSession
from app.discovery.base import fingerprint
from app.otp.classifier import classify, classify_regex
from app.otp.extractor import extract, extract_code, extract_link
from app.otp.processor import InboundPayload, find_client_by_alias, process_inbound
from app.otp.registry import expire_stale, match_waiting, register_session, wait_for_value
from app.reporting import mail


@pytest.fixture()
def alex(seed_clients):
    return next(c for c in seed_clients if c.name == "Alex Rivera")


@pytest.fixture()
def submitted_app(db, alex):
    job = JobPosting(fingerprint=fingerprint("Kyndryl", "Data Engineer", "Remote"), url="https://kyndryl.wd5.myworkdayjobs.com/x", ats_type="workday", company_slug="kyndryl", company_name="Kyndryl", title="Data Engineer", location="Remote", posted_at=utcnow())
    db.add(job)
    db.flush()
    app = Application(client_id=alex.id, job_id=job.id, job_fingerprint=job.fingerprint, company_key="kyndryl", ats_type="workday", status="submitted", trace_id="t", submitted_at=utcnow(), job_snapshot={"company": "Kyndryl", "company_slug": "kyndryl", "title": "Data Engineer", "url": job.url})
    db.add(app)
    db.commit()
    return app


# ------------------------------------------------------------------ extraction
@pytest.mark.parametrize(
    "text,code",
    [
        ("Your verification code is 482913. It expires in 10 minutes.", "482913"),
        ("Use code 7731 to sign in", "7731"),
        ("Security code: 12345678", "12345678"),
        ("Enter this one-time passcode\n\n  553921\n\nto continue", "553921"),
        ("Hi,\n\n913 004\n\nis your Workday verification code", "913004"),
        ("Please verify: your OTP is A1B2C3", "A1B2C3"),
        ("Order #123456 shipped on 2024-01-01", None),  # no keyword -> no code
        ("Call us at 555-0142 for support", None),
    ],
)
def test_extract_code(text, code):
    assert extract_code(text) == code


def test_extract_link_prefers_verify_urls():
    body = "Welcome! Confirm your email: https://acme.myworkday.com/verify?token=abc123def456 \n Unsubscribe: https://acme.com/unsubscribe?u=1 \n https://acme.com/"
    assert extract_link(body) == "https://acme.myworkday.com/verify?token=abc123def456"
    assert extract_link("See https://acme.com/careers for details") is None


def test_extract_combined():
    e = extract("Your code", "Your verification code is 111222")
    assert e.kind == "otp_code" and e.code == "111222"


# ------------------------------------------------------------------ classification
@pytest.mark.parametrize(
    "subject,body,expected,by",
    [
        ("Your Workday verification code", "Enter code 604213 to verify your account.", "otp_code", "regex"),
        ("Verify your email address", "Click this link to verify your account: https://x.greenhouse.io/confirm?token=zzz9", "verification_link", "regex"),
        ("Next steps - Data Engineer", "Hi Alex, we'd love to schedule a phone screen. What is your availability this week?", "interview_request", "regex"),
        ("Update on your application", "Thank you for your interest. Unfortunately we have decided to move forward with other candidates.", "rejection", "regex"),
        ("Application received", "Thank you for applying to Acme. We have received your application and will be in touch.", "spam", "regex"),
        ("Jobs for you", "10 recommended jobs this week. Unsubscribe here.", "spam", "regex"),
        ("Re: Data Engineer role", "Hi Alex, I'm a recruiter at Acme. Could you send me your portfolio? Regards, Sam", "recruiter_reply", "llm"),
    ],
)
def test_classify(subject, body, expected, by):
    c = classify(subject, body, "someone@acme.com")
    assert c.type == expected and c.by == by, (c, expected)


def test_llm_failure_fails_safe_to_recruiter_reply(monkeypatch):
    from app.otp import classifier
    from app.llm import LLMError

    class Boom:
        def json_call(self, *a, **k):
            raise LLMError("down")

    monkeypatch.setattr(classifier, "get_llm", lambda: Boom())
    assert classify("Question from our recruiting team", "Could you clarify your notice period?", "x@acme.com").type == "recruiter_reply"


# ------------------------------------------------------------------ registry
def test_register_match_fulfil_and_expire(db, alex):
    s = register_session(db, alex.id, alex.alias_email, "acme.wd5.myworkday.com", kind="otp_code")
    db.commit()
    assert s.status == "waiting" and s.expires_at > utcnow() + timedelta(seconds=170)
    assert match_waiting(db, alex.alias_email, "notify.myworkday.com", "otp_code") is s  # ATS mailer domain accepted
    assert match_waiting(db, alex.alias_email, "acme.myworkday.com", "otp_code") is s
    assert match_waiting(db, alex.alias_email, "evil.example.net", "otp_code") is None
    assert match_waiting(db, "client99@apply.test", "myworkday.com", "otp_code") is None
    assert match_waiting(db, alex.alias_email, "myworkday.com", "verification_link") is None  # kind mismatch
    s.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert match_waiting(db, alex.alias_email, "myworkday.com", "otp_code") is None
    assert expire_stale() == 1
    db.expire_all()
    assert db.get(OTPSession, s.id).status == "expired"


def test_wait_for_value_returns_code_delivered_by_webhook(db, alex, submitted_app, _settings):
    from app.main import create_app

    s = register_session(db, alex.id, alex.alias_email, "myworkday.com", application_id=submitted_app.id)
    db.commit()
    client = TestClient(create_app(enable_scheduler=False))

    async def scenario():
        waiter = asyncio.create_task(wait_for_value(s.session_id, timeout_seconds=20, poll=0.2))
        await asyncio.sleep(0.5)
        r = await asyncio.to_thread(
            client.post,
            "/webhooks/inbound-mail",
            json={"recipient": alex.alias_email, "sender": "Workday <noreply@kyndryl.myworkday.com>", "subject": "Your verification code", "body_text": "Your one-time verification code is 246810.", "message_id": "<m1@wd>"},
            headers={"X-Webhook-Secret": "test-secret"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["classification"] == "otp_code"
        return await waiter

    assert asyncio.run(scenario()) == ("otp_code", "246810")
    db.expire_all()
    row = db.get(OTPSession, s.id)
    assert row.status == "fulfilled" and row.value == "246810"
    mail_row = db.scalar(select(InboundEmail).where(InboundEmail.message_id == "<m1@wd>"))
    assert mail_row.application_id == submitted_app.id and mail_row.classified_by == "regex"


def test_wait_for_value_times_out(db, alex):
    s = register_session(db, alex.id, alex.alias_email, "x.com")
    db.commit()
    assert asyncio.run(wait_for_value(s.session_id, timeout_seconds=1, poll=0.2)) is None
    db.expire_all()
    assert db.get(OTPSession, s.id).status == "expired"


# ------------------------------------------------------------------ webhook auth + formats
def test_webhook_rejects_bad_secret(_settings):
    from app.main import create_app

    client = TestClient(create_app(enable_scheduler=False))
    r = client.post("/webhooks/inbound-mail", json={"recipient": "client1@apply.test", "sender": "a@b.c"}, headers={"X-Webhook-Secret": "wrong"})
    assert r.status_code == 401
    r = client.post("/webhooks/inbound-mail", json={"recipient": "client1@apply.test", "sender": "a@b.c"})
    assert r.status_code == 401


def test_webhook_accepts_mailgun_signature_and_form(db, alex, _settings):
    from app.main import create_app

    client = TestClient(create_app(enable_scheduler=False))
    ts, token = "1700000000", "abc123"
    sig = hmac.new(b"test-secret", f"{ts}{token}".encode(), hashlib.sha256).hexdigest()
    r = client.post(
        "/webhooks/inbound-mail",
        data={"recipient": alex.alias_email, "sender": "noreply@greenhouse-mail.io", "subject": "Confirm your account", "body-plain": "Please confirm your email address by clicking https://boards.greenhouse.io/confirm?token=abcdef123", "Message-Id": "<mg1>", "timestamp": ts, "token": token, "signature": sig},
    )
    assert r.status_code == 200, r.text
    assert r.json()["classification"] == "verification_link"
    r2 = client.post("/webhooks/inbound-mail", data={"recipient": alex.alias_email, "sender": "x@y.z", "timestamp": ts, "token": token, "signature": "bad"})
    assert r2.status_code == 401


# ------------------------------------------------------------------ routing
def test_interview_request_is_forwarded_and_flagged(db, alex, submitted_app):
    before = len(mail.sent_log)
    email = process_inbound(db, InboundPayload(recipient=alex.alias_email, sender="Sam Recruiter <sam@kyndryl.com>", subject="Interview - Data Engineer", body_text="Hi Alex, we'd like to schedule an interview. What's your availability?", message_id="<i1>"))
    db.commit()
    assert email.classification == "interview_request" and email.flagged and email.forwarded
    assert email.application_id == submitted_app.id
    sent = mail.sent_log[-1]
    assert len(mail.sent_log) == before + 1
    assert sent.to == alex.real_email and sent.reply_to == "sam@kyndryl.com" and "[Interview request]" in sent.subject
    assert "Kyndryl - Data Engineer" in sent.text and "schedule an interview" in sent.text
    assert any("interview_request received" in (e.note or "") for e in submitted_app.events)


def test_rejection_marks_application(db, alex, submitted_app):
    email = process_inbound(db, InboundPayload(recipient=alex.alias_email, sender="talent@kyndryl.com", subject="Your application", body_text="Unfortunately we will not be moving forward with your application.", message_id="<r1>"))
    db.commit()
    db.refresh(submitted_app)
    assert email.classification == "rejection" and submitted_app.status == "rejected" and email.application_id == submitted_app.id


def test_duplicate_message_id_ignored(db, alex):
    p = InboundPayload(recipient=alex.alias_email, sender="a@b.com", subject="x", body_text="Unsubscribe from this newsletter", message_id="<dup>")
    e1 = process_inbound(db, p)
    e2 = process_inbound(db, p)
    assert e1.id == e2.id


def test_unknown_recipient_is_recorded(db):
    e = process_inbound(db, InboundPayload(recipient="nobody@apply.test", sender="a@b.com", subject="hi", body_text="hi"))
    assert e.classification == "unknown_recipient" and e.client_id is None


def test_find_client_tolerates_display_name_and_plus(db, alex):
    assert find_client_by_alias(db, f"Alex <{alex.alias_email}>") is alex
    assert find_client_by_alias(db, f"client{alex.id}+workday@apply.test") is alex


def test_otp_without_waiting_session_is_logged_not_lost(db, alex):
    e = process_inbound(db, InboundPayload(recipient=alex.alias_email, sender="no-reply@myworkday.com", subject="Code", body_text="Your verification code is 999000"))
    assert e.classification == "otp_code" and e.extracted_value == "999000" and e.application_id is None
