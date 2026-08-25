"""
Human review actions: approve, dispute, and override. Load lookups go
through load_service, line-item overrides go through matching_service, and
the audit trail goes through audit_service — this router only parses the
request, orchestrates the three calls in order, and builds the response.

Notes live on the Review itself (ReviewCreate.note) rather than a separate
"add a note" endpoint — dispute and override both require one (enforced by
the schema itself, see app/schemas/reviews.py); approve accepts one optionally.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.models.database import get_db
from app.models.models import AuditActorType, LoadStatus, Review, ReviewAction, User
from app.schemas.reviews import ReviewCreate, ReviewOut
from app.services import audit_service, load_service, matching_service

router = APIRouter()


@router.post("", response_model=ReviewOut, status_code=status.HTTP_201_CREATED)
def create_review(
    payload: ReviewCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ReviewOut:
    load = load_service.get_load(db, current_user.company_id, payload.load_id)
    previous_status = load_service.compute_load_match_status(db, load.id)

    if payload.action == ReviewAction.APPROVE:
        new_status = "approved"
        # A dispute deliberately leaves load.status alone — see the DISPUTE
        # branch below — but approving is the one action that closes a load.
        load.status = LoadStatus.CLOSED

    elif payload.action == ReviewAction.DISPUTE:
        # Doesn't change load.status: a dispute means the broker is
        # following up with the carrier outside Recon, and the load stays
        # visible (still MATCHED) until that's resolved with an approve or
        # an override.
        new_status = "disputed"

    else:  # ReviewAction.OVERRIDE — schema validation guarantees new_status and note are set
        matching_service.override_line_items(
            db,
            load_id=load.id,
            line_item_id=payload.line_item_id,
            document_id=payload.document_id,
            new_status=payload.new_status,
            note=payload.note,
        )
        new_status = payload.new_status.value

    review = Review(
        company_id=current_user.company_id,
        load_id=load.id,
        document_id=payload.document_id,
        reviewer_id=current_user.id,
        action=payload.action,
        previous_status=previous_status,
        new_status=new_status,
        note=payload.note,
    )
    db.add(review)
    db.flush()  # assigns review.id

    audit_service.write_audit_log(
        db,
        company_id=current_user.company_id,
        entity_type="review",
        entity_id=review.id,
        event_type="override" if payload.action == ReviewAction.OVERRIDE else "review_action",
        actor_type=AuditActorType.USER,
        actor_id=current_user.id,
        details={
            "load_id": str(load.id),
            "document_id": str(payload.document_id) if payload.document_id else None,
            "line_item_id": str(payload.line_item_id) if payload.line_item_id else None,
            "action": payload.action.value,
            "previous_status": previous_status,
            "new_status": new_status,
            "note": payload.note,
        },
    )

    db.commit()
    db.refresh(review)
    return ReviewOut.model_validate(review)


@router.get("", response_model=list[ReviewOut])
def list_reviews(
    load_id: uuid.UUID = Query(..., description="Review history for this load, most recent first."),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ReviewOut]:
    load = load_service.get_load(db, current_user.company_id, load_id)
    reviews = load_service.list_reviews_for_load(db, load.id)
    return [ReviewOut.model_validate(r) for r in reviews]
