"""
Document ingestion: the single entry point for getting a rate confirmation,
invoice, or POD into Recon — however it arrived, a manual upload
(app/api/documents.py) or an inbound email attachment
(app/services/email_service.py).

receive_document() is deliberately just the fast, synchronous half: validate
the file, store the bytes, create the Document row, record
"document_received", and enqueue the actual work. Extraction, load-linking,
and matching all happen in a background job (app/jobs/document_jobs.py) so
neither an upload request nor an inbound-email webhook has to block on one
or more Claude API calls before responding. See app/core/queue.py for how
that queue is configured (and how to run it fully synchronously for local
dev without a worker process).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from redis.exceptions import RedisError
from sqlalchemy.orm import Session

from app.ai.matching import SUPPORTED_MEDIA_TYPES
from app.core.queue import document_queue
from app.core.storage import storage
from app.jobs.document_jobs import process_document
from app.models.models import AuditActorType, Company, Document, DocumentSource, DocumentStatus, DocumentType, Load
from app.services.audit_service import write_audit_log
from app.services.errors import NotFoundError, ProcessingError, ValidationError


@dataclass
class DocumentReceiveResult:
    document: Document
    # Set only when the caller already knew which load this belongs to
    # (a manual upload that passed load_id). None means resolution — by
    # extracted load number, or auto-creating one for a rate confirmation —
    # hasn't happened yet; it happens in the background job, so the load
    # isn't knowable synchronously here.
    load: Load | None


def receive_document(
    db: Session,
    *,
    company: Company,
    actor_user_id: uuid.UUID | None,
    doc_type: DocumentType,
    load: Load | None,
    filename: str,
    content_type: str,
    file_bytes: bytes,
    source: DocumentSource = DocumentSource.UPLOAD,
    extra_audit_details: dict[str, Any] | None = None,
    actor_type_override: AuditActorType | None = None,
) -> DocumentReceiveResult:
    """
    Validates and stores one document, creates its row, and queues
    extraction + load-linking + matching as a background job. Returns as
    soon as the job is queued — this function never calls Claude.

    A human upload (app/api/documents.py) passes actor_user_id and leaves
    source at its UPLOAD default. Inbound email (app/services/email_service.py)
    passes actor_user_id=None — there's no human in the loop for that event —
    and source=DocumentSource.EMAIL; extra_audit_details is its way of
    recording who the email was from without adding email-specific columns
    to the audit log itself.

    A broker portal upload (app/api/broker.py) also passes actor_user_id=None
    — a CarrierUser isn't a User, so there's no users.id to record — but
    unlike an inbound email it does have a real human on the other end, so
    it passes actor_type_override=AuditActorType.CARRIER (with the broker's
    identity recorded in extra_audit_details instead, the same pattern
    email_service uses for from_email) rather than falling through to the
    actor_user_id-is-None-means-SYSTEM default below.

    Raises:
        ValidationError: unsupported file type, or an empty file.
        ProcessingError: the file was stored fine, but the document couldn't
            be queued for processing (e.g. Redis is unreachable) — the
            Document row and its "document_received" audit entry are still
            committed either way, so nothing is silently lost; it just sits
            at status=RECEIVED until manually re-queued.
    """
    if content_type not in SUPPORTED_MEDIA_TYPES:
        raise ValidationError(
            f"Unsupported file type {content_type!r}; expected one of {sorted(SUPPORTED_MEDIA_TYPES)}."
        )
    if not file_bytes:
        raise ValidationError("Uploaded file is empty.")

    document = Document(
        company_id=company.id,
        load_id=load.id if load else None,
        doc_type=doc_type,
        source=source,
        original_filename=filename or "unnamed",
        content_type=content_type or None,
        storage_key="",  # set right after, once we have document.id to key it by
        status=DocumentStatus.RECEIVED,
    )
    db.add(document)
    db.flush()  # assigns document.id without ending the transaction

    document.storage_key = storage.save(company.id, document.id, document.original_filename, file_bytes)

    audit_details: dict[str, Any] = {
        "doc_type": doc_type.value,
        "filename": document.original_filename,
        "source": source.value,
    }
    if extra_audit_details:
        audit_details.update(extra_audit_details)

    write_audit_log(
        db,
        company_id=company.id,
        entity_type="document",
        entity_id=document.id,
        event_type="document_received",
        actor_type=actor_type_override or (AuditActorType.USER if actor_user_id is not None else AuditActorType.SYSTEM),
        actor_id=actor_user_id,
        details=audit_details,
    )

    db.commit()
    db.refresh(document)

    try:
        document_queue.enqueue(process_document, str(document.id))
    except RedisError as exc:
        raise ProcessingError(
            f"Document was saved but could not be queued for processing: {exc}"
        ) from exc

    return DocumentReceiveResult(document=document, load=load)


def get_document(db: Session, company_id: uuid.UUID, document_id: uuid.UUID) -> Document:
    """Fetches a Document scoped to company_id."""
    document = (
        db.query(Document)
        .filter(Document.company_id == company_id, Document.id == document_id)
        .one_or_none()
    )
    if document is None:
        raise NotFoundError("Document not found.")
    return document


def get_document_for_carrier(db: Session, carrier_id: uuid.UUID, document_id: uuid.UUID) -> Document:
    """
    The broker-portal equivalent of get_document — scoped by carrier_id
    (via the document's load) instead of company_id. A broker can only ever
    reach a document that belongs to a load an admin explicitly assigned to
    their carrier (see app/services/carrier_service.py:assign_carrier_to_load);
    a document with no load, or whose load belongs to a different carrier,
    is invisible here exactly as if it didn't exist.
    """
    document = (
        db.query(Document)
        .join(Load, Document.load_id == Load.id)
        .filter(Load.carrier_id == carrier_id, Document.id == document_id)
        .one_or_none()
    )
    if document is None:
        raise NotFoundError("Document not found.")
    return document
