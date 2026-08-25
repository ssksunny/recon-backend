from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.models.models import LineItemType, LoadStatus, MatchStatus
from app.schemas.documents import DocumentOut, MatchResultOut
from app.schemas.reviews import ReviewOut


class LineItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    line_type: LineItemType
    description: str | None
    billed_amount: Decimal
    expected_amount: Decimal | None
    variance_amount: Decimal | None
    match_status: MatchStatus
    match_reason: str | None


class LoadListItem(BaseModel):
    """
    Not built via model_validate(load) — match_status is computed from the
    load's line items, not a column on Load, so routers construct this
    directly with keyword arguments.
    """
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    load_number: str
    carrier_name: str
    status: LoadStatus
    match_status: str  # "clean" | "discrepancy" | "needs_info" | "no_data"
    linehaul_rate: Decimal | None
    pickup_date: date | None
    delivery_date: date | None
    created_at: datetime


class LoadDetail(LoadListItem):
    origin: str | None
    destination: str | None
    equipment_type: str | None
    fuel_surcharge_terms: dict
    detention_terms: dict
    accessorials_allowed: list
    documents: list[DocumentOut]
    line_items: list[LineItemOut]
    reviews: list[ReviewOut]
    # The most recent match_decision audit entry's payload — summary,
    # recommended_action, confidence, and totals — or None if matching
    # hasn't run yet for this load. Not a column on Load; see
    # load_service.get_load_detail for how it's pulled from the audit log.
    match_result: MatchResultOut | None
