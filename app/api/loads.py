"""
Loads: list, the exception queue (the default reviewer view), and
single-load detail. All querying and computed-status logic lives in
app/services/load_service.py — this router only translates HTTP <-> service
calls, and never touches company_id filtering itself (that's load_service's
job, so it can't be forgotten in a route that adds a new query).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.models.database import get_db
from app.models.models import Load, User
from app.schemas.audit import AuditLogEntryOut
from app.schemas.documents import DocumentOut, MatchResultOut
from app.schemas.loads import LineItemOut, LoadDetail, LoadListItem
from app.schemas.reviews import ReviewOut
from app.services import audit_service, load_service

router = APIRouter()


def _to_list_item(load: Load, match_status: str) -> LoadListItem:
    return LoadListItem(
        id=load.id,
        load_number=load.load_number,
        carrier_name=load.carrier_name,
        status=load.status,
        match_status=match_status,
        linehaul_rate=load.linehaul_rate,
        pickup_date=load.pickup_date,
        delivery_date=load.delivery_date,
        created_at=load.created_at,
    )


@router.get("", response_model=list[LoadListItem])
def list_loads(
    match_status: str | None = Query(
        None, description="Filter by computed status: clean, discrepancy, needs_info, no_data."
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[LoadListItem]:
    pairs = load_service.list_loads(db, current_user.company_id, match_status_filter=match_status)
    return [_to_list_item(load, status) for load, status in pairs]


@router.get("/exceptions", response_model=list[LoadListItem])
def exception_queue(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[LoadListItem]:
    pairs = load_service.list_exception_queue(db, current_user.company_id)
    return [_to_list_item(load, status) for load, status in pairs]


@router.get("/{load_id}", response_model=LoadDetail)
def get_load(
    load_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> LoadDetail:
    detail = load_service.get_load_detail(db, current_user.company_id, load_id)
    load = detail.load
    return LoadDetail(
        id=load.id,
        load_number=load.load_number,
        carrier_name=load.carrier_name,
        status=load.status,
        match_status=detail.match_status,
        linehaul_rate=load.linehaul_rate,
        pickup_date=load.pickup_date,
        delivery_date=load.delivery_date,
        created_at=load.created_at,
        origin=load.origin,
        destination=load.destination,
        equipment_type=load.equipment_type,
        fuel_surcharge_terms=load.fuel_surcharge_terms,
        detention_terms=load.detention_terms,
        accessorials_allowed=load.accessorials_allowed,
        documents=[DocumentOut.model_validate(d) for d in detail.documents],
        line_items=[LineItemOut.model_validate(li) for li in detail.line_items],
        reviews=[ReviewOut.model_validate(r) for r in detail.reviews],
        match_result=MatchResultOut(**detail.match_result) if detail.match_result else None,
    )


@router.get("/{load_id}/audit", response_model=list[AuditLogEntryOut])
def get_load_audit_trail(
    load_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[AuditLogEntryOut]:
    load_service.get_load(db, current_user.company_id, load_id)  # 404s if it's not this company's load
    entries = audit_service.list_audit_log_for_load(db, current_user.company_id, load_id)
    return [AuditLogEntryOut.model_validate(e) for e in entries]
