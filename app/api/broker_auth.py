"""
Broker portal auth: accept an admin-issued invite (set a password, activate
the account) and log in. Deliberately its own router/prefix (/broker/auth,
see app/main.py) rather than reusing app/api/auth.py's routes — a broker is
a CarrierUser, not a User, and the two token shapes must never be
interchangeable (see app/api/deps.py:get_current_broker_user).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app.api.deps import get_current_broker_user
from app.models.database import get_db
from app.models.models import CarrierUser
from app.schemas.auth import TokenResponse
from app.schemas.broker import AcceptInviteRequest, CarrierUserOut
from app.services import carrier_service

router = APIRouter()


@router.post("/accept-invite", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def accept_invite(payload: AcceptInviteRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """Redeems an invite token from POST /carriers/{id}/invite, sets a password, and logs the broker in immediately."""
    carrier_user = carrier_service.accept_invite(db, token=payload.token, password=payload.password)
    token = carrier_service.issue_broker_token(carrier_user)
    return TokenResponse(access_token=token)


@router.post("/login", response_model=TokenResponse)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)) -> TokenResponse:
    """Standard OAuth2 password flow (username=email), same shape as /auth/login."""
    invalid_credentials = HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        detail="Incorrect email or password.",
        headers={"WWW-Authenticate": "Bearer"},
    )

    carrier_user = carrier_service.authenticate_broker(db, email=form_data.username, password=form_data.password)
    if carrier_user is None:
        raise invalid_credentials
    if not carrier_user.carrier.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="This carrier account is inactive.")

    token = carrier_service.issue_broker_token(carrier_user)
    return TokenResponse(access_token=token)


@router.get("/me", response_model=CarrierUserOut)
def get_me(current_broker: CarrierUser = Depends(get_current_broker_user)) -> CarrierUserOut:
    return CarrierUserOut.model_validate(current_broker)
