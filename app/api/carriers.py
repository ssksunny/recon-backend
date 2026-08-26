"""
Admin-side carrier management: create a carrier, invite a broker to it, and
list carriers for the assign-carrier dropdown on a load. Load assignment
itself lives on app/api/loads.py (POST /loads/{id}/assign-carrier) since
it's fundamentally an action on a Load, not a Carrier.

Every route here requires get_current_admin, not just get_current_user —
inviting a broker or creating a carrier is an account-management action,
same tier as anything else Admin-gated in this app.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_admin
from app.core.config import settings
from app.models.database import get_db
from app.models.models import User
from app.schemas.broker import CarrierCreate, CarrierInviteCreate, CarrierInviteOut, CarrierOut, CarrierUserOut
from app.services import carrier_service

router = APIRouter()


@router.post("", response_model=CarrierOut, status_code=status.HTTP_201_CREATED)
def create_carrier(
    payload: CarrierCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin),
) -> CarrierOut:
    carrier = carrier_service.create_carrier(db, company_id=current_user.company_id, name=payload.name)
    return CarrierOut.model_validate(carrier)


@router.get("", response_model=list[CarrierOut])
def list_carriers(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin),
) -> list[CarrierOut]:
    carriers = carrier_service.list_carriers(db, current_user.company_id)
    return [CarrierOut.model_validate(c) for c in carriers]


@router.post("/{carrier_id}/invite", response_model=CarrierInviteOut, status_code=status.HTTP_201_CREATED)
def invite_carrier_user(
    carrier_id: uuid.UUID,
    payload: CarrierInviteCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin),
) -> CarrierInviteOut:
    """
    Creates (or re-invites) a broker login for this carrier and returns a
    one-time invite link. There's no transactional email set up yet (see
    Recon-Build-Status.md), so the admin sends invite_url to the broker
    themselves — it's a complete, ready-to-share link.
    """
    result = carrier_service.invite_carrier_user(
        db,
        company_id=current_user.company_id,
        carrier_id=carrier_id,
        email=payload.email,
        full_name=payload.full_name,
        inviting_user_id=current_user.id,
    )
    invite_url = f"{settings.broker_portal_url}/accept-invite?token={result.invite_token}"
    return CarrierInviteOut(
        carrier_user=CarrierUserOut.model_validate(result.carrier_user),
        invite_token=result.invite_token,
        invite_url=invite_url,
    )
