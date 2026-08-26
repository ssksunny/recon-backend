"""
Core multi-tenant data models for Recon.

Tenancy model: every table except `companies` carries a `company_id` foreign
key, and every query in the app MUST be scoped by the caller's company_id —
there is no separate database per tenant in the MVP. Enforcing that scoping
lives in the API/service layer (see app/api), not in these model definitions.

Tables:
    Company    - a brokerage tenant
    User       - a person who logs into Recon, belongs to one Company
    Load       - a shipment / rate confirmation Recon matches invoices against
    Document   - an ingested file (rate confirmation, invoice, or POD)
    LineItem   - one billed line on an invoice, matched against the Load's terms
    Review     - a human action taken on an invoice/load (approve/dispute/override)
    AuditLog   - an immutable record of every state-changing event
"""

import enum
import uuid
from datetime import datetime, date
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.database import Base


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------

class UserRole(str, enum.Enum):
    ADMIN = "admin"
    REVIEWER = "reviewer"


class DocumentType(str, enum.Enum):
    RATE_CONFIRMATION = "rate_confirmation"
    INVOICE = "invoice"
    POD = "pod"


class DocumentSource(str, enum.Enum):
    EMAIL = "email"
    UPLOAD = "upload"


class DocumentStatus(str, enum.Enum):
    RECEIVED = "received"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"


class LoadStatus(str, enum.Enum):
    ACTIVE = "active"       # rate confirmation on file, awaiting invoice/POD
    MATCHED = "matched"     # invoice has been matched and decisioned
    CLOSED = "closed"       # reviewed and resolved (paid, disputed-resolved, etc.)


class LineItemType(str, enum.Enum):
    LINEHAUL = "linehaul"
    FUEL_SURCHARGE = "fuel_surcharge"
    DETENTION = "detention"
    ACCESSORIAL = "accessorial"
    OTHER = "other"


class MatchStatus(str, enum.Enum):
    CLEAN = "clean"
    DISCREPANCY = "discrepancy"
    NEEDS_INFO = "needs_info"


class ReviewAction(str, enum.Enum):
    APPROVE = "approve"
    DISPUTE = "dispute"
    OVERRIDE = "override"


class AuditActorType(str, enum.Enum):
    SYSTEM = "system"
    USER = "user"
    CARRIER = "carrier"


# --------------------------------------------------------------------------
# Mixins
# --------------------------------------------------------------------------

class UUIDPkMixin:
    """Adds a UUID primary key, generated application-side."""
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )


class TimestampMixin:
    """Adds created_at / updated_at columns managed by the database."""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class TenantMixin:
    """Adds the company_id foreign key every tenant-scoped table needs."""

    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True
    )


# --------------------------------------------------------------------------
# Company (tenant)
# --------------------------------------------------------------------------

class Company(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "companies"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)

    # Inbound address documents can be emailed to for this tenant, e.g.
    # "<slug>@inbound.reconapp.io" — stored explicitly so it can be rotated
    # independently of the slug.
    inbound_email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    users: Mapped[list["User"]] = relationship(back_populates="company", cascade="all, delete-orphan")
    loads: Mapped[list["Load"]] = relationship(back_populates="company", cascade="all, delete-orphan")
    documents: Mapped[list["Document"]] = relationship(back_populates="company", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Company {self.slug}>"


# --------------------------------------------------------------------------
# User
# --------------------------------------------------------------------------

class User(UUIDPkMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("company_id", "email", name="uq_users_company_email"),
    )

    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        SAEnum(UserRole, name="user_role"), nullable=False, default=UserRole.REVIEWER
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    company: Mapped["Company"] = relationship(back_populates="users")
    reviews: Mapped[list["Review"]] = relationship(back_populates="reviewer")

    def __repr__(self) -> str:
        return f"<User {self.email} ({self.role.value})>"


# --------------------------------------------------------------------------
# Load (shipment / rate confirmation terms Recon matches invoices against)
# --------------------------------------------------------------------------

class Load(UUIDPkMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "loads"
    __table_args__ = (
        UniqueConstraint("company_id", "load_number", name="uq_loads_company_load_number"),
    )

    load_number: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    carrier_name: Mapped[str] = mapped_column(String(255), nullable=False)

    # The carrier's own login to the broker portal, if any. Deliberately
    # separate from carrier_name above and NEVER auto-populated from it —
    # carrier_name is free-text extracted by Claude from a rate
    # confirmation and two real carriers can share a near-identical name,
    # so linking a load to a Carrier account is always an explicit admin
    # action (see app/services/carrier_service.py:assign_carrier_to_load).
    # Nullable and SET NULL on delete: an unassigned load is simply
    # invisible to every carrier, not an error state.
    carrier_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("carriers.id", ondelete="SET NULL"), nullable=True, index=True
    )

    origin: Mapped[str | None] = mapped_column(String(255), nullable=True)
    destination: Mapped[str | None] = mapped_column(String(255), nullable=True)
    equipment_type: Mapped[str | None] = mapped_column(String(100), nullable=True)

    pickup_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    delivery_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Rate confirmation terms, as extracted by Claude from the source document.
    linehaul_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    fuel_surcharge_terms: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    detention_terms: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # A list of {"type": ..., "max_amount": ...} objects, not a dict.
    accessorials_allowed: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    status: Mapped[LoadStatus] = mapped_column(
        SAEnum(LoadStatus, name="load_status"), nullable=False, default=LoadStatus.ACTIVE
    )

    company: Mapped["Company"] = relationship(back_populates="loads")
    documents: Mapped[list["Document"]] = relationship(back_populates="load")
    line_items: Mapped[list["LineItem"]] = relationship(back_populates="load", cascade="all, delete-orphan")
    reviews: Mapped[list["Review"]] = relationship(back_populates="load")
    carrier: Mapped["Carrier | None"] = relationship(back_populates="loads")

    def __repr__(self) -> str:
        return f"<Load {self.load_number} ({self.status.value})>"


# --------------------------------------------------------------------------
# Document (an ingested rate confirmation, invoice, or POD)
# --------------------------------------------------------------------------

class Document(UUIDPkMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "documents"

    load_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("loads.id", ondelete="SET NULL"), nullable=True, index=True
    )

    doc_type: Mapped[DocumentType] = mapped_column(SAEnum(DocumentType, name="document_type"), nullable=False)
    source: Mapped[DocumentSource] = mapped_column(SAEnum(DocumentSource, name="document_source"), nullable=False)

    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1000), nullable=False)  # S3 object key
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)  # dedupe
    # The uploaded file's MIME type (e.g. "application/pdf"), captured at
    # upload time so the file-download endpoint can send an accurate
    # Content-Type back without re-sniffing the bytes.
    content_type: Mapped[str | None] = mapped_column(String(200), nullable=True)

    status: Mapped[DocumentStatus] = mapped_column(
        SAEnum(DocumentStatus, name="document_status"), nullable=False, default=DocumentStatus.RECEIVED
    )

    # Raw structured output from the Claude extraction step.
    extracted_data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    extraction_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)  # 0.000-1.000

    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    company: Mapped["Company"] = relationship(back_populates="documents")
    load: Mapped["Load | None"] = relationship(back_populates="documents")
    line_items: Mapped[list["LineItem"]] = relationship(back_populates="invoice_document")

    def __repr__(self) -> str:
        return f"<Document {self.doc_type.value} {self.original_filename}>"


# --------------------------------------------------------------------------
# LineItem (one billed line on an invoice, matched against the Load's terms)
# --------------------------------------------------------------------------

class LineItem(UUIDPkMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "line_items"

    load_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("loads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    invoice_document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )

    line_type: Mapped[LineItemType] = mapped_column(SAEnum(LineItemType, name="line_item_type"), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)

    billed_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    expected_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    variance_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)

    match_status: Mapped[MatchStatus] = mapped_column(
        SAEnum(MatchStatus, name="match_status"), nullable=False, default=MatchStatus.NEEDS_INFO
    )
    # Plain-language reason surfaced to reviewers, e.g.
    # "Fuel surcharge billed at 22% but rate confirmation specifies a flat $150."
    match_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    load: Mapped["Load"] = relationship(back_populates="line_items")
    invoice_document: Mapped["Document"] = relationship(back_populates="line_items")

    def __repr__(self) -> str:
        return f"<LineItem {self.line_type.value} {self.billed_amount} ({self.match_status.value})>"


# --------------------------------------------------------------------------
# Review (a human action taken on an invoice/load)
# --------------------------------------------------------------------------

class Review(UUIDPkMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "reviews"

    load_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("loads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )
    reviewer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    action: Mapped[ReviewAction] = mapped_column(SAEnum(ReviewAction, name="review_action"), nullable=False)
    previous_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    new_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    load: Mapped["Load"] = relationship(back_populates="reviews")
    reviewer: Mapped["User"] = relationship(back_populates="reviews")

    def __repr__(self) -> str:
        return f"<Review {self.action.value} by {self.reviewer_id}>"


# --------------------------------------------------------------------------
# AuditLog (immutable record of every state-changing event)
# --------------------------------------------------------------------------

class AuditLog(UUIDPkMixin, TenantMixin, Base):
    __tablename__ = "audit_logs"

    # created_at only — audit rows are never updated, so no updated_at.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    entity_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)  # "document", "load", ...
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)

    # e.g. "document_received", "extraction_completed", "match_decision",
    # "review_action", "override"
    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)

    actor_type: Mapped[AuditActorType] = mapped_column(
        SAEnum(AuditActorType, name="audit_actor_type"), nullable=False
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    def __repr__(self) -> str:
        return f"<AuditLog {self.event_type} on {self.entity_type}:{self.entity_id}>"


# --------------------------------------------------------------------------
# Carrier / CarrierUser (broker self-service portal)
# --------------------------------------------------------------------------

class Carrier(UUIDPkMixin, TimestampMixin, TenantMixin, Base):
    """
    An external carrier company an admin has chosen to give portal access
    to. Scoped by company_id like every other tenant table — two brokerages
    using Recon never share a Carrier row even if the real-world carrier is
    the same company.

    Deliberately NOT linked automatically to Load.carrier_name (the
    free-text string Claude extracts from a rate confirmation) — see the
    comment on Load.carrier_id for why. A Carrier only gains visibility
    into a Load through an explicit admin assignment.
    """
    __tablename__ = "carriers"
    __table_args__ = (
        UniqueConstraint("company_id", "name", name="uq_carriers_company_name"),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    company: Mapped["Company"] = relationship()
    users: Mapped[list["CarrierUser"]] = relationship(back_populates="carrier", cascade="all, delete-orphan")
    loads: Mapped[list["Load"]] = relationship(back_populates="carrier")

    def __repr__(self) -> str:
        return f"<Carrier {self.name}>"


class CarrierUser(UUIDPkMixin, TimestampMixin, Base):
    """
    A broker's own login to the broker portal — a second, parallel
    principal type alongside User, not a User with a different role. Kept
    fully separate on purpose: a broker's JWT carries carrier_id (never
    company_id-as-tenant-scope the way a User's does) and role="broker",
    "typ"="broker", so app.api.deps.get_current_broker_user can't be
    confused with get_current_user even in a hypothetical uuid collision
    between the users and carrier_users tables.

    Not a TenantMixin table — a CarrierUser belongs to a Carrier, and a
    Carrier already carries company_id, so this reaches its company via
    carrier.company_id rather than duplicating the column.

    hashed_password is nullable because the row is created the moment an
    admin sends an invite (see carrier_service.invite_carrier_user), before
    the broker has ever set a password; is_active flips true only once
    carrier_service.accept_invite runs.
    """
    __tablename__ = "carrier_users"
    __table_args__ = (
        UniqueConstraint("carrier_id", "email", name="uq_carrier_users_carrier_email"),
    )

    carrier_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("carriers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    hashed_password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    carrier: Mapped["Carrier"] = relationship(back_populates="users")

    def __repr__(self) -> str:
        return f"<CarrierUser {self.email}>"
