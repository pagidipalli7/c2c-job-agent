from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.db.models import Client

from .intake import IntakeError, approve_base_resume, upsert_client

router = APIRouter(prefix="/clients", tags=["clients"])


@router.post("")
def create_or_update_client(doc: dict, db: Session = Depends(get_db)):
    try:
        client = upsert_client(db, doc)
        db.commit()
    except IntakeError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"id": client.id, "alias_email": client.alias_email, "name": client.name}


@router.get("")
def list_clients(db: Session = Depends(get_db)):
    rows = db.scalars(select(Client)).all()
    return [
        {
            "id": c.id,
            "name": c.name,
            "alias_email": c.alias_email,
            "status": c.status,
            "base_resume_approved": bool(c.base_resume and c.base_resume.approved),
        }
        for c in rows
    ]


@router.post("/{client_id}/approve-resume")
def approve_resume(client_id: int, db: Session = Depends(get_db)):
    try:
        br = approve_base_resume(db, client_id)
        db.commit()
    except IntakeError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"client_id": client_id, "approved": br.approved, "version": br.version}
