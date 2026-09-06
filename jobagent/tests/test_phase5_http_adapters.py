import asyncio
import hashlib
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.clients.intake import get_profile_data
from app.db.base import utcnow
from app.db.models import AdapterResult, AdapterState, Application, ApplicationEvent, EscalationItem, JobPosting, ResumeArtifact
from app.discovery.base import fingerprint
from app.submission import http as shttp
from app.submission.ashby import AshbyAdapter
from app.submission.base import NeedsHuman, SubmissionContext, SubmissionResult, TransientError
from app.submission.greenhouse import GreenhouseAdapter
from app.submission.lever import LeverAdapter, parse_form
from app.submission.simulators import SubmissionSimulator
from app.submission.worker import Worker
from app.queue import DBQueue

PDF = b"%PDF-1.4 fake resume bytes"


@pytest.fixture()
def sim():
    s = SubmissionSimulator()
    shttp.set_transport(s.transport())
    yield s
    shttp.set_transport(None)


@pytest.fixture()
def alex(seed_clients):
    return next(c for c in seed_clients if c.name == "Alex Rivera")


@pytest.fixture()
def priya(seed_clients):
    return next(c for c in seed_clients if c.name == "Priya Natarajan")


def make_app(db, client, ats="greenhouse", slug="acme", ext="123", status="queued", title="Power Platform Developer", scheduled_at=None):
    job = JobPosting(
        fingerprint=fingerprint(slug, title, ext), url=f"https://x/{slug}/{ext}", apply_url=f"https://x/{slug}/{ext}/apply", ats_type=ats,
        company_slug=slug, company_name=slug.title(), title=title, location="Remote", remote=True, description_text="Power Apps Dataverse", posted_at=utcnow(),
    )
    db.add(job)
    db.flush()
    app = Application(client_id=client.id, job_id=job.id, job_fingerprint=job.fingerprint, company_key=slug, ats_type=ats, status=status, trace_id=f"tr{ext}", scheduled_at=scheduled_at or utcnow() - timedelta(minutes=1),
                      job_snapshot={"company": job.company_name, "company_slug": slug, "external_id": ext, "title": title, "url": job.url, "apply_url": job.apply_url, "description_text": job.description_text, "ats_type": ats})
    db.add(app)
    db.commit()
    return app


def ctx_for(db, app, client, pdf=PDF):
    return SubmissionContext(session=db, application=app, client=client, profile=get_profile_data(client), resume_pdf=pdf, trace_id=app.trace_id)


# ------------------------------------------------------------------ greenhouse
def test_greenhouse_submits_with_mapped_fields_and_resume(db, sim, alex):
    app = make_app(db, alex)
    r = asyncio.run(GreenhouseAdapter().submit(ctx_for(db, app, alex)))
    assert r.status == "success" and "Thank you for applying" in r.confirmation_text
    sub = sim.submissions[-1]
    f = sub.fields
    assert f["job_application[first_name]"] == ["Alex"] and f["job_application[email]"] == [alex.alias_email]
    assert f["job_application[answers_attributes][0][text_value]"][0].startswith("https://linkedin.com")
    assert f["job_application[answers_attributes][1][answer_selected_options_attributes][0][question_option_id]"] == ["2"]  # careers site
    assert f["job_application[answers_attributes][3][boolean_value]"] == ["1"]  # authorized: from answer bank
    assert f["job_application[gender]"] == ["3"] and f["job_application[veteran_status]"] == ["1"]
    assert f["job_application[answers_attributes][2][text_value]"][0]  # LLM-answered "why here"
    assert sub.files["job_application[resume]"] == PDF


def test_greenhouse_forbidden_question_escalates_to_needs_human(db, sim, priya):
    app = make_app(db, priya, slug="salaryco", ext="9")
    with pytest.raises(NeedsHuman) as ei:
        asyncio.run(GreenhouseAdapter().submit(ctx_for(db, app, priya)))
    assert ei.value.escalation_ids
    item = db.get(EscalationItem, ei.value.escalation_ids[0])
    assert item.reason == "forbidden_class:salary" and item.application_id == app.id
    assert not [s for s in sim.submissions if s.slug == "salaryco"]


def test_greenhouse_captcha_is_needs_human(db, sim, alex):
    app = make_app(db, alex, slug="captchaco", ext="5")
    with pytest.raises(NeedsHuman):
        asyncio.run(GreenhouseAdapter().submit(ctx_for(db, app, alex)))


def test_greenhouse_gone_and_5xx(db, sim, alex):
    app = make_app(db, alex, slug="gone-co", ext="1")
    assert asyncio.run(GreenhouseAdapter().submit(ctx_for(db, app, alex))).status == "failed"
    app2 = make_app(db, alex, slug="brokenco", ext="2")
    with pytest.raises(TransientError):
        asyncio.run(GreenhouseAdapter().submit(ctx_for(db, app2, alex)))


# ------------------------------------------------------------------ lever
def test_lever_parse_form():
    from app.submission.simulators import LEVER_FORM

    qs, hidden, action = parse_form(LEVER_FORM.format(slug="s", pid="p", extra=""))
    names = {q.name: q for q in qs}
    assert hidden == {"csrf": "tok-p"} and action.endswith("/s/p/apply")
    assert names["resume"].type == "file" and names["name"].required
    assert names["cards[abc][field1]"].options == [("Yes", "Yes"), ("No", "No")]
    assert names["eeo[gender]"].type == "select" and len(names["eeo[gender]"].options) == 3
    assert names["consent[marketing]"].type == "checkbox"


def test_lever_submits(db, sim, priya):
    app = make_app(db, priya, ats="lever", slug="netflix", ext="abc")
    r = asyncio.run(LeverAdapter().submit(ctx_for(db, app, priya)))
    assert r.status == "success"
    f = sim.submissions[-1].fields
    assert f["name"] == ["Priya Natarajan"] and f["email"] == [priya.alias_email] and f["org"] == ["Globex Analytics"]
    assert f["urls[LinkedIn]"][0].startswith("https://linkedin.com") and f["csrf"] == ["tok-abc"]
    assert f["cards[abc][field1]"] == ["Yes"]  # sponsorship: from Priya's answer bank ("Yes, H-1B transfer")
    assert f["eeo[gender]"] == ["Decline to self identify"]
    assert f["consent[marketing]"] == ["true"] and "comments" not in f
    assert sim.submissions[-1].files["resume"] == PDF


def test_lever_clearance_question_escalates(db, sim, priya):
    app = make_app(db, priya, ats="lever", slug="clearanceco", ext="c1")
    with pytest.raises(NeedsHuman):
        asyncio.run(LeverAdapter().submit(ctx_for(db, app, priya)))
    assert db.scalar(select(EscalationItem).where(EscalationItem.reason == "forbidden_class:clearance")) is not None


def test_lever_hcaptcha(db, sim, alex):
    app = make_app(db, alex, ats="lever", slug="captchaco", ext="c2")
    with pytest.raises(NeedsHuman):
        asyncio.run(LeverAdapter().submit(ctx_for(db, app, alex)))


# ------------------------------------------------------------------ ashby
def test_ashby_api_submission(db, sim, alex):
    app = make_app(db, alex, ats="ashby", slug="ramp", ext="as-1")
    r = asyncio.run(AshbyAdapter().submit(ctx_for(db, app, alex)))
    assert r.status == "success" and "app-as-1" in r.confirmation_text
    f = sim.submissions[-1].fields
    assert f["_systemfield_name"] == ["Alex Rivera"] and f["_systemfield_email"] == [alex.alias_email]
    assert f["custom_remote"] == ["True"] and f["custom_source"] == ["web"]
    assert sim.submissions[-1].files["resume"] == PDF


def test_ashby_falls_back_to_browser_when_api_disabled(db, sim, alex, monkeypatch):
    from app.submission import generic_browser

    called = {}

    class FakeGeneric:
        def __init__(self, ats_type="generic"):
            called["ats"] = ats_type

        async def submit(self, ctx, url):
            called["url"] = url
            return SubmissionResult(status="success", step_reached="submitted", confirmation_text="browser ok")

    monkeypatch.setattr(generic_browser, "GenericBrowserAdapter", FakeGeneric)
    app = make_app(db, alex, ats="ashby", slug="noapi", ext="noapi-7")
    r = asyncio.run(AshbyAdapter().submit(ctx_for(db, app, alex)))
    assert r.status == "success" and called == {"ats": "ashby", "url": app.job_snapshot["apply_url"]}


# ------------------------------------------------------------------ queue
def test_db_queue_claim_release_and_stale_lock(db, alex):
    a = make_app(db, alex, ext="q1")
    b = make_app(db, alex, ext="q2", scheduled_at=utcnow() + timedelta(hours=1))
    q = DBQueue()
    assert q.claim("w1") == [a.id]
    assert q.claim("w2") == []  # locked
    db.expire_all()
    assert db.get(Application, a.id).status == "in_progress"
    q.release(a.id)
    db.expire_all()
    assert db.get(Application, a.id).status == "queued"
    # stale lock recovery
    ids = q.claim("w1")
    db.expire_all()
    row = db.get(Application, a.id)
    row.locked_at = utcnow() - timedelta(hours=2)
    db.commit()
    assert q.claim("w3") == [a.id]


# ------------------------------------------------------------------ worker end-to-end
def _in_window_tz():
    """Pick an Etc/GMT zone whose local hour is ~12 right now."""
    h = utcnow().hour
    off = 12 - h
    if off > 12:
        off -= 24
    if off < -11:
        off += 24
    return f"Etc/GMT{'-' if off > 0 else '+'}{abs(off)}" if off else "Etc/GMT"


def _out_of_window_tz():
    h = utcnow().hour
    off = 3 - h
    if off > 12:
        off -= 24
    if off < -11:
        off += 24
    return f"Etc/GMT{'-' if off > 0 else '+'}{abs(off)}" if off else "Etc/GMT"


def test_worker_end_to_end_greenhouse(db, sim, alex):
    alex.timezone = _in_window_tz()
    db.commit()
    app = make_app(db, alex)
    w = Worker(worker_id="t")
    assert asyncio.run(w.run_once()) == 1
    db.expire_all()
    app = db.get(Application, app.id)
    assert app.status == "submitted" and app.submitted_at and app.confirmation_text
    assert app.resume_pdf_path and app.resume_pdf_hash and app.tailored_resume
    art = db.scalar(select(ResumeArtifact).where(ResumeArtifact.application_id == app.id))
    assert art and art.content_hash == app.resume_pdf_hash and open(art.path, "rb").read()[:4] == b"%PDF"
    assert hashlib.sha256(sim.submissions[-1].files["job_application[resume]"]).hexdigest() == app.resume_pdf_hash
    assert [e.status for e in app.events] == ["in_progress", "submitted"]
    assert db.scalar(select(AdapterResult).where(AdapterResult.application_id == app.id)).success is True
    assert alex.id in w.next_ok


def test_worker_defers_outside_window(db, sim, alex):
    alex.timezone = _out_of_window_tz()
    db.commit()
    app = make_app(db, alex)
    assert asyncio.run(Worker(worker_id="t").run_once()) == 0
    db.expire_all()
    app = db.get(Application, app.id)
    assert app.status == "queued" and app.scheduled_at > utcnow() + timedelta(hours=1)


def test_worker_daily_cap(db, sim, alex, monkeypatch):
    alex.timezone = _in_window_tz()
    db.commit()
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "daily_cap_per_client", 1)
    done = make_app(db, alex, ext="done", status="submitted")
    app = make_app(db, alex, ext="new")
    assert asyncio.run(Worker(worker_id="t").run_once()) == 0
    db.expire_all()
    assert db.get(Application, app.id).status == "queued" and db.get(Application, app.id).scheduled_at > utcnow()


def test_worker_jitter_between_client_submissions(db, sim, alex):
    alex.timezone = _in_window_tz()
    db.commit()
    a1 = make_app(db, alex, ext="j1")
    a2 = make_app(db, alex, ext="j2")
    w = Worker(worker_id="t")
    assert asyncio.run(w.run_once()) == 1
    db.expire_all()
    s = {db.get(Application, a1.id).status, db.get(Application, a2.id).status}
    assert s == {"submitted", "queued"}
    deferred = next(a for a in (db.get(Application, a1.id), db.get(Application, a2.id)) if a.status == "queued")
    assert timedelta(minutes=2) < deferred.scheduled_at - utcnow() <= timedelta(minutes=12)


def test_worker_transient_retry_then_needs_human(db, sim, alex):
    alex.timezone = _in_window_tz()
    db.commit()
    app = make_app(db, alex, slug="brokenco", ext="b1")
    for attempt in (1, 2, 3):
        w = Worker(worker_id="t")
        db.expire_all()
        row = db.get(Application, app.id)
        row.scheduled_at = utcnow() - timedelta(seconds=1)
        db.commit()
        asyncio.run(w.run_once())
        db.expire_all()
        row = db.get(Application, app.id)
        assert row.attempts == attempt
        if attempt < 3:
            assert row.status == "queued" and row.scheduled_at > utcnow()
    assert row.status == "needs_human" and "after 3 attempts" in row.error


def test_worker_forbidden_question_marks_needs_human_and_links_escalation(db, sim, priya):
    priya.timezone = _in_window_tz()
    db.commit()
    app = make_app(db, priya, slug="salaryco", ext="s1")
    asyncio.run(Worker(worker_id="t").run_once())
    db.expire_all()
    row = db.get(Application, app.id)
    assert row.status == "needs_human"
    item = db.scalar(select(EscalationItem).where(EscalationItem.application_id == row.id))
    assert item and item.status == "open"


def test_adapter_health_pauses_below_70_percent(db, sim, alex):
    from app.submission import health

    for i in range(4):
        health.record_result(db, "lever", "failed")
    health.record_result(db, "lever", "success")
    st = health.evaluate_health(db, "lever")
    assert st.paused and "20%" in st.paused_reason
    alex.timezone = _in_window_tz()
    db.commit()
    app = make_app(db, alex, ats="lever", slug="netflix", ext="h1")
    assert asyncio.run(Worker(worker_id="t").run_once()) == 0
    db.expire_all()
    assert db.get(Application, app.id).status == "queued"
    health.resume_adapter(db, "lever")
    db.commit()
    assert not db.get(AdapterState, "lever").paused


def test_graceful_stop_requeues_unstarted(db, sim, alex):
    alex.timezone = _in_window_tz()
    db.commit()
    app = make_app(db, alex, ext="g1")
    w = Worker(worker_id="t")
    w.request_stop()
    asyncio.run(w.run_once())
    db.expire_all()
    assert db.get(Application, app.id).status == "queued"
