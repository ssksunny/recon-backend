from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.models import LoadStatus
from app.schemas.documents import DocumentOut, MatchResultOut
from app.schemas.loads import LineItemOut


class CarrierOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    name: str
    is_active: bool
    created_at: datetime


class CarrierCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)


class CarrierInviteCreate(BaseModel):
    email: EmailStr
    full_name: str = Field(..., min_length=1, max_length=255)


class CarrierUserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    carrier_id: uuid.UUID
    email: str
    full_name: str
    is_active: bool


class CarrierInviteOut(BaseModel):
    """
    Returned to the admin after POST /carriers/{id}/invite. There's no
    transactional email wired up yet (see Recon-Build-Status.md), so
    invite_url is the thing an admin copies and sends the broker directly —
    it's a complete link the broker just opens; there's nothing else they
    need to be told.
    """

    carrier_user: CarrierUserOut
    invite_token: str
    invite_url: str


class AssignCarrierRequest(BaseModel):
    # None unassigns — revokes portal visibility without deleting the carrier.
    carrier_id: uuid.UUID | None = None


class AcceptInviteRequest(BaseModel):
    token: str
    password: str = Field(
        ..., min_length=8, max_length=72,
        description="8-72 characters (bcrypt's hard limit; longer passwords are rejected here rather than silently truncated).",
    )


class BrokerRespondRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)


class BrokerDocumentType(str, enum.Enum):
    """
    The subset of DocumentType a broker is allowed to upload. Deliberately
    excludes rate_confirmation — that's the document that establishes a
    Load in the first place and stays admin/email-ingestion only, so a
    broker portal upload can never create or silently overwrite the terms
    Recon audits their own invoice against.
    """

    INVOICE = "invoice"
    POD = "pod"


class BrokerLoadListItem(BaseModel):
    """The broker-portal analog of LoadListItem — no carrier_name column, since a broker already knows which carrier they are."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    load_number: str
    status: LoadStatus
    match_status: str  # "clean" | "discrepancy" | "needs_info" | "no_data"
    linehaul_rate: Decimal | None
    pickup_date: date | None
    delivery_date: date | None
    created_at: datetime


class BrokerLoadDetail(BrokerLoadListItem):
    """
    The broker-portal analog of LoadDetail — deliberately omits `reviews`:
    internal reviewer notes and identities are not part of what a broker
    was scoped to see (status + reason, their own documents, and the
    ability to respond) — see the scoping decision behind Task #56.
    """

    origin: str | None
    destination: str | None
    equipment_type: str | None
    documents: list[DocumentOut]
    line_items: list[LineItemOut]
    match_result: MatchResultOut | None
