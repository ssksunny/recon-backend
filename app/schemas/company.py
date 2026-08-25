from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict


class CompanyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    # Documents emailed here are ingested automatically — see
    # app/services/email_service.py. None only if a company somehow
    # predates this field; registration always sets it.
    inbound_email: str | None
    is_active: bool
