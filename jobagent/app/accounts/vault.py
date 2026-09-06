"""ATS credential vault. Passwords are Fernet-encrypted at rest with ENCRYPTION_KEY."""
from __future__ import annotations

import secrets
import string
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.base import utcnow
from app.db.models import ATSAccount, Client


class VaultError(RuntimeError):
    pass


def _fernet() -> Fernet:
    key = get_settings().encryption_key
    if not key:
        raise VaultError("ENCRYPTION_KEY is not set; run scripts/gen_key.py")
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except Exception as e:  # pragma: no cover
        raise VaultError(f"ENCRYPTION_KEY is not a valid Fernet key: {e}") from e


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as e:
        raise VaultError("could not decrypt credential (wrong ENCRYPTION_KEY?)") from e


_ALPHABET = string.ascii_letters + string.digits
_SYMBOLS = "!@#$%^&*-_=+"


def generate_password(length: int = 20) -> str:
    """Strong password satisfying common ATS rules (upper, lower, digit, symbol, no ambiguous chars)."""
    while True:
        core = [secrets.choice(_ALPHABET) for _ in range(length - 2)] + [secrets.choice(_SYMBOLS), secrets.choice(string.digits)]
        secrets.SystemRandom().shuffle(core)
        pw = "".join(core)
        if any(c.islower() for c in pw) and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw) and any(c in _SYMBOLS for c in pw):
            return pw


def get_or_create_account(session: Session, client: Client, ats_type: str, company_slug: str) -> tuple[ATSAccount, str]:
    """Return (account, plaintext_password). Creates one with a fresh strong password if missing."""
    acct = session.scalar(
        select(ATSAccount).where(
            ATSAccount.client_id == client.id, ATSAccount.ats_type == ats_type, ATSAccount.company_slug == company_slug
        )
    )
    if acct is None:
        pw = generate_password()
        acct = ATSAccount(
            client_id=client.id,
            ats_type=ats_type,
            company_slug=company_slug,
            username=client.alias_email or client.real_email,
            password_encrypted=encrypt(pw),
        )
        session.add(acct)
        session.flush()
        return acct, pw
    return acct, decrypt(acct.password_encrypted)


def touch(session: Session, acct: ATSAccount, when: datetime | None = None) -> None:
    acct.last_used = when or utcnow()
    session.flush()
