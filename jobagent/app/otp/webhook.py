"""POST /webhooks/inbound-mail. Accepts:
  * generic JSON  {recipient, sender, subject, body_text, body_html, message_id}  (Cloudflare Email Worker)
  * Mailgun inbound routes (form-encoded: recipient, sender/from, subject, body-plain, stripped-html, Message-Id,
    timestamp, token, signature)
Auth: header `X-Webhook-Secret: <MAIL_WEBHOOK_SECRET>` or `Authorization: Bearer <secret>`, or a valid Mailgun
HMAC signature computed with the same secret (set MAIL_WEBHOOK_SECRET to your Mailgun webhook signing key)."""
from __future__ import annotations

import hashlib
import hmac
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.logging import get_logger

from .processor import InboundPayload, process_inbound

log = get_logger("otp.webhook")
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _mailgun_signature_ok(secret: str, timestamp: str, token: str, signature: str) -> bool:
    if not (timestamp and token and signature):
        return False
    digest = hmac.new(secret.encode(), f"{timestamp}{token}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, signature)


async def parse_request(request: Request) -> tuple[InboundPayload, dict]:
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("application/json"):
        data = await request.json()
        if not isinstance(data, dict):
            raise HTTPException(400, "json body must be an object")
        sig = data.get("signature") if isinstance(data.get("signature"), dict) else {}
        return (
            InboundPayload(
                recipient=str(data.get("recipient") or data.get("to") or ""),
                sender=str(data.get("sender") or data.get("from") or ""),
                subject=str(data.get("subject") or ""),
                body_text=str(data.get("body_text") or data.get("body-plain") or data.get("text") or ""),
                body_html=str(data.get("body_html") or data.get("stripped-html") or data.get("html") or ""),
                message_id=(data.get("message_id") or data.get("Message-Id") or None),
                raw={k: v for k, v in data.items() if k not in ("body_html", "stripped-html", "html")},
            ),
            {"timestamp": str(sig.get("timestamp", "")), "token": str(sig.get("token", "")), "signature": str(sig.get("signature", ""))},
        )
    form = await request.form()
    get = lambda *keys: next((str(form.get(k)) for k in keys if form.get(k) is not None), "")  # noqa: E731
    return (
        InboundPayload(
            recipient=get("recipient", "To", "to"),
            sender=get("sender", "from", "From"),
            subject=get("subject", "Subject"),
            body_text=get("body-plain", "stripped-text", "body_text", "text"),
            body_html=get("stripped-html", "body-html", "body_html", "html"),
            message_id=get("Message-Id", "message-id", "message_id") or None,
            raw={"provider": "form"},
        ),
        {"timestamp": get("timestamp"), "token": get("token"), "signature": get("signature")},
    )


def _authorized(request: Request, sig: dict) -> bool:
    secret = get_settings().mail_webhook_secret
    if not secret or secret == "change-me":
        log.error("webhook_secret_not_configured")
        return False
    header = request.headers.get("x-webhook-secret") or ""
    auth = request.headers.get("authorization") or ""
    if header and secrets.compare_digest(header, secret):
        return True
    if auth.lower().startswith("bearer ") and secrets.compare_digest(auth[7:].strip(), secret):
        return True
    return _mailgun_signature_ok(secret, sig.get("timestamp", ""), sig.get("token", ""), sig.get("signature", ""))


@router.post("/inbound-mail")
async def inbound_mail(request: Request, db: Session = Depends(get_db)):
    payload, sig = await parse_request(request)
    if not _authorized(request, sig):
        raise HTTPException(status_code=401, detail="bad webhook secret/signature")
    if not payload.recipient:
        raise HTTPException(status_code=400, detail="recipient missing")
    email = process_inbound(db, payload)
    db.commit()
    return {"id": email.id, "client_id": email.client_id, "classification": email.classification, "application_id": email.application_id, "forwarded": email.forwarded}
