from __future__ import annotations

import base64

from pydantic import BaseModel, ConfigDict, Field

from app.services.email_service import EmailIngestResult


class PostmarkAttachment(BaseModel):
    """
    Subset of Postmark's inbound attachment object. Postmark base64-encodes
    attachment bytes directly into the JSON payload (unlike Mailgun, which
    sends them as real multipart file parts — see app/api/email.py).
    """

    model_config = ConfigDict(extra="ignore")

    Name: str
    Content: str
    ContentType: str | None = None

    def decoded_content(self) -> bytes:
        return base64.b64decode(self.Content)


class PostmarkInboundPayload(BaseModel):
    """
    Subset of Postmark's inbound webhook payload we actually use. Postmark
    sends many more fields (MessageID, full header list, MailboxHash, spam
    score, ...); extra="ignore" means new Postmark fields never break this
    endpoint, we just don't look at them.
    """

    model_config = ConfigDict(extra="ignore")

    From: str
    To: str
    Subject: str | None = None
    Attachments: list[PostmarkAttachment] = Field(default_factory=list)


class AttachmentResultOut(BaseModel):
    filename: str
    document_id: str | None
    doc_type: str | None
    status: str  # "queued" | "failed" — see AttachmentResult in app/services/email_service.py
    error: str | None


class EmailIngestResponseOut(BaseModel):
    """
    What both provider endpoints return: per-attachment outcomes, so a
    webhook's caller (or whoever's debugging a support ticket) can see
    exactly what happened to each file without digging through logs. This
    reflects only what happened synchronously — classification and storage
    — not extraction or matching, which run in the background afterward
    (see app/jobs/document_jobs.py); check the relevant load for the actual
    outcome once its documents move past "processing".
    """

    company_id: str
    processed: list[AttachmentResultOut]
    skipped: list[str]

    @classmethod
    def from_result(cls, result: EmailIngestResult) -> "EmailIngestResponseOut":
        return cls(
            company_id=str(result.company_id),
            processed=[
                AttachmentResultOut(
                    filename=r.filename,
                    document_id=str(r.document_id) if r.document_id else None,
                    doc_type=r.doc_type,
                    status=r.status,
                    error=r.error,
                )
                for r in result.processed
            ],
            skipped=result.skipped,
        )
