"""
Carrier (broker) account management for the broker self-service portal.

This is the one place that enforces the two isolation rules the whole
feature depends on:

  1. A carrier only ever sees a Load an admin explicitly assigned to it
     (Load.carrier_id) — never anything derived from the free-text
     Load.carrier_name Claude extracts from a rate confirmation. That
     string is untrusted model output and two real carriers can share a
     near-identical name, so auto-matching on it would risk one carrier
     seeing another's invoices. See assign_carrier_to_load.

  2. A broker's session can never be confused with a Recon User's. Broker
     JWTs are minted by issue_broker_token (role="broker", "typ"="broker")
     and read back by app.api.deps.get_current_broker_user — a completely
     separate code path from User auth, not a shared one branching on role.

Every "list" or "get" function here takes carrier_id (and, for
defense-in-depth, company_id too where the caller already has it) and
filters by it — there is no function in this module that returns a Load or
Document without that filter, mirroring how app/services/load_service.py
never queries Load without company_id.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core.security import (
    TokenError,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from app.models.models import AuditActorType, Carrier, CarrierUser, Document, LineItem, Load
from app.services import load_service
from app.services.audit_service import write_audit_log
from app.services.errors import NotFoundError, ValidationError

# How long an invite link stays valid before a broker who hasn't accepted
# it yet needs a fresh one. Generous on purpose — there's no automated
# email delivery yet (see Recon-Build-Status.md), so an admin is manually
# forwarding this link and it may sit in an inbox for a few days.
INVITE_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days


# --------------------------------------------------------------------------
# Admin side: carriers and invites
# --------------------------------------------------------------------------

def create_carrier(db: Session, *, company_id: uuid.UUID, name: str) -> Carrier:
    existing = db.query(Carrier).filter(Carrier.company_id == company_id, Carrier.name == name).one_or_none()
    if existing is not None:
        raise ValidationError(f"A carrier named {name!r} already exists.")
    carrier = Carrier(company_id=company_id, name=name)
    db.add(carrier)
    db.commit()
    db.refresh(carrier)
    return carrier


def list_carriers(db: Session, company_id: uuid.UUID) -> list[Carrier]:
    return db.query(Carrier).filter(Carrier.company_id == company_id).order_by(Carrier.name).all()


def get_carrier(db: Session, company_id: uuid.UUID, carrier_id: uuid.UUID) -> Carrier:
    carrier = db.query(Carrier).filter(Carrier.company_id == company_id, Carrier.id == carrier_id).one_or_none()
    if carrier is None:
        raise NotFoundError("Carrier not found.")
    return carrier


@dataclass
class InviteResult:
    carrier_user: CarrierUser
    invite_token: str


def invite_carrier_user(
    db: Session,
    *,
    company_id: uuid.UUID,
    carrier_id: uuid.UUID,
    email: str,
    full_name: str,
    inviting_user_id: uuid.UUID,
) -> InviteResult:
    """
    Creates (or re-invites) a CarrierUser and returns a one-time invite
    token to hand the broker — there's no transactional email wired up yet,
    so the admin is expected to send this link themselves (e.g. the reply-
    to address already set up on the company's inbound email). The link
    embeds no password; accept_invite() is where the broker sets one.
    """
    carrier = get_carrier(db, company_id, carrier_id)  # 404s if this isn't the caller's carrier

    email = email.strip().lower()
    existing = (
        db.query(CarrierUser)
        .filter(CarrierUser.carrier_id == carrier.id, CarrierUser.email == email)
        .one_or_none()
    )
    if existing is not None and existing.hashed_password is not None:
        raise ValidationError(f"{email!r} has already accepted an invite for this carrier.")

    carrier_user = existing or CarrierUser(carrier_id=carrier.id, email=email, is_active=False)
    carrier_user.full_name = full_name
    db.add(carrier_user)
    db.flush()  # assigns carrier_user.id on first invite

    write_audit_log(
        db,
        company_id=company_id,
        entity_type="carrier",
        entity_id=carrier.id,
        event_type="broker_invited",
        actor_type=AuditActorType.USER,
        actor_id=inviting_user_id,
        details={"carrier_user_email": email, "carrier_user_id": str(carrier_user.id)},
    )
    db.commit()
    db.refresh(carrier_user)

    invite_token = create_access_token(
        {"sub": str(carrier_user.id), "typ": "broker_invite"},
        expires_delta=timedelta(minutes=INVITE_TOKEN_EXPIRE_MINUTES),
    )
    return InviteResult(carrier_user=carrier_user, invite_token=invite_token)


def accept_invite(db: Session, *, token: str, password: str) -> CarrierUser:
    """Redeems an invite token (from invite_carrier_user) by setting a password and activating the account."""
    invalid = ValidationError("This invite link is invalid or has expired.")

    try:
        payload = decode_access_token(token)
    except TokenError as exc:
        raise invalid from exc

    if payload.get("typ") != "broker_invite":
        raise invalid

    raw_id = payload.get("sub")
    try:
        carrier_user_id = uuid.UUID(raw_id) if raw_id else None
    except ValueError:
        carrier_user_id = None
    if carrier_user_id is None:
        raise invalid

    carrier_user = db.get(CarrierUser, carrier_user_id)
    if carrier_user is None:
        raise invalid

    carrier_user.hashed_password = hash_password(password)
    carrier_user.is_active = True
    db.commit()
    db.refresh(carrier_user)
    return carrier_user


def assign_carrier_to_load(
    db: Session,
    *,
    company_id: uuid.UUID,
    load_id: uuid.UUID,
    carrier_id: uuid.UUID | None,
    actor_user_id: uuid.UUID,
) -> Load:
    """
    The only place Load.carrier_id is ever set. Both the load and the
    carrier are looked up scoped to company_id, so this can't be used to
    hand a load to a carrier belonging to a different tenant even if a
    caller somehow supplied a foreign carrier_id. Pass carrier_id=None to
    unassign (revoke portal visibility) without deleting the carrier.
    """
    load = db.query(Load).filter(Load.company_id == company_id, Load.id == load_id).one_or_none()
    if load is None:
        raise NotFoundError("Load not found.")

    carrier = get_carrier(db, company_id, carrier_id) if carrier_id is not None else None

    load.carrier_id = carrier.id if carrier is not None else None
    write_audit_log(
        db,
        company_id=company_id,
        entity_type="load",
        entity_id=load.id,
        event_type="carrier_assigned" if carrier is not None else "carrier_unassigned",
        actor_type=AuditActorType.USER,
        actor_id=actor_user_id,
        details={"carrier_id": str(carrier.id) if carrier else None, "carrier_name": carrier.name if carrier else None},
    )
    db.commit()
    db.refresh(load)
    return load


# --------------------------------------------------------------------------
# Broker side: auth and scoped data access
# --------------------------------------------------------------------------

def authenticate_broker(db: Session, *, email: str, password: str) -> CarrierUser | None:
    """Returns the matching active CarrierUser, or None on any auth failure — the router decides the HTTP response."""
    carrier_user = (
        db.query(CarrierUser)
        .filter(CarrierUser.email == email.strip().lower(), CarrierUser.is_active.is_(True))
        .order_by(CarrierUser.created_at)
        .first()
    )
    if carrier_user is None or not carrier_user.hashed_password:
        return None
    if not verify_password(password, carrier_user.hashed_password):
        return None
    return carrier_user


def issue_broker_token(carrier_user: CarrierUser) -> str:
    return create_access_token(
        {
            "sub": str(carrier_user.id),
            "carrier_id": str(carrier_user.carrier_id),
            "company_id": str(carrier_user.carrier.company_id),
            "role": "broker",
            "typ": "broker",
        }
    )


def list_loads_for_carrier(db: Session, *, company_id: uuid.UUID, carrier_id: uuid.UUID) -> list[Load]:
    return (
        db.query(Load)
        .filter(Load.company_id == company_id, Load.carrier_id == carrier_id)
        .order_by(Load.created_at.desc())
        .all()
    )


def get_load_for_carrier(db: Session, *, company_id: uuid.UUID, carrier_id: uuid.UUID, load_id: uuid.UUID) -> Load:
    load = (
        db.query(Load)
        .filter(Load.company_id == company_id, Load.carrier_id == carrier_id, Load.id == load_id)
        .one_or_none()
    )
    if load is None:
        raise NotFoundError("Load not found.")
    return load


@dataclass
class CarrierLoadDetail:
    """
    The broker-scoped analog of load_service.LoadDetail — no `reviews`
    field; internal reviewer notes aren't part of what a broker was scoped
    to see (see schemas/broker.py:BrokerLoadDetail for the full rationale).
    """

    load: Load
    documents: list[Document]
    line_items: list[LineItem]
    match_status: str
    match_result: dict[str, Any] | None = None


def get_load_detail_for_carrier(
    db: Session, *, company_id: uuid.UUID, carrier_id: uuid.UUID, load_id: uuid.UUID
) -> CarrierLoadDetail:
    load = get_load_for_carrier(db, company_id=company_id, carrier_id=carrier_id, load_id=load_id)
    documents = db.query(Document).filter(Document.load_id == load.id).order_by(Document.received_at.asc()).all()
    line_items = db.query(LineItem).filter(LineItem.load_id == load.id).order_by(LineItem.created_at.asc()).all()
    return CarrierLoadDetail(
        load=load,
        documents=documents,
        line_items=line_items,
        match_status=load_service.compute_load_match_status(db, load.id),
        match_result=load_service.get_latest_match_result(db, company_id, load.id),
    )


def record_broker_response(
    db: Session,
    *,
    company_id: uuid.UUID,
    load_id: uuid.UUID,
    carrier_user: CarrierUser,
    message: str,
) -> None:
    """
    Writes a broker's reply (to a Needs Information flag, most commonly)
    straight to the audit log rather than creating a Review row — Review.
    reviewer_id is a hard FK to users.id (ondelete=RESTRICT), which is
    correct for internal reviewer accountability but doesn't fit a
    principal type that isn't a User at all. This shows up automatically
    in the load's merged timeline (app.services.audit_service) since that
    query already matches on entity_type=="load".
    """
    write_audit_log(
        db,
        company_id=company_id,
        entity_type="load",
        entity_id=load_id,
        event_type="broker_response",
        actor_type=AuditActorType.CARRIER,
        actor_id=None,
        details={
            "carrier_user_id": str(carrier_user.id),
            "carrier_user_name": carrier_user.full_name,
            "message": message,
        },
    )
    db.commit()
