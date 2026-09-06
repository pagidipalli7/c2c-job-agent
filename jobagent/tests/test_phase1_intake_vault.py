import copy

import pytest
from sqlalchemy import select

from app.accounts.vault import decrypt, encrypt, generate_password, get_or_create_account
from app.clients.intake import IntakeError, load_intake_file, upsert_client, validate_intake
from app.clients.schemas import normalize_question
from app.db.models import ATSAccount, AnswerBank, Client, Profile
from tests.conftest import ROOT

ALEX = ROOT / "seed" / "client_alex.yaml"


def _doc():
    return load_intake_file(ALEX)


def test_seed_creates_two_clients_with_full_profiles(seed_clients, db):
    clients = db.scalars(select(Client)).all()
    assert len(clients) == 2
    for c in clients:
        assert c.alias_email == f"client{c.id}@apply.test"
        assert c.current_profile is not None
        data = c.current_profile.data
        assert data["work_history"] and data["blacklist"]["companies"] and data["preferences"]["target_titles"]
        assert c.base_resume is not None and c.base_resume.approved is True
        assert c.base_resume.data["work_history"]
    alex = next(c for c in clients if c.name == "Alex Rivera")
    assert len(alex.answers) == 6


@pytest.mark.parametrize(
    "mutate,fragment",
    [
        (lambda d: d["profile"].pop("blacklist"), "blacklist"),
        (lambda d: d["profile"].pop("work_auth"), "work_auth"),
        (lambda d: d["profile"]["preferences"].pop("min_salary"), "min_salary"),
        (lambda d: d["profile"]["preferences"].pop("target_titles"), "target_titles"),
        (lambda d: d["profile"]["preferences"].__setitem__("target_titles", []), "target_titles"),
        (lambda d: d["profile"]["blacklist"].__setitem__("companies", []), "blacklist"),
        (lambda d: d.__setitem__("consent", False), "consent"),
    ],
)
def test_validation_rejects_incomplete_profiles(mutate, fragment):
    d = copy.deepcopy(_doc())
    mutate(d)
    with pytest.raises(IntakeError) as ei:
        validate_intake(d)
    assert fragment in str(ei.value)


def test_current_employer_is_always_blacklisted():
    d = copy.deepcopy(_doc())
    d["profile"]["blacklist"]["companies"] = ["SomeOtherCo"]
    intake = validate_intake(d)
    assert "Contoso Consulting" in intake.profile.blacklist.companies


def test_eeo_defaults_to_decline():
    d = copy.deepcopy(_doc())
    d["profile"]["eeo"] = {"gender": None}
    intake = validate_intake(d)
    assert intake.profile.eeo.gender == "Decline to self-identify"
    assert intake.profile.eeo.race == "Decline to self-identify"


def test_reupsert_bumps_profile_version_only_on_change(db):
    c = upsert_client(db, _doc())
    db.commit()
    assert [p.version for p in c.profiles] == [1]
    upsert_client(db, _doc())  # identical -> no new version
    db.commit()
    assert len(db.scalars(select(Profile).where(Profile.client_id == c.id)).all()) == 1
    d = copy.deepcopy(_doc())
    d["profile"]["skills"].append("Power Fx")
    upsert_client(db, d)
    db.commit()
    profiles = db.scalars(select(Profile).where(Profile.client_id == c.id).order_by(Profile.version)).all()
    assert [p.version for p in profiles] == [1, 2]
    assert [p.is_current for p in profiles] == [False, True]


def test_base_resume_change_resets_approval(db):
    c = upsert_client(db, _doc())
    db.commit()
    assert c.base_resume.approved is True
    d = copy.deepcopy(_doc())
    d["base_resume"] = {"name": "Alex Rivera", "work_history": [], "skills": ["x"]}
    d["base_resume_approved"] = False
    upsert_client(db, d)
    db.commit()
    db.refresh(c)
    assert c.base_resume.approved is False and c.base_resume.version == 2


def test_answer_bank_normalisation(db):
    c = upsert_client(db, _doc())
    db.commit()
    key = normalize_question("  Are you legally authorized to work in the United States?* ")
    row = db.scalar(select(AnswerBank).where(AnswerBank.client_id == c.id, AnswerBank.normalized_key == key))
    assert row is not None and row.answer == "Yes" and row.source == "client"
    assert normalize_question("How did you hear about this job?") == normalize_question("how did you HEAR about this job")


def test_encryption_round_trip():
    for secret in ("hunter2", "p@ss w0rd with spaces", "ünïcødé✓"):
        token = encrypt(secret)
        assert token != secret
        assert decrypt(token) == secret


def test_generate_password_strength():
    pws = {generate_password() for _ in range(50)}
    assert len(pws) == 50
    for pw in pws:
        assert len(pw) == 20
        assert any(ch.islower() for ch in pw) and any(ch.isupper() for ch in pw) and any(ch.isdigit() for ch in pw)


def test_get_or_create_account_is_idempotent_and_encrypted(seed_clients, db):
    client = seed_clients[0]
    acct, pw = get_or_create_account(db, client, "workday", "acme")
    db.commit()
    assert acct.username == client.alias_email
    assert acct.password_encrypted != pw and pw not in acct.password_encrypted
    acct2, pw2 = get_or_create_account(db, client, "workday", "acme")
    assert acct2.id == acct.id and pw2 == pw
    _, pw3 = get_or_create_account(db, client, "workday", "other")
    assert pw3 != pw
    assert len(db.scalars(select(ATSAccount)).all()) == 2
