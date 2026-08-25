"""
Single choke point for writing AuditLog rows, so every caller produces the
same shape and nobody accidentally forgets a field.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.models.models import AuditActorType, AuditLog, Document, Review, User


def write_audit_log(
    db: Session,
    *,
    company_id: uuid.UUID,
    entity_type: str,
    entity_id: uuid.UUID,
    event_type: str,
    actor_type: AuditActorType,
    actor_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    """
    Adds an AuditLog row to the session. Deliberately does NOT commit — the
    caller commits as part of its own request-scoped transaction, so the
    audit entry lands atomically with whatever it's documenting, and a
    failure partway through a request rolls the audit entry back with it
    rather than leaving an orphaned log for something that didn't happen.
    """
    entry = AuditLog(
        company_id=company_id,
        entity_type=entity_type,
        entity_id=entity_id,
        event_type=event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        details=details or {},
    )
    db.add(entry)
    return entry


@dataclass
class AuditLogEntry:
    """AuditLog plus the reviewer's name resolved in, for list_audit_log_for_load's callers."""

    id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    event_type: str
    actor_type: AuditActorType
    actor_name: str | None
    details: dict[str, Any]
    created_at: datetime


def list_audit_log_for_load(db: Session, company_id: uuid.UUID, load_id: uuid.UUID) -> list[AuditLogEntry]:
    """
    The full system-of-record trail for one load, merged into a single
    chronological timeline: every document received and extracted, every AI
    match decision (and any that failed), and every human review action.

    AuditLog rows are keyed by whatever entity they're actually about — a
    document's id, a review's id, or the load's own id for match decisions —
    never the load id uniformly, so this first looks up which documents and
    reviews belong to the load before it can know which rows to include.
    Ordered oldest first, so it reads top-to-bottom as the story of what
    happened.
    """
    document_ids = [
        row[0]
        for row in db.query(Document.id)
        .filter(Document.company_id == company_id, Document.load_id == load_id)
        .all()
    ]
    review_ids = [
        row[0]
        for row in db.query(Review.id).filter(Review.company_id == company_id, Review.load_id == load_id).all()
    ]

    conditions = [and_(AuditLog.entity_type == "load", AuditLog.entity_id == load_id)]
    if document_ids:
        conditions.append(and_(AuditLog.entity_type == "document", AuditLog.entity_id.in_(document_ids)))
    if review_ids:
        conditions.append(and_(AuditLog.entity_type == "review", AuditLog.entity_id.in_(review_ids)))

    entries = (
        db.query(AuditLog)
        .filter(AuditLog.company_id == company_id, or_(*conditions))
        .order_by(AuditLog.created_at.asc())
        .all()
    )

    actor_ids = {e.actor_id for e in entries if e.actor_id is not None}
    names: dict[uuid.UUID, str] = {}
    if actor_ids:
        for user in db.query(User).filter(User.id.in_(actor_ids)).all():
            names[user.id] = user.full_name

    return [
        AuditLogEntry(
            id=e.id,
            entity_type=e.entity_type,
            entity_id=e.entity_id,
            event_type=e.event_type,
            actor_type=e.actor_type,
            actor_name=names.get(e.actor_id) if e.actor_id is not None else None,
            details=e.details,
            created_at=e.created_at,
        )
        for e in entries
    ]
