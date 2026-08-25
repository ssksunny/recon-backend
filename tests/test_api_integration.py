"""
End-to-end integration test against a real PostgreSQL database (JSONB/UUID
columns don't work on SQLite, so this intentionally isn't a sqlite-in-memory
test). Requires DATABASE_URL to point at a reachable, disposable Postgres —
see the README note this test's docstring doubles as:

    createdb recon_test   # or let this test's fixture create it
    DATABASE_URL=postgresql+psycopg2://recon:recon@localhost:5432/recon_test \
        pytest tests/test_api_integration.py -v

The Claude calls (extraction + matching) are monkeypatched — this test is
about verifying the API/DB/service wiring, not re-testing app.ai.matching's
own logic (that's tests/test_matching.py's job).
"""

import base64
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("JWT_SECRET_KEY", "test-secret")
# Both providers' keys are set (even though AI_PROVIDER defaults to
# "gemini") so Settings() doesn't raise regardless of which provider is
# active — the real AI calls are always monkeypatched in these tests
# anyway (see mocked_ai below), never live.
os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("INBOUND_EMAIL_WEBHOOK_USERNAME", "test-webhook-user")
os.environ.setdefault("INBOUND_EMAIL_WEBHOOK_PASSWORD", "test-webhook-pass")
os.environ.setdefault("MAILGUN_SIGNING_KEY", "test-mailgun-signing-key")
# Document processing jobs run synchronously (still through a real Redis —
# see app/core/queue.py) so upload/email assertions can check outcomes
# immediately without a separate worker process or polling. Requires Redis
# reachable at REDIS_URL (defaults to localhost:6379/0); this must be set
# before app.core.config is first imported by anything, same as the
# JWT/Anthropic defaults above.
os.environ.setdefault("BACKGROUND_JOBS_ENABLED", "false")

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg2://recon:recon@localhost:5432/recon_test"
)


@pytest.fixture(scope="session", autouse=True)
def _create_test_database():
    admin_engine = create_engine(
        "postgresql+psycopg2://recon:recon@localhost:5432/recon", isolation_level="AUTOCOMMIT"
    )
    with admin_engine.connect() as conn:
        conn.execute(text("DROP DATABASE IF EXISTS recon_test"))
        conn.execute(text("CREATE DATABASE recon_test"))
    admin_engine.dispose()
    yield


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)

    # Fresh settings/engine bound to the test database for this test only.
    from app.core.config import Settings
    test_settings = Settings(database_url=TEST_DATABASE_URL)

    import app.core.config as config_module
    monkeypatch.setattr(config_module, "settings", test_settings)

    import app.models.database as database_module
    test_engine = create_engine(test_settings.database_url, future=True)
    TestSessionLocal = sessionmaker(bind=test_engine, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(database_module, "engine", test_engine)
    monkeypatch.setattr(database_module, "SessionLocal", TestSessionLocal)

    from app.models.models import Base
    Base.metadata.drop_all(test_engine)
    Base.metadata.create_all(test_engine)

    # Every module that imported `settings` or `get_db` directly needs to see
    # the same swapped objects — patch them at each import site.
    for mod_name in ["app.api.auth", "app.api.deps", "app.api.email"]:
        import importlib
        mod = importlib.import_module(mod_name)
        if hasattr(mod, "settings"):
            monkeypatch.setattr(mod, "settings", test_settings)

    from app.main import app as fastapi_app

    def override_get_db():
        db = TestSessionLocal()
        try:
            yield db
        finally:
            db.close()

    fastapi_app.dependency_overrides[database_module.get_db] = override_get_db

    with TestClient(fastapi_app) as test_client:
        yield test_client

    fastapi_app.dependency_overrides.clear()
    test_engine.dispose()


@pytest.fixture()
def mocked_ai(monkeypatch):
    """
    Replaces app.ai.matching's Claude calls with deterministic fakes so the
    integration test never hits the network and doesn't need a real API key.
    """
    from app.ai import matching

    fake_rate_con = {
        "load_number": "LOAD-1001",
        "carrier_name": "Acme Trucking",
        "origin": "Atlanta, GA",
        "destination": "Charlotte, NC",
        "equipment_type": "Dry Van",
        "pickup_date": "2026-08-20",
        "delivery_date": "2026-08-21",
        "linehaul_rate": 1000.0,
        "fuel_surcharge_terms": {"type": "all_in", "value": None},
        "detention_terms": {"free_time_hours": 2, "rate_per_hour": 50},
        "accessorials_allowed": [{"type": "lumper", "max_amount": 150}],
        "confidence": 0.95,
        "warnings": [],
    }
    fake_invoice = {
        "invoice_number": "INV-9001",
        "load_number": "LOAD-1001",
        "carrier_name": "Acme Trucking",
        "line_items": [
            {"line_type": "linehaul", "description": "Linehaul", "amount": 1000.0},
            {"line_type": "fuel_surcharge", "description": "FSC", "amount": 180.0},
        ],
        "total_amount": 1180.0,
        "confidence": 0.9,
        "warnings": [],
    }

    def fake_extract(file_bytes, media_type, doc_type):
        if doc_type == matching.DocumentType.RATE_CONFIRMATION:
            return dict(fake_rate_con)
        if doc_type == matching.DocumentType.INVOICE:
            return dict(fake_invoice)
        raise AssertionError(f"unexpected doc_type in test: {doc_type}")

    def fake_match(rate_confirmation, invoice, pod=None):
        # Mirrors what the real engine would conclude for this fixture data:
        # all-in rate + a separately billed FSC is a discrepancy.
        decision = {
            "status": "clean",  # deliberately "wrong" to prove finalize recomputes it
            "summary": "Fuel surcharge billed separately on an all-in rate confirmation.",
            "confidence": 0.9,
            "recommended_action": "Route to reviewer: fuel surcharge overbilled by $180.",
            "line_items": [
                {"line_type": "linehaul", "billed_amount": 1000.0, "expected_amount": 1000.0, "decision": "clean", "reason": "Matches rate confirmation."},
                {"line_type": "fuel_surcharge", "billed_amount": 180.0, "expected_amount": 0.0, "decision": "discrepancy", "reason": "All-in rate; no separate FSC authorized."},
            ],
        }
        return matching._finalize_decision(decision, pod_present=pod is not None)

    def fake_classify(file_bytes, media_type):
        # Email tests distinguish attachments by planting a marker in the
        # fake bytes (real classification reads the actual PDF content;
        # nothing in this test suite exercises that, see test_matching.py
        # for classify_document_type's own unit tests).
        if b"RATECON" in file_bytes:
            return matching.DocumentType.RATE_CONFIRMATION, 0.95, "Looks like a rate confirmation."
        if b"INVOICE" in file_bytes:
            return matching.DocumentType.INVOICE, 0.9, "Looks like an invoice."
        if b"POD" in file_bytes:
            return matching.DocumentType.POD, 0.9, "Looks like a proof of delivery."
        raise matching.ExtractionError("test fixture: no classification marker found in file_bytes")

    monkeypatch.setattr(matching, "extract_document_data", fake_extract)
    monkeypatch.setattr(matching, "match_invoice", fake_match)
    monkeypatch.setattr(matching, "classify_document_type", fake_classify)

    # app.jobs.document_jobs, matching_service, and email_service each
    # imported these by name (`from app.ai.matching import ...`), so patch
    # those bindings too — patching the `matching` module alone wouldn't
    # reach them. extract_document_data now runs inside the background job
    # (see app/jobs/document_jobs.py), not document_service directly.
    from app.jobs import document_jobs
    from app.services import email_service, matching_service
    monkeypatch.setattr(document_jobs, "extract_document_data", fake_extract)
    monkeypatch.setattr(matching_service, "match_invoice", fake_match)
    monkeypatch.setattr(email_service, "classify_document_type", fake_classify)


def _register_and_login(client: TestClient) -> str:
    slug = f"acme-{uuid.uuid4().hex[:8]}"
    resp = client.post(
        "/api/v1/auth/register",
        json={
            "company_name": "Acme Freight",
            "company_slug": slug,
            "admin_email": "admin@example.com",
            "admin_password": "supersecret123",
            "admin_full_name": "Ada Min",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["access_token"]


def test_full_ingest_and_review_flow(client: TestClient, mocked_ai):
    token = _register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}

    me = client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["role"] == "admin"

    # Document processing (extraction, load-linking, matching) now happens
    # in a background job — see app/jobs/document_jobs.py — so the upload
    # response itself no longer carries load_id/match_result; the test
    # fixture runs jobs synchronously (BACKGROUND_JOBS_ENABLED=false), so by
    # the time each request returns the job has already finished, but the
    # response body still reflects the pre-processing state on purpose
    # (see DocumentReceiveResult's docstring) — that's the real production
    # contract, not a test artifact. Assertions below re-fetch from the API
    # instead of trusting the upload response's own fields.
    rc_resp = client.post(
        "/api/v1/documents/upload",
        headers=headers,
        data={"doc_type": "rate_confirmation"},
        files={"file": ("ratecon.pdf", b"%PDF-fake", "application/pdf")},
    )
    assert rc_resp.status_code == 201, rc_resp.text
    assert rc_resp.json()["load_id"] is None  # not known synchronously — resolved by the background job

    # No load_id given here either — this also exercises the auto-link-by-
    # extracted-load-number path (both fixtures' load_number is LOAD-1001).
    inv_resp = client.post(
        "/api/v1/documents/upload",
        headers=headers,
        data={"doc_type": "invoice"},
        files={"file": ("invoice.pdf", b"%PDF-fake", "application/pdf")},
    )
    assert inv_resp.status_code == 201, inv_resp.text
    assert inv_resp.json()["load_id"] is None

    all_loads = client.get("/api/v1/loads", headers=headers).json()
    assert len(all_loads) == 1  # the RC's auto-created load and the invoice's auto-found load are the same one
    load_id = all_loads[0]["id"]

    load_detail = client.get(f"/api/v1/loads/{load_id}", headers=headers)
    assert load_detail.status_code == 200
    detail = load_detail.json()
    assert detail["match_status"] == "discrepancy"
    assert len(detail["line_items"]) == 2
    assert detail["status"] == "matched"
    # The load's persisted match_result (pulled from the audit log) should
    # match what the upload response returned live.
    assert detail["match_result"] is not None
    assert detail["match_result"]["status"] == "discrepancy"
    assert detail["match_result"]["variance"] == pytest.approx(180.0, abs=0.01)
    assert detail["match_result"]["recommended_action"]

    exceptions = client.get("/api/v1/loads/exceptions", headers=headers)
    assert exceptions.status_code == 200
    assert any(item["id"] == load_id for item in exceptions.json())

    all_loads = client.get("/api/v1/loads", headers=headers)
    assert all_loads.status_code == 200
    assert len(all_loads.json()) == 1

    fsc_line_item_id = next(li["id"] for li in detail["line_items"] if li["line_type"] == "fuel_surcharge")
    override_resp = client.post(
        "/api/v1/reviews",
        headers=headers,
        json={
            "load_id": load_id,
            "line_item_id": fsc_line_item_id,
            "action": "override",
            "new_status": "clean",
            "note": "Confirmed with carrier this was pre-approved outside the rate confirmation.",
        },
    )
    assert override_resp.status_code == 201, override_resp.text
    assert override_resp.json()["new_status"] == "clean"

    load_detail_after = client.get(f"/api/v1/loads/{load_id}", headers=headers).json()
    assert load_detail_after["match_status"] == "clean"

    approve_resp = client.post(
        "/api/v1/reviews",
        headers=headers,
        json={"load_id": load_id, "action": "approve", "note": "Looks good."},
    )
    assert approve_resp.status_code == 201, approve_resp.text

    load_after_approve = client.get(f"/api/v1/loads/{load_id}", headers=headers).json()
    assert load_after_approve["status"] == "closed"

    reviews = client.get("/api/v1/reviews", headers=headers, params={"load_id": load_id})
    assert reviews.status_code == 200
    assert len(reviews.json()) == 2  # override + approve


def test_login_rejects_wrong_password(client: TestClient):
    _register_and_login(client)  # creates admin@example.com in some company
    resp = client.post("/api/v1/auth/login", data={"username": "admin@example.com", "password": "wrong"})
    assert resp.status_code == 401


def test_unauthenticated_requests_are_rejected(client: TestClient):
    resp = client.get("/api/v1/loads")
    assert resp.status_code == 401


def test_company_data_isolation(client: TestClient, mocked_ai):
    token_a = _register_and_login(client)
    token_b = _register_and_login(client)

    upload = client.post(
        "/api/v1/documents/upload",
        headers={"Authorization": f"Bearer {token_a}"},
        data={"doc_type": "rate_confirmation"},
        files={"file": ("ratecon.pdf", b"%PDF-fake", "application/pdf")},
    )
    assert upload.status_code == 201, upload.text
    # load_id isn't known synchronously anymore (see test_full_ingest_and_review_flow) —
    # the background job (run synchronously in this test fixture) has already
    # created it by the time the upload request returns, so list it back.
    load_id = client.get("/api/v1/loads", headers={"Authorization": f"Bearer {token_a}"}).json()[0]["id"]

    # Company B must not be able to see company A's load.
    resp = client.get(f"/api/v1/loads/{load_id}", headers={"Authorization": f"Bearer {token_b}"})
    assert resp.status_code == 404

    resp_a = client.get(f"/api/v1/loads/{load_id}", headers={"Authorization": f"Bearer {token_a}"})
    assert resp_a.status_code == 200


def test_document_file_download_roundtrips_bytes_and_is_tenant_scoped(client: TestClient, mocked_ai):
    token_a = _register_and_login(client)
    token_b = _register_and_login(client)
    original_bytes = b"%PDF-fake rate confirmation bytes"

    upload = client.post(
        "/api/v1/documents/upload",
        headers={"Authorization": f"Bearer {token_a}"},
        data={"doc_type": "rate_confirmation"},
        files={"file": ("ratecon.pdf", original_bytes, "application/pdf")},
    )
    assert upload.status_code == 201, upload.text
    document_id = upload.json()["document"]["id"]
    assert upload.json()["document"]["content_type"] == "application/pdf"

    file_resp = client.get(
        f"/api/v1/documents/{document_id}/file", headers={"Authorization": f"Bearer {token_a}"}
    )
    assert file_resp.status_code == 200
    assert file_resp.content == original_bytes
    assert file_resp.headers["content-type"] == "application/pdf"
    assert "inline" in file_resp.headers["content-disposition"]
    assert "ratecon.pdf" in file_resp.headers["content-disposition"]

    # Company B must not be able to fetch company A's file.
    cross_tenant = client.get(
        f"/api/v1/documents/{document_id}/file", headers={"Authorization": f"Bearer {token_b}"}
    )
    assert cross_tenant.status_code == 404


def test_load_audit_trail_covers_ingestion_matching_and_review(client: TestClient, mocked_ai):
    token = _register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}

    client.post(
        "/api/v1/documents/upload",
        headers=headers,
        data={"doc_type": "rate_confirmation"},
        files={"file": ("ratecon.pdf", b"%PDF-fake", "application/pdf")},
    )
    client.post(
        "/api/v1/documents/upload",
        headers=headers,
        data={"doc_type": "invoice"},  # no load_id — auto-linked by extracted load number
        files={"file": ("invoice.pdf", b"%PDF-fake", "application/pdf")},
    )

    load_id = client.get("/api/v1/loads", headers=headers).json()[0]["id"]
    detail = client.get(f"/api/v1/loads/{load_id}", headers=headers).json()
    fsc_line_item_id = next(li["id"] for li in detail["line_items"] if li["line_type"] == "fuel_surcharge")
    client.post(
        "/api/v1/reviews",
        headers=headers,
        json={
            "load_id": load_id,
            "line_item_id": fsc_line_item_id,
            "action": "override",
            "new_status": "clean",
            "note": "Confirmed with carrier this was pre-approved.",
        },
    )

    audit_resp = client.get(f"/api/v1/loads/{load_id}/audit", headers=headers)
    assert audit_resp.status_code == 200
    entries = audit_resp.json()
    event_types = [e["event_type"] for e in entries]

    # Two documents received + extracted, one match decision, one override —
    # in chronological order, oldest first.
    assert event_types.count("document_received") == 2
    assert event_types.count("extraction_completed") == 2
    assert "match_decision" in event_types
    assert event_types[-1] == "override"
    assert entries == sorted(entries, key=lambda e: e["created_at"])

    override_entry = entries[-1]
    assert override_entry["actor_type"] == "user"
    assert override_entry["actor_name"] == "Ada Min"
    assert override_entry["details"]["note"] == "Confirmed with carrier this was pre-approved."

    # A load in another company can't be audited by this user.
    other_token = _register_and_login(client)
    cross_tenant = client.get(
        f"/api/v1/loads/{load_id}/audit", headers={"Authorization": f"Bearer {other_token}"}
    )
    assert cross_tenant.status_code == 404


def test_get_my_company_returns_inbound_email(client: TestClient):
    token = _register_and_login(client)
    resp = client.get("/api/v1/company/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["inbound_email"]
    assert body["slug"] in body["inbound_email"]


def test_inbound_email_postmark_ingests_attachments_and_triggers_matching(client: TestClient, mocked_ai):
    token = _register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    inbound_address = client.get("/api/v1/company/me", headers=headers).json()["inbound_email"]

    def b64(data: bytes) -> str:
        return base64.b64encode(data).decode("ascii")

    # Invoice listed before the rate confirmation on purpose — ingestion is
    # supposed to reorder rate confirmations first regardless of attachment
    # order, so this also proves that reordering actually happens.
    payload = {
        "From": "dispatch@acmetrucking.com",
        "To": f"Recon Inbox <{inbound_address}>",
        "Subject": "Docs for load LOAD-1001",
        "Attachments": [
            {"Name": "invoice.pdf", "Content": b64(b"%PDF-fake INVOICE bytes"), "ContentType": "application/pdf"},
            {"Name": "ratecon.pdf", "Content": b64(b"%PDF-fake RATECON bytes"), "ContentType": "application/pdf"},
            {"Name": "signature-logo.png", "Content": b64(b"not a pdf"), "ContentType": "image/png"},
        ],
    }

    resp = client.post(
        "/api/v1/email/inbound/postmark",
        json=payload,
        auth=("test-webhook-user", "test-webhook-pass"),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()

    assert body["skipped"] == ["signature-logo.png"]
    assert len(body["processed"]) == 2
    by_filename = {p["filename"]: p for p in body["processed"]}
    # "queued", not "processed" — extraction/matching happen in the
    # background job (app/jobs/document_jobs.py), which the test fixture
    # runs synchronously, so the outcome is already there by the time we
    # check the load below, but the webhook's own response only reflects
    # what happened synchronously (classification + storage).
    assert by_filename["ratecon.pdf"]["status"] == "queued"
    assert by_filename["ratecon.pdf"]["doc_type"] == "rate_confirmation"
    assert by_filename["invoice.pdf"]["status"] == "queued"
    assert by_filename["invoice.pdf"]["doc_type"] == "invoice"

    loads = client.get("/api/v1/loads", headers=headers).json()
    assert len(loads) == 1  # the RC's auto-created load and the invoice's auto-found load are the same one
    load_id = loads[0]["id"]
    # Matching should have run automatically once both documents were in —
    # no separate "trigger matching" call needed.
    load_detail = client.get(f"/api/v1/loads/{load_id}", headers=headers).json()
    assert load_detail["match_status"] == "discrepancy"
    assert load_detail["match_result"] is not None

    audit = client.get(f"/api/v1/loads/{load_id}/audit", headers=headers).json()
    received = [e for e in audit if e["event_type"] == "document_received"]
    assert len(received) == 2
    assert all(e["details"]["source"] == "email" for e in received)
    assert all(e["details"]["from_email"] == "dispatch@acmetrucking.com" for e in received)
    assert all(e["actor_type"] == "system" for e in received)


def test_inbound_email_postmark_rejects_bad_or_missing_credentials(client: TestClient):
    payload = {"From": "a@b.com", "To": "nobody@inbound.reconapp.io", "Subject": None, "Attachments": []}

    wrong_creds = client.post("/api/v1/email/inbound/postmark", json=payload, auth=("wrong", "creds"))
    assert wrong_creds.status_code == 401

    no_creds = client.post("/api/v1/email/inbound/postmark", json=payload)
    assert no_creds.status_code == 401


def test_inbound_email_postmark_unknown_recipient_returns_404(client: TestClient):
    resp = client.post(
        "/api/v1/email/inbound/postmark",
        json={"From": "a@b.com", "To": "doesnotexist@inbound.reconapp.io", "Subject": None, "Attachments": []},
        auth=("test-webhook-user", "test-webhook-pass"),
    )
    assert resp.status_code == 404


def _mailgun_signature(timestamp: str, token: str, signing_key: str = "test-mailgun-signing-key") -> str:
    import hashlib
    import hmac

    return hmac.new(signing_key.encode(), f"{timestamp}{token}".encode(), hashlib.sha256).hexdigest()


def test_inbound_email_mailgun_ingests_multipart_attachments(client: TestClient, mocked_ai):
    token = _register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    inbound_address = client.get("/api/v1/company/me", headers=headers).json()["inbound_email"]

    timestamp, mg_token = "1690000000", "faketoken123"
    form_data = {
        "recipient": inbound_address,
        "sender": "dispatch@acmetrucking.com",
        "subject": "Docs for load LOAD-1001",
        "timestamp": timestamp,
        "token": mg_token,
        "signature": _mailgun_signature(timestamp, mg_token),
        "attachment-count": "2",
    }
    files = {
        "attachment-1": ("ratecon.pdf", b"%PDF-fake RATECON bytes", "application/pdf"),
        "attachment-2": ("invoice.pdf", b"%PDF-fake INVOICE bytes", "application/pdf"),
    }

    resp = client.post("/api/v1/email/inbound/mailgun", data=form_data, files=files)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert len(body["processed"]) == 2
    by_filename = {p["filename"]: p for p in body["processed"]}
    assert by_filename["ratecon.pdf"]["status"] == "queued"
    assert by_filename["invoice.pdf"]["status"] == "queued"

    loads = client.get("/api/v1/loads", headers=headers).json()
    assert len(loads) == 1
    load_detail = client.get(f"/api/v1/loads/{loads[0]['id']}", headers=headers).json()
    assert load_detail["match_result"] is not None


def test_inbound_email_mailgun_rejects_bad_signature(client: TestClient):
    form_data = {
        "recipient": "nobody@inbound.reconapp.io",
        "sender": "a@b.com",
        "timestamp": "123",
        "token": "abc",
        "signature": "not-a-real-signature",
        "attachment-count": "0",
    }
    resp = client.post("/api/v1/email/inbound/mailgun", data=form_data)
    assert resp.status_code == 401


def test_document_processing_is_genuinely_async_with_a_real_worker(client: TestClient, mocked_ai, monkeypatch):
    """
    Every other test in this file runs with BACKGROUND_JOBS_ENABLED=false
    (see the module-level env defaults) for speed and determinism — jobs
    execute synchronously the instant they're enqueued. That's convenient
    for testing, but it doesn't prove the real production path (a job
    sitting queued until a separate `rq worker` process picks it up)
    actually works. This test swaps in a real is_async=True queue for just
    itself, uploads a document, confirms it is NOT processed yet (nothing
    is consuming the queue), then runs an in-process RQ worker in burst
    mode and confirms it IS processed afterward.
    """
    from rq import Queue, SimpleWorker

    from app.core import queue as queue_module
    from app.services import document_service

    real_async_queue = Queue(
        queue_module.DOCUMENT_QUEUE_NAME, connection=queue_module.get_redis_connection(), is_async=True
    )
    monkeypatch.setattr(document_service, "document_queue", real_async_queue)

    token = _register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}

    upload = client.post(
        "/api/v1/documents/upload",
        headers=headers,
        data={"doc_type": "rate_confirmation"},
        files={"file": ("ratecon.pdf", b"%PDF-fake", "application/pdf")},
    )
    assert upload.status_code == 201, upload.text
    document_id = upload.json()["document"]["id"]

    # Enqueued but genuinely unprocessed — no worker has run yet.
    before = client.get(f"/api/v1/documents/{document_id}", headers=headers).json()
    assert before["status"] == "received"
    assert client.get("/api/v1/loads", headers=headers).json() == []

    # SimpleWorker runs jobs in-process (no fork) — the right choice here,
    # since a forked child wouldn't share this test's monkeypatched,
    # transaction-scoped test database session/engine.
    worker = SimpleWorker([real_async_queue], connection=queue_module.get_redis_connection())
    worker.work(burst=True)  # processes everything currently queued, then returns

    after = client.get(f"/api/v1/documents/{document_id}", headers=headers).json()
    assert after["status"] == "processed"
    loads = client.get("/api/v1/loads", headers=headers).json()
    assert len(loads) == 1
