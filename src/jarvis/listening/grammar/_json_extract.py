"""Fail-closed JSON extraction and the constrained-output schemas.

The judges prefer the backend's native structured output (an OpenAI
``response_format`` JSON schema, or Ollama's ``format``), which constrains the
model to emit exactly one schema-valid object. When the active backend does not
support a schema, the parser here is the contract — and it fails closed:

- exactly one balanced top-level ``{...}`` object must be present;
- it must be valid JSON;
- no non-whitespace content may surround it (no prose, no second object);
- anything else is rejected, never "last object wins".

This replaces the earlier balanced-brace "last object wins" extractor.
"""

from __future__ import annotations

import json
from typing import Any, Optional


def extract_strict_json(text: str) -> Optional[Any]:
    """Return the single JSON object that *is* the model's answer, or ``None``.

    Fail-closed: surrounding prose, multiple objects, or malformed JSON all
    yield ``None`` so the caller surfaces a ``parse_error`` instead of guessing.
    A leading/trailing markdown fence is tolerated only when it wraps exactly
    one object and nothing else.
    """
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped:
        return None

    # Strip a single wrapping markdown fence if present.
    if stripped.startswith("```"):
        # Remove the opening fence line (``` or ```json).
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1:]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
        stripped = stripped.strip()

    if not stripped.startswith("{"):
        return None

    # Walk exactly one balanced object; everything after it must be whitespace.
    depth = 0
    in_string = False
    escape = False
    end = -1
    for i, ch in enumerate(stripped):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end == -1:
        return None  # unbalanced -> malformed
    if stripped[end:].strip():
        return None  # trailing content (prose or a second object) -> reject

    try:
        return json.loads(stripped[:end])
    except (ValueError, TypeError):
        return None


# --- Constrained-output schemas -------------------------------------------------

#: OpenAI ``response_format`` / Ollama ``format`` JSON schema for the Grammar
#: Judge (Pass 1). ``strict`` is honoured by servers that support it; the
#: ``fail-closed`` parser above is the fallback for those that do not.
GRAMMAR_JUDGE_JSON_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "language",
        "valid",
        "naturalnessScore",
        "grammarConfidence",
        "issues",
        "correctionType",
        "correctedText",
        "meaningChanged",
        "likelyAsrCorruption",
        "linguisticValidity",
        "recommendation",
    ],
    "properties": {
        "language": {"type": "string"},
        "valid": {"type": "boolean"},
        "naturalnessScore": {"type": "number"},
        "grammarConfidence": {"type": "number"},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["span", "type", "severity", "explanation"],
                "properties": {
                    "span": {"type": "string"},
                    "type": {
                        "type": "string",
                        "enum": [
                            "grammar", "agreement", "morphology", "syntax",
                            "word_order", "semantic_anomaly",
                            "invalid_construction", "likely_asr_corruption",
                            "other",
                        ],
                    },
                    "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                    "explanation": {"type": "string"},
                },
            },
        },
        "correctionType": {
            "type": "string",
            "enum": ["none", "surface", "semantic", "uncertain"],
        },
        "correctedText": {"type": ["string", "null"]},
        "meaningChanged": {"type": "boolean"},
        "likelyAsrCorruption": {"type": "boolean"},
        "linguisticValidity": {
            "type": "string",
            "enum": [
                "valid", "valid_but_unusual", "malformed", "nonsense",
                "likely_asr_noise",
            ],
        },
        "recommendation": {
            "type": "string",
            "enum": [
                "accept_original", "accept_surface_correction",
                "run_asr_recovery", "redecode", "ask_user",
            ],
        },
    },
}

#: JSON schema for the ASR Recovery Judge (Pass 2).
ASR_RECOVERY_JSON_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "language", "recoverable", "candidates", "bestCandidate",
        "confidence", "recommendation",
    ],
    "properties": {
        "language": {"type": "string"},
        "recoverable": {"type": "boolean"},
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "text", "confidence", "phoneticSimilarity",
                    "semanticDistance", "changedSpans",
                ],
                "properties": {
                    "text": {"type": "string"},
                    "confidence": {"type": "number"},
                    "phoneticSimilarity": {"type": "number"},
                    "semanticDistance": {"type": "number"},
                    "changedSpans": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["from", "to"],
                            "properties": {
                                "from": {"type": "string"},
                                "to": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
        "bestCandidate": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
        "recommendation": {
            "type": "string",
            "enum": ["accept_recovery", "redecode", "ask_user"],
        },
    },
}


def grammar_response_format() -> dict:
    """OpenAI ``response_format`` envelope for Pass 1."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "grammar_judge_result",
            "strict": True,
            "schema": GRAMMAR_JUDGE_JSON_SCHEMA,
        },
    }


def recovery_response_format() -> dict:
    """OpenAI ``response_format`` envelope for Pass 2."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "asr_recovery_result",
            "strict": True,
            "schema": ASR_RECOVERY_JSON_SCHEMA,
        },
    }
