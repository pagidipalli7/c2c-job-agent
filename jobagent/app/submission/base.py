"""Shared adapter interface.

class Adapter(Protocol):
    ats_type: str
    async def submit(self, ctx: SubmissionContext) -> SubmissionResult
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy.orm import Session

from app.clients.schemas import ProfileData
from app.db.models import Application, Client


@dataclass
class SubmissionResult:
    status: str  # success | failed | needs_human | needs_otp
    step_reached: str = ""
    screenshot_path: str | None = None
    confirmation_text: str | None = None
    error: str | None = None
    transient: bool = False  # failed but worth retrying
    escalation_ids: list[int] = field(default_factory=list)


@dataclass
class FormQuestion:
    """A question/field on an application form, normalised across ATSs."""

    name: str  # field name/path used when posting
    label: str
    type: str  # text | textarea | select | multiselect | file | checkbox | hidden | boolean
    required: bool = False
    options: list[tuple[str, str]] = field(default_factory=list)  # (label, value)
    value: str | None = None  # hidden/prefilled


@dataclass
class SubmissionContext:
    session: Session
    application: Application
    client: Client
    profile: ProfileData
    resume_pdf: bytes
    trace_id: str
    screenshots: list[str] = field(default_factory=list)

    @property
    def job(self) -> dict:
        return self.application.job_snapshot or {}


class TransientError(Exception):
    """Network/5xx/timeouts: retry with backoff."""


class NeedsHuman(Exception):
    def __init__(self, reason: str, escalation_ids: list[int] | None = None, step: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.escalation_ids = escalation_ids or []
        self.step = step


class NeedsOTP(Exception):
    """Raised by browser adapters when an OTP screen blocks and no code arrived in time."""

    def __init__(self, session_id: str, step: str = ""):
        super().__init__(f"otp timeout for session {session_id}")
        self.session_id = session_id
        self.step = step


class Adapter(Protocol):
    ats_type: str

    async def submit(self, ctx: SubmissionContext) -> SubmissionResult: ...
