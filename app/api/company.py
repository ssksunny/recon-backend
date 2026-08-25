"""
Company profile: just enough for a reviewer/admin to see their own tenant's
details in the frontend — most usefully the inbound email address they'd
give to carriers (or set up as a forwarding target) for automatic document
ingestion. No update endpoint yet; that's a natural next addition once the
frontend needs to let an Admin rename the company or rotate the address.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_current_user
from app.models.models import User
from app.schemas.company import CompanyOut

router = APIRouter()


@router.get("/me", response_model=CompanyOut)
def get_my_company(current_user: User = Depends(get_current_user)) -> CompanyOut:
    return CompanyOut.model_validate(current_user.company)
