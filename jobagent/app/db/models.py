"""All SQLAlchemy models. Portable types only (JSON, String, DateTime) so a Postgres swap is a URL change."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, utcnow


# --------------------------------------------------------------------------- clients
class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    real_email: Mapped[str] = mapped_column(String(320), unique=True)
    phone: Mapped[str | None] = mapped_column(String(40))
    alias_email: Mapped[str | None] = mapped_column(String(320), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active|paused|churned
    timezone: Mapped[str] = mapped_column(String(64), default="America/Chicago")
    resume_template: Mapped[str] = mapped_column(String(40), default="classic")
    consent_given_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    profiles: Mapped[list["Profile"]] = relationship(back_populates="client", cascade="all, delete-orphan")
    answers: Mapped[list["AnswerBank"]] = relationship(back_populates="client", cascade="all, delete-orphan")
    base_resume: Mapped["BaseResume | None"] = relationship(back_populates="client", uselist=False, cascade="all, delete-orphan")
    accounts: Mapped[list["ATSAccount"]] = relationship(back_populates="client", cascade="all, delete-orphan")

    @property
    def current_profile(self) -> "Profile | None":
        for p in self.profiles:
            if p.is_current:
                return p
        return None


class Profile(Base):
    """Versioned profile JSON. Exactly one row per client has is_current=True."""

    __tablename__ = "profiles"
    __table_args__ = (UniqueConstraint("client_id", "version", name="uq_profile_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    client: Mapped[Client] = relationship(back_populates="profiles")


class AnswerBank(Base):
    __tablename__ = "answer_bank"
    __table_args__ = (UniqueConstraint("client_id", "normalized_key", name="uq_answer_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    question_text: Mapped[str] = mapped_column(Text)
    normalized_key: Mapped[str] = mapped_column(String(300))
    answer: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(20), default="client")  # client|llm_approved
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    client: Mapped[Client] = relationship(back_populates="answers")


class BaseResume(Base):
    __tablename__ = "base_resumes"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), unique=True)
    data: Mapped[dict] = mapped_column(JSON)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    client: Mapped[Client] = relationship(back_populates="base_resume")


# --------------------------------------------------------------------------- accounts
class ATSAccount(Base):
    __tablename__ = "ats_accounts"
    __table_args__ = (UniqueConstraint("client_id", "ats_type", "company_slug", name="uq_ats_account"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    ats_type: Mapped[str] = mapped_column(String(40))
    company_slug: Mapped[str] = mapped_column(String(200))
    username: Mapped[str] = mapped_column(String(320))
    password_encrypted: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used: Mapped[datetime | None] = mapped_column(DateTime)

    client: Mapped[Client] = relationship(back_populates="accounts")


# --------------------------------------------------------------------------- discovery
class CompanyRegistry(Base):
    __tablename__ = "company_registry"
    __table_args__ = (UniqueConstraint("slug", "ats_type", name="uq_company_slug_ats"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(200), index=True)
    ats_type: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(200))
    careers_url: Mapped[str | None] = mapped_column(String(500))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_crawled: Mapped[datetime | None] = mapped_column(DateTime)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class JobPosting(Base):
    __tablename__ = "job_postings"
    __table_args__ = (Index("ix_job_status_seen", "status", "last_seen"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    external_id: Mapped[str | None] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(1000))
    apply_url: Mapped[str | None] = mapped_column(String(1000))
    ats_type: Mapped[str] = mapped_column(String(40), index=True)
    company_slug: Mapped[str] = mapped_column(String(200), index=True)
    company_name: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(300))
    location: Mapped[str] = mapped_column(String(300), default="")
    remote: Mapped[bool] = mapped_column(Boolean, default=False)
    department: Mapped[str | None] = mapped_column(String(200))
    description_text: Mapped[str] = mapped_column(Text, default="")
    posted_at: Mapped[datetime | None] = mapped_column(DateTime)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    missing_crawls: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="open")  # open|closed
    raw: Mapped[dict | None] = mapped_column(JSON)


# --------------------------------------------------------------------------- applications
class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint("client_id", "job_fingerprint", name="uq_app_client_job"),
        Index("ix_app_status_sched", "status", "scheduled_at"),
        Index("ix_app_client_company", "client_id", "company_key", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("job_postings.id"), index=True)
    job_fingerprint: Mapped[str] = mapped_column(String(64))
    company_key: Mapped[str] = mapped_column(String(200))  # normalized company name
    ats_type: Mapped[str] = mapped_column(String(40))
    # queued|scheduled|in_progress|needs_otp|needs_human|submitted|failed|rejected|cancelled
    status: Mapped[str] = mapped_column(String(20), default="queued")
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    trace_id: Mapped[str] = mapped_column(String(40))
    match_score: Mapped[int | None] = mapped_column(Integer)
    match_reasons: Mapped[list | None] = mapped_column(JSON)
    missing_keywords: Mapped[list | None] = mapped_column(JSON)
    job_snapshot: Mapped[dict | None] = mapped_column(JSON)
    tailored_resume: Mapped[dict | None] = mapped_column(JSON)
    resume_pdf_path: Mapped[str | None] = mapped_column(String(500))
    resume_pdf_hash: Mapped[str | None] = mapped_column(String(64))
    step_reached: Mapped[str | None] = mapped_column(String(100))
    confirmation_text: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    locked_by: Mapped[str | None] = mapped_column(String(100))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime)

    client: Mapped[Client] = relationship()
    job: Mapped[JobPosting] = relationship()
    events: Mapped[list["ApplicationEvent"]] = relationship(back_populates="application", cascade="all, delete-orphan", order_by="ApplicationEvent.created_at")
    screenshots: Mapped[list["Screenshot"]] = relationship(back_populates="application", cascade="all, delete-orphan")


class ApplicationEvent(Base):
    """Status history, one row per transition."""

    __tablename__ = "application_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), index=True)
    status: Mapped[str] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    application: Mapped[Application] = relationship(back_populates="events")


class Screenshot(Base):
    __tablename__ = "screenshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), index=True)
    step: Mapped[str] = mapped_column(String(100))
    path: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    application: Mapped[Application] = relationship(back_populates="screenshots")


class ResumeArtifact(Base):
    """Every rendered PDF, content-addressed, linked to the application it was sent with."""

    __tablename__ = "resume_artifacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int | None] = mapped_column(ForeignKey("applications.id"), index=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    path: Mapped[str] = mapped_column(String(500))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    template: Mapped[str] = mapped_column(String(40))
    page_count: Mapped[int | None] = mapped_column(Integer)
    tailored_json: Mapped[dict | None] = mapped_column(JSON)
    fallback_to_base: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# --------------------------------------------------------------------------- escalation
class EscalationItem(Base):
    __tablename__ = "escalations"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int | None] = mapped_column(ForeignKey("applications.id"), index=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    question: Mapped[str] = mapped_column(Text)
    normalized_key: Mapped[str] = mapped_column(String(300))
    llm_draft: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(String(100))  # forbidden_class|low_confidence|unknown_field
    status: Mapped[str] = mapped_column(String(20), default="open")  # open|answered|dismissed
    answer: Mapped[str | None] = mapped_column(Text)
    context: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime)


# --------------------------------------------------------------------------- inbound mail / OTP
class InboundEmail(Base):
    __tablename__ = "inbound_emails"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"), index=True)
    application_id: Mapped[int | None] = mapped_column(ForeignKey("applications.id"))
    recipient: Mapped[str] = mapped_column(String(320))
    sender: Mapped[str] = mapped_column(String(320))
    sender_domain: Mapped[str] = mapped_column(String(200))
    subject: Mapped[str] = mapped_column(Text, default="")
    body_text: Mapped[str] = mapped_column(Text, default="")
    message_id: Mapped[str | None] = mapped_column(String(300), unique=True)
    classification: Mapped[str | None] = mapped_column(String(40))
    extracted_value: Mapped[str | None] = mapped_column(Text)
    classified_by: Mapped[str | None] = mapped_column(String(20))  # regex|llm
    forwarded: Mapped[bool] = mapped_column(Boolean, default=False)
    flagged: Mapped[bool] = mapped_column(Boolean, default=False)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OTPSession(Base):
    """Registered when a submission hits an OTP screen; fulfilled by the inbound-mail webhook."""

    __tablename__ = "otp_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), unique=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    application_id: Mapped[int | None] = mapped_column(ForeignKey("applications.id"))
    alias: Mapped[str] = mapped_column(String(320), index=True)
    expected_sender_domain: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="waiting")  # waiting|fulfilled|expired|cancelled
    kind: Mapped[str] = mapped_column(String(20), default="otp_code")  # otp_code|verification_link|any
    value: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime)


# --------------------------------------------------------------------------- ops
class AdapterResult(Base):
    """One row per adapter attempt; used for the rolling 24h success rate."""

    __tablename__ = "adapter_results"
    __table_args__ = (Index("ix_adapter_results_type_time", "ats_type", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ats_type: Mapped[str] = mapped_column(String(40))
    success: Mapped[bool] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(String(20))
    application_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AdapterState(Base):
    __tablename__ = "adapter_state"

    ats_type: Mapped[str] = mapped_column(String(40), primary_key=True)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    paused_reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class QueueItem(Base):
    """Generic DB-backed queue (used when REDIS_URL is unset)."""

    __tablename__ = "queue_items"
    __table_args__ = (Index("ix_queue_kind_status_avail", "kind", "status", "available_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|claimed|done|failed
    available_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    claimed_by: Mapped[str | None] = mapped_column(String(100))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DiscoveryRun(Base):
    __tablename__ = "discovery_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    companies_crawled: Mapped[int] = mapped_column(Integer, default=0)
    companies_failed: Mapped[int] = mapped_column(Integer, default=0)
    jobs_seen: Mapped[int] = mapped_column(Integer, default=0)
    jobs_new: Mapped[int] = mapped_column(Integer, default=0)
    jobs_closed: Mapped[int] = mapped_column(Integer, default=0)
