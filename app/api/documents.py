"""
Document upload: the HTTP entry point for getting a rate confirmation,
invoice, or POD into Recon. All the actual work — storage, extraction, load
linking, matching — lives in app/services/document_service.py; this router
only translates the HTTP request into a service call and the service's
result into a response schema. No direct ORM queries here on purpose.

Note this router's path functions are plain `def`, not `async def`. That's
deliberate: everything downstream (the SQLAlchemy session, the Claude SDK
call inside document_service) is synchronous/blocking, and FastAPI runs
sync path functions in a worker thread automatically — writing `async def`
here and then blocking the event loop inside it would be the actual bug.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.storage import storage
from app.models.database import get_db
from app.models.models import DocumentType, User
from app.schemas.documents import DocumentOut, DocumentUploadResponse
from app.services import document_service, load_service

router = APIRouter()


@router.post("/upload", response_model=DocumentUploadResponse, status_code=status.HTTP_201_CREATED)
def upload_document(
    doc_type: DocumentType = Form(..., description="rate_confirmation | invoice | pod"),
    load_id: uuid.UUID | None = Form(
        None,
        description="Attach to this load. Omit for a rate confirmation to auto-create a load, "
        "or for an invoice/POD to auto-link by load number.",
    ),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DocumentUploadResponse:
    load = load_service.get_load(db, current_user.company_id, load_id) if load_id is not None else None

    # Sync read on the underlying SpooledTemporaryFile — correct here
    # because this path function is sync (see module docstring).
    file_bytes = file.file.read()

    result = document_service.receive_document(
        db,
        company=current_user.company,
        actor_user_id=current_user.id,
        doc_type=doc_type,
        load=load,
        filename=file.filename or "unnamed",
        content_type=file.content_type or "",
        file_bytes=file_bytes,
    )

    if result.load is not None:
        message = "Received — queued for extraction and matching against the rest of this load's documents."
    else:
        message = "Received — queued for extraction. Will link to a load automatically once processed."

    return DocumentUploadResponse(
        document=DocumentOut.model_validate(result.document),
        load_id=result.load.id if result.load else None,
        message=message,
    )


@router.get("/{document_id}", response_model=DocumentOut)
def get_document(
    document_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DocumentOut:
    document = document_service.get_document(db, current_user.company_id, document_id)
    return DocumentOut.model_validate(document)


@router.get("/{document_id}/file")
def get_document_file(
    document_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    """
    Streams the original uploaded file back — the rate confirmation PDF, the
    invoice, the POD — so a reviewer can open the source document alongside
    Recon's extraction instead of taking it on faith. Scoped by
    document_service.get_document exactly like the metadata endpoint above,
    so this can't be used to pull another company's files by guessing an id.

    "inline" (not "attachment") so the browser previews PDFs/images directly
    rather than forcing a download.
    """
    document = document_service.get_document(db, current_user.company_id, document_id)
    file_bytes = storage.load(document.storage_key)
    safe_filename = document.original_filename.replace('"', "'")
    return Response(
        content=file_bytes,
        media_type=document.content_type or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{safe_filename}"'},
    )
