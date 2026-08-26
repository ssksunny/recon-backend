"""
Broker-portal-facing routes: a carrier's own view of their loads. Every
query here is scoped by current_broker.carrier_id (and company_id, for
defense-in-depth) via app/services/carrier_service.py — never by anything
a broker's request could itself supply, exactly like app/api/loads.py
never trusts a request-supplied company_id.

A broker sees only what the scoping decision behind this feature allows:
status + reason, their own documents, and the ability to respond to a flag
or upload a corrected/missing document. No internal reviewer notes, no
other carriers' loads, nothing unassigned.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_broker_user
from app.core.storage import storage
from app.models.database import get_db
from app.models.models import AuditActorType, CarrierUser, DocumentType, Load
from app.schemas.audit import AuditLogEntryOut
from app.schemas.broker import (
    BrokerDocumentType,
    BrokerLoadDetail,
    BrokerLoadListItem,
    BrokerRespondRequest,
)
from app.schemas.documents import DocumentOut, DocumentUploadResponse, MatchResultOut
from app.schemas.loads import LineItemOut
from app.services import audit_service, carrier_service, document_service, load_service

router = APIRouter()


def _to_list_item(load: Load, match_status: str) -> BrokerLoadListItem:
    return BrokerLoadListItem(
        id=load.id,
        load_number=load.load_number,
        status=load.status,
        match_status=match_status,
        linehaul_rate=load.linehaul_rate,
        pickup_date=load.pickup_date,
        delivery_date=load.delivery_date,
        created_at=load.created_at,
    )


@router.get("/loads", response_model=list[BrokerLoadListItem])
def list_loads(
    db: Session = Depends(get_db),
    current_broker: CarrierUser = Depends(get_current_broker_user),
) -> list[BrokerLoadListItem]:
    loads = carrier_service.list_loads_for_carrier(
        db, company_id=current_broker.carrier.company_id, carrier_id=current_broker.carrier_id
    )
    return [_to_list_item(load, load_service.compute_load_match_status(db, load.id)) for load in loads]


@router.get("/loads/{load_id}", response_model=BrokerLoadDetail)
def get_load(
    load_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_broker: CarrierUser = Depends(get_current_broker_user),
) -> BrokerLoadDetail:
    detail = carrier_service.get_load_detail_for_carrier(
        db, company_id=current_broker.carrier.company_id, carrier_id=current_broker.carrier_id, load_id=load_id
    )
    load = detail.load
    return BrokerLoadDetail(
        id=load.id,
        load_number=load.load_number,
        status=load.status,
        match_status=detail.match_status,
        linehaul_rate=load.linehaul_rate,
        pickup_date=load.pickup_date,
        delivery_date=load.delivery_date,
        created_at=load.created_at,
        origin=load.origin,
        destination=load.destination,
        equipment_type=load.equipment_type,
        documents=[DocumentOut.model_validate(d) for d in detail.documents],
        line_items=[LineItemOut.model_validate(li) for li in detail.line_items],
        match_result=MatchResultOut(**detail.match_result) if detail.match_result else None,
    )


@router.get("/loads/{load_id}/audit", response_model=list[AuditLogEntryOut])
def get_load_audit_trail(
    load_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_broker: CarrierUser = Depends(get_current_broker_user),
) -> list[AuditLogEntryOut]:
    company_id = current_broker.carrier.company_id
    carrier_service.get_load_for_carrier(  # 404s if this load isn't assigned to this carrier
        db, company_id=company_id, carrier_id=current_broker.carrier_id, load_id=load_id
    )
    entries = audit_service.list_audit_log_for_load_broker_view(db, company_id, load_id)
    return [AuditLogEntryOut.model_validate(e) for e in entries]


@router.post("/loads/{load_id}/documents", response_model=DocumentUploadResponse, status_code=status.HTTP_201_CREATED)
def upload_document(
    load_id: uuid.UUID,
    doc_type: BrokerDocumentType = Form(..., description="invoice | pod — rate confirmations are admin-only."),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_broker: CarrierUser = Depends(get_current_broker_user),
) -> DocumentUploadResponse:
    company_id = current_broker.carrier.company_id
    load = carrier_service.get_load_for_carrier(  # 404s if this load isn't assigned to this carrier
        db, company_id=company_id, carrier_id=current_broker.carrier_id, load_id=load_id
    )

    # Sync read on the underlying SpooledTemporaryFile — correct here
    # because this path function is sync; see app/api/documents.py's
    # module docstring for why FastAPI requires that for this to be safe.
    file_bytes = file.file.read()

    result = document_service.receive_document(
        db,
        company=current_broker.carrier.company,
        actor_user_id=None,
        doc_type=DocumentType(doc_type.value),
        load=load,
        filename=file.filename or "unnamed",
        content_type=file.content_type or "",
        file_bytes=file_bytes,
        extra_audit_details={
            "uploaded_by": "broker",
            "carrier_user_id": str(current_broker.id),
            "carrier_user_name": current_broker.full_name,
        },
        actor_type_override=AuditActorType.CARRIER,
    )

    return DocumentUploadResponse(
        document=DocumentOut.model_validate(result.document),
        load_id=result.load.id if result.load else None,
        message="Received — queued for extraction and matching against the rest of this load's documents.",
    )


@router.get("/documents/{document_id}/file")
def get_document_file(
    document_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_broker: CarrierUser = Depends(get_current_broker_user),
) -> Response:
    """Streams a source document's original bytes — same behavior as app/api/documents.py's admin equivalent, scoped by carrier instead of company."""
    document = document_service.get_document_for_carrier(db, current_broker.carrier_id, document_id)
    file_bytes = storage.load(document.storage_key)
    safe_filename = document.original_filename.replace('"', "'")
    return Response(
        content=file_bytes,
        media_type=document.content_type or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{safe_filename}"'},
    )


@router.post("/loads/{load_id}/respond", status_code=status.HTTP_204_NO_CONTENT)
def respond_to_load(
    load_id: uuid.UUID,
    payload: BrokerRespondRequest,
    db: Session = Depends(get_db),
    current_broker: CarrierUser = Depends(get_current_broker_user),
) -> Response:
    company_id = current_broker.carrier.company_id
    carrier_service.get_load_for_carrier(  # 404s if this load isn't assigned to this carrier
        db, company_id=company_id, carrier_id=current_broker.carrier_id, load_id=load_id
    )
    carrier_service.record_broker_response(
        db, company_id=company_id, load_id=load_id, carrier_user=current_broker, message=payload.message
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
