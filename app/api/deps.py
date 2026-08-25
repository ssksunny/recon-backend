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
from app.models.models import User, UserRole

# tokenUrl is just what Swagger's "Authorize" button POSTs to — it doesn't
# affect how tokens issued elsewhere are validated.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.api_v1_prefix}/auth/login")


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
