from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.models.models import AuditActorType


class AuditLogEntryOut(BaseModel):
    """One row of a load's merged audit timeline — see audit_service.list_audit_log_for_load."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    event_type: str
    actor_type: AuditActorType
    actor_name: str | None
    details: dict[str, Any]
    created_at: datetime
