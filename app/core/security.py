"""
Password hashing and JWT helpers. Nothing here talks to the database or
FastAPI — that's app/api/deps.py's job. Keeping this module framework- and
ORM-agnostic makes it trivial to unit test and to reuse from, say, a CLI
admin-user-creation script later.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt

from app.core.config import settings

# Hashing directly with the `bcrypt` package rather than through passlib:
# passlib 1.7.x is unmaintained and its bcrypt backend-detection probe
# breaks against bcrypt>=4.1 (a known upstream incompatibility), so this
# sidesteps that entirely rather than pinning around it.
#
# bcrypt has a hard 72-byte input limit — see CompanyRegisterRequest's
# max_length on admin_password for where that's enforced before it ever
# reaches here.


def hash_password(plain_password: str) -> str:
    return bcrypt.hashpw(plain_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
    except ValueError:
        # Malformed stored hash, or a >72-byte password on a login attempt
        # against an old hash — either way, that's a failed verification,
        # not a 500.
        return False


class TokenError(Exception):
    """Raised when a JWT is missing, malformed, expired, or otherwise invalid."""


def create_access_token(data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
    """
    Encodes `data` into a signed JWT. Callers pass whatever claims they need
    (this app uses "sub" = user id, "company_id", "role") — this function
    just adds the expiry and signs it.
    """
    to_encode = dict(data)
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.jwt_access_token_expire_minutes)
    )
    to_encode["exp"] = expire
    return jwt.encode(to_encode, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("Token has expired.") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("Token is invalid.") from exc
