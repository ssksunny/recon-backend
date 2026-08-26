"""
Shared FastAPI dependencies: current-user resolution and role checks.

This is where multi-tenant scoping actually gets enforced: every route
depends on `get_current_user` (or `get_current_admin`) and then filters its
queries on `current_user.company_id` — never on a company_id read from the
request body or path, which a caller could tamper with.
"""

from __future__ import annotations

import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import TokenError, decode_access_token
from app.models.database import get_db
from app.models.models import CarrierUser, User, UserRole

# tokenUrl is just what Swagger's "Authorize" button POSTs to — it doesn't
# affect how tokens issued elsewhere are validated.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.api_v1_prefix}/auth/login")

# A separate scheme (and a separate token endpoint) for the broker portal —
# see get_current_broker_user below for why admin/reviewer and broker
# tokens are never interchangeable, not even accidentally.
broker_oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.api_v1_prefix}/broker/auth/login")


def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials.",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = decode_access_token(token)
    except TokenError as exc:
        raise credentials_error from exc

    # Defense in depth against a broker token ever being accepted here: a
    # User token always carries "typ": "user" (see app/api/auth.py); a
    # broker token carries "typ": "broker" and would be rejected below.
    if payload.get("typ") != "user":
        raise credentials_error

    raw_user_id = payload.get("sub")
    if raw_user_id is None:
        raise credentials_error

    try:
        user_id = uuid.UUID(raw_user_id)
    except ValueError:
        raise credentials_error from None

    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise credentials_error

    return user


def get_current_admin(current_user: User = Depends(get_current_user)) -> User:
    """Use as a dependency on routes that only Admins should reach (e.g. user management)."""
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="This action requires an admin role.")
    return current_user


def get_current_broker_user(
    token: str = Depends(broker_oauth2_scheme), db: Session = Depends(get_db)
) -> CarrierUser:
    """
    The broker-portal equivalent of get_current_user. Resolves a
    CarrierUser instead of a User, from a token issued by
    app.services.carrier_service.issue_broker_token.

    The "typ": "broker" check is the load-bearing line here: users.id and
    carrier_users.id are both application-generated uuid4s, so in principle
    a value could collide between the two tables. Checking "typ" first
    means a token minted for one principal type can never be decoded into
    the other, regardless of any id collision — it fails here before the
    id is ever looked up.
    """
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials.",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = decode_access_token(token)
    except TokenError as exc:
        raise credentials_error from exc

    if payload.get("typ") != "broker":
        raise credentials_error

    raw_id = payload.get("sub")
    if raw_id is None:
        raise credentials_error

    try:
        carrier_user_id = uuid.UUID(raw_id)
    except ValueError:
        raise credentials_error from None

    carrier_user = db.get(CarrierUser, carrier_user_id)
    if carrier_user is None or not carrier_user.is_active or not carrier_user.carrier.is_active:
        raise credentials_error

    return carrier_user
