"""Outbound mail. MAIL_PROVIDER=log (default, prints), mailgun, or smtp. Every send is recorded in-memory
(`sent_log`) so tests and the admin page can inspect what went out."""
from __future__ import annotations

import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage

from app.config import get_settings
from app.logging import get_logger

log = get_logger("mail")


@dataclass
class OutboundMail:
    to: str
    subject: str
    text: str
    html: str | None = None
    reply_to: str | None = None
    headers: dict = field(default_factory=dict)


sent_log: list[OutboundMail] = []


def send_mail(to: str, subject: str, text: str, html: str | None = None, reply_to: str | None = None, headers: dict | None = None) -> bool:
    settings = get_settings()
    msg = OutboundMail(to=to, subject=subject, text=text, html=html, reply_to=reply_to, headers=headers or {})
    sent_log.append(msg)
    if len(sent_log) > 500:
        del sent_log[:-500]
    provider = settings.mail_provider
    try:
        if provider == "mailgun":
            _send_mailgun(settings, msg)
        elif provider == "smtp":
            _send_smtp(settings, msg)
        else:
            log.info("mail_logged", to=to, subject=subject, provider="log", preview=text[:200])
        return True
    except Exception as e:  # noqa: BLE001
        log.error("mail_failed", to=to, subject=subject, error=str(e))
        return False


def _send_mailgun(settings, msg: OutboundMail) -> None:
    import httpx

    data = {"from": settings.mail_from, "to": msg.to, "subject": msg.subject, "text": msg.text}
    if msg.html:
        data["html"] = msg.html
    if msg.reply_to:
        data["h:Reply-To"] = msg.reply_to
    for k, v in msg.headers.items():
        data[f"h:{k}"] = v
    r = httpx.post(f"https://api.mailgun.net/v3/{settings.mailgun_domain}/messages", auth=("api", settings.mailgun_api_key), data=data, timeout=30)
    r.raise_for_status()
    log.info("mail_sent", to=msg.to, subject=msg.subject, provider="mailgun")


def _send_smtp(settings, msg: OutboundMail) -> None:
    em = EmailMessage()
    em["From"] = settings.mail_from
    em["To"] = msg.to
    em["Subject"] = msg.subject
    if msg.reply_to:
        em["Reply-To"] = msg.reply_to
    for k, v in msg.headers.items():
        em[k] = v
    em.set_content(msg.text)
    if msg.html:
        em.add_alternative(msg.html, subtype="html")
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as s:
        s.starttls()
        if settings.smtp_user:
            s.login(settings.smtp_user, settings.smtp_password)
        s.send_message(em)
    log.info("mail_sent", to=msg.to, subject=msg.subject, provider="smtp")
