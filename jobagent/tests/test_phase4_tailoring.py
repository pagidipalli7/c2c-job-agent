import copy
import json

import pytest

from app.clients.intake import load_intake_file
from app.llm.client import LLMClient
from app.tailoring.render import render_resume
from app.tailoring.tailor import tailor_resume
from app.tailoring.validator import validate
from tests.conftest import ROOT

SAMPLE_JD = """Senior Power Platform Developer - Remote (US)
We need an engineer to own our Dataverse data model and build model-driven Power Apps for finance.
You will design Power Automate cloud flows, integrate Dynamics 365 with Azure Functions, and set up
ALM pipelines in Azure DevOps. Requirements: 5+ years Power Platform, PL-400 certification, strong SQL,
experience mentoring developers. Nice to have: Power Pages, Kubernetes, React."""


@pytest.fixture()
def base():
    from app.clients.intake import default_base_resume, validate_intake

    intake = validate_intake(load_intake_file(ROOT / "seed" / "client_alex.yaml"))
    return default_base_resume(intake.profile)


def _entities(resume):
    return {
        "jobs": {(w["company"].lower(), w["title"].lower(), str(w["start"]), str(w.get("end") or "Present").lower()) for w in resume["work_history"]},
        "edu": {(e["school"].lower(), (e.get("degree") or "").lower()) for e in resume["education"]},
        "certs": {c["name"].lower() for c in resume["certifications"]},
        "skills": {s.lower() for s in resume["skills"]},
    }


# ------------------------------------------------------------------ golden test
def test_golden_tailor_no_fabrication_and_pdf_under_two_pages(base):
    result = tailor_resume(base, SAMPLE_JD, "Senior Power Platform Developer")
    assert result.fallback_to_base is False and result.attempts == 1
    t = result.resume
    b, e = _entities(base), _entities(t)
    assert e["jobs"] == b["jobs"] and e["edu"] == b["edu"] and e["certs"] == b["certs"]
    assert e["skills"] <= b["skills"]
    assert t["summary"] and t["summary"] != base["summary"]
    # JD keywords float to the top of skills
    assert t["skills"][0].lower() in ("power apps", "power automate", "dataverse", "dynamics 365", "azure devops", "sql", "alm", "azure functions")
    assert validate(base, t) == []
    for template in ("classic", "modern"):
        rendered = render_resume(t, template, contact_email="client1@apply.test", contact_phone="+1 512 555 0142")
        assert rendered.pdf[:4] == b"%PDF" and rendered.page_count <= 2
        assert "client1@apply.test" in rendered.html and "Contoso Consulting" in rendered.html
        assert "<table" not in rendered.html and "<img" not in rendered.html  # ATS-safe
        assert len(rendered.content_hash) == 64


# ------------------------------------------------------------------ validator
def _t(base, mutate):
    t = copy.deepcopy(base)
    mutate(t)
    return t


@pytest.mark.parametrize(
    "label,mutate,section",
    [
        ("added employer", lambda t: t["work_history"].append({"company": "Google", "title": "Staff Engineer", "start": "2023-01", "end": "Present", "bullets": []}), "work_history"),
        ("changed title", lambda t: t["work_history"][0].__setitem__("title", "Principal Power Platform Architect"), "work_history"),
        ("changed start date", lambda t: t["work_history"][0].__setitem__("start", "2019-01"), "work_history"),
        ("changed end date", lambda t: t["work_history"][1].__setitem__("end", "2022-06"), "work_history"),
        ("dropped employer", lambda t: t["work_history"].pop(), "work_history"),
        ("changed location", lambda t: t["work_history"][0].__setitem__("location", "New York, NY"), "work_history"),
        ("invented bullets", lambda t: t["work_history"][0]["bullets"].extend(["Led 50-person org", "Saved $10M", "Shipped v2"]), "work_history"),
        ("inflated metric", lambda t: t["work_history"][0]["bullets"].__setitem__(0, "Built 120 model-driven apps serving 9,000 users"), "work_history"),
        ("added degree", lambda t: t["education"].append({"school": "MIT", "degree": "M.S.", "field": "CS"}), "education"),
        ("changed degree", lambda t: t["education"][0].__setitem__("degree", "Ph.D."), "education"),
        ("dropped school", lambda t: t["education"].clear(), "education"),
        ("added certification", lambda t: t["certifications"].append({"name": "AWS Solutions Architect"}), "certifications"),
        ("changed cert year", lambda t: t["certifications"][0].__setitem__("year", "2019"), "certifications"),
        ("added skill", lambda t: t["skills"].append("Kubernetes"), "skills"),
        ("changed name", lambda t: t.__setitem__("name", "Alexander Rivera"), "name"),
        ("extra section", lambda t: t.__setitem__("awards", ["Employee of the year"]), "awards"),
    ],
)
def test_validator_rejects_fabrication(base, label, mutate, section):
    violations = validate(base, _t(base, mutate))
    assert violations, label
    assert any(v.section == section for v in violations), (label, violations)


def test_validator_accepts_reorder_reword_and_drop(base):
    t = copy.deepcopy(base)
    t["summary"] = "Completely new summary emphasising Dataverse and Power Automate."
    t["headline"] = "Senior Power Platform Developer"
    t["skills"] = list(reversed(base["skills"]))[:8]
    t["work_history"][0]["bullets"] = [b.replace("Built", "Delivered") for b in reversed(base["work_history"][0]["bullets"])]
    t["work_history"][1]["bullets"] = base["work_history"][1]["bullets"][:1]
    t["work_history"] = list(reversed(t["work_history"]))  # order of jobs is free
    t["work_history"][0]["location"] = ""  # dropping a location is fine
    t["certifications"][0] = {"name": base["certifications"][0]["name"].upper(), "issuer": "Microsoft"}
    assert validate(base, t) == []


def test_validator_case_and_whitespace_insensitive(base):
    t = copy.deepcopy(base)
    t["work_history"][0]["company"] = "  contoso   CONSULTING "
    t["work_history"][0]["end"] = "Current"
    assert validate(base, t) == []


# ------------------------------------------------------------------ retry + fallback
def test_fabrication_is_retried_then_falls_back(base, monkeypatch):
    calls = []
    fabricated = copy.deepcopy(base)
    fabricated["work_history"].append({"company": "OpenAI", "title": "CTO", "start": "2024", "end": "Present", "bullets": []})

    def fake_json_call(self, tier, system, user, *, purpose, max_tokens=4096):
        calls.append(user)
        return copy.deepcopy(fabricated)

    monkeypatch.setattr(LLMClient, "json_call", fake_json_call)
    r = tailor_resume(base, SAMPLE_JD, "x")
    assert r.fallback_to_base is True and r.attempts == 2 and len(calls) == 2
    assert "REJECTED for fabrication" in calls[1] and "OpenAI" in " ".join(r.violations).lower() or "openai" in " ".join(r.violations)
    assert r.resume == base


def test_second_attempt_can_succeed(base, monkeypatch):
    good = copy.deepcopy(base)
    good["summary"] = "fixed"
    bad = copy.deepcopy(base)
    bad["skills"].append("Rust")
    answers = [bad, good]

    def fake_json_call(self, tier, system, user, *, purpose, max_tokens=4096):
        return copy.deepcopy(answers.pop(0))

    monkeypatch.setattr(LLMClient, "json_call", fake_json_call)
    r = tailor_resume(base, SAMPLE_JD, "x")
    assert r.fallback_to_base is False and r.attempts == 2 and r.resume["summary"] == "fixed"


def test_identity_fields_always_copied_from_base(base, monkeypatch):
    tampered = copy.deepcopy(base)
    tampered["name"] = "Someone Else"
    tampered["contact"] = {"email": "evil@example.com"}
    monkeypatch.setattr(LLMClient, "json_call", lambda self, *a, **k: copy.deepcopy(tampered))
    r = tailor_resume(base, SAMPLE_JD, "x")
    assert r.resume["name"] == base["name"] and r.resume["contact"] == base["contact"] and not r.fallback_to_base
