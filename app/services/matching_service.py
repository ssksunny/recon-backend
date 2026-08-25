"""
Matching: turns a load's processed documents into a persisted audit
decision, and applies human overrides to that decision afterward.

This is the one place that calls app.ai.matching.match_invoice, writes
LineItem rows, advances Load.status, and logs the "match_decision" audit
event. app/services/document_service.py calls run_matching_for_load() right
after ingesting a document; app/api/reviews.py calls override_line_items()
for a reviewer's override action. Neither touches LineItem rows directly.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.ai.matching import MatchingError, match_invoice
from app.models.models import (
    AuditActorType,
    Company,
    Document,
    DocumentStatus,
    DocumentType,
    LineItem,
    LineItemType,
    Load,
    LoadStatus,
    MatchStatus,
)
from app.services.audit_service import write_audit_log
from app.services.errors import NotFoundError, ValidationError

logger = logging.getLogger(__name__)


def _latest_processed_document(db: Session, load_id: uuid.UUID, doc_type: DocumentType) -> Document | None:
    return (
        db.query(Document)
        .filter(
            Document.load_id == load_id,
            Document.doc_type == doc_type,
            Document.status == DocumentStatus.PROCESSED,
        )
        .order_by(Document.received_at.desc())
        .first()
    )


def run_matching_for_load(db: Session, company: Company, load: Load) -> dict[str, Any] | None:
    """
    Runs the Claude matching engine for a load if it has at least a
    processed rate confirmation and invoice on file (a POD is used when
    present but is not required to attempt matching — detention lines will
    simply come back needs_info without one, per app.ai.matching's rules).

    Persists the resulting line items, advances the load's status to
    MATCHED, and writes a "match_decision" audit log entry.

    Returns the match decision dict, or None if matching wasn't attempted
    (missing rate confirmation or invoice) or failed. A failure is logged
    and audited as "match_failed", not raised — a bad match run shouldn't
    take down the document upload request that triggered it; the load just
    stays without line items until it's retried (e.g. on the next document
    upload for that load).
    """
    rate_confirmation = _latest_processed_document(db, load.id, DocumentType.RATE_CONFIRMATION)
    invoice = _latest_processed_document(db, load.id, DocumentType.INVOICE)
    if rate_confirmation is None or invoice is None:
        return None

    pod = _latest_processed_document(db, load.id, DocumentType.POD)

    try:
        decision = match_invoice(
            rate_confirmation.extracted_data,
            invoice.extracted_data,
            pod.extracted_data if pod is not None else None,
        )
    except MatchingError as exc:
        logger.error("Matching failed for load %s: %s", load.id, exc)
        write_audit_log(
            db,
            company_id=company.id,
            entity_type="load",
            entity_id=load.id,
            event_type="match_failed",
            actor_type=AuditActorType.SYSTEM,
            details={"error": str(exc), "invoice_document_id": str(invoice.id)},
        )
        return None

    # Idempotent: clear any prior line items for this invoice (e.g. a
    # corrected re-upload triggering a re-run) instead of accumulating
    # duplicates every time matching runs for the same invoice document.
    db.query(LineItem).filter(LineItem.invoice_document_id == invoice.id).delete()

    for item in decision["line_items"]:
        expected = item.get("expected_amount")
        billed = item.get("billed_amount") or 0
        variance = (billed - expected) if expected is not None else None
        db.add(
            LineItem(
                company_id=company.id,
                load_id=load.id,
                invoice_document_id=invoice.id,
                line_type=LineItemType(item["line_type"]),
                description=item.get("description"),
                billed_amount=billed,
                expected_amount=expected,
                variance_amount=variance,
                match_status=MatchStatus(item["decision"]),
                match_reason=item.get("reason"),
            )
        )

    load.status = LoadStatus.MATCHED

    write_audit_log(
        db,
        company_id=company.id,
        entity_type="load",
        entity_id=load.id,
        event_type="match_decision",
        actor_type=AuditActorType.SYSTEM,
        details=decision,
    )

    db.flush()
    return decision


def override_line_items(
    db: Session,
    *,
    load_id: uuid.UUID,
    line_item_id: uuid.UUID | None,
    document_id: uuid.UUID | None,
    new_status: MatchStatus,
    note: str,
) -> int:
    """
    Overrides match_status (and appends to match_reason) on the targeted
    line item(s): one specific line if line_item_id is given, otherwise
    every line item belonging to the given invoice document.

    Raises:
        ValidationError: neither line_item_id nor document_id was given —
            there's nothing to identify what to override.
        NotFoundError: the identifier was given but matched no line items
            on this load.

    Returns the number of line items changed.
    """
    if line_item_id is None and document_id is None:
        raise ValidationError("An override needs either line_item_id (one line) or document_id (the whole invoice).")

    query = db.query(LineItem).filter(LineItem.load_id == load_id)
    if line_item_id is not None:
        query = query.filter(LineItem.id == line_item_id)
    else:
        query = query.filter(LineItem.invoice_document_id == document_id)

    line_items = query.all()
    if not line_items:
        raise NotFoundError("No matching line item(s) found to override.")

    for item in line_items:
        item.match_status = new_status
        tag = f"Reviewer override: {note}"
        item.match_reason = f"{item.match_reason} ({tag})" if item.match_reason else tag

    return len(line_items)
