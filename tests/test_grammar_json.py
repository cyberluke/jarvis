"""Fail-closed JSON parser tests.

The structured-output contract: exactly one schema object, no surrounding
prose, no second object, no trailing content, no malformed JSON. The old
"last balanced object wins" behaviour is explicitly removed.
"""

from jarvis.listening.grammar._json_extract import (
    extract_strict_json,
    GRAMMAR_JUDGE_JSON_SCHEMA,
    ASR_RECOVERY_JSON_SCHEMA,
    grammar_response_format,
    recovery_response_format,
)


def test_accepts_single_clean_object():
    assert extract_strict_json('{"a": 1}') == {"a": 1}


def test_accepts_object_in_single_fence():
    assert extract_strict_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_rejects_leading_prose():
    assert extract_strict_json('Here is the answer: {"a": 1}') is None


def test_rejects_trailing_prose():
    assert extract_strict_json('{"a": 1} hope this helps') is None


def test_rejects_two_objects():
    assert extract_strict_json('{"a": 1} {"b": 2}') is None


def test_rejects_malformed():
    assert extract_strict_json('{"a": 1') is None


def test_rejects_non_object():
    assert extract_strict_json('[1, 2, 3]') is None
    assert extract_strict_json('just text') is None


def test_rejects_empty():
    assert extract_strict_json('') is None
    assert extract_strict_json(None) is None


def test_schemas_are_well_formed():
    # Both schemas carry the discriminator fields the decision layer reads.
    assert "linguisticValidity" in GRAMMAR_JUDGE_JSON_SCHEMA["properties"]
    assert "likelyAsrCorruption" in GRAMMAR_JUDGE_JSON_SCHEMA["properties"]
    assert "changedSpans" in ASR_RECOVERY_JSON_SCHEMA["properties"]["candidates"]["items"]["properties"]
    # The response_format envelopes are OpenAI json_schema shaped.
    assert grammar_response_format()["json_schema"]["strict"] is True
    assert recovery_response_format()["json_schema"]["strict"] is True
