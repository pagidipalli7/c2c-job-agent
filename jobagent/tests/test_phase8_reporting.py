import base64
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.base import utcnow
from app.db.models import Application, ApplicationEvent, DiscoveryRun, EscalationItem, InboundEmail, JobPosting, Screenshot
from app.discovery.base import fingerprint
from app.reporting import mail
from app.reporting.backup import run_backup
from app.reporting.digest import client_digest, operator_summary, render_client_digest, render_operator_summary, send_all_weekly_digests, send_operator_daily_summary
from app.reporting.proof import proof_of_work
from app.submission import health


@pytest.fixture()
def populated(db, seed_clients):
    alex, priya = seed_clients
    now = utcnow()
    apps = []
    for i, (client, company, title, status, days) in enumerate([
        (alex, "Stripe", "Power Apps Developer", "submitted", 1),
        (alex, "Netflix", "Dynamics 365 Developer", "submitted", 3),
        (alex, "Figma", "Power Platform Developer", "rejected", 5),
        (alex, "Ramp", "Power BI Developer", "needs_human", 0),
        (priya, "Adobe", "Senior Data Engineer", "submitted", 2),
        (priya, "Brex", "Data Engineer", "queued", 0),
    ]):
        job = JobPosting(fingerprint=fingerprint(company, title, str(i)), url=f"https://jobs/{company}/{i}", ats_type="greenhouse", company_slug=company.lower(), company_name=company, title=title, location="Remote", posted_at=now)
        db.add(job)
        db.flush()
        a = Application(client_id=client.id, job_id=job.id, job_fingerprint=job.fingerprint, company_key=company.lower(), ats_type="greenhouse", status=status, trace_id=f"t{i}", match_score=80,
                        submitted_at=(now - timedelta(days=days)) if status in ("submitted", "rejected") else None, job_snapshot={"company": company, "title": title, "url": job.url},
                        resume_pdf_path="/tmp/x.pdf", resume_pdf_hash="ab" * 32, confirmation_text="Thank you for applying")
        a.events.append(ApplicationEvent(status=status, note="seeded"))
        db.add(a)
        db.flush()
        db.add(Screenshot(application_id=a.id, step="confirmation", path="/tmp/s.png"))
        apps.append(a)
    db.add(EscalationItem(client_id=alex.id, application_id=apps[3].id, question="What are your salary expectations?", normalized_key="what are your salary expectations", reason="forbidden_class:salary"))
    db.add(InboundEmail(client_id=alex.id, application_id=apps[0].id, recipient=alex.alias_email, sender="recruiter@stripe.com", sender_domain="stripe.com", subject="Interview - Power Apps Developer", body_text="...", classification="interview_request", flagged=True, forwarded=True))
    db.add(DiscoveryRun(finished_at=now, companies_crawled=20, companies_failed=1, jobs_seen=200, jobs_new=15, jobs_closed=3))
    health.record_result(db, "greenhouse", "success")
    health.record_result(db, "greenhouse", "failed")
    db.commit()
    return apps


def test_client_digest_content(db, seed_clients, populated):
    alex = seed_clients[0]
    d = client_digest(db, alex)
    assert d["applied_count"] == 3 and len(d["rejections"]) == 1 and len(d["interviews"]) == 1 and len(d["pending_escalations"]) == 1
    subject, text = render_client_digest(d)
    assert "3 submitted, 1 interview request(s)" in subject
    assert "Stripe — Power Apps Developer" in text and "https://jobs/Stripe/0" in text and "[rejected]" in text
    assert "interview request from recruiter@stripe.com" in text
    assert "salary expectations" in text


def test_weekly_digests_go_to_real_email(db, seed_clients, populated):
    before = len(mail.sent_log)
    assert send_all_weekly_digests() == 2
    sent = mail.sent_log[before:]
    assert {m.to for m in sent} == {c.real_email for c in seed_clients}
    assert all("apply.test" not in m.to for m in sent)


def test_operator_summary(db, populated):
    assert dict(operator_summary(db)["per_client"]) == {}  # nothing submitted in the last 24h
    d = operator_summary(db, hours=96)
    assert d["escalations_open"] == 1 and d["jobs_open"] == 6 and d["replies_forwarded"] == 1
    assert dict(d["per_client"]) == {"Alex Rivera": 2, "Priya Natarajan": 1}
    subject, text = render_operator_summary(d)
    assert "escalations open" in subject and "greenhouse: 50% over 2 attempts" in text and "crawled=20 failed=1 new=15" in text
    before = len(mail.sent_log)
    assert send_operator_daily_summary()
    assert mail.sent_log[-1].to == "operator@example.com" and len(mail.sent_log) == before + 1


def test_proof_of_work_bundle(db, populated):
    a = populated[0]
    p = proof_of_work(db, a)
    assert p["resume"]["hash"] == "ab" * 32 and p["screenshots"][0]["step"] == "confirmation"
    assert p["events"][0]["status"] == "submitted" and p["emails"][0]["classification"] == "interview_request"
    assert p["job"]["company"] == "Stripe" and p["confirmation_text"]


@pytest.fixture()
def admin_client(_settings):
    from app.main import create_app

    c = TestClient(create_app(enable_scheduler=False))
    c.headers["Authorization"] = "Basic " + base64.b64encode(b"admin:admin").decode()
    return c


def test_admin_pages_render(db, populated, admin_client):
    for path in ("/admin", "/admin/clients", "/admin/applications", "/admin/applications?status=submitted", f"/admin/applications/{populated[0].id}", "/admin/health", "/admin/inbound", "/escalations", "/health", "/clients"):
        r = admin_client.get(path)
        assert r.status_code == 200, (path, r.status_code, r.text[:200])
    assert "Stripe" in admin_client.get("/admin/applications").text
    assert "salary expectations" in admin_client.get("/escalations").text
    assert "Thank you for applying" in admin_client.get(f"/admin/applications/{populated[0].id}").text


def test_admin_requires_auth(_settings):
    from app.main import create_app

    c = TestClient(create_app(enable_scheduler=False))
    assert c.get("/admin").status_code == 401
    assert c.get("/escalations").status_code == 401
    c.headers["Authorization"] = "Basic " + base64.b64encode(b"admin:wrong").decode()
    assert c.get("/admin").status_code == 401


def test_escalation_answer_via_html_form(db, populated, admin_client):
    item = db.scalar(select(EscalationItem))
    r = admin_client.post(f"/escalations/{item.id}/answer", data={"answer": "$140k-$155k", "save_to_bank": "1"}, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert db.get(EscalationItem, item.id).status == "answered"
    assert db.get(Application, populated[3].id).status == "queued"


def test_backup_creates_db_copy_and_rotates(db, _settings, populated, tmp_path):
    (_settings.pdf_dir / "1").mkdir(parents=True, exist_ok=True)
    (_settings.pdf_dir / "1" / "x.pdf").write_bytes(b"%PDF")
    dest = run_backup(keep=2)
    assert (dest / "jobagent.db").exists() and (dest / "pdfs" / "1" / "x.pdf").exists()
    import sqlite3

    con = sqlite3.connect(str(dest / "jobagent.db"))
    assert con.execute("select count(*) from applications").fetchone()[0] == 6
    con.close()
    run_backup(keep=2)
    run_backup(keep=2)
    assert len([p for p in _settings.backup_dir.iterdir() if p.is_dir()]) == 2


def test_health_pause_visible_and_resumable_in_admin(db, admin_client):
    for _ in range(5):
        health.record_result(db, "workday", "failed")
    health.evaluate_health(db, "workday")
    db.commit()
    assert "PAUSED" in admin_client.get("/admin/health").text
    r = admin_client.post("/admin/health/workday/resume", follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert not health.is_paused(db, "workday")
