"""Structured Proofread contract parsing and the Fix All policy.

Proofread never returns a bare corrected paragraph: the model must answer
with the strict JSON shape declared in ``prompts.DEFAULT_ACTION_PROMPTS``.
This module parses that shape, validates offsets against the original text,
and applies the configured Fix All confidence policy (no magic thresholds
inside the UI code).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from .prompts import PROOFREAD_CATEGORIES


def parse_proofread(raw: str, *, source_hash: str) -> Tuple[
        Optional[Dict[str, Any]], str]:
    """Parse one model answer. Returns ``(result, failure_reason)``.

    ``result`` keys: ``source_hash``, ``issues`` (list of dicts with
    ``start_utf16``, ``end_utf16``, ``original``, ``replacement``,
    ``category``, ``rule``, ``explanation``, ``confidence``), and
    ``corrected_text``. Failure reasons: ``invalid_json``, ``bad_shape``,
    ``hash_mismatch``, ``bad_offset``, ``bad_category``, ``empty``.
    """
    if not raw or not raw.strip():
        return None, "empty"
    text = raw.strip()
    # Tolerate fenced code blocks around the JSON object.
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None, "invalid_json"
    if not isinstance(obj, dict):
        return None, "bad_shape"

    declared_hash = str(obj.get("source_hash") or "")
    if declared_hash and source_hash and declared_hash != source_hash:
        return None, "hash_mismatch"

    issues_raw = obj.get("issues")
    if not isinstance(issues_raw, list):
        return None, "bad_shape"
    issues: List[Dict[str, Any]] = []
    for item in issues_raw:
        if not isinstance(item, dict):
            return None, "bad_shape"
        try:
            start = int(item.get("start_utf16"))
            end = int(item.get("end_utf16"))
        except (TypeError, ValueError):
            return None, "bad_offset"
        if start < 0 or end < start:
            return None, "bad_offset"
        category = str(item.get("category") or "").strip()
        if category and category not in PROOFREAD_CATEGORIES:
            return None, "bad_category"
        try:
            confidence = float(item.get("confidence", 1.0))
        except (TypeError, ValueError):
            confidence = 0.0
        issues.append({
            "start_utf16": start,
            "end_utf16": end,
            "original": str(item.get("original") or ""),
            "replacement": str(item.get("replacement") or ""),
            "category": category,
            "rule": str(item.get("rule") or ""),
            "explanation": str(item.get("explanation") or ""),
            "confidence": confidence,
        })

    corrected = obj.get("corrected_text")
    return {
        "source_hash": declared_hash or source_hash,
        "issues": issues,
        "corrected_text": str(corrected) if isinstance(corrected, str) else "",
    }, ""


def apply_selected(source_text: str, issues: List[Dict[str, Any]],
                   accepted: List[int]) -> str:
    """Apply accepted issues (by index) to the source, right-to-left.

    Right-to-left keeps earlier offsets valid while editing. An issue whose
    recorded ``original`` no longer matches the source slice is skipped
    (the caller revalidates against the snapshot before applying).
    """
    ordered = sorted(
        (i for i in accepted if 0 <= i < len(issues)),
        key=lambda i: issues[i]["start_utf16"],
        reverse=True,
    )
    out = source_text
    for i in ordered:
        issue = issues[i]
        start, end = issue["start_utf16"], issue["end_utf16"]
        if start > len(out) or end > len(out):
            continue
        if out[start:end] != issue["original"]:
            continue
        out = out[:start] + issue["replacement"] + out[end:]
    return out


def fix_all_select(issues: List[Dict[str, Any]],
                   min_confidence: float) -> List[int]:
    """Indices of issues at or above the configured Fix All threshold."""
    threshold = float(min_confidence)
    return [
        i for i, issue in enumerate(issues)
        if float(issue.get("confidence", 0.0)) >= threshold
    ]
