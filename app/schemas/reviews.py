from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, model_validator

from app.models.models import MatchStatus, ReviewAction


class ReviewCreate(BaseModel):
    load_id: uuid.UUID
    document_id: uuid.UUID | None = None
    line_item_id: uuid.UUID | None = None
    action: ReviewAction
    new_status: MatchStatus | None = None
    note: str | None = None

    @model_validator(mode="after")
    def validate_action_requirements(self) -> "ReviewCreate":
        if self.action == ReviewAction.OVERRIDE:
            if self.new_status is None:
                raise ValueError("new_status is required when action is 'override'.")
            if not self.note:
                raise ValueError("note is required when action is 'override' — explain what you're changing and why.")
        if self.action == ReviewAction.DISPUTE and not self.note:
            raise ValueError("note is required when action is 'dispute' — this becomes the record of why.")
        return self


class ReviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    load_id: uuid.UUID
    document_id: uuid.UUID | None
    reviewer_id: uuid.UUID
    action: ReviewAction
    previous_status: str | None
    new_status: str | None
    note: str | None
    created_at: datetime
