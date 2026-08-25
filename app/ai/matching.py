"""
Recon's core AI layer: document extraction and invoice matching, both powered
by a schema-constrained tool/function call (never free-text JSON parsing —
the model is required to call a schema-constrained tool, so output shape is
guaranteed).

Two providers are supported, switched by the explicit `AI_PROVIDER` setting
(same philosophy as `STORAGE_BACKEND`'s explicit switch elsewhere in this
codebase — never inferred from which API key happens to be set):

    "gemini"    — Google's Gemini API via the Interactions API
                  (client.interactions.create), forcing a specific named
                  function call via generation_config.tool_choice. Default,
                  since Gemini has a genuinely free tier suitable for an
                  MVP's volume.
    "anthropic" — Claude via the Messages API's forced tool use
                  (tool_choice={"type": "tool", "name": ...}). Kept as a
                  drop-in fallback: flip AI_PROVIDER back to "anthropic" (and
                  set ANTHROPIC_API_KEY) with no code changes if Gemini's
                  free-tier limits or its free-tier data-usage terms stop
                  being the right tradeoff — see DEPLOYMENT.md.

Both providers are driven through the *same* tool/function schemas and the
*same* system prompts defined below — only the thin plumbing (request
shape, retry/error handling, response parsing) differs per provider, in
_build_request / _call_with_retries / _extract_tool_result. Every business
rule and every prompt is provider-neutral by construction.

Two entry points:

    extract_document_data(file_bytes, media_type, doc_type) -> dict
        Reads a single source document (rate confirmation, invoice, or POD —
        PDF or image, native or scanned) and returns its structured fields.

    match_invoice(rate_confirmation, invoice, pod) -> dict
        Takes the three extracted dicts and returns a full audit decision:
        overall status, per-line-item decisions, totals/variance, confidence,
        and a recommended next action.

Design notes:
    - The rate confirmation is always treated as the source of truth; the
      business rules below are enforced via an explicit system prompt, not
      left to the model's judgment.
    - The model computes the qualitative, judgment-heavy parts (what a term
      like "all-in" implies, whether an accessorial is authorized, how to
      read POD timestamps). This module then deterministically recomputes
      the totals and the overall status from the model's own itemized
      output, and applies a couple of hard guardrails (e.g. detention
      without a POD is always needs_info) regardless of what the model
      concluded. This keeps the numbers reproducible and auditable even if
      the model's own arithmetic or top-level status field drifts from its
      itemized reasoning.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

import anthropic
from google import genai
from google.genai import errors as genai_errors

from app.core.config import settings
from app.models.models import DocumentType

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

class ReconAIError(Exception):
    """Base class for errors raised by the AI extraction/matching layer."""


class ExtractionError(ReconAIError):
    """Raised when the AI provider fails to extract structured data from a document."""


class MatchingError(ReconAIError):
    """Raised when the AI provider fails to produce a matching decision, or the inputs given to it are unusable."""


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

_PROVIDER_ANTHROPIC = "anthropic"
_PROVIDER_GEMINI = "gemini"

SUPPORTED_MEDIA_TYPES = {"application/pdf", "image/png", "image/jpeg", "image/webp"}
MAX_FILE_SIZE_BYTES = 32 * 1024 * 1024  # conservative inline-upload ceiling, comfortable for both providers

_MAX_RETRIES = 3
_RETRY_BASE_DELAY_SECONDS = 1.5
_MAX_TOKENS = 4096

_TWO_PLACES = Decimal("0.01")

# HTTP status codes worth retrying for Gemini's google.genai.errors.APIError
# (rate limit, and the usual transient-5xx family). Anthropic's retryable
# set is expressed as exception types instead — see _is_retryable below.
_GEMINI_RETRYABLE_CODES = {429, 500, 502, 503, 529}


def _anthropic_client() -> anthropic.Anthropic:
    # A fresh client per call is cheap (no network I/O at construction time)
    # and avoids any cross-request state; swap for a module-level singleton
    # if profiling ever shows construction cost matters.
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def _gemini_client() -> genai.Client:
    return genai.Client(api_key=settings.gemini_api_key)


def _round_money(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(Decimal(str(value)).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _is_retryable(exc: Exception) -> bool:
    """Transient failures (rate limits, connection drops, 5xx) are worth a
    backoff-and-retry; anything else (bad request, auth failure, an
    unrecognized model name) would just fail the same way three times
    slower, so those are raised immediately by the caller instead."""
    if isinstance(exc, (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError)):
        return True
    if isinstance(exc, genai_errors.APIError):
        return exc.code in _GEMINI_RETRYABLE_CODES
    return False


def _call_with_retries(**kwargs: Any):
    """
    Calls the configured provider's API with a small exponential-backoff
    retry loop for transient failures. `kwargs` must already be shaped for
    whichever provider is active — see _build_request, the one place that
    builds them correctly for both. Tests monkeypatch this whole function
    to inject a canned response, bypassing the real API entirely.
    """
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            if settings.ai_provider == _PROVIDER_ANTHROPIC:
                return _anthropic_client().messages.create(**kwargs)
            return _gemini_client().interactions.create(**kwargs)
        except Exception as exc:
            if not _is_retryable(exc):
                raise
            last_exc = exc
            if attempt == _MAX_RETRIES:
                break
            delay = _RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
            logger.warning(
                "%s API call failed (attempt %s/%s): %s — retrying in %.1fs",
                settings.ai_provider, attempt, _MAX_RETRIES, exc, delay,
            )
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def _build_request(*, system_prompt: str, tool: dict[str, Any], blocks: list[dict[str, Any]], max_tokens: int) -> dict[str, Any]:
    """
    Builds the provider-specific kwargs for _call_with_retries from a single
    provider-neutral description of the call: a system prompt, one forced
    tool (using this module's existing {"name", "description", "input_schema"}
    shape), and an ordered list of content blocks, each either
    {"kind": "document"|"image", "media_type": str, "data_b64": str} or
    {"kind": "text", "text": str}.
    """
    if settings.ai_provider == _PROVIDER_ANTHROPIC:
        content = [
            {"type": "text", "text": b["text"]} if b["kind"] == "text"
            else {"type": b["kind"], "source": {"type": "base64", "media_type": b["media_type"], "data": b["data_b64"]}}
            for b in blocks
        ]
        return {
            "model": settings.anthropic_model,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "tools": [tool],
            "tool_choice": {"type": "tool", "name": tool["name"]},
            "messages": [{"role": "user", "content": content}],
        }

    # Gemini's Interactions API: content blocks use "data"/"mime_type"
    # directly (no nested "source" wrapper), and a tool is declared as
    # {"type": "function", "name", "description", "parameters"} rather than
    # Anthropic's {"name", "description", "input_schema"} — same JSON Schema
    # underneath, different wrapper key.
    content = [
        {"type": "text", "text": b["text"]} if b["kind"] == "text"
        else {"type": b["kind"], "data": b["data_b64"], "mime_type": b["media_type"]}
        for b in blocks
    ]
    return {
        "model": settings.gemini_model,
        "system_instruction": system_prompt,
        "input": content,
        "tools": [{"type": "function", "name": tool["name"], "description": tool["description"], "parameters": tool["input_schema"]}],
        "generation_config": {
            # A single allowed tool + mode "any" forces exactly that tool to
            # be called, the same guarantee Anthropic's named tool_choice
            # gives us — there's nothing else in the list it could pick.
            "tool_choice": {"allowed_tools": {"mode": "any", "tools": [tool["name"]]}},
            "max_output_tokens": max_tokens,
        },
    }


def _first_tool_input(message: Any, tool_name: str) -> dict[str, Any] | None:
    """Anthropic response parsing: pulls the `.input` dict off the first tool_use block matching tool_name."""
    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == tool_name:
            return block.input
    return None


def _first_function_call_arguments(interaction: Any, tool_name: str) -> dict[str, Any] | None:
    """Gemini response parsing: pulls the `.arguments` dict off the first function_call step matching tool_name."""
    for step in getattr(interaction, "steps", None) or []:
        if getattr(step, "type", None) == "function_call" and getattr(step, "name", None) == tool_name:
            return dict(step.arguments) if step.arguments is not None else {}
    return None


def _extract_tool_result(response: Any, tool_name: str) -> dict[str, Any] | None:
    if settings.ai_provider == _PROVIDER_ANTHROPIC:
        return _first_tool_input(response, tool_name)
    return _first_function_call_arguments(response, tool_name)


def _diagnostic_status(response: Any) -> str:
    """Anthropic's Message has .stop_reason; Gemini's Interaction has .status — surface whichever applies, for error messages."""
    return str(getattr(response, "stop_reason", None) or getattr(response, "status", None) or "unknown")


# --------------------------------------------------------------------------
# Extraction: tool schemas
# --------------------------------------------------------------------------

_RATE_CONFIRMATION_TOOL = {
    "name": "record_rate_confirmation_data",
    "description": "Record the structured data extracted from a carrier rate confirmation document.",
    "input_schema": {
        "type": "object",
        "properties": {
            "load_number": {"type": ["string", "null"]},
            "carrier_name": {"type": ["string", "null"]},
            "origin": {"type": ["string", "null"]},
            "destination": {"type": ["string", "null"]},
            "equipment_type": {"type": ["string", "null"]},
            "pickup_date": {"type": ["string", "null"], "description": "ISO 8601 date, e.g. 2026-08-20"},
            "delivery_date": {"type": ["string", "null"]},
            "linehaul_rate": {"type": ["number", "null"]},
            "fuel_surcharge_terms": {
                "type": "object",
                "description": "How fuel is billed under this agreement.",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["all_in", "flat", "percentage", "per_mile", "none", "unknown"],
                        "description": (
                            "'all_in' means fuel is baked into linehaul_rate with no separate "
                            "line ever expected. 'unknown' means the document doesn't say."
                        ),
                    },
                    "value": {"type": ["number", "null"], "description": "The flat $, %, or $/mile value, if applicable."},
                    "notes": {"type": ["string", "null"]},
                },
                "required": ["type"],
            },
            "detention_terms": {
                "type": "object",
                "properties": {
                    "free_time_hours": {"type": ["number", "null"]},
                    "rate_per_hour": {"type": ["number", "null"]},
                    "notes": {"type": ["string", "null"]},
                },
                "required": [],
            },
            "accessorials_allowed": {
                "type": "array",
                "description": "Every accessorial explicitly authorized by this rate confirmation.",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string"},
                        "max_amount": {"type": ["number", "null"]},
                    },
                    "required": ["type"],
                },
            },
            "confidence": {"type": "number", "description": "Self-assessed extraction confidence, 0.0-1.0."},
            "warnings": {"type": "array", "items": {"type": "string"}, "description": "Anything unclear, illegible, or missing."},
        },
        "required": ["linehaul_rate", "fuel_surcharge_terms", "confidence", "warnings"],
    },
}

_INVOICE_TOOL = {
    "name": "record_invoice_data",
    "description": "Record the structured data extracted from a carrier invoice document.",
    "input_schema": {
        "type": "object",
        "properties": {
            "invoice_number": {"type": ["string", "null"]},
            "load_number": {"type": ["string", "null"]},
            "carrier_name": {"type": ["string", "null"]},
            "line_items": {
                "type": "array",
                "description": "Every billed line on the invoice, itemized — do not collapse multiple lines into one.",
                "items": {
                    "type": "object",
                    "properties": {
                        "line_type": {
                            "type": "string",
                            "enum": ["linehaul", "fuel_surcharge", "detention", "accessorial", "other"],
                        },
                        "description": {"type": ["string", "null"], "description": "The invoice's own label for this line."},
                        "amount": {"type": "number"},
                    },
                    "required": ["line_type", "amount"],
                },
            },
            "total_amount": {"type": ["number", "null"]},
            "confidence": {"type": "number"},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["line_items", "confidence", "warnings"],
    },
}

_POD_TOOL = {
    "name": "record_pod_data",
    "description": "Record the structured data extracted from a proof-of-delivery document.",
    "input_schema": {
        "type": "object",
        "properties": {
            "load_number": {"type": ["string", "null"]},
            "carrier_name": {"type": ["string", "null"]},
            "delivery_confirmed": {"type": "boolean"},
            "arrival_time": {"type": ["string", "null"], "description": "ISO 8601 datetime, if stated."},
            "departure_time": {"type": ["string", "null"]},
            "check_in_time": {"type": ["string", "null"], "description": "Use if the POD labels times as check-in/out rather than arrival/departure."},
            "check_out_time": {"type": ["string", "null"]},
            "signed": {"type": "boolean"},
            "notes": {"type": ["string", "null"]},
            "confidence": {"type": "number"},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["delivery_confirmed", "signed", "confidence", "warnings"],
    },
}

_EXTRACTION_TOOLS: dict[DocumentType, dict[str, Any]] = {
    DocumentType.RATE_CONFIRMATION: _RATE_CONFIRMATION_TOOL,
    DocumentType.INVOICE: _INVOICE_TOOL,
    DocumentType.POD: _POD_TOOL,
}

_EXTRACTION_SYSTEM_PROMPTS: dict[DocumentType, str] = {
    DocumentType.RATE_CONFIRMATION: (
        "You are Recon's document extraction engine for a freight brokerage. "
        "You will be shown one carrier rate confirmation (a PDF or an image, "
        "possibly a scan or phone photo). Extract exactly the fields in the "
        "record_rate_confirmation_data tool.\n\n"
        "Rules:\n"
        "- Read every page before answering.\n"
        "- Pay special attention to whether fuel is described as included in "
        "the linehaul rate ('all-in', 'all inclusive', no separate FSC line) "
        "versus billed separately (a flat $, a %, or a $/mile figure) — this "
        "distinction drives downstream auditing and must not be guessed.\n"
        "- If a field is not clearly stated, use null and add a one-line note "
        "to `warnings` explaining what's missing. Never invent a number that "
        "is not printed on the document.\n"
        "- Set `confidence` conservatively: a clean, fully legible, unambiguous "
        "document warrants 0.9+; anything hand-annotated, low-resolution, or "
        "ambiguous should score lower."
    ),
    DocumentType.INVOICE: (
        "You are Recon's document extraction engine for a freight brokerage. "
        "You will be shown one carrier invoice (a PDF or an image). Extract "
        "exactly the fields in the record_invoice_data tool.\n\n"
        "Rules:\n"
        "- List EVERY billed line item separately in `line_items` — do not "
        "sum or collapse lines, even if several share a line_type.\n"
        "- Classify each line's `line_type` using the invoice's own wording as "
        "your guide (e.g. 'FSC', 'fuel', 'fuel surcharge' -> fuel_surcharge; "
        "'lumper', 'layover', 'TONU' -> accessorial; 'detention', 'wait time' "
        "-> detention). If a line genuinely doesn't fit any category, use "
        "'other' rather than forcing it.\n"
        "- If a field is unclear or illegible, use null and note it in "
        "`warnings` rather than guessing."
    ),
    DocumentType.POD: (
        "You are Recon's document extraction engine for a freight brokerage. "
        "You will be shown one proof-of-delivery document (a PDF or an image, "
        "often a scan or phone photo of a signed paper POD). Extract exactly "
        "the fields in the record_pod_data tool.\n\n"
        "Rules:\n"
        "- Timestamps are the most important field on this document type — "
        "look carefully for arrival/departure or check-in/check-out times, "
        "including handwritten annotations. Use ISO 8601 (e.g. "
        "'2026-08-20T14:30:00') when a date is determinable; if only a time "
        "is legible with no date, note that in `warnings` rather than "
        "fabricating a date.\n"
        "- `delivery_confirmed` should be true only if there is clear evidence "
        "of delivery (a signature, a stamp, an explicit delivered notation).\n"
        "- If a field is unclear or illegible, use null/false as appropriate "
        "and note it in `warnings` rather than guessing."
    ),
}


# --------------------------------------------------------------------------
# 1. Extraction
# --------------------------------------------------------------------------

def extract_document_data(
    file_bytes: bytes,
    media_type: str,
    doc_type: DocumentType,
) -> dict[str, Any]:
    """
    Extract structured data from a single source document using the
    configured AI provider (see AI_PROVIDER).

    Args:
        file_bytes: raw bytes of the uploaded or emailed file.
        media_type: one of SUPPORTED_MEDIA_TYPES. Scanned/photographed
            documents go through the same path as native PDFs/images —
            the model reads the page content directly, no separate OCR step.
        doc_type: which of the three document types this is; selects the
            extraction schema and system prompt.

    Returns:
        A dict matching the schema for `doc_type` (see the _*_TOOL
        definitions above). Always includes "confidence" (float) and
        "warnings" (list[str]), defaulted if the model omits them.

    Raises:
        ValueError: unsupported media_type, or an empty/oversized file.
        ExtractionError: the API call failed, or returned no usable tool
            call (e.g. it refused, or hit an unexpected stop reason/status).
    """
    if not file_bytes:
        raise ValueError("file_bytes is empty — nothing to extract from.")
    if media_type not in SUPPORTED_MEDIA_TYPES:
        raise ValueError(f"Unsupported media_type {media_type!r}; expected one of {sorted(SUPPORTED_MEDIA_TYPES)}")
    if len(file_bytes) > MAX_FILE_SIZE_BYTES:
        raise ValueError(f"File is {len(file_bytes)} bytes, exceeds the {MAX_FILE_SIZE_BYTES}-byte limit.")

    tool = _EXTRACTION_TOOLS[doc_type]
    system_prompt = _EXTRACTION_SYSTEM_PROMPTS[doc_type]
    encoded = base64.standard_b64encode(file_bytes).decode("ascii")

    # PDFs use the "document" content block; everything else (png/jpeg/webp) uses "image".
    block_kind = "document" if media_type == "application/pdf" else "image"
    blocks = [
        {"kind": block_kind, "media_type": media_type, "data_b64": encoded},
        {
            "kind": "text",
            "text": f"Extract this document's data using the {tool['name']} tool. Read every page before answering.",
        },
    ]
    kwargs = _build_request(system_prompt=system_prompt, tool=tool, blocks=blocks, max_tokens=_MAX_TOKENS)

    try:
        response = _call_with_retries(**kwargs)
    except (anthropic.APIError, genai_errors.APIError) as exc:
        raise ExtractionError(f"{settings.ai_provider} extraction call failed for {doc_type.value}: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - normalize anything unexpected into our error type
        raise ExtractionError(f"Unexpected error extracting {doc_type.value}: {exc}") from exc

    result = _extract_tool_result(response, tool["name"])
    if result is None:
        raise ExtractionError(
            f"{settings.ai_provider} returned no structured data for {doc_type.value} "
            f"(status={_diagnostic_status(response)})."
        )

    result.setdefault("confidence", 0.0)
    result.setdefault("warnings", [])
    return result


# --------------------------------------------------------------------------
# 1b. Classification (for inbound email attachments, which arrive with no
# doc_type — unlike a manual upload, where the uploader picks it explicitly)
# --------------------------------------------------------------------------

_CLASSIFY_TOOL = {
    "name": "record_document_classification",
    "description": "Record which of the three document types this file is.",
    "input_schema": {
        "type": "object",
        "properties": {
            "document_type": {
                "type": "string",
                "enum": ["rate_confirmation", "invoice", "pod"],
                "description": (
                    "rate_confirmation: a carrier rate agreement / booking confirmation, "
                    "issued before a load moves. invoice: a carrier's bill for payment, "
                    "issued after a load moves. pod: a proof-of-delivery / delivery "
                    "receipt, typically short, often with a signature or delivery stamp."
                ),
            },
            "confidence": {"type": "number", "description": "0.0-1.0."},
            "reason": {"type": "string", "description": "One short sentence citing what on the document indicated this type."},
        },
        "required": ["document_type", "confidence", "reason"],
    },
}

_CLASSIFY_SYSTEM_PROMPT = (
    "You are Recon's document classification engine for a freight brokerage. "
    "You will be shown one document (a PDF or image) that arrived as an email "
    "attachment with no label on what type it is. Decide whether it is a "
    "rate_confirmation, an invoice, or a pod — see the record_document_classification "
    "tool's field descriptions for what distinguishes each. If the document is "
    "clearly none of these (a packing slip, an unrelated attachment, spam), still "
    "pick the closest of the three types but set confidence low and explain why in "
    "`reason` — Recon routes low-confidence classifications to a human either way, "
    "it never silently drops a document."
)


def classify_document_type(file_bytes: bytes, media_type: str) -> tuple[DocumentType, float, str]:
    """
    Identifies which of the three document types an unlabeled file is, using
    the same forced-tool-use pattern as extract_document_data for a
    guaranteed, schema-constrained answer.

    Returns:
        (doc_type, confidence, reason) — reason is the model's one-line
        justification, useful to log/audit even though it isn't persisted
        anywhere structured.

    Raises:
        ValueError: unsupported media_type, or an empty/oversized file.
        ExtractionError: the API call failed, returned no tool call, or
            returned a document_type outside the three known enum values.
    """
    if not file_bytes:
        raise ValueError("file_bytes is empty — nothing to classify.")
    if media_type not in SUPPORTED_MEDIA_TYPES:
        raise ValueError(f"Unsupported media_type {media_type!r}; expected one of {sorted(SUPPORTED_MEDIA_TYPES)}")
    if len(file_bytes) > MAX_FILE_SIZE_BYTES:
        raise ValueError(f"File is {len(file_bytes)} bytes, exceeds the {MAX_FILE_SIZE_BYTES}-byte limit.")

    encoded = base64.standard_b64encode(file_bytes).decode("ascii")
    block_kind = "document" if media_type == "application/pdf" else "image"
    blocks = [
        {"kind": block_kind, "media_type": media_type, "data_b64": encoded},
        {"kind": "text", "text": "Classify this document using the record_document_classification tool."},
    ]
    kwargs = _build_request(system_prompt=_CLASSIFY_SYSTEM_PROMPT, tool=_CLASSIFY_TOOL, blocks=blocks, max_tokens=512)

    try:
        response = _call_with_retries(**kwargs)
    except (anthropic.APIError, genai_errors.APIError) as exc:
        raise ExtractionError(f"{settings.ai_provider} classification call failed: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - normalize anything unexpected into our error type
        raise ExtractionError(f"Unexpected error classifying document: {exc}") from exc

    result = _extract_tool_result(response, _CLASSIFY_TOOL["name"])
    if result is None:
        raise ExtractionError(f"{settings.ai_provider} returned no classification (status={_diagnostic_status(response)}).")

    raw_type = result.get("document_type")
    try:
        doc_type = DocumentType(raw_type)
    except ValueError:
        raise ExtractionError(f"{settings.ai_provider} returned an unrecognized document_type: {raw_type!r}") from None

    confidence = float(result.get("confidence") or 0.0)
    reason = str(result.get("reason") or "")
    return doc_type, confidence, reason


# --------------------------------------------------------------------------
# 2. Matching
# --------------------------------------------------------------------------

_MATCH_TOOL = {
    "name": "record_match_decision",
    "description": "Record the line-by-line matching decision between a carrier invoice, its rate confirmation, and its POD.",
    "input_schema": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["clean", "discrepancy", "needs_info"],
                "description": "Your best single-word summary. clean only if every line item is clean.",
            },
            "summary": {
                "type": "string",
                "description": "One or two plain-language sentences a reviewer can act on without opening the source PDFs.",
            },
            "line_items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "line_type": {
                            "type": "string",
                            "enum": ["linehaul", "fuel_surcharge", "detention", "accessorial", "other"],
                        },
                        "description": {"type": ["string", "null"]},
                        "billed_amount": {"type": "number"},
                        "expected_amount": {
                            "type": ["number", "null"],
                            "description": "What this line should be per the rate confirmation (and, for detention, the POD). Null only if truly undeterminable.",
                        },
                        "decision": {"type": "string", "enum": ["clean", "discrepancy", "needs_info"]},
                        "reason": {
                            "type": "string",
                            "description": "One specific, numeric sentence — e.g. 'Billed $310 fuel surcharge but rate confirmation is all-in with no separate FSC.'",
                        },
                    },
                    "required": ["line_type", "billed_amount", "decision", "reason"],
                },
            },
            "confidence": {"type": "number", "description": "Overall self-assessed confidence, 0.0-1.0."},
            "recommended_action": {
                "type": "string",
                "description": "The single next step, e.g. 'Approve for payment.', 'Route to reviewer: fuel surcharge overbilled by $310.', 'Request POD from carrier before deciding detention.'",
            },
        },
        "required": ["status", "summary", "line_items", "confidence", "recommended_action"],
    },
}

_MATCH_SYSTEM_PROMPT = """You are Recon's invoice-auditing engine for a freight brokerage. You will be given three JSON objects: rate_confirmation, invoice, and pod (pod may be null if none has been received yet). Decide, line item by line item, whether each billed line on the invoice is clean, a discrepancy, or needs_info, and explain why in plain language a non-technical accounts-payable reviewer can act on immediately without opening the source documents.

Apply exactly these rules, and only these rules:

1. SOURCE OF TRUTH: rate_confirmation is authoritative for what was agreed. The invoice is the thing being audited — never treat its own labels, characterizations, or totals as evidence of what was agreed.

2. LINEHAUL: the billed linehaul line must equal rate_confirmation.linehaul_rate, within $1.00 to absorb rounding. Any other amount — over OR under — is a discrepancy.

3. FUEL SURCHARGE:
   - If rate_confirmation.fuel_surcharge_terms.type is "all_in", fuel is already included in the linehaul rate. Any separate fuel_surcharge line on the invoice is a discrepancy with expected_amount 0.
   - If type is "flat", "percentage", or "per_mile", compute the expected amount from fuel_surcharge_terms.value and the information available. Within $1.00 or 2% of billed (whichever is larger) is clean; otherwise discrepancy.
   - If type is "unknown", or the value needed to compute an expected amount is missing, the line is needs_info — do not guess a number.

4. DETENTION — the strictest rule:
   - If pod is null, or contains no usable arrival/departure or check-in/check-out timestamps, ANY detention line item MUST be needs_info with expected_amount null. Never clean, never discrepancy, no matter what the billed amount is — you cannot verify time on site without the POD.
   - If pod does have usable timestamps: compute actual hours on site, subtract rate_confirmation.detention_terms.free_time_hours (if free_time_hours itself is missing, the line is needs_info — do not assume zero free time), multiply any positive remainder by detention_terms.rate_per_hour to get the expected amount, and compare to billed within $1.00.

5. ACCESSORIALS: any accessorial line item whose type is not present in rate_confirmation.accessorials_allowed is a discrepancy (unauthorized accessorial), expected_amount 0. An allowed accessorial billed above its max_amount (when set) is also a discrepancy, with expected_amount capped at max_amount.

6. Any line item you cannot map to rules 2-5 with confidence: needs_info, never clean by default.

7. Never fabricate a number not derivable from the inputs given. If you cannot compute expected_amount, set it to null and use needs_info.

8. Be specific and numeric in every `reason` field. "Fuel surcharge billed at $310 but rate confirmation specifies an all-in rate with no separate fuel line" is correct; "fuel surcharge issue" is not acceptable.

Your top-level `status` should be your best single-word read of the whole invoice (clean only if every line is clean) — the caller will also recompute this mechanically from your line items as a safety check, so focus your effort on getting each line item right."""


def match_invoice(
    rate_confirmation: dict[str, Any],
    invoice: dict[str, Any],
    pod: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Match a carrier invoice against its rate confirmation (and POD, if
    available) and produce a structured audit decision.

    Args:
        rate_confirmation: output of extract_document_data(..., DocumentType.RATE_CONFIRMATION).
        invoice: output of extract_document_data(..., DocumentType.INVOICE).
        pod: output of extract_document_data(..., DocumentType.POD), or None
            if no POD has been received yet for this load.

    Returns:
        {
            "status": "clean" | "discrepancy" | "needs_info",
            "summary": str,
            "total_rate_con": float,
            "total_invoiced": float,
            "variance": float,               # total_invoiced - total_rate_con
            "line_items": [
                {"line_type", "description", "billed_amount", "expected_amount", "decision", "reason"},
                ...
            ],
            "confidence": float,             # 0.0-1.0
            "recommended_action": str,
        }

    Raises:
        MatchingError: the invoice has no usable line items, or the API
            call failed / returned no usable decision.
    """
    invoice_line_items = invoice.get("line_items") or []
    if not invoice_line_items:
        raise MatchingError("Invoice has no line_items to match — nothing to audit.")
    if rate_confirmation.get("linehaul_rate") in (None, ""):
        logger.warning("Rate confirmation is missing linehaul_rate — matching will lean heavily on needs_info.")

    payload = {"rate_confirmation": rate_confirmation, "invoice": invoice, "pod": pod}
    blocks = [
        {
            "kind": "text",
            "text": (
                "Here are the three extracted documents for one load. Apply the "
                f"rules in your system prompt and record your decision with the "
                f"{_MATCH_TOOL['name']} tool.\n\n{json.dumps(payload, indent=2, default=str)}"
            ),
        }
    ]
    kwargs = _build_request(system_prompt=_MATCH_SYSTEM_PROMPT, tool=_MATCH_TOOL, blocks=blocks, max_tokens=_MAX_TOKENS)

    try:
        response = _call_with_retries(**kwargs)
    except (anthropic.APIError, genai_errors.APIError) as exc:
        raise MatchingError(f"{settings.ai_provider} matching call failed: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise MatchingError(f"Unexpected error during matching: {exc}") from exc

    decision = _extract_tool_result(response, _MATCH_TOOL["name"])
    if decision is None:
        raise MatchingError(f"{settings.ai_provider} returned no matching decision (status={_diagnostic_status(response)}).")

    return _finalize_decision(decision, pod_present=pod is not None)


def _finalize_decision(decision: dict[str, Any], *, pod_present: bool) -> dict[str, Any]:
    """
    Applies deterministic guardrails on top of the model's raw decision:
      - forces any detention line item to needs_info when no POD was supplied,
        regardless of what the model decided (belt-and-suspenders on rule 4);
      - recomputes totals and the overall status mechanically from the line
        items, rather than trusting the model's own aggregate fields.
    This keeps the numbers and the top-level status reproducible even if the
    model's own arithmetic or status field drifts from its itemized reasoning.
    """
    line_items = list(decision.get("line_items") or [])

    for item in line_items:
        if item.get("line_type") == "detention" and not pod_present and item.get("decision") != "needs_info":
            item["decision"] = "needs_info"
            item["expected_amount"] = None
            item["reason"] = "No POD on file to verify detention timestamps — cannot determine what's owed."

    def _amount(value: Any) -> Decimal:
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return Decimal("0")

    total_invoiced = sum((_amount(item.get("billed_amount", 0)) for item in line_items), Decimal("0"))
    total_rate_con = sum(
        (_amount(item["expected_amount"]) for item in line_items if item.get("expected_amount") is not None),
        Decimal("0"),
    )
    variance = total_invoiced - total_rate_con

    if any(item.get("decision") == "discrepancy" for item in line_items):
        overall_status = "discrepancy"
    elif any(item.get("decision") == "needs_info" for item in line_items):
        overall_status = "needs_info"
    else:
        overall_status = "clean"

    if overall_status != decision.get("status"):
        logger.info(
            "Recomputed overall status (%s) differs from the model's reported status (%s) for this invoice — "
            "using the recomputed value, which is derived mechanically from the line items.",
            overall_status, decision.get("status"),
        )

    try:
        confidence = max(0.0, min(1.0, float(decision.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0

    return {
        "status": overall_status,
        "summary": decision.get("summary") or "",
        "total_rate_con": _round_money(total_rate_con),
        "total_invoiced": _round_money(total_invoiced),
        "variance": _round_money(variance),
        "line_items": [
            {
                "line_type": item.get("line_type", "other"),
                "description": item.get("description"),
                "billed_amount": _round_money(item.get("billed_amount")),
                "expected_amount": _round_money(item.get("expected_amount")) if item.get("expected_amount") is not None else None,
                "decision": item.get("decision", "needs_info"),
                "reason": item.get("reason") or "",
            }
            for item in line_items
        ],
        "confidence": round(confidence, 2),
        "recommended_action": decision.get("recommended_action") or "Route to reviewer.",
    }
