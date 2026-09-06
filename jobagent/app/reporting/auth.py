from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import get_settings

_basic = HTTPBasic()


def require_admin(creds: HTTPBasicCredentials = Depends(_basic)) -> str:
    s = get_settings()
    ok = secrets.compare_digest(creds.username.encode(), s.admin_user.encode()) and secrets.compare_digest(creds.password.encode(), s.admin_password.encode())
    if not ok:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized", headers={"WWW-Authenticate": "Basic"})
    return creds.username
