"""
Inbound email ingestion: turns a parsed email (however its provider's
webhook shaped it — see app/api/email.py's per-provider adapters) into
stored, extracted, matched documents, exactly as if each PDF attachment had
been manually uploaded.

The provider adapters in app/api/email.py each do only one job: parse their
own webhook's wire format into the InboundEmail/InboundEmailAttachment DTOs
below. Everything from ingest_inbound_email() down is provider-agnostic, so
adding a fourth provider later never touches this file.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from email.utils import parseaddr

from sqlalchemy.orm import Session

from app.ai.matching import ExtractionError, classify_document_type
from app.models.models import Company, DocumentSource, DocumentType
from app.services.document_service import receive_document
from app.services.errors import NotFoundError, ProcessingError, ValidationError

logger = logging.getLogger(__name__)


@dataclass
class InboundEmailAttachment:
    filename: str
    content_type: str | None
    data: bytes


@dataclass
class InboundEmail:
    """Provider-agnostic shape every adapter in app/api/email.py normalizes its webhook payload into."""

    to_address: str
    from_address: str
    subject: str | None
    attachments: list[InboundEmailAttachment] = field(default_factory=list)


@dataclass
class AttachmentResult:
    filename: str
    document_id: uuid.UUID | None
    doc_type: str | None
    # "queued": stored and handed to the background job (see
    # app/jobs/document_jobs.py) — extraction, load-linking, and matching
    # haven't happened yet. "failed": classification or storage failed
    # before anything could be queued; see `error`.
    status: str
    error: str | None


@dataclass
class EmailIngestResult:
    company_id: uuid.UUID
    processed: list[AttachmentResult]
    skipped: list[str]  # filenames of non-PDF attachments (logos, signature images, etc.) — not errors


def _candidate_addresses(raw_header: str) -> list[str]:
    """
    A To header can be "Recon <acme-freight@inbound.reconapp.io>" and can
    legally list more than one address, comma-separated (e.g. the company's
    address cc'd alongside an internal address). Returns bare, lowercased
    addresses in header order so the caller can try each against known
    companies rather than assuming the first one is always the right one.
    """
    addresses = []
    for part in raw_header.split(","):
        _, addr = parseaddr(part.strip())
        if addr:
            addresses.append(addr.lower())
    return addresses


def _find_company_for_address(db: Session, to_header: str) -> Company | None:
    for address in _candidate_addresses(to_header):
        company = db.query(Company).filter(Company.inbound_email == address).one_or_none()
        if company is not None:
            return company
    return None


def _is_pdf(attachment: InboundEmailAttachment) -> bool:
    if attachment.content_type == "application/pdf":
        return True
    return attachment.filename.lower().endswith(".pdf")


def ingest_inbound_email(db: Session, email: InboundEmail) -> EmailIngestResult:
    """
    Processes every PDF attachment on one inbound email for whichever
    company owns the recipient address.

    For each PDF: classify what kind of document it is (rate confirmation /
    invoice / POD — unlike a manual upload, an emailed attachment doesn't
    come pre-labeled — this one Claude call stays synchronous, since we need
    doc_type before we can even store the row correctly), then hand it to
    the same receive_document() manual uploads use to store it and queue
    extraction + load-linking + matching as a background job. This function
    itself never waits on that heavier work — see app/jobs/document_jobs.py.

    Rate confirmations are queued before invoices/PODs within the same email
    (reordered here, not by requiring a particular attachment order), so
    that — in the common case of a single background worker processing the
    queue in order — a load number a rate confirmation just established is
    already there for an invoice attached alongside it to link to. This is
    a best-effort ordering, not a hard guarantee: with more than one worker
    process consuming the queue concurrently, an invoice's job can still run
    before its rate confirmation's job finishes, in which case the invoice
    is stored but left unlinked (same as if it had arrived with no matching
    load at all) — closing that gap for good needs a "relink orphaned
    documents" pass, which doesn't exist yet.

    Non-PDF attachments are skipped, not treated as errors — logos and
    inline signature images are common on real carrier email. A failure on
    one PDF (an unreadable scan, a classification error) doesn't abort the
    others; each attachment's outcome is reported independently in the
    result.

    Raises:
        NotFoundError: no company's inbound_email matches any address on
            the To header — this is the one thing that fails the whole
            request, since without a company there's nowhere to file the
            documents. Provider webhooks generally treat a non-2xx as
            "retry me", which is the right behavior for a config problem
            (a stale/mistyped forwarding address) that a human needs to fix.
        ValidationError: the matched company is inactive.
    """
    company = _find_company_for_address(db, email.to_address)
    if company is None:
        raise NotFoundError(f"No company found for inbound address {email.to_address!r}.")
    if not company.is_active:
        raise ValidationError(f"Company {company.slug!r} is not active.")

    pdf_attachments = [a for a in email.attachments if a.data and _is_pdf(a)]
    pdf_attachment_ids = {id(a) for a in pdf_attachments}
    skipped = [a.filename for a in email.attachments if id(a) not in pdf_attachment_ids]

    # Classify everything up front (not lazily during the ingest loop below)
    # so ingestion order can be rearranged — rate confirmations first — no
    # matter what order they were attached in.
    classified: list[tuple[InboundEmailAttachment, DocumentType | None, str | None]] = []
    for attachment in pdf_attachments:
        try:
            doc_type, confidence, reason = classify_document_type(attachment.data, "application/pdf")
            logger.info(
                "Classified email attachment %r for company %s as %s (confidence %.2f): %s",
                attachment.filename, company.id, doc_type.value, confidence, reason,
            )
            classified.append((attachment, doc_type, None))
        except (ValueError, ExtractionError) as exc:
            classified.append((attachment, None, str(exc)))

    classified.sort(key=lambda item: 0 if item[1] == DocumentType.RATE_CONFIRMATION else 1)

    results: list[AttachmentResult] = []
    for attachment, doc_type, classification_error in classified:
        if doc_type is None:
            results.append(
                AttachmentResult(
                    filename=attachment.filename,
                    document_id=None,
                    doc_type=None,
                    status="failed",
                    error=f"Could not classify document type: {classification_error}",
                )
            )
            continue

        try:
            receive_result = receive_document(
                db,
                company=company,
                actor_user_id=None,
                doc_type=doc_type,
                load=None,  # resolved in the background job by doc_type + extracted load_number
                filename=attachment.filename,
                content_type=attachment.content_type or "application/pdf",
                file_bytes=attachment.data,
                source=DocumentSource.EMAIL,
                extra_audit_details={"from_email": email.from_address, "email_subject": email.subject},
            )
        except (ValidationError, ProcessingError) as exc:
            results.append(
                AttachmentResult(
                    filename=attachment.filename,
                    document_id=None,
                    doc_type=doc_type.value,
                    status="failed",
                    error=str(exc),
                )
            )
            continue

        results.append(
            AttachmentResult(
                filename=attachment.filename,
                document_id=receive_result.document.id,
                doc_type=doc_type.value,
                status="queued",
                error=None,
            )
        )

    return EmailIngestResult(company_id=company.id, processed=results, skipped=skipped)
