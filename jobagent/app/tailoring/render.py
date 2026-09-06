"""Resume JSON -> HTML (Jinja2, ATS-safe templates) -> PDF (WeasyPrint). Every PDF is content-hashed and
stored under data/pdfs/<client_id>/<hash>.pdf with a ResumeArtifact row."""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session

from app.config import PACKAGE_ROOT, get_settings
from app.db.models import ResumeArtifact
from app.logging import get_logger

log = get_logger("tailoring.render")

TEMPLATE_DIR = PACKAGE_ROOT / "templates" / "resume"
TEMPLATES = {"classic": "classic.html", "modern": "modern.html"}

_env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=select_autoescape(["html"]))


@dataclass
class Rendered:
    pdf: bytes
    html: str
    content_hash: str
    page_count: int
    path: Path | None = None


def render_html(resume: dict, template: str = "classic", contact_email: str | None = None, contact_phone: str | None = None) -> str:
    name = TEMPLATES.get(template, TEMPLATES["classic"])
    contact = dict(resume.get("contact") or {})
    if contact_email:
        contact["email"] = contact_email
    if contact_phone:
        contact["phone"] = contact_phone
    return _env.get_template(name).render(r=resume, contact=contact)


def html_to_pdf(html: str) -> tuple[bytes, int]:
    from weasyprint import HTML

    doc = HTML(string=html, base_url=str(TEMPLATE_DIR)).render()
    buf = io.BytesIO()
    doc.write_pdf(buf)
    return buf.getvalue(), len(doc.pages)


def render_resume(resume: dict, template: str = "classic", contact_email: str | None = None, contact_phone: str | None = None) -> Rendered:
    html = render_html(resume, template, contact_email, contact_phone)
    pdf, pages = html_to_pdf(html)
    return Rendered(pdf=pdf, html=html, content_hash=hashlib.sha256(pdf).hexdigest(), page_count=pages)


def store_resume(session: Session, client_id: int, rendered: Rendered, template: str, tailored_json: dict | None, application_id: int | None = None, fallback_to_base: bool = False) -> ResumeArtifact:
    settings = get_settings()
    d = settings.pdf_dir / str(client_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{rendered.content_hash}.pdf"
    if not path.exists():
        path.write_bytes(rendered.pdf)
    rendered.path = path
    art = ResumeArtifact(
        application_id=application_id,
        client_id=client_id,
        path=str(path),
        content_hash=rendered.content_hash,
        template=template,
        page_count=rendered.page_count,
        tailored_json=tailored_json,
        fallback_to_base=fallback_to_base,
    )
    session.add(art)
    session.flush()
    log.info("resume_stored", client_id=client_id, application_id=application_id, hash=rendered.content_hash[:12], pages=rendered.page_count)
    return art
