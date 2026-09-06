"""Branch-coverage tests for the four non-negotiable areas + ops hardening (proxy, shutdown, scheduler)."""
import asyncio
import copy
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.base import utcnow
from app.db.models import OTPSession
from app.otp.extractor import Extracted, extract, extract_code
from app.otp.registry import expire_stale, match_waiting, register_session, wait_for_value
from app.tailoring.validator import Violation, _cert_key, is_valid, validate


@pytest.fixture()
def alex(seed_clients):
    return next(c for c in seed_clients if c.name == "Alex Rivera")


# ------------------------------------------------------------------ validator remaining branches
def test_validator_rejects_non_object():
    v = validate({"name": "x"}, ["not", "a", "dict"])
    assert v and v[0].section == "root"
    assert not is_valid({"name": "x"}, "nope")


def test_validator_duplicate_work_entries():
    base = {"name": "A", "work_history": [{"company": "Acme", "title": "Dev", "start": "2020", "end": None, "bullets": []}]}
    t = copy.deepcopy(base)
    t["work_history"].append(copy.deepcopy(t["work_history"][0]))
    assert any(v.kind == "added" and "duplicate" in v.detail for v in validate(base, t))


def test_validator_string_certifications_and_str():
    base = {"name": "A", "work_history": [], "certifications": ["PL-400"], "skills": []}
    assert _cert_key("PL-400") == ("pl-400", "", "")
    assert validate(base, copy.deepcopy(base)) == []
    v = validate(base, {**base, "certifications": ["PL-400", "AZ-900"]})
    assert v and str(v[0]).startswith("certifications: added")
    assert Violation("s", "k", "d") == Violation("s", "k", "d")


def test_validator_metric_rules():
    base = {"name": "A", "work_history": [{"company": "Acme", "title": "Dev", "start": "2020", "end": None, "bullets": ["Cut costs 30% for 4,000 users", "Led 12 apps"]}]}
    ok = copy.deepcopy(base)
    ok["work_history"][0]["bullets"] = ["Reduced spend by 30% across 4000 users", "12 apps shipped"]  # same numbers, reformatted
    assert validate(base, ok) == []
    bad = copy.deepcopy(base)
    bad["work_history"][0]["bullets"] = ["Reduced spend by 45%"]
    assert any("metric" in v.detail for v in validate(base, bad))
    # bullets with no numbers at all are fine
    none = copy.deepcopy(base)
    none["work_history"][0]["bullets"] = ["Improved things"]
    assert validate(base, none) == []


def test_validator_education_changed_vs_added_and_cert_year_kept():
    base = {"name": "A", "work_history": [], "education": [{"school": "UT", "degree": "B.S.", "field": "CS"}], "certifications": [{"name": "PL-400", "year": "2022"}]}
    t = copy.deepcopy(base)
    t["education"][0]["field"] = "Math"
    assert any(v.section == "education" and v.kind == "changed" for v in validate(base, t))
    t2 = copy.deepcopy(base)
    t2["certifications"][0] = {"name": "PL-400"}  # dropping the year is fine
    assert validate(base, t2) == []
    t3 = copy.deepcopy(base)
    t3["work_history"] = [{"company": "New", "title": "X", "start": "2020"}]
    assert any(v.kind == "added" and "new employer" in v.detail for v in validate(base, t3))


def test_validator_missing_sections_default_empty():
    base = {"name": "A", "work_history": [{"company": "Acme", "title": "Dev", "start": "2020"}]}
    assert validate(base, {"name": "A", "work_history": [{"company": "Acme", "title": "Dev", "start": "2020", "location": "NYC"}]})  # location added -> changed
    assert validate(base, {"name": "A"}) and validate(base, {"name": "A"})[0].kind == "removed"


# ------------------------------------------------------------------ extractor remaining branches
def test_extracted_kind_and_spaced_code():
    assert Extracted().kind is None
    assert Extracted(link="https://x/verify").kind == "verification_link"
    assert extract_code("Your login code:\n\n482 913") == "482913"
    assert extract_code("482 913") is None  # spaced digits without any keyword
    assert extract_code("verification: ABCDEF") is None  # letters only is a word, not a code
    e = extract("Confirm", "Confirm here https://a.b/confirm?token=1")
    assert e.code is None and e.kind == "verification_link"


# ------------------------------------------------------------------ registry remaining branches
def test_match_waiting_kind_filter_and_domains(db, alex):
    s1 = register_session(db, alex.id, alex.alias_email, "", kind="verification_link")  # empty expected domain matches any sender
    s2 = register_session(db, alex.id, alex.alias_email, "acme.com", kind="otp_code")
    db.commit()
    assert match_waiting(db, alex.alias_email, "random.org", "verification_link") is s1
    assert match_waiting(db, alex.alias_email, "mail.acme.com", "otp_code") is s2
    assert match_waiting(db, alex.alias_email, "random.org", "otp_code") is None
    assert expire_stale() == 0


def test_wait_for_value_unknown_cancelled_and_expired(db, alex):
    assert asyncio.run(wait_for_value("nope", timeout_seconds=1, poll=0.1)) is None
    s = register_session(db, alex.id, alex.alias_email, "x.com")
    s.status = "cancelled"
    db.commit()
    assert asyncio.run(wait_for_value(s.session_id, timeout_seconds=2, poll=0.1)) is None
    s2 = register_session(db, alex.id, alex.alias_email, "x.com")
    db.commit()

    async def fulfil_late():
        await asyncio.sleep(0.3)
        db.expire_all()
        row = db.get(OTPSession, s2.id)
        row.status, row.value, row.kind = "fulfilled", "https://x.com/verify?t=1", "verification_link"
        db.commit()

    async def run():
        t = asyncio.create_task(fulfil_late())
        r = await wait_for_value(s2.session_id, timeout_seconds=5, poll=0.1)
        await t
        return r

    assert asyncio.run(run()) == ("verification_link", "https://x.com/verify?t=1")
    # timeout path when the row was already expired by the scheduler in the meantime
    s3 = register_session(db, alex.id, alex.alias_email, "x.com")
    db.commit()

    async def expire_then_wait():
        async def exp():
            await asyncio.sleep(0.2)
            db.expire_all()
            db.get(OTPSession, s3.id).status = "expired"
            db.commit()

        t = asyncio.create_task(exp())
        r = await wait_for_value(s3.session_id, timeout_seconds=3, poll=0.1)
        await t
        return r

    assert asyncio.run(expire_then_wait()) is None


# ------------------------------------------------------------------ ops hardening
def test_proxy_stub(monkeypatch):
    from app.config import get_settings
    from app.submission.browser import proxy_for_client

    assert proxy_for_client(1) is None
    monkeypatch.setattr(get_settings(), "proxy_url", "http://user:pw@proxy.example:8080")
    assert proxy_for_client(1) == {"server": "http://user:pw@proxy.example:8080"}
    from app.discovery import http as dhttp

    c = dhttp.make_client()
    assert c is not None
    asyncio.run(c.aclose())


def test_scheduler_builds_all_jobs():
    from app.scheduler import build_scheduler

    sched = build_scheduler()
    assert {j.id for j in sched.get_jobs()} == {"discovery", "weekly_digest", "operator_daily", "backup", "otp_expiry"}


def test_worker_run_forever_stops_gracefully(db):
    from app.submission.worker import Worker

    w = Worker(worker_id="t", poll_seconds=0.2)

    async def run():
        task = asyncio.create_task(w.run_forever())
        await asyncio.sleep(0.5)
        w.request_stop()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(run())
    assert w.stop.is_set() and not w.inflight


def test_llm_live_client_construction(monkeypatch):
    """Live mode builds an Anthropic client without making a call; api errors map to LLMError."""
    from app.llm.client import LLMClient, LLMError

    c = LLMClient(mode="live")
    assert c.mode == "live" and c._client is not None and c.models["haiku"].startswith("claude-haiku") and c.models["sonnet"].startswith("claude-sonnet")
    import anthropic

    class Boom:
        def create(self, **kw):
            raise anthropic.APIConnectionError(request=None)

    c._client.messages = Boom()
    with pytest.raises(LLMError):
        c.complete("haiku", "s", "u", purpose="x")
    with pytest.raises(LLMError):
        LLMClient(mode="mock").complete("haiku", "s", "u", purpose="unregistered_purpose")


def test_lone_six_digit_line_is_a_code():
    assert extract_code("Hi Alex,\n\n553921\n\nRegards") == "553921"


def test_wait_for_value_timeout_when_fulfilled_with_empty_value(db, alex):
    s = register_session(db, alex.id, alex.alias_email, "x.com")
    s.status, s.value = "fulfilled", ""  # malformed fulfilment: never returns a value
    db.commit()
    assert asyncio.run(wait_for_value(s.session_id, timeout_seconds=1, poll=0.2)) is None
    db.expire_all()
    assert db.get(OTPSession, s.id).status == "fulfilled"


def test_bad_api_key_aborts_matching_instead_of_scoring_zero(db, seed_clients, monkeypatch):
    from app.llm import LLMAuthError
    from app.matching import scorer as sc
    from app.matching.pipeline import match_client
    from tests.test_phase3_matching import mk_job

    class Bad:
        def json_call(self, *a, **k):
            raise LLMAuthError("API key is invalid")

    monkeypatch.setattr(sc, "get_llm", lambda: Bad())
    alex = seed_clients[0]
    with pytest.raises(LLMAuthError):
        match_client(db, alex, [mk_job(db)], dry_run=True)


def test_live_client_maps_auth_error(monkeypatch):
    import anthropic
    import httpx

    from app.llm.client import LLMAuthError, LLMClient

    c = LLMClient(mode="live")

    class Boom:
        def create(self, **kw):
            resp = httpx.Response(401, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"), json={"error": {"message": "API key is invalid."}})
            raise anthropic.AuthenticationError("invalid", response=resp, body=None)

    c._client.messages = Boom()
    with pytest.raises(LLMAuthError):
        c.complete("haiku", "s", "u", purpose="x")


def test_settings_ignore_inline_env_comments(tmp_path, monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("PROXY_URL", "   # optional, e.g. http://user:pass@host:port")
    monkeypatch.setenv("REDIS_URL", "# empty -> DB-backed queue")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///x.db   # or postgres later")
    s = Settings()
    assert not s.proxy_url and not s.redis_url
    assert s.database_url == "sqlite:///x.db"
    monkeypatch.setenv("PROXY_URL", "http://user:pw@proxy.example:8080")
    assert Settings().proxy_url == "http://user:pw@proxy.example:8080"
