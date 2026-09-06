from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.clients.intake import get_profile_data
from app.db.base import utcnow
from app.db.models import Application, JobPosting
from app.discovery.base import fingerprint
from app.llm import parse_json_text, LLMError
from app.matching.dedup import company_cooldown_hit, stagger_schedule
from app.matching.filters import hard_filter
from app.matching.pipeline import evaluate, match_client
from app.matching.scorer import ScoreResult, score_job


def mk_job(db, company="Acme", title="Power Platform Developer", location="Austin, TX", remote=False, desc="Power Apps Power Automate Dataverse Dynamics 365 SQL", posted_days=1, slug=None, ats="greenhouse", raw=None):
    j = JobPosting(
        fingerprint=fingerprint(company, title, location),
        url=f"https://boards.greenhouse.io/{slug or company.lower()}/jobs/{abs(hash((company, title, location))) % 10**6}",
        ats_type=ats,
        company_slug=slug or company.lower(),
        company_name=company,
        title=title,
        location=location,
        remote=remote,
        description_text=desc,
        posted_at=utcnow() - timedelta(days=posted_days),
        raw=raw or {},
    )
    db.add(j)
    db.flush()
    return j


@pytest.fixture()
def alex(seed_clients):
    return next(c for c in seed_clients if c.name == "Alex Rivera")


@pytest.fixture()
def priya(seed_clients):
    return next(c for c in seed_clients if c.name == "Priya Natarajan")


# ------------------------------------------------------------------ hard filters
def test_blacklist_blocks_current_employer_and_affiliates(db, alex):
    p = get_profile_data(alex)
    for i, company in enumerate(("Contoso Consulting", "Contoso Holdings", "CONTOSO CONSULTING, INC.")):
        ok, name, reason = hard_filter(p, mk_job(db, company=company, title=f"Power Platform Developer {i}"))
        assert not ok and name == "blacklist", (company, reason)
    ok, *_ = hard_filter(p, mk_job(db, company="Acme"))
    assert ok


def test_blacklist_domain(db, alex):
    p = get_profile_data(alex)
    j = mk_job(db, company="Other")
    j.url = "https://careers.contoso.com/jobs/1"
    ok, name, _ = hard_filter(p, j)
    assert not ok and name == "blacklist"


def test_location_and_remote_filters(db, alex):
    p = get_profile_data(alex)
    assert hard_filter(p, mk_job(db, location="Austin, TX"))[0]
    assert hard_filter(p, mk_job(db, location="Dallas, Texas"))[0]
    assert hard_filter(p, mk_job(db, location="Remote - US", remote=True))[0]
    ok, name, _ = hard_filter(p, mk_job(db, location="New York, NY"))
    assert not ok and name == "location"
    p.preferences.remote = "only"
    ok, name, _ = hard_filter(p, mk_job(db, company="B", location="Austin, TX"))
    assert not ok and name == "location"
    p.preferences.remote = "exclude"
    ok, name, _ = hard_filter(p, mk_job(db, company="C", location="Remote", remote=True))
    assert not ok and name == "location"


def test_contract_type_filter(db, priya):
    p = get_profile_data(priya)  # full_time only
    ok, name, _ = hard_filter(p, mk_job(db, title="Data Engineer (Contract)", location="Remote", remote=True))
    assert not ok and name == "contract_type"
    ok, name, _ = hard_filter(p, mk_job(db, company="X", title="Data Engineer Intern", location="Remote", remote=True))
    assert not ok and name == "contract_type"
    assert hard_filter(p, mk_job(db, company="Y", title="Data Engineer", location="Remote", remote=True))[0]


def test_title_prefilter(db, alex):
    p = get_profile_data(alex)
    assert hard_filter(p, mk_job(db, title="Senior Power Apps Developer"))[0]
    assert hard_filter(p, mk_job(db, company="B", title="Dynamics 365 Developer II"))[0]
    ok, name, _ = hard_filter(p, mk_job(db, company="C", title="Account Executive"))
    assert not ok and name == "title"


def test_freshness_filter(db, alex):
    p = get_profile_data(alex)
    ok, name, _ = hard_filter(p, mk_job(db, posted_days=15))
    assert not ok and name == "freshness"
    assert hard_filter(p, mk_job(db, company="B", posted_days=13))[0]


# ------------------------------------------------------------------ scorer parsing
def test_parse_json_text_strips_fences_and_prose():
    assert parse_json_text('```json\n{"score": 80}\n```') == {"score": 80}
    assert parse_json_text('Sure! Here it is: {"score": 80, "apply": true} hope that helps') == {"score": 80, "apply": True}
    with pytest.raises(LLMError):
        parse_json_text("no json here")


def test_json_call_retries_once_on_invalid_json(monkeypatch):
    from app.llm.client import LLMClient

    calls = []

    def fake_complete(self, tier, system, user, *, purpose, max_tokens=4096):
        calls.append(user)
        return "garbage" if len(calls) == 1 else '{"ok": true}'

    monkeypatch.setattr(LLMClient, "complete", fake_complete)
    out = LLMClient(mode="mock").json_call("haiku", "s", "u", purpose="x")
    assert out == {"ok": True} and len(calls) == 2 and "not valid JSON" in calls[1]


def test_mock_scorer_prefers_matching_jobs(db, alex, priya):
    pa, pp = get_profile_data(alex), get_profile_data(priya)
    power = mk_job(db, desc="Power Apps, Power Automate, Dataverse, Dynamics 365, SQL, Azure DevOps. Must be authorized.")
    data = mk_job(db, company="DataCo", title="Senior Data Engineer", location="Remote", remote=True, desc="Spark Airflow Snowflake dbt Kafka Python SQL AWS")
    assert score_job(pa, power).score >= 65 and score_job(pa, power).apply
    assert score_job(pp, data).score >= 65
    assert score_job(pa, data).score < 65


def test_scoring_failure_is_safe(db, alex, monkeypatch):
    from app.matching import scorer as sc

    class Boom:
        def json_call(self, *a, **k):
            raise LLMError("down")

    monkeypatch.setattr(sc, "get_llm", lambda: Boom())
    r = score_job(get_profile_data(alex), mk_job(db))
    assert r.score == 0 and r.apply is False and "scoring failed" in r.reasons[0]


# ------------------------------------------------------------------ dedup rules
def _apply_scorer(profile, job):
    return ScoreResult(score=90, apply=True, reasons=["test"])


def test_never_apply_twice_to_same_job(db, alex):
    p = get_profile_data(alex)
    job = mk_job(db)
    d1 = evaluate(db, alex, p, job, scorer=_apply_scorer)
    db.commit()
    assert d1.stage == "queued"
    d2 = evaluate(db, alex, p, job, scorer=_apply_scorer)
    assert d2.decision == "skip" and d2.stage == "dedup" and "already applied" in d2.reason
    # the DB constraint holds even if code is bypassed
    with pytest.raises(IntegrityError):
        db.add(Application(client_id=alex.id, job_id=job.id, job_fingerprint=job.fingerprint, company_key="acme", ats_type="greenhouse", trace_id="x"))
        db.flush()
    db.rollback()


def test_company_cooldown_90_days(db, alex):
    p = get_profile_data(alex)
    j1 = mk_job(db, title="Power Platform Developer")
    j2 = mk_job(db, title="Power Apps Developer")  # same company, different job
    assert evaluate(db, alex, p, j1, scorer=_apply_scorer).stage == "queued"
    db.commit()
    d = evaluate(db, alex, p, j2, scorer=_apply_scorer)
    assert d.stage == "dedup" and "cooldown" in d.reason
    # age the first application past the window -> allowed again
    a = db.scalar(select(Application).where(Application.job_id == j1.id))
    a.created_at = utcnow() - timedelta(days=91)
    db.commit()
    assert company_cooldown_hit(db, alex.id, "acme") is None
    assert evaluate(db, alex, p, j2, scorer=_apply_scorer).stage == "queued"


def test_cooldown_ignores_cancelled(db, alex):
    p = get_profile_data(alex)
    j1 = mk_job(db, title="Power Platform Developer")
    j2 = mk_job(db, title="Power Apps Developer")
    evaluate(db, alex, p, j1, scorer=_apply_scorer)
    db.commit()
    a = db.scalar(select(Application).where(Application.job_id == j1.id))
    a.status = "cancelled"
    db.commit()
    assert evaluate(db, alex, p, j2, scorer=_apply_scorer).stage == "queued"


def test_cross_client_stagger_three_hours(db, alex, priya, monkeypatch):
    from app.matching import pipeline

    monkeypatch.setattr(pipeline, "hard_filter", lambda p, j: (True, None, None))  # both clients match the same job
    job = mk_job(db, title="Data Engineer", location="Remote", remote=True, desc="Spark Airflow")
    da = evaluate(db, alex, get_profile_data(alex), job, scorer=_apply_scorer)
    db.commit()
    dp = evaluate(db, priya, get_profile_data(priya), job, scorer=_apply_scorer)
    db.commit()
    apps = {a.client_id: a for a in db.scalars(select(Application).where(Application.job_fingerprint == job.fingerprint))}
    assert len(apps) == 2
    gap = apps[priya.id].scheduled_at - apps[alex.id].scheduled_at
    assert gap >= timedelta(hours=3), gap
    assert stagger_schedule(db, job.fingerprint) >= apps[priya.id].scheduled_at + timedelta(hours=3)


def test_unapproved_resume_blocks_queueing(db, alex):
    alex.base_resume.approved = False
    db.commit()
    job = mk_job(db)
    assert match_client(db, alex, [job], scorer=_apply_scorer) == []
    assert match_client(db, alex, [job], dry_run=True, scorer=_apply_scorer)[0].decision == "apply"
    assert db.scalar(select(Application)) is None


def test_below_threshold_not_queued(db, alex):
    job = mk_job(db)
    d = evaluate(db, alex, get_profile_data(alex), job, scorer=lambda p, j: ScoreResult(score=64, apply=True))
    assert d.decision == "skip" and d.stage == "llm" and d.score == 64
    d = evaluate(db, alex, get_profile_data(alex), mk_job(db, company="B"), scorer=lambda p, j: ScoreResult(score=90, apply=False, reasons=["dealbreaker"]))
    assert d.decision == "skip" and "dealbreaker" in d.reason
    assert db.scalar(select(Application)) is None
