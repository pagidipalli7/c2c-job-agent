import pytest
from sqlalchemy import select

from app.db.base import utcnow
from app.db.models import AnswerBank, Application, ApplicationEvent, EscalationItem
from app.discovery.base import fingerprint
from app.db.models import JobPosting
from app.escalation.engine import answer_question
from app.escalation.forbidden import FORBIDDEN_PATTERNS, forbidden_class, is_forbidden
from app.escalation.service import answer_escalation, dismiss_escalation, open_items
from app.reporting import mail


@pytest.fixture()
def alex(seed_clients):
    return next(c for c in seed_clients if c.name == "Alex Rivera")


@pytest.fixture()
def priya(seed_clients):
    return next(c for c in seed_clients if c.name == "Priya Natarajan")


@pytest.fixture()
def application(db, alex):
    job = JobPosting(fingerprint=fingerprint("Acme", "Power Platform Developer", "Austin"), url="https://boards.greenhouse.io/acme/jobs/1", ats_type="greenhouse", company_slug="acme", company_name="Acme", title="Power Platform Developer", location="Austin, TX", posted_at=utcnow())
    db.add(job)
    db.flush()
    app = Application(client_id=alex.id, job_id=job.id, job_fingerprint=job.fingerprint, company_key="acme", ats_type="greenhouse", status="needs_human", trace_id="t1", job_snapshot={"company": "Acme", "title": "Power Platform Developer", "url": job.url})
    db.add(app)
    db.commit()
    return app


FORBIDDEN_QUESTIONS = [
    ("What are your salary expectations?", "salary"),
    ("Desired compensation", "salary"),
    ("What is your expected hourly rate?", "salary"),
    ("Are you willing to relocate to Seattle?", "relocation"),
    ("Have you ever been convicted of a felony?", "criminal"),
    ("Do you consent to a background check?", "criminal"),
    ("Do you hold an active security clearance?", "clearance"),
    ("Do you have a valid driver's license?", "license"),
    ("Please provide three professional references", "references"),
    ("What is your earliest start date?", "start_date"),
    ("What is your notice period?", "start_date"),
    ("Are you bound by a non-compete agreement?", "legal"),
    ("Have you ever been involved in litigation with a former employer?", "legal"),
    ("Are you subject to export control restrictions (ITAR)?", "legal"),
    ("Are you legally authorized to work in the United States?", "work_auth"),
    ("Will you require visa sponsorship?", "work_auth"),
    ("What is your date of birth?", "age_or_protected"),
]


@pytest.mark.parametrize("q,cls", FORBIDDEN_QUESTIONS)
def test_forbidden_classifier(q, cls):
    assert forbidden_class(q) == cls and is_forbidden(q)


@pytest.mark.parametrize("q", ["Why do you want to work here?", "Describe a project you are proud of", "How many years of Power Apps experience do you have?", "Are you comfortable working remotely?", "What is your LinkedIn profile?"])
def test_benign_questions_not_forbidden(q):
    assert forbidden_class(q) is None


def test_every_forbidden_pattern_has_a_test():
    covered = {cls for _, cls in FORBIDDEN_QUESTIONS}
    assert covered == set(FORBIDDEN_PATTERNS)


# ------------------------------------------------------------------ engine order
def test_answer_bank_hit_wins(db, alex):
    r = answer_question(db, alex, "What is your desired salary?")
    assert r.status == "answered" and r.source == "bank_client" and "$135,000" in r.answer


def test_bank_answer_fitted_to_options(db, alex):
    r = answer_question(db, alex, "Are you legally authorized to work in the United States?", options=["Yes, I am", "No, I am not"])
    assert r.status == "answered" and r.answer == "Yes, I am"


def test_profile_derived_answers(db, priya):
    assert answer_question(db, priya, "Phone number").answer == "+1 408 555 0199"
    assert answer_question(db, priya, "Email address").answer == priya.alias_email
    assert answer_question(db, priya, "LinkedIn profile URL").answer.startswith("https://linkedin.com")
    assert answer_question(db, priya, "Are you at least 18 years old?").answer == "Yes"
    r = answer_question(db, priya, "Veteran status", options=["I am a veteran", "I am not a veteran", "I decline to self-identify"])
    assert r.answer == "I decline to self-identify" and r.source == "profile"
    assert answer_question(db, priya, "Current company").answer == "Globex Analytics"


def test_forbidden_without_stored_answer_always_escalates(db, priya, application):
    """Priya has no salary answer stored -> must escalate, never LLM."""
    before = len(mail.sent_log)
    for q in ("What are your salary expectations?", "Are you willing to relocate?", "Do you have a security clearance?", "Earliest start date?", "Have you been convicted of a crime?"):
        r = answer_question(db, priya, q, application_id=application.id)
        assert r.status == "escalated" and r.answer is None and r.escalation_id, q
        item = db.get(EscalationItem, r.escalation_id)
        assert item.reason.startswith("forbidden_class:") and item.llm_draft is None
    assert len(mail.sent_log) - before == 5  # operator notified per new escalation
    assert "Escalation #" in mail.sent_log[-1].subject


def test_forbidden_with_client_answer_is_allowed(db, alex):
    r = answer_question(db, alex, "Are you willing to relocate?")
    assert r.status == "answered" and r.source == "bank_client"


def test_llm_high_confidence_saved_as_llm_approved(db, priya):
    r = answer_question(db, priya, "Why are you interested in joining our team?")
    assert r.status == "answered" and r.source == "llm" and r.confidence >= 0.8
    row = db.scalar(select(AnswerBank).where(AnswerBank.client_id == priya.id, AnswerBank.source == "llm_approved"))
    assert row is not None and row.answer == r.answer
    r2 = answer_question(db, priya, "Why are you interested in joining our team?")
    assert r2.source == "bank_llm_approved"


def test_llm_low_confidence_escalates_with_draft(db, priya, application):
    r = answer_question(db, priya, "What is your favorite programming language and why?", application_id=application.id)
    assert r.status == "escalated"
    item = db.get(EscalationItem, r.escalation_id)
    assert item.reason in ("low_confidence", "no_answer")
    assert db.scalar(select(AnswerBank).where(AnswerBank.client_id == priya.id, AnswerBank.source == "llm_approved")) is None


def test_llm_failure_escalates(db, priya, monkeypatch):
    from app.escalation import engine
    from app.llm import LLMError

    class Boom:
        def json_call(self, *a, **k):
            raise LLMError("down")

    monkeypatch.setattr(engine, "get_llm", lambda: Boom())
    r = answer_question(db, priya, "Tell us something unusual")
    assert r.status == "escalated"


# ------------------------------------------------------------------ escalation lifecycle
def test_answered_escalation_resumes_application(db, alex, application):
    r = answer_question(db, alex, "Do you hold an active security clearance?", application_id=application.id)
    assert r.status == "escalated"
    assert application.status == "needs_human"
    assert len(open_items(db)) == 1
    answer_escalation(db, r.escalation_id, "No")
    db.commit()
    db.refresh(application)
    assert application.status == "queued" and application.scheduled_at <= utcnow()
    assert any(e.status == "queued" and "escalation" in (e.note or "") for e in application.events)
    assert open_items(db) == []
    # and the answer is now in the bank -> no more escalation
    r2 = answer_question(db, alex, "Do you hold an active security clearance?", application_id=application.id)
    assert r2.status == "answered" and r2.answer == "No" and r2.source == "bank_client"


def test_application_waits_for_all_open_escalations(db, alex, application):
    r1 = answer_question(db, alex, "Security clearance?", application_id=application.id)
    r2 = answer_question(db, alex, "Three references please", application_id=application.id)
    assert r1.escalation_id != r2.escalation_id
    answer_escalation(db, r1.escalation_id, "None")
    db.refresh(application)
    assert application.status == "needs_human"
    answer_escalation(db, r2.escalation_id, "Available on request")
    db.refresh(application)
    assert application.status == "queued"


def test_duplicate_open_question_is_merged(db, alex, application):
    r1 = answer_question(db, alex, "What is your salary expectation?", application_id=application.id)
    # Alex has a stored salary answer... use a forbidden one without an answer instead
    r1 = answer_question(db, alex, "Do you have a security clearance?", application_id=application.id)
    r2 = answer_question(db, alex, "Do you have a security clearance?", application_id=999)
    assert r1.escalation_id == r2.escalation_id
    assert db.get(EscalationItem, r1.escalation_id).context["other_applications"] == [999]


def test_dismiss_cancels_application(db, alex, application):
    r = answer_question(db, alex, "Do you have a security clearance?", application_id=application.id)
    dismiss_escalation(db, r.escalation_id)
    db.refresh(application)
    assert application.status == "cancelled"


@pytest.mark.parametrize(
    "options,expected",
    [
        (["I am not a protected veteran", "I identify as a protected veteran", "I don't wish to answer"], "I don't wish to answer"),
        (["Male", "Female", "I do not wish to answer"], "I do not wish to answer"),
        (["Yes", "No", "Prefer not to say"], "Prefer not to say"),
        (["Hispanic or Latino", "White", "Decline To Self Identify"], "Decline To Self Identify"),
        (["Yes, I have a disability", "No, I do not have a disability", "I do not want to answer"], "I do not want to answer"),
    ],
)
def test_eeo_decline_matches_every_common_phrasing(db, priya, options, expected):
    q = {"I am not a protected veteran": "Veteran Status", "Male": "Gender", "Yes": "Disability status", "Hispanic or Latino": "Race/Ethnicity", "Yes, I have a disability": "Disability"}[options[0]]
    r = answer_question(db, priya, q, options=options)
    assert r.status == "answered" and r.answer == expected and r.source == "profile", r
