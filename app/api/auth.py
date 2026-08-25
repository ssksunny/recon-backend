"""
Auth: register a new company (tenant) with its first Admin user, log in,
and fetch the current user. There's no email-based user invite flow yet —
an Admin adding teammates is a natural next endpoint, out of scope here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.security import create_access_token, hash_password, verify_password
from app.models.database import get_db
from app.models.models import AuditActorType, Company, User, UserRole
from app.schemas.auth import CompanyRegisterRequest, TokenResponse, UserOut
from app.services.audit_service import write_audit_log

router = APIRouter()


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register_company(payload: CompanyRegisterRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """Creates a new tenant and its first Admin user, and logs that user in immediately."""
    existing_company = db.query(Company).filter(Company.slug == payload.company_slug).one_or_none()
    if existing_company is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"company_slug {payload.company_slug!r} is already taken.")

    company = Company(
        name=payload.company_name,
        slug=payload.company_slug,
        inbound_email=f"{payload.company_slug}@{settings.inbound_email_domain}",
    )
    db.add(company)
    db.flush()  # assigns company.id

    admin_user = User(
        company_id=company.id,
        email=payload.admin_email,
        hashed_password=hash_password(payload.admin_password),
        full_name=payload.admin_full_name,
        role=UserRole.ADMIN,
    )
    db.add(admin_user)
    db.flush()  # assigns admin_user.id

    write_audit_log(
        db,
        company_id=company.id,
        entity_type="company",
        entity_id=company.id,
        event_type="company_registered",
        actor_type=AuditActorType.USER,
        actor_id=admin_user.id,
        details={"company_name": company.name, "admin_email": admin_user.email},
    )

    db.commit()

    token = create_access_token({"sub": str(admin_user.id), "company_id": str(company.id), "role": admin_user.role.value})
    return TokenResponse(access_token=token)


@router.post("/login", response_model=TokenResponse)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)) -> TokenResponse:
    """
    Standard OAuth2 password flow (username=email, password=password) so
    Swagger's built-in "Authorize" button and standard OAuth2 client
    libraries work out of the box.
    """
    invalid_credentials = HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        detail="Incorrect email or password.",
        headers={"WWW-Authenticate": "Bearer"},
    )

    # Email is unique per-company (see User's uq_users_company_email
    # constraint), not globally — if this ever needs to support one person
    # belonging to multiple companies, this lookup is the place that changes.
    user = (
        db.query(User)
        .filter(User.email == form_data.username, User.is_active.is_(True))
        .order_by(User.created_at)
        .first()
    )
    if user is None or not verify_password(form_data.password, user.hashed_password):
        raise invalid_credentials
    if not user.company.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="This company's account is inactive.")

    token = create_access_token({"sub": str(user.id), "company_id": str(user.company_id), "role": user.role.value})
    return TokenResponse(access_token=token)


@router.get("/me", response_model=UserOut)
def get_me(current_user: User = Depends(get_current_user)) -> UserOut:
    return UserOut.model_validate(current_user)
