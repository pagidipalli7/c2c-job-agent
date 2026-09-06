"""Pydantic schemas for client intake. Validation rules live here."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator


class WorkHistoryItem(BaseModel):
    company: str
    title: str
    start: str  # YYYY-MM or YYYY
    end: str | None = None  # None/"Present" = current
    location: str | None = None
    bullets: list[str] = Field(default_factory=list)


class EducationItem(BaseModel):
    school: str
    degree: str | None = None
    field: str | None = None
    start: str | None = None
    end: str | None = None
    gpa: str | None = None


class Certification(BaseModel):
    name: str
    issuer: str | None = None
    year: str | None = None


class Links(BaseModel):
    linkedin: str | None = None
    github: str | None = None
    portfolio: str | None = None


DECLINE = "Decline to self-identify"


class EEO(BaseModel):
    gender: str | None = DECLINE
    race: str | None = DECLINE
    veteran: str | None = DECLINE
    disability: str | None = DECLINE

    @model_validator(mode="after")
    def _default_decline(self):
        for f in ("gender", "race", "veteran", "disability"):
            if getattr(self, f) is None:
                setattr(self, f, DECLINE)
        return self


class WorkAuth(BaseModel):
    authorized_us: bool
    needs_sponsorship: bool
    visa_type: str | None = None


class Preferences(BaseModel):
    target_titles: list[str] = Field(min_length=1)
    locations: list[str] = Field(default_factory=list)
    remote: Literal["include", "only", "exclude"] = "include"
    min_salary: int = Field(ge=0)
    max_commute_miles: int | None = None
    notice_period: str | None = None
    contract_types: list[str] = Field(default_factory=lambda: ["full_time"])
    exclude_title_keywords: list[str] = Field(default_factory=list)


class Blacklist(BaseModel):
    companies: list[str] = Field(min_length=1, description="MUST include current employer + affiliates")
    domains: list[str] = Field(default_factory=list)


class Address(BaseModel):
    line1: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country: str = "United States"


class ProfileData(BaseModel):
    first_name: str
    last_name: str
    headline: str | None = None
    summary: str | None = None
    address: Address = Field(default_factory=Address)
    work_history: list[WorkHistoryItem] = Field(min_length=1)
    education: list[EducationItem] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    links: Links = Field(default_factory=Links)
    eeo: EEO = Field(default_factory=EEO)
    work_auth: WorkAuth
    preferences: Preferences
    blacklist: Blacklist

    @model_validator(mode="after")
    def _blacklist_covers_current_employer(self):
        """Current employer must be blacklisted. Auto-add rather than reject: the rule is what matters."""
        current = [w.company for w in self.work_history if not w.end or w.end.lower() in ("present", "current", "now")]
        existing = {c.lower().strip() for c in self.blacklist.companies}
        for c in current:
            if c.lower().strip() not in existing:
                self.blacklist.companies.append(c)
        return self


class AnswerIn(BaseModel):
    question: str
    answer: str


class ClientIntake(BaseModel):
    """Top-level intake document (YAML/JSON)."""

    name: str
    real_email: EmailStr
    alias_email: EmailStr | None = None  # override the client{id}@APPLY_DOMAIN alias (e.g. use the real inbox until a domain exists)
    phone: str | None = None
    timezone: str = "America/Chicago"
    resume_template: Literal["classic", "modern"] = "classic"
    consent: bool = Field(description="Explicit written consent to apply on their behalf")
    profile: ProfileData
    answers: list[AnswerIn] = Field(default_factory=list)
    base_resume: dict | None = None  # defaults to a resume derived from the profile
    base_resume_approved: bool = False

    @field_validator("consent")
    @classmethod
    def _must_consent(cls, v):
        if not v:
            raise ValueError("client consent is required (consent: true)")
        return v


_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^a-z0-9 ]")


def normalize_question(text: str) -> str:
    """Normalise a form question into a stable AnswerBank key."""
    t = (text or "").lower().strip().rstrip("*:?. ")
    t = _PUNCT.sub(" ", t)
    t = _WS.sub(" ", t).strip()
    return t[:300]
