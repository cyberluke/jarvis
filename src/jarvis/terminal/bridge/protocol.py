"""Versioned, length-prefixed JSON bridge protocol (bridge/2).

Wire format: 4-byte big-endian payload length + that many bytes of
UTF-8 JSON. Strict validation, bounded sizes, no fallback parsing.

Two logical message families on one pipe (§P1-2):
  telemetry  — 'terminal_context' / 'execution_record' beacons;
  actions    — 'insert_request' + 'insert_ack', each carrying
               message_id, nonce, turn_epoch, proposal_hash and a
               monotonic expiry; delivered immediately on the SAME
               connected pipe instance (no next-beacon coupling).

Unknown protocol ids and out-of-shape fields are rejected, not ignored.
"""

from __future__ import annotations

import json
from typing import Any, Optional, Tuple

PROTOCOL_ID = "toustovac-terminal-bridge/2"
# M2.4/M3.4 framing contract: the 4-byte big-endian prefix encodes the
# PAYLOAD length only. A 65,536-byte payload therefore yields a
# 65,540-byte frame. (If the whole-frame interpretation were wanted,
# MAX_PAYLOAD_BYTES would be 65_532; that is NOT the advertised contract
# — payload capacity is 65,536.) All runtime size decisions use the
# explicit payload/frame constants below; the legacy alias exists only
# for external import compatibility.
FRAME_HEADER_BYTES = 4
MAX_PAYLOAD_BYTES = 65_536
MAX_FRAME_BYTES = FRAME_HEADER_BYTES + MAX_PAYLOAD_BYTES  # 65_540
# Deprecated compatibility alias (= payload capacity). Not used for any
# runtime decision inside this package (M3.4 §5).
MAX_MESSAGE_BYTES = MAX_PAYLOAD_BYTES
MAX_FIELDS = 40

_BEACON_FIELDS = (
    "protocol", "kind", "session_id", "pid", "parent_pid",
    "process_created_ns", "shell", "shell_version", "ps_edition",
    "cwd", "hostname", "wt_session", "term_program", "timestamp_ns",
    "remote_kind", "remote_authority", "remote_host_fingerprint",
    "target_os", "transport", "shell_integration_ready", "command",
    "exit_code", "output_tail", "execution_kind", "success",
)
_ACTION_FIELDS = (
    "protocol", "kind", "message_id", "extension_instance_nonce",
    "session_id", "foreground_hwnd", "turn_epoch", "proposal_hash",
    "expires_at_monotonic", "command", "auto_execute", "timestamp_ns",
)


def encode(message: dict) -> bytes:
    text = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    data = text.encode("utf-8")
    if len(data) > MAX_PAYLOAD_BYTES:
        raise ValueError("message too large")
    return len(data).to_bytes(4, "big") + data


def decode_frame(frame: bytes) -> Tuple[Optional[dict], str]:
    """Parse one length-prefixed frame with an exact reason code (M2.4).

    Returns (obj, reason). reason is one of the frame_* counter names:
    frame_truncated / frame_oversized_advertised / frame_length_mismatch /
    frame_extra_bytes / frame_invalid_utf8 / frame_invalid_json /
    frame_accepted. JSON is parsed only after the exact payload is
    complete and UTF-8-valid; rejected bytes never reach a handler.
    """
    if len(frame) < FRAME_HEADER_BYTES:
        return None, "frame_truncated"
    n = int.from_bytes(frame[:FRAME_HEADER_BYTES], "big")
    if n <= 0:
        return None, "frame_length_mismatch"
    if n > MAX_PAYLOAD_BYTES:
        return None, "frame_oversized_advertised"
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


def decode(frame: bytes) -> Optional[dict]:
    """Parse one length-prefixed frame; None on any protocol violation."""
    obj, _reason = decode_frame(frame)
    return obj


def _str_ok(val: Any, limit: int = 256) -> bool:
    return val is None or (isinstance(val, str) and len(val) <= limit)


def _int_ok(val: Any) -> bool:
    return val is None or (isinstance(val, int) and not isinstance(val, bool))


def validate_beacon(obj: dict) -> Tuple[bool, str]:
    """Strict telemetry shape; returns (ok, reason)."""
    if not isinstance(obj, dict) or len(obj) > MAX_FIELDS:
        return False, "shape"
    if obj.get("protocol") != PROTOCOL_ID:
        return False, "protocol_mismatch"
    kind = obj.get("kind")
    if kind not in ("terminal_context", "execution_record"):
        return False, "unknown_kind"
    sid = obj.get("session_id")
    if not isinstance(sid, str) or not 8 <= len(sid) <= 64:
        return False, "bad_session_id"
    if not _int_ok(obj.get("pid")) or not _int_ok(obj.get("timestamp_ns")):
        return False, "bad_int"
    if obj.get("timestamp_ns") is None:
        return False, "missing:timestamp_ns"
    if not _str_ok(obj.get("cwd"), 2048):
        return False, "bad:cwd"
    for key in ("shell", "shell_version", "ps_edition", "hostname",
                "wt_session", "term_program", "remote_kind",
                "remote_authority", "remote_host_fingerprint",
                "target_os", "transport"):
        if not _str_ok(obj.get(key)):
            return False, f"bad:{key}"
    if "output_tail" in obj and not _str_ok(obj["output_tail"], 4096):
        return False, "bad:output_tail"
    if not _int_ok(obj.get("exit_code")):
        return False, "bad:exit_code"
    ek = obj.get("execution_kind")
    if ek is not None and ek not in ("native", "powershell", "unknown"):
        return False, "bad:execution_kind"
    ok = obj.get("success")
    if ok is not None and not isinstance(ok, bool):
        return False, "bad:success"
    return True, ""


def validate_action(obj: dict) -> Tuple[bool, str]:
    """Strict action-frame shape (both directions)."""
    if not isinstance(obj, dict) or len(obj) > MAX_FIELDS:
        return False, "shape"
    if obj.get("protocol") != PROTOCOL_ID:
        return False, "protocol_mismatch"
    if obj.get("kind") not in ("insert_request", "insert_ack"):
        return False, "unknown_kind"
    for key in ("message_id", "session_id"):
        val = obj.get(key)
        if not isinstance(val, str) or not 4 <= len(val) <= 64:
            return False, f"bad:{key}"
    if not _int_ok(obj.get("foreground_hwnd")) or \
            not _int_ok(obj.get("turn_epoch")):
        return False, "bad_int"
    if not _str_ok(obj.get("proposal_hash"), 64) or \
            not _str_ok(obj.get("extension_instance_nonce"), 64):
        return False, "bad_str"
    exp = obj.get("expires_at_monotonic")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        return False, "bad:expires_at_monotonic"
    return True, ""


def project(obj: dict, fields: tuple) -> dict:
    """Whitelist projection (drops unknown keys)."""
    return {k: obj[k] for k in fields if k in obj}
