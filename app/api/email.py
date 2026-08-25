"""
Inbound email ingestion: one endpoint per email provider, each translating
that provider's webhook payload into a provider-agnostic InboundEmail and
handing it to app/services/email_service.ingest_inbound_email(). See that
module's docstring for what happens next (classification, storage,
extraction, load-linking, matching) — this router's only job is speaking
each provider's wire format and verifying the request actually came from
that provider.

Neither endpoint uses the app's normal Bearer-token auth — the caller is an
email provider's server, not a logged-in user — but neither is open, either:

    /inbound/postmark  — protected by HTTP Basic Auth credentials baked into
                          the webhook URL you register with Postmark (their
                          own recommended approach for inbound webhooks).
    /inbound/mailgun   — protected by verifying Mailgun's HMAC signature
                          (timestamp + token + signature) against your
                          Mailgun signing key.

Both refuse every request (503) if their corresponding secret isn't
configured, rather than silently accepting unauthenticated mail — an open
inbound-email endpoint lets anyone create loads and documents for any
company whose inbound address they can guess.

See the project's build-status doc / the chat response this shipped with
for the step-by-step production setup for each provider.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.database import get_db
from app.schemas.email import EmailIngestResponseOut, PostmarkInboundPayload
from app.services.email_service import InboundEmail, InboundEmailAttachment, ingest_inbound_email

router = APIRouter()

_basic_auth = HTTPBasic(auto_error=False)


def _verify_postmark_auth(credentials: HTTPBasicCredentials | None = Depends(_basic_auth)) -> None:
    expected_user = settings.inbound_email_webhook_username
    expected_pass = settings.inbound_email_webhook_password
    if not expected_user or not expected_pass:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Inbound email webhook is not configured (INBOUND_EMAIL_WEBHOOK_USERNAME/PASSWORD unset).",
        )
    valid = (
        credentials is not None
        and secrets.compare_digest(credentials.username, expected_user)
        and secrets.compare_digest(credentials.password, expected_pass)
    )
    if not valid:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook credentials.",
            headers={"WWW-Authenticate": "Basic"},
        )


@router.post("/inbound/postmark", response_model=EmailIngestResponseOut, status_code=status.HTTP_201_CREATED)
def inbound_postmark(
    payload: PostmarkInboundPayload,
    db: Session = Depends(get_db),
    _auth: None = Depends(_verify_postmark_auth),
) -> EmailIngestResponseOut:
    email = InboundEmail(
        to_address=payload.To,
        from_address=payload.From,
        subject=payload.Subject,
        attachments=[
            InboundEmailAttachment(filename=a.Name, content_type=a.ContentType, data=a.decoded_content())
            for a in payload.Attachments
        ],
    )
    result = ingest_inbound_email(db, email)
    return EmailIngestResponseOut.from_result(result)


def _verify_mailgun_signature(token: str, timestamp: str, signature: str) -> bool:
    if not settings.mailgun_signing_key or not token or not timestamp or not signature:
        return False
    digest = hmac.new(
        key=settings.mailgun_signing_key.encode("utf-8"),
        msg=f"{timestamp}{token}".encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()
    return secrets.compare_digest(digest, signature)


@router.post("/inbound/mailgun", response_model=EmailIngestResponseOut, status_code=status.HTTP_201_CREATED)
async def inbound_mailgun(request: Request, db: Session = Depends(get_db)) -> EmailIngestResponseOut:
    """
    Mailgun posts inbound email as multipart/form-data — attachments arrive
    as real file parts (attachment-1, attachment-2, ...), not base64 text —
    so this reads the form directly rather than declaring a Pydantic body
    model the way the Postmark endpoint does.
    """
    if not settings.mailgun_signing_key:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Inbound email webhook is not configured (MAILGUN_SIGNING_KEY unset).",
        )

    form = await request.form()

    token = str(form.get("token", ""))
    timestamp = str(form.get("timestamp", ""))
    signature = str(form.get("signature", ""))
    if not _verify_mailgun_signature(token, timestamp, signature):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid Mailgun signature.")

    try:
        attachment_count = int(form.get("attachment-count", 0) or 0)
    except ValueError:
        attachment_count = 0

    attachments: list[InboundEmailAttachment] = []
    for i in range(1, attachment_count + 1):
        upload = form.get(f"attachment-{i}")
        if upload is None or not hasattr(upload, "read"):
            continue
        attachments.append(
            InboundEmailAttachment(
                filename=getattr(upload, "filename", None) or f"attachment-{i}",
                content_type=getattr(upload, "content_type", None),
                data=await upload.read(),
            )
        )

    email = InboundEmail(
        to_address=str(form.get("recipient", "")),
        from_address=str(form.get("sender", "") or form.get("from", "")),
        subject=(str(form.get("subject")) if form.get("subject") else None),
        attachments=attachments,
    )
    result = ingest_inbound_email(db, email)
    return EmailIngestResponseOut.from_result(result)
