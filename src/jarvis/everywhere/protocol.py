"""Versioned, length-prefixed JSON protocol for the Everywhere plane.

Wire format matches the terminal bridge: 4-byte big-endian payload length
+ that many bytes of UTF-8 JSON. Strict validation, bounded sizes, no
fallback parsing, no silent provider/engine switching.

One duplex message family on ``\\\\.\\pipe\\toastovac-everywhere-v1``:
the native host sends ``snapshot`` / ``action`` / ``apply`` / ``cancel`` /
``ping`` frames; the broker answers with ``action_result`` / ``apply_result``
/ ``pong`` correlated by ``request_id``.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple

PROTOCOL_ID = "toustovac-everywhere/1"

PIPE_NAME = r"\\.\pipe\toastovac-everywhere-v1"

FRAME_HEADER_BYTES = 4
MAX_PAYLOAD_BYTES = 262_144
MAX_FRAME_BYTES = FRAME_HEADER_BYTES + MAX_PAYLOAD_BYTES
MAX_FIELDS = 48

#: Message kinds accepted from the native host (inbound).
INBOUND_KINDS = ("snapshot", "action", "apply", "cancel", "ping", "subtitles",
                 "coach", "ocr")
#: Message kinds emitted by the broker (outbound).
OUTBOUND_KINDS = ("action_result", "apply_result", "pong", "error",
                  "subtitles_options", "subtitles_started",
                  "subtitles_stopped", "subtitles_poll",
                  "coach_started", "coach_stopped", "coach_poll",
                  "coach_summary", "ocr_result", "ocr_page")

#: Typed failure codes (§Failure codes). One code per failure, never a
#: generic "something went wrong".
FAILURE_CODES = (
    "NO_SELECTION",
    "EMPTY_SELECTION",
    "PASSWORD_FIELD",
    "READ_ONLY_TARGET",
    "TARGET_GONE",
    "TARGET_CHANGED",
    "SELECTION_CHANGED",
    "DOCUMENT_CHANGED",
    "REMOTE_CONTEXT_CHANGED",
    "PROVIDER_UNAVAILABLE",
    "HOTKEY_CONFLICT",
    "OCR_BACKEND_UNAVAILABLE",
    "OCR_NO_TEXT",
    "INPUT_BLOCKED_BY_UIPI",
    "REPLACE_UNSUPPORTED",
    "MODEL_UNAVAILABLE",
    "MODEL_CANCELLED",
    "PIPE_DISCONNECTED",
)

#: Transaction states (§Context transactions). STALE and CANCELLED are final
#: with respect to automatic insertion.
TX_STATES = (
    "IDLE",
    "SNAPSHOT_READY",
    "ACTION_PENDING",
    "STREAMING",
    "RESULT_READY",
    "APPLYING",
    "DONE",
    "CANCELLED",
    "STALE",
)

#: Replace capabilities a target may expose (§Replace capabilities).
REPLACE_CAPABILITIES = (
    "SEMANTIC_RANGE_EDIT",
    "VALUE_PATTERN_EDIT",
    "UNICODE_INPUT_REPLACE",
    "CLIPBOARD_TRANSACTION_REPLACE",
    "COPY_ONLY",
)

#: Deterministic selection provider hierarchy (§Provider hierarchy).
SOURCE_KINDS = (
    "vscode-editor",
    "vscode-terminal",
    "powershell",
    "windows-terminal",
    "uia-text",
    "ocr-region",
)


def encode(message: Dict[str, Any]) -> bytes:
    """Serialize one message into a length-prefixed frame."""
    text = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    data = text.encode("utf-8")
    if len(data) > MAX_PAYLOAD_BYTES:
        raise ValueError("message too large")
    return len(data).to_bytes(FRAME_HEADER_BYTES, "big") + data


def decode(frame: bytes) -> Tuple[Optional[Dict[str, Any]], str]:
    """Parse one frame; returns ``(obj, reason)`` like the terminal bridge."""
    if len(frame) < FRAME_HEADER_BYTES:
        return None, "frame_truncated"
    n = int.from_bytes(frame[:FRAME_HEADER_BYTES], "big")
    if n <= 0:
        return None, "frame_length_mismatch"
    if n > MAX_PAYLOAD_BYTES:
        return None, "frame_oversized"
    total = FRAME_HEADER_BYTES + n
    if len(frame) < total:
        return None, "frame_truncated"
    if len(frame) > total:
        return None, "frame_extra_bytes"
    payload = frame[FRAME_HEADER_BYTES:total]
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return None, "frame_invalid_utf8"
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None, "frame_invalid_json"
    if not isinstance(obj, dict):
        return None, "frame_invalid_json"
    return obj, "frame_accepted"


def validate_inbound(obj: Dict[str, Any]) -> Tuple[bool, str]:
    """Strict shape check for host -> broker frames. Fail closed."""
    if not isinstance(obj, dict) or len(obj) > MAX_FIELDS:
        return False, "shape"
    if obj.get("protocol") != PROTOCOL_ID:
        return False, "protocol_mismatch"
    kind = obj.get("kind")
    if kind not in INBOUND_KINDS:
        return False, "unknown_kind"
    rid = obj.get("request_id")
    if not isinstance(rid, str) or not 4 <= len(rid) <= 64:
        return False, "bad:request_id"
    if kind == "snapshot":
        snap = obj.get("snapshot")
        if not isinstance(snap, dict):
            return False, "missing:snapshot"
        if snap.get("source_kind") not in SOURCE_KINDS:
            return False, "bad:source_kind"
        if not isinstance(snap.get("snapshot_id"), str) or not snap["snapshot_id"]:
            return False, "bad:snapshot_id"
    elif kind == "action":
        if obj.get("action") not in ACTION_IDS:
            return False, "bad:action"
        if not isinstance(obj.get("snapshot_id"), str) or not obj["snapshot_id"]:
            return False, "bad:snapshot_id"
    elif kind == "apply":
        if not isinstance(obj.get("snapshot_id"), str) or not obj["snapshot_id"]:
            return False, "bad:snapshot_id"
        if not isinstance(obj.get("request_id"), str):
            return False, "bad:request_id"
    return True, ""


#: Action ids the broker serves (§Actions).
ACTION_IDS = (
    "rewrite",
    "proofread",
    "alternatives",
    "explain",
    "translate",
    "prompt",
    # Terminal-surface extras routed through the existing composer.
    "fix_command",
    "explain_command",
    "safer_variant",
    "docker_help",
    "devops_help",
)


def make_result(request_id: str, kind: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Build one outbound result frame body (dict, not yet encoded)."""
    out = {"protocol": PROTOCOL_ID, "kind": kind, "request_id": request_id}
    out.update(payload)
    return out


def make_error(request_id: str, failure: str) -> Dict[str, Any]:
    """Outbound error with a typed failure code (§Failure codes)."""
    code = failure if failure in FAILURE_CODES else "PROVIDER_UNAVAILABLE"
    return {
        "protocol": PROTOCOL_ID,
        "kind": "error",
        "request_id": request_id,
        "failure": code,
    }
