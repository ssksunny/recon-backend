"""
The RQ job that does the actual work of turning a stored-but-unprocessed
Document into extracted data, a linked Load, and (once both a rate
confirmation and an invoice are on file) a matching decision.

app/services/document_service.py's receive_document() does the fast
synchronous half of ingestion — validate, store the raw file, create the
Document row, record "document_received" — and enqueues process_document()
with nothing but the document's id. Only plain, serializable arguments cross
the process boundary to a worker; an open DB session or an ORM object isn't
safely shareable that way, so this job opens its own session and re-fetches
everything it needs from scratch.

Run a worker to consume this queue with:

    python -m app.worker
    # or, equivalently:
    rq worker documents --url $REDIS_URL

With BACKGROUND_JOBS_ENABLED=false (see app/core/config.py), this function
still runs — just synchronously, inside app/core/queue.py's enqueue() call,
in whatever process called it. No separate worker needed in that mode.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.ai.matching import ExtractionError, extract_document_data
from app.core.storage import storage
from app.models import database as database_module
from app.models.models import AuditActorType, Document, DocumentStatus, DocumentType
from app.services.audit_service import write_audit_log
from app.services.load_service import find_load_by_number, get_or_create_load_from_rate_confirmation
from app.services.matching_service import run_matching_for_load

logger = logging.getLogger(__name__)


def process_document(document_id: str) -> dict[str, Any]:
    """
    Extracts one document's structured data via Claude, links it to a Load
    if it isn't already (rate confirmations create/find one by their own
    extracted load number; invoices/PODs find one by load number), and runs
    matching if the load then has enough documents on file.

    Returns a small summary dict — mainly useful for RQ's dashboard/logs and
    for tests running in synchronous mode to assert against without a
    second DB round-trip; callers that need the authoritative state should
    still re-fetch the Load/Document from the database rather than trust
    this return value as a cache.

    Not safe to run twice concurrently on the same document_id (no row
    locking) — a non-issue with RQ's default of one worker process handling
    one job at a time, worth revisiting only if that ever changes.
    """
    db = database_module.SessionLocal()
    try:
        document = db.query(Document).filter(Document.id == document_id).one_or_none()
        if document is None:
            # Nothing sensible to do — the document was deleted (no delete
            # endpoint exists yet, but this guards against one existing
            # later) or the id was wrong. Not worth raising: RQ would just
            # retry, endlessly failing the same way.
            logger.error("process_document: document %s no longer exists.", document_id)
            return {"status": "skipped", "reason": "document not found"}

        company = document.company
        load = document.load  # pre-set only when the uploader gave an explicit load_id up front

        document.status = DocumentStatus.PROCESSING
        db.flush()

        try:
            file_bytes = storage.load(document.storage_key)
            extracted = extract_document_data(
                file_bytes, document.content_type or "application/pdf", document.doc_type
            )
        except (ExtractionError, ValueError, OSError) as exc:
            document.status = DocumentStatus.FAILED
            write_audit_log(
                db,
                company_id=company.id,
                entity_type="document",
                entity_id=document.id,
                event_type="extraction_failed",
                actor_type=AuditActorType.SYSTEM,
                details={"error": str(exc)},
            )
            db.commit()
            logger.warning("Extraction failed for document %s: %s", document_id, exc)
            return {"status": "failed", "error": str(exc)}

        document.extracted_data = extracted
        document.extraction_confidence = extracted.get("confidence")
        document.status = DocumentStatus.PROCESSED
        document.processed_at = datetime.now(timezone.utc)

        write_audit_log(
            db,
            company_id=company.id,
            entity_type="document",
            entity_id=document.id,
            event_type="extraction_completed",
            actor_type=AuditActorType.SYSTEM,
            details={"confidence": document.extraction_confidence, "warnings": extracted.get("warnings", [])},
        )

        if load is None:
            if document.doc_type == DocumentType.RATE_CONFIRMATION:
                load = get_or_create_load_from_rate_confirmation(db, company, extracted)
                document.load_id = load.id
            else:
                load = find_load_by_number(db, company.id, extracted.get("load_number"))
                if load is not None:
                    document.load_id = load.id
                # else: left unattached, same as the synchronous path used
                # to do — visible via the document but nothing to match yet.

        match_result = None
        if load is not None:
            db.flush()
            match_result = run_matching_for_load(db, company, load)

        db.commit()
        return {
            "status": "processed",
            "load_id": str(load.id) if load is not None else None,
            "match_status": match_result["status"] if match_result else None,
        }
    finally:
        db.close()
