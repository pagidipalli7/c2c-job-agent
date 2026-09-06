"""Create/update clients from a YAML/JSON intake document (CLI + API share this)."""
from __future__ import annotations

import json
from pathlib import Path

import yaml
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import AnswerBank, BaseResume, Client, Profile
from app.db.base import utcnow

from .schemas import ClientIntake, ProfileData, normalize_question


class IntakeError(ValueError):
    pass


REQUIRED_HINTS = {
    "blacklist": "profile.blacklist.companies must list the current employer and affiliates",
    "work_auth": "profile.work_auth {authorized_us, needs_sponsorship} is required",
    "min_salary": "profile.preferences.min_salary is required",
    "target_titles": "profile.preferences.target_titles must have at least one title",
}


def load_intake_file(path: str | Path) -> dict:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in (".yaml", ".yml"):
        return yaml.safe_load(text)
    return json.loads(text)


def validate_intake(doc: dict) -> ClientIntake:
    try:
        return ClientIntake.model_validate(doc)
    except ValidationError as e:
        msgs = []
        for err in e.errors():
            loc = ".".join(str(x) for x in err["loc"])
            hint = next((h for k, h in REQUIRED_HINTS.items() if k in loc), "")
            msgs.append(f"{loc}: {err['msg']}" + (f" ({hint})" if hint else ""))
        raise IntakeError("; ".join(msgs)) from e


def default_base_resume(profile: ProfileData) -> dict:
    """A base resume is the structured, client-approved source of truth for tailoring."""
    return {
        "name": f"{profile.first_name} {profile.last_name}",
        "headline": profile.headline or (profile.preferences.target_titles[0] if profile.preferences.target_titles else ""),
        "summary": profile.summary or "",
        "contact": {
            "email": "",  # filled with alias at render time
            "phone": "",
            "location": ", ".join(x for x in (profile.address.city, profile.address.state) if x),
            "linkedin": profile.links.linkedin,
            "github": profile.links.github,
            "portfolio": profile.links.portfolio,
        },
        "work_history": [w.model_dump() for w in profile.work_history],
        "education": [e.model_dump() for e in profile.education],
        "skills": list(profile.skills),
        "certifications": [c.model_dump() for c in profile.certifications],
    }


def upsert_client(session: Session, doc: dict) -> Client:
    """Create a client (by real_email) or bump its profile version. Returns the Client."""
    intake = validate_intake(doc)
    settings = get_settings()

    client = session.scalar(select(Client).where(Client.real_email == str(intake.real_email)))
    created = client is None
    if created:
        client = Client(
            name=intake.name,
            real_email=str(intake.real_email),
            phone=intake.phone,
            timezone=intake.timezone,
            resume_template=intake.resume_template,
            consent_given_at=utcnow(),
        )
        session.add(client)
        session.flush()  # need id for alias
        client.alias_email = str(intake.alias_email) if intake.alias_email else f"client{client.id}@{settings.apply_domain}"
    else:
        if intake.alias_email:
            client.alias_email = str(intake.alias_email)
        elif client.alias_email and "@" + settings.apply_domain not in client.alias_email:
            client.alias_email = f"client{client.id}@{settings.apply_domain}"  # override removed -> back to the managed alias
        client.name = intake.name
        client.phone = intake.phone
        client.timezone = intake.timezone
        client.resume_template = intake.resume_template

    # --- profile (versioned) ---------------------------------------------
    profile_dict = intake.profile.model_dump()
    current = next((p for p in client.profiles if p.is_current), None)
    if current is None or current.data != profile_dict:
        version = (max((p.version for p in client.profiles), default=0) + 1)
        for p in client.profiles:
            p.is_current = False
        client.profiles.append(Profile(client_id=client.id, version=version, is_current=True, data=profile_dict))

    # --- answer bank -------------------------------------------------------
    for a in intake.answers:
        key = normalize_question(a.question)
        row = session.scalar(select(AnswerBank).where(AnswerBank.client_id == client.id, AnswerBank.normalized_key == key))
        if row:
            row.answer = a.answer
            row.source = "client"
            row.confidence = 1.0
        else:
            client.answers.append(AnswerBank(client_id=client.id, question_text=a.question, normalized_key=key, answer=a.answer, source="client", confidence=1.0))

    # --- base resume -------------------------------------------------------
    resume_data = intake.base_resume or default_base_resume(intake.profile)
    br = session.scalar(select(BaseResume).where(BaseResume.client_id == client.id))
    if br is None:
        client.base_resume = BaseResume(client_id=client.id, data=resume_data, approved=intake.base_resume_approved)
    else:
        if br.data != resume_data:
            br.data = resume_data
            br.version += 1
            br.approved = intake.base_resume_approved  # any change needs re-approval
        elif intake.base_resume_approved:
            br.approved = True

    session.flush()
    return client


def approve_base_resume(session: Session, client_id: int, approved: bool = True) -> BaseResume:
    br = session.scalar(select(BaseResume).where(BaseResume.client_id == client_id))
    if br is None:
        raise IntakeError(f"client {client_id} has no base resume")
    br.approved = approved
    session.flush()
    return br


def get_profile_data(client: Client) -> ProfileData:
    p = client.current_profile
    if p is None:
        raise IntakeError(f"client {client.id} has no profile")
    return ProfileData.model_validate(p.data)
