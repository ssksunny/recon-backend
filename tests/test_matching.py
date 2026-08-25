"""
Unit tests for app.ai.matching.

These tests never call a real AI provider's API — they monkeypatch
`_call_with_retries` so the deterministic guardrails (no-POD detention,
status precedence, totals/rounding, error paths) can be verified in
isolation from the model itself.

The default/tested provider is Gemini (AI_PROVIDER=gemini, this test
session's default — see conftest-equivalent env setup in
test_api_integration.py) — fake responses below mirror the shape of a
google-genai Interaction with a function_call step. A separate,
smaller set of tests at the bottom exercises _build_request's Anthropic
branch directly, proving the provider switch actually produces a valid
Claude-shaped request too, without re-deriving the full scenario suite
against both providers.
"""

from types import SimpleNamespace

import pytest

from app.ai import matching
from app.core.config import settings
from app.models.models import DocumentType


class FakeFunctionCallStep:
    def __init__(self, name: str, arguments: dict):
        self.type = "function_call"
        self.name = name
        self.arguments = arguments


def fake_interaction(tool_name: str, arguments: dict, status: str = "completed"):
    return SimpleNamespace(steps=[FakeFunctionCallStep(tool_name, arguments)], status=status)


# --------------------------------------------------------------------------
# extract_document_data
# --------------------------------------------------------------------------

def test_extract_document_data_returns_tool_input(monkeypatch):
    captured = {}

    def fake_call(**kwargs):
        captured.update(kwargs)
        return fake_interaction(
            "record_rate_confirmation_data",
            {"linehaul_rate": 1200.0, "fuel_surcharge_terms": {"type": "all_in"}, "confidence": 0.95, "warnings": []},
        )

    monkeypatch.setattr(matching, "_call_with_retries", fake_call)

    result = matching.extract_document_data(b"%PDF-fake-bytes", "application/pdf", DocumentType.RATE_CONFIRMATION)

    assert result["linehaul_rate"] == 1200.0
    assert result["fuel_surcharge_terms"]["type"] == "all_in"
    # PDFs must use the "document" content block, not "image"
    assert captured["input"][0]["type"] == "document"
    assert captured["generation_config"]["tool_choice"] == {
        "allowed_tools": {"mode": "any", "tools": ["record_rate_confirmation_data"]}
    }


def test_extract_document_data_image_uses_image_block(monkeypatch):
    captured = {}

    def fake_call(**kwargs):
        captured.update(kwargs)
        return fake_interaction("record_pod_data", {"delivery_confirmed": True, "signed": True, "confidence": 0.8, "warnings": []})

    monkeypatch.setattr(matching, "_call_with_retries", fake_call)

    matching.extract_document_data(b"\x89PNGfakebytes", "image/png", DocumentType.POD)

    assert captured["input"][0]["type"] == "image"


def test_extract_document_data_rejects_unsupported_media_type():
    with pytest.raises(ValueError):
        matching.extract_document_data(b"data", "text/plain", DocumentType.INVOICE)


def test_extract_document_data_rejects_empty_file():
    with pytest.raises(ValueError):
        matching.extract_document_data(b"", "application/pdf", DocumentType.INVOICE)


def test_extract_document_data_raises_when_no_tool_call_returned(monkeypatch):
    monkeypatch.setattr(matching, "_call_with_retries", lambda **kwargs: fake_interaction("some_other_tool", {}, status="incomplete"))

    with pytest.raises(matching.ExtractionError):
        matching.extract_document_data(b"data", "application/pdf", DocumentType.INVOICE)


def test_extract_document_data_wraps_api_errors(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("network exploded")

    monkeypatch.setattr(matching, "_call_with_retries", boom)

    with pytest.raises(matching.ExtractionError):
        matching.extract_document_data(b"data", "application/pdf", DocumentType.INVOICE)


# --------------------------------------------------------------------------
# match_invoice / _finalize_decision
# --------------------------------------------------------------------------

RATE_CON = {
    "linehaul_rate": 1000.0,
    "fuel_surcharge_terms": {"type": "all_in", "value": None},
    "detention_terms": {"free_time_hours": 2, "rate_per_hour": 50},
    "accessorials_allowed": [{"type": "lumper", "max_amount": 150}],
}

INVOICE = {
    "line_items": [
        {"line_type": "linehaul", "amount": 1000.0},
        {"line_type": "fuel_surcharge", "amount": 200.0},
        {"line_type": "detention", "amount": 150.0},
    ]
}


def test_match_invoice_raises_on_empty_line_items():
    with pytest.raises(matching.MatchingError):
        matching.match_invoice(RATE_CON, {"line_items": []}, None)


def test_detention_forced_to_needs_info_without_pod(monkeypatch):
    # Model (wrongly) marks detention "clean" even though no POD was given —
    # the finalize step must override this regardless.
    decision = {
        "status": "clean",
        "summary": "Looks fine.",
        "confidence": 0.9,
        "recommended_action": "Approve for payment.",
        "line_items": [
            {"line_type": "linehaul", "billed_amount": 1000.0, "expected_amount": 1000.0, "decision": "clean", "reason": "Matches rate confirmation."},
            {"line_type": "fuel_surcharge", "billed_amount": 200.0, "expected_amount": 0.0, "decision": "discrepancy", "reason": "All-in rate, no separate FSC allowed."},
            {"line_type": "detention", "billed_amount": 150.0, "expected_amount": 150.0, "decision": "clean", "reason": "Wrongly approved by the model."},
        ],
    }
    monkeypatch.setattr(matching, "_call_with_retries", lambda **kwargs: fake_interaction("record_match_decision", decision))

    result = matching.match_invoice(RATE_CON, INVOICE, pod=None)

    detention_line = next(li for li in result["line_items"] if li["line_type"] == "detention")
    assert detention_line["decision"] == "needs_info"
    assert detention_line["expected_amount"] is None
    # Overall status must reflect the worst finding: discrepancy beats needs_info.
    assert result["status"] == "discrepancy"


def test_status_precedence_discrepancy_beats_needs_info(monkeypatch):
    decision = {
        "status": "needs_info",  # model's own top-level field, deliberately wrong
        "summary": "Mixed bag.",
        "confidence": 0.7,
        "recommended_action": "Route to reviewer.",
        "line_items": [
            {"line_type": "linehaul", "billed_amount": 1000.0, "expected_amount": 1000.0, "decision": "clean", "reason": "ok"},
            {"line_type": "fuel_surcharge", "billed_amount": 200.0, "expected_amount": 0.0, "decision": "discrepancy", "reason": "all-in violated"},
            {"line_type": "other", "billed_amount": 10.0, "expected_amount": None, "decision": "needs_info", "reason": "unclear line"},
        ],
    }
    monkeypatch.setattr(matching, "_call_with_retries", lambda **kwargs: fake_interaction("record_match_decision", decision))

    result = matching.match_invoice(RATE_CON, INVOICE, pod={"delivery_confirmed": True})

    assert result["status"] == "discrepancy"


def test_totals_and_variance_are_recomputed_and_rounded(monkeypatch):
    decision = {
        "status": "discrepancy",
        "summary": "Fuel surcharge overbilled.",
        "confidence": 1.5,  # deliberately out of range, should clamp to 1.0
        "recommended_action": "Route to reviewer.",
        "line_items": [
            {"line_type": "linehaul", "billed_amount": 1000.004, "expected_amount": 1000.0, "decision": "clean", "reason": "ok"},
            {"line_type": "fuel_surcharge", "billed_amount": 200.0, "expected_amount": 0.0, "decision": "discrepancy", "reason": "all-in violated"},
        ],
    }
    monkeypatch.setattr(matching, "_call_with_retries", lambda **kwargs: fake_interaction("record_match_decision", decision))

    result = matching.match_invoice(RATE_CON, {"line_items": decision["line_items"]}, pod=None)

    assert result["total_invoiced"] == pytest.approx(1200.0, abs=0.01)
    assert result["total_rate_con"] == pytest.approx(1000.0, abs=0.01)
    assert result["variance"] == pytest.approx(200.0, abs=0.01)
    assert result["confidence"] == 1.0  # clamped


def test_clean_when_all_lines_clean(monkeypatch):
    decision = {
        "status": "clean",
        "summary": "All good.",
        "confidence": 0.95,
        "recommended_action": "Approve for payment.",
        "line_items": [
            {"line_type": "linehaul", "billed_amount": 1000.0, "expected_amount": 1000.0, "decision": "clean", "reason": "ok"},
        ],
    }
    monkeypatch.setattr(matching, "_call_with_retries", lambda **kwargs: fake_interaction("record_match_decision", decision))

    result = matching.match_invoice(RATE_CON, {"line_items": decision["line_items"]}, pod=None)

    assert result["status"] == "clean"
    assert result["variance"] == 0.0


def test_match_invoice_raises_when_no_decision_returned(monkeypatch):
    monkeypatch.setattr(
        matching, "_call_with_retries",
        lambda **kwargs: fake_interaction("some_other_tool", {}, status="incomplete"),
    )

    with pytest.raises(matching.MatchingError):
        matching.match_invoice(RATE_CON, INVOICE, pod=None)


# --------------------------------------------------------------------------
# classify_document_type
# --------------------------------------------------------------------------

def test_classify_document_type_returns_type_confidence_and_reason(monkeypatch):
    monkeypatch.setattr(
        matching, "_call_with_retries",
        lambda **kwargs: fake_interaction(
            "record_document_classification",
            {"document_type": "invoice", "confidence": 0.88, "reason": "Itemized charges billed to the broker."},
        ),
    )

    doc_type, confidence, reason = matching.classify_document_type(b"%PDF-fake", "application/pdf")

    assert doc_type == DocumentType.INVOICE
    assert confidence == pytest.approx(0.88)
    assert reason == "Itemized charges billed to the broker."


def test_classify_document_type_rejects_unrecognized_type(monkeypatch):
    monkeypatch.setattr(
        matching, "_call_with_retries",
        lambda **kwargs: fake_interaction(
            "record_document_classification",
            {"document_type": "bill_of_lading", "confidence": 0.5, "reason": "..."},
        ),
    )

    with pytest.raises(matching.ExtractionError):
        matching.classify_document_type(b"%PDF-fake", "application/pdf")


def test_classify_document_type_rejects_empty_file():
    with pytest.raises(ValueError):
        matching.classify_document_type(b"", "application/pdf")


def test_classify_document_type_rejects_unsupported_media_type():
    with pytest.raises(ValueError):
        matching.classify_document_type(b"data", "text/plain")


# --------------------------------------------------------------------------
# Provider switch: proves AI_PROVIDER=anthropic still builds a valid,
# differently-shaped request — the fallback path documented in
# app/ai/matching.py's module docstring and DEPLOYMENT.md actually works,
# not just "looks plausible by inspection".
# --------------------------------------------------------------------------

def test_build_request_gemini_shape():
    kwargs = matching._build_request(
        system_prompt="sys",
        tool=matching._CLASSIFY_TOOL,
        blocks=[{"kind": "document", "media_type": "application/pdf", "data_b64": "AAAA"}],
        max_tokens=512,
    )
    assert kwargs["model"] == settings.gemini_model
    assert kwargs["system_instruction"] == "sys"
    assert kwargs["input"][0] == {"type": "document", "data": "AAAA", "mime_type": "application/pdf"}
    assert kwargs["tools"][0]["type"] == "function"
    assert kwargs["tools"][0]["parameters"] is matching._CLASSIFY_TOOL["input_schema"]
    assert kwargs["generation_config"]["tool_choice"]["allowed_tools"]["tools"] == ["record_document_classification"]


def test_build_request_anthropic_shape(monkeypatch):
    monkeypatch.setattr(settings, "ai_provider", "anthropic")
    try:
        kwargs = matching._build_request(
            system_prompt="sys",
            tool=matching._CLASSIFY_TOOL,
            blocks=[{"kind": "document", "media_type": "application/pdf", "data_b64": "AAAA"}],
            max_tokens=512,
        )
    finally:
        monkeypatch.setattr(settings, "ai_provider", "gemini")

    assert kwargs["model"] == settings.anthropic_model
    assert kwargs["system"] == "sys"
    assert kwargs["messages"][0]["content"][0] == {
        "type": "document",
        "source": {"type": "base64", "media_type": "application/pdf", "data": "AAAA"},
    }
    assert kwargs["tool_choice"] == {"type": "tool", "name": "record_document_classification"}
