"""
Everything about Load entities: fetching (tenant-scoped), listing, the
exception queue, the computed match-status rollup, and creating/finding a
Load from an extracted rate confirmation.

Nothing here is FastAPI-specific — get_load raises NotFoundError rather than
HTTPException, and app/main.py's exception handler turns that into a 404.
That keeps this module (and its callers, including document_service and
matching_service) usable outside a request context.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.models import AuditLog, Company, Document, LineItem, Load, LoadStatus, MatchStatus, Review
from app.services.errors import NotFoundError


def get_load(db: Session, company_id: uuid.UUID, load_id: uuid.UUID) -> Load:
    """Fetches a Load scoped to company_id — the one place every caller enforces tenant scoping for loads."""
    load = db.query(Load).filter(Load.company_id == company_id, Load.id == load_id).one_or_none()
    if load is None:
        raise NotFoundError(f"Load {load_id} not found.")
    return load


def compute_load_match_status(db: Session, load_id: uuid.UUID) -> str:
    """
    The reviewer-facing status for a load: "clean" | "discrepancy" |
    "needs_info" | "no_data" (no line items yet — matching hasn't run).
    A single discrepancy anywhere outranks everything else, since one
    confirmed problem is reason enough to route the whole load to review —
    this mirrors the precedence app.ai.matching._finalize_decision uses.
    """
    statuses = {
        row[0] for row in db.query(LineItem.match_status).filter(LineItem.load_id == load_id).all()
    }
    if not statuses:
        return "no_data"
    if MatchStatus.DISCREPANCY in statuses:
        return "discrepancy"
    if MatchStatus.NEEDS_INFO in statuses:
        return "needs_info"
    return "clean"


def list_loads(
    db: Session, company_id: uuid.UUID, match_status_filter: str | None = None
) -> list[tuple[Load, str]]:
    """All loads for a tenant, newest first, each paired with its computed match status."""
    loads = (
        db.query(Load)
        .filter(Load.company_id == company_id)
        .order_by(Load.created_at.desc())
        .all()
    )
    pairs = [(load, compute_load_match_status(db, load.id)) for load in loads]
    if match_status_filter:
        pairs = [(load, status) for load, status in pairs if status == match_status_filter]
    return pairs


def list_exception_queue(db: Session, company_id: uuid.UUID) -> list[tuple[Load, str]]:
    """
    The default reviewer view: every load with at least one line item
    flagged discrepancy or needs_info, oldest first (the ones waiting
    longest surface first).
    """
    flagged_load_ids = (
        db.query(LineItem.load_id)
        .join(Load, Load.id == LineItem.load_id)
        .filter(
            Load.company_id == company_id,
            LineItem.match_status.in_([MatchStatus.DISCREPANCY, MatchStatus.NEEDS_INFO]),
        )
        .distinct()
        .all()
    )
    load_ids = [row[0] for row in flagged_load_ids]
    if not load_ids:
        return []

    loads = (
        db.query(Load)
        .filter(Load.company_id == company_id, Load.id.in_(load_ids))
        .order_by(Load.created_at.asc())
        .all()
    )
    return [(load, compute_load_match_status(db, load.id)) for load in loads]


@dataclass
class LoadDetail:
    """Everything a load's detail view needs, bundled so the router does zero querying of its own."""
    load: Load
    documents: list[Document]
    line_items: list[LineItem]
    reviews: list[Review]
    match_status: str
    match_result: dict[str, Any] | None = None


def _latest_match_result(db: Session, company_id: uuid.UUID, load_id: uuid.UUID) -> dict[str, Any] | None:
    """
    The full match_invoice() decision — summary, recommended_action,
    confidence, totals — isn't stored as columns on Load; it's persisted
    as the "match_decision" audit log entry's `details` (see
    matching_service.run_matching_for_load). Pulling the most recent one
    back out here avoids adding a second source of truth for the same data.
    """
    entry = (
        db.query(AuditLog)
        .filter(
            AuditLog.company_id == company_id,
            AuditLog.entity_type == "load",
            AuditLog.entity_id == load_id,
            AuditLog.event_type == "match_decision",
        )
        .order_by(AuditLog.created_at.desc())
        .first()
    )
    return entry.details if entry is not None else None


def get_load_detail(db: Session, company_id: uuid.UUID, load_id: uuid.UUID) -> LoadDetail:
    load = get_load(db, company_id, load_id)
    documents = (
        db.query(Document).filter(Document.load_id == load.id).order_by(Document.received_at.asc()).all()
    )
    line_items = (
        db.query(LineItem).filter(LineItem.load_id == load.id).order_by(LineItem.created_at.asc()).all()
    )
    reviews = (
        db.query(Review).filter(Review.load_id == load.id).order_by(Review.created_at.desc()).all()
    )
    return LoadDetail(
        load=load,
        documents=documents,
        line_items=line_items,
        reviews=reviews,
        match_status=compute_load_match_status(db, load.id),
        match_result=_latest_match_result(db, company_id, load.id),
    )


def list_reviews_for_load(db: Session, load_id: uuid.UUID) -> list[Review]:
    return db.query(Review).filter(Review.load_id == load_id).order_by(Review.created_at.desc()).all()


def _parse_extracted_date(value: Any) -> date | None:
    """Best-effort parse of a date/datetime string Claude returned. Never raises — bad input just becomes None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def get_or_create_load_from_rate_confirmation(db: Session, company: Company, extracted: dict[str, Any]) -> Load:
    """
    Finds the Load this rate confirmation belongs to (by load_number), or
    creates one. Rate confirmations are the only document type allowed to
    create a Load, since they're the source of truth for its terms — an
    invoice or POD that shows up first is held unattached until a rate
    confirmation establishes the load (see find_load_by_number).
    """
    load_number = extracted.get("load_number") or f"UNKNOWN-{uuid.uuid4().hex[:8]}"

    existing = (
        db.query(Load)
        .filter(Load.company_id == company.id, Load.load_number == load_number)
        .one_or_none()
    )
    if existing is not None:
        return existing

    load = Load(
        company_id=company.id,
        load_number=load_number,
        carrier_name=extracted.get("carrier_name") or "Unknown carrier",
        origin=extracted.get("origin"),
        destination=extracted.get("destination"),
        equipment_type=extracted.get("equipment_type"),
        pickup_date=_parse_extracted_date(extracted.get("pickup_date")),
        delivery_date=_parse_extracted_date(extracted.get("delivery_date")),
        linehaul_rate=extracted.get("linehaul_rate"),
        fuel_surcharge_terms=extracted.get("fuel_surcharge_terms") or {},
        detention_terms=extracted.get("detention_terms") or {},
        accessorials_allowed=extracted.get("accessorials_allowed") or [],
        status=LoadStatus.ACTIVE,
    )
    db.add(load)
    db.flush()  # assigns load.id without ending the request's transaction
    return load


def find_load_by_number(db: Session, company_id: uuid.UUID, load_number: str | None) -> Load | None:
    """Used to auto-link an invoice/POD to an existing load when the caller doesn't pass load_id explicitly."""
    if not load_number:
        return None
    return (
        db.query(Load)
        .filter(Load.company_id == company_id, Load.load_number == load_number)
        .one_or_none()
    )
