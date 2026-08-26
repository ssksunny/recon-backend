"""
Integration tests for the broker self-service portal: invite -> accept ->
login, and — the central concern of this feature — that a carrier can never
see another carrier's loads, an unassigned load, or use its token against
the admin/reviewer API (and vice versa).

Same real-Postgres convention as test_api_integration.py (JSONB/UUID columns
don't work on SQLite) — see that file's module docstring for how to point
this at a disposable database. The fixtures below are deliberately
self-contained rather than shared via a conftest.py, matching how this
repo's existing integration test file is structured.
"""

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("JWT_SECRET_KEY", "test-secret")
os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("INBOUND_EMAIL_WEBHOOK_USERNAME", "test-webhook-user")
os.environ.setdefault("INBOUND_EMAIL_WEBHOOK_PASSWORD", "test-webhook-pass")
os.environ.setdefault("MAILGUN_SIGNING_KEY", "test-mailgun-signing-key")
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

    for mod_name in ["app.api.auth", "app.api.deps", "app.api.email", "app.api.carriers"]:
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
    """Same deterministic fakes as test_api_integration.py's fixture of the same name — see there for rationale."""
    from app.ai import matching

    fake_rate_con = {
        "load_number": "LOAD-2001",
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
        "load_number": "LOAD-2001",
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
        decision = {
            "status": "clean",
            "summary": "Fuel surcharge billed separately on an all-in rate confirmation.",
            "confidence": 0.9,
            "recommended_action": "Route to reviewer: fuel surcharge overbilled by $180.",
            "line_items": [
                {"line_type": "linehaul", "billed_amount": 1000.0, "expected_amount": 1000.0, "decision": "clean", "reason": "Matches rate confirmation."},
                {"line_type": "fuel_surcharge", "billed_amount": 180.0, "expected_amount": 0.0, "decision": "discrepancy", "reason": "All-in rate; no separate FSC authorized."},
            ],
        }
        return matching._finalize_decision(decision, pod_present=pod is not None)

    monkeypatch.setattr(matching, "extract_document_data", fake_extract)
    monkeypatch.setattr(matching, "match_invoice", fake_match)

    from app.jobs import document_jobs
    from app.services import matching_service
    monkeypatch.setattr(document_jobs, "extract_document_data", fake_extract)
    monkeypatch.setattr(matching_service, "match_invoice", fake_match)


def _register_and_login(client: TestClient) -> tuple[str, str]:
    """Returns (admin_access_token, company_slug)."""
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
    return resp.json()["access_token"], slug


def _create_load(client: TestClient, admin_headers: dict) -> str:
    """Uploads a rate confirmation (mocked_ai extraction creates LOAD-2001) and returns its id."""
    resp = client.post(
        "/api/v1/documents/upload",
        headers=admin_headers,
        data={"doc_type": "rate_confirmation"},
        files={"file": ("ratecon.pdf", b"%PDF-fake", "application/pdf")},
    )
    assert resp.status_code == 201, resp.text
    loads = client.get("/api/v1/loads", headers=admin_headers).json()
    return next(l["id"] for l in loads if l["load_number"] == "LOAD-2001")


def _invite_and_accept_broker(
    client: TestClient, admin_headers: dict, carrier_id: str, email: str = "broker@carrier.example"
) -> str:
    """Creates an invite for `carrier_id` and immediately accepts it. Returns the broker's access token."""
    invite = client.post(
        f"/api/v1/carriers/{carrier_id}/invite",
        headers=admin_headers,
        json={"email": email, "full_name": "Bob Broker"},
    )
    assert invite.status_code == 201, invite.text
    token = invite.json()["invite_token"]

    accept = client.post(
        "/api/v1/broker/auth/accept-invite",
        json={"token": token, "password": "brokerpassword123"},
    )
    assert accept.status_code == 201, accept.text
    return accept.json()["access_token"]


def test_invite_accept_and_login_flow(client: TestClient):
    admin_token, _ = _register_and_login(client)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    carrier = client.post("/api/v1/carriers", headers=admin_headers, json={"name": "Swift Lines"})
    assert carrier.status_code == 201, carrier.text
    carrier_id = carrier.json()["id"]

    broker_token = _invite_and_accept_broker(client, admin_headers, carrier_id)
    me = client.get("/api/v1/broker/auth/me", headers={"Authorization": f"Bearer {broker_token}"})
    assert me.status_code == 200
    assert me.json()["carrier_id"] == carrier_id
    assert me.json()["email"] == "broker@carrier.example"

    # And a normal password login works too, independent of the invite token.
    login = client.post(
        "/api/v1/broker/auth/login", data={"username": "broker@carrier.example", "password": "brokerpassword123"}
    )
    assert login.status_code == 200, login.text


def test_unassigned_load_is_invisible_to_every_carrier(client: TestClient, mocked_ai):
    admin_token, _ = _register_and_login(client)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    load_id = _create_load(client, admin_headers)  # never assigned to any carrier

    carrier = client.post("/api/v1/carriers", headers=admin_headers, json={"name": "Swift Lines"}).json()
    broker_token = _invite_and_accept_broker(client, admin_headers, carrier["id"])
    broker_headers = {"Authorization": f"Bearer {broker_token}"}

    assert client.get("/api/v1/broker/loads", headers=broker_headers).json() == []
    assert client.get(f"/api/v1/broker/loads/{load_id}", headers=broker_headers).status_code == 404


def test_carrier_isolation_between_two_carriers(client: TestClient, mocked_ai):
    admin_token, _ = _register_and_login(client)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    load_id = _create_load(client, admin_headers)

    carrier_a = client.post("/api/v1/carriers", headers=admin_headers, json={"name": "Carrier A"}).json()
    carrier_b = client.post("/api/v1/carriers", headers=admin_headers, json={"name": "Carrier B"}).json()

    assign = client.post(
        f"/api/v1/loads/{load_id}/assign-carrier", headers=admin_headers, json={"carrier_id": carrier_a["id"]}
    )
    assert assign.status_code == 200, assign.text
    assert assign.json()["carrier_id"] == carrier_a["id"]

    token_a = _invite_and_accept_broker(client, admin_headers, carrier_a["id"], email="a@carrier-a.example")
    token_b = _invite_and_accept_broker(client, admin_headers, carrier_b["id"], email="b@carrier-b.example")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    # Carrier A (the assigned one) can see it.
    resp_a = client.get(f"/api/v1/broker/loads/{load_id}", headers=headers_a)
    assert resp_a.status_code == 200
    loads_a = client.get("/api/v1/broker/loads", headers=headers_a).json()
    assert [l["id"] for l in loads_a] == [load_id]

    # Carrier B must not be able to see, respond to, or upload against it.
    assert client.get(f"/api/v1/broker/loads/{load_id}", headers=headers_b).status_code == 404
    assert client.get("/api/v1/broker/loads", headers=headers_b).json() == []
    assert client.post(f"/api/v1/broker/loads/{load_id}/respond", headers=headers_b, json={"message": "hi"}).status_code == 404
    upload_b = client.post(
        f"/api/v1/broker/loads/{load_id}/documents",
        headers=headers_b,
        data={"doc_type": "pod"},
        files={"file": ("pod.pdf", b"%PDF-fake", "application/pdf")},
    )
    assert upload_b.status_code == 404

    # Unassigning revokes A's access too.
    unassign = client.post(f"/api/v1/loads/{load_id}/assign-carrier", headers=admin_headers, json={"carrier_id": None})
    assert unassign.status_code == 200
    assert client.get(f"/api/v1/broker/loads/{load_id}", headers=headers_a).status_code == 404


def test_broker_can_view_respond_and_upload_documents(client: TestClient, mocked_ai):
    admin_token, _ = _register_and_login(client)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    load_id = _create_load(client, admin_headers)
    carrier = client.post("/api/v1/carriers", headers=admin_headers, json={"name": "Swift Lines"}).json()
    client.post(f"/api/v1/loads/{load_id}/assign-carrier", headers=admin_headers, json={"carrier_id": carrier["id"]})

    broker_token = _invite_and_accept_broker(client, admin_headers, carrier["id"])
    broker_headers = {"Authorization": f"Bearer {broker_token}"}

    detail = client.get(f"/api/v1/broker/loads/{load_id}", headers=broker_headers)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert "reviews" not in body  # internal reviewer notes are not part of the broker's scope
    assert len(body["documents"]) == 1  # the rate confirmation
    assert body["load_number"] == "LOAD-2001"

    # Broker cannot upload a rate confirmation — that stays admin/email-only.
    bad_upload = client.post(
        f"/api/v1/broker/loads/{load_id}/documents",
        headers=broker_headers,
        data={"doc_type": "rate_confirmation"},
        files={"file": ("ratecon2.pdf", b"%PDF-fake", "application/pdf")},
    )
    assert bad_upload.status_code == 422

    upload = client.post(
        f"/api/v1/broker/loads/{load_id}/documents",
        headers=broker_headers,
        data={"doc_type": "pod"},
        files={"file": ("pod.pdf", b"%PDF-fake", "application/pdf")},
    )
    assert upload.status_code == 201, upload.text

    respond = client.post(
        f"/api/v1/broker/loads/{load_id}/respond", headers=broker_headers, json={"message": "POD attached, please re-check."}
    )
    assert respond.status_code == 204

    # The broker's response shows up in the admin's full audit trail, with
    # the broker's name resolved from the audit details (no users.id to join on).
    admin_audit = client.get(f"/api/v1/loads/{load_id}/audit", headers=admin_headers).json()
    broker_entries = [e for e in admin_audit if e["event_type"] == "broker_response"]
    assert len(broker_entries) == 1
    assert broker_entries[0]["actor_type"] == "carrier"
    assert broker_entries[0]["actor_name"] == "Bob Broker"
    assert broker_entries[0]["details"]["message"] == "POD attached, please re-check."

    # And the broker's own (filtered) audit view also shows it.
    broker_audit = client.get(f"/api/v1/broker/loads/{load_id}/audit", headers=broker_headers).json()
    assert any(e["event_type"] == "broker_response" for e in broker_audit)
    assert all(e["event_type"] not in {"review_action", "override"} for e in broker_audit)


def test_broker_token_rejected_by_admin_endpoints_and_vice_versa(client: TestClient, mocked_ai):
    admin_token, _ = _register_and_login(client)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    carrier = client.post("/api/v1/carriers", headers=admin_headers, json={"name": "Swift Lines"}).json()
    broker_token = _invite_and_accept_broker(client, admin_headers, carrier["id"])
    broker_headers = {"Authorization": f"Bearer {broker_token}"}

    # A broker token must not authenticate as an admin/reviewer...
    assert client.get("/api/v1/loads", headers=broker_headers).status_code == 401
    assert client.get("/api/v1/auth/me", headers=broker_headers).status_code == 401

    # ...and an admin token must not authenticate as a broker.
    assert client.get("/api/v1/broker/loads", headers=admin_headers).status_code == 401
    assert client.get("/api/v1/broker/auth/me", headers=admin_headers).status_code == 401


def test_invalid_invite_token_is_rejected(client: TestClient):
    resp = client.post(
        "/api/v1/broker/auth/accept-invite", json={"token": "not-a-real-token", "password": "supersecret123"}
    )
    assert resp.status_code == 400


def test_carrier_name_never_auto_assigns_carrier_id(client: TestClient, mocked_ai):
    """
    The central safety property behind Load.carrier_id: a rate confirmation
    naming "Acme Trucking" as the carrier must NOT automatically grant any
    Carrier account visibility, even if a Carrier happens to share that name.
    Only an explicit admin assignment does.
    """
    admin_token, _ = _register_and_login(client)
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    # The mocked rate confirmation's carrier_name is "Acme Trucking".
    load_id = _create_load(client, admin_headers)
    load_detail = client.get(f"/api/v1/loads/{load_id}", headers=admin_headers).json()
    assert load_detail["carrier_name"] == "Acme Trucking"
    assert load_detail["carrier_id"] is None

    same_name_carrier = client.post("/api/v1/carriers", headers=admin_headers, json={"name": "Acme Trucking"}).json()
    broker_token = _invite_and_accept_broker(client, admin_headers, same_name_carrier["id"])
    broker_headers = {"Authorization": f"Bearer {broker_token}"}

    assert client.get("/api/v1/broker/loads", headers=broker_headers).json() == []
    assert client.get(f"/api/v1/broker/loads/{load_id}", headers=broker_headers).status_code == 404
