from __future__ import annotations

import re
import uuid

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.models import UserRole

_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class CompanyRegisterRequest(BaseModel):
    """Creates a brand-new tenant plus its first Admin user in one step."""

    company_name: str = Field(..., min_length=2, max_length=255)
    company_slug: str = Field(
        ..., min_length=2, max_length=100,
        description="Lowercase, hyphen-separated, unique across all tenants — e.g. 'acme-freight'.",
    )
    admin_email: EmailStr
    admin_password: str = Field(
        ..., min_length=8, max_length=72,
        description="8-72 characters (bcrypt's hard limit; longer passwords are rejected here rather than silently truncated).",
    )
    admin_full_name: str = Field(..., min_length=1, max_length=255)

    @field_validator("company_slug")
    @classmethod
    def validate_slug(cls, value: str) -> str:
        value = value.lower()
        if not _SLUG_RE.match(value):
            raise ValueError("company_slug must be lowercase letters, numbers, and hyphens only (e.g. 'acme-freight').")
        return value


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    company_id: uuid.UUID
    email: str
    full_name: str
    role: UserRole
    is_active: bool
