from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.models.models import DocumentSource, DocumentStatus, DocumentType


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    load_id: uuid.UUID | None
    doc_type: DocumentType
    source: DocumentSource
    status: DocumentStatus
    original_filename: str
    content_type: str | None
    extracted_data: dict[str, Any]
    extraction_confidence: float | None
    received_at: datetime
    processed_at: datetime | None


class MatchResultOut(BaseModel):
    """Mirrors the dict shape returned by app.ai.matching.match_invoice."""

    status: str
    summary: str
    total_rate_con: float | None
    total_invoiced: float | None
    variance: float | None
    confidence: float
    recommended_action: str


class DocumentUploadResponse(BaseModel):
    """
    What POST /documents/upload returns — immediately, before extraction or
    matching has run (see app/jobs/document_jobs.py). `load_id` is set only
    when the caller already told us which load this belongs to; otherwise
    it's resolved in the background and this is null even for a document
    that will end up linked a moment later. Poll GET /loads/{id} (or its
    exception queue / all-invoices listing) to see the outcome once
    document.status moves past "processing".
    """

    document: DocumentOut
    load_id: uuid.UUID | None
    message: str
