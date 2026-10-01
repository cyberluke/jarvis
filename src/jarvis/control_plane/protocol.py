"""v271-local/1 protocol contract.

Canonical message envelope, primitive types, and canonical error codes
for the Toastovač desktop control plane. Transport-neutral: domain code
speaks in envelopes, transports (HTTP today, native messaging / relay
later) map them onto the wire.

See ``control_plane.spec.md`` for the full contract.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, Optional

PROTOCOL_VERSION = "v271-local/1"

#: Required primitives (§3 of the control-plane spec).
REQUIRED_PRIMITIVES = frozenset({
    "hello",
    "pair",
    "session.register",
    "session.unregister",
    "capabilities.get",
    "dispatch",
    "tool.call",
    "tool.result",
    "event",
})

#: Additive primitives (spec §19: future additions are additive).
ADDITIVE_PRIMITIVES = frozenset({
    "session.continuity",
})

ALL_PRIMITIVES = REQUIRED_PRIMITIVES | ADDITIVE_PRIMITIVES

#: Primitives a remote peer may send us (everything except outbound-only
#: types such as ``dispatch`` / ``tool.result`` / ``event``).
INBOUND_PRIMITIVES = frozenset({
    "hello",
    "pair",
    "session.register",
    "session.unregister",
    "session.continuity",
    "capabilities.get",
    "tool.call",
})


class ErrorCode:
    """Canonical error codes shared by the REST bindings and envelopes."""

    INVALID_REQUEST = "INVALID_REQUEST"
    PROTOCOL_UNSUPPORTED = "PROTOCOL_UNSUPPORTED"
    FORBIDDEN_ORIGIN = "FORBIDDEN_ORIGIN"
    PAIRING_REQUIRED = "PAIRING_REQUIRED"
    PAIRING_INVALID = "PAIRING_INVALID"
    SESSION_UNKNOWN = "SESSION_UNKNOWN"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    APP_UNKNOWN = "APP_UNKNOWN"
    MCP_UNAVAILABLE = "MCP_UNAVAILABLE"
    MCP_NOT_RUNNING = "MCP_NOT_RUNNING"
    TOOL_UNKNOWN = "TOOL_UNKNOWN"
    TOOL_NAMESPACE_MISMATCH = "TOOL_NAMESPACE_MISMATCH"
    V271_BROWSER_UNAVAILABLE = "V271_BROWSER_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


#: Default retryability. Transient conditions are retryable; contract
#: violations are not.
_ERROR_RETRYABLE = {
    ErrorCode.INVALID_REQUEST: False,
    ErrorCode.PROTOCOL_UNSUPPORTED: False,
    ErrorCode.FORBIDDEN_ORIGIN: False,
    ErrorCode.PAIRING_REQUIRED: False,
    ErrorCode.PAIRING_INVALID: False,
    ErrorCode.SESSION_UNKNOWN: False,
    ErrorCode.SESSION_EXPIRED: True,
    ErrorCode.APP_UNKNOWN: False,
    ErrorCode.MCP_UNAVAILABLE: True,
    ErrorCode.MCP_NOT_RUNNING: True,
    ErrorCode.TOOL_UNKNOWN: False,
    ErrorCode.TOOL_NAMESPACE_MISMATCH: False,
    ErrorCode.V271_BROWSER_UNAVAILABLE: True,
    ErrorCode.INTERNAL_ERROR: True,
}


class ProtocolError(Exception):
    """Carries a canonical error code + message for envelope responses.

    ``retryable`` defaults from ``_ERROR_RETRYABLE`` unless overridden.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: Optional[bool] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = (
            _ERROR_RETRYABLE.get(code, False) if retryable is None else retryable
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


def new_id(prefix: str = "req") -> str:
    """Fresh envelope id, e.g. ``req-3f9c...``."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def make_envelope(
    msg_type: str,
    payload: Optional[Dict[str, Any]] = None,
    *,
    msg_id: Optional[str] = None,
    session_id: Optional[str] = None,
    timestamp: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a canonical ``v271-local/1`` envelope."""
    return {
        "protocol": PROTOCOL_VERSION,
        "id": msg_id or new_id(),
        "type": msg_type,
        "timestamp": timestamp or _iso_now(),
        **({"sessionId": session_id} if session_id is not None else {}),
        "payload": payload if payload is not None else {},
    }


def make_result(
    msg_type: str,
    payload: Optional[Dict[str, Any]] = None,
    *,
    msg_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Result envelope: echoes the request id/type, ``ok: true``."""
    return {
        **make_envelope(
            msg_type,
            payload,
            msg_id=msg_id,
            session_id=session_id,
        ),
        "ok": True,
    }


def make_error(
    error: ProtocolError,
    *,
    msg_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Error envelope carrying the canonical error object."""
    return {
        **make_envelope("error", msg_id=msg_id, session_id=session_id),
        "ok": False,
        "error": error.to_dict(),
    }


def parse_envelope(raw: Any) -> Dict[str, Any]:
    """Validate an incoming message as a ``v271-local/1`` envelope.

    Raises ``ProtocolError`` (``INVALID_REQUEST`` /
    ``PROTOCOL_UNSUPPORTED``) on contract violations. Returns the
    envelope dict for further handling.
    """
    if not isinstance(raw, dict):
        raise ProtocolError(
            ErrorCode.INVALID_REQUEST, "Envelope must be a JSON object"
        )
    protocol = raw.get("protocol")
    if not isinstance(protocol, str) or not protocol:
        raise ProtocolError(
            ErrorCode.INVALID_REQUEST, "Envelope 'protocol' is missing"
        )
    if protocol != PROTOCOL_VERSION:
        raise ProtocolError(
            ErrorCode.PROTOCOL_UNSUPPORTED,
            f"Unsupported protocol {protocol!r}; expected {PROTOCOL_VERSION}",
        )
    msg_id = raw.get("id")
    if not isinstance(msg_id, str) or not msg_id:
        raise ProtocolError(
            ErrorCode.INVALID_REQUEST, "Envelope 'id' must be a non-empty string"
        )
    msg_type = raw.get("type")
    if not isinstance(msg_type, str) or msg_type not in ALL_PRIMITIVES:
        raise ProtocolError(
            ErrorCode.INVALID_REQUEST,
            f"Unknown envelope type {msg_type!r}; "
            f"supported: {', '.join(sorted(ALL_PRIMITIVES))}",
        )
    if msg_type not in INBOUND_PRIMITIVES:
        raise ProtocolError(
            ErrorCode.INVALID_REQUEST,
            f"Type {msg_type!r} is outbound-only and cannot be sent to Toastovač",
        )
    payload = raw.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ProtocolError(
            ErrorCode.INVALID_REQUEST, "Envelope 'payload' must be a JSON object"
        )
    session_id = raw.get("sessionId")
    if session_id is not None and not isinstance(session_id, str):
        raise ProtocolError(
            ErrorCode.INVALID_REQUEST, "Envelope 'sessionId' must be a string"
        )
    return {
        "protocol": protocol,
        "id": msg_id,
        "type": msg_type,
        "timestamp": raw.get("timestamp") or _iso_now(),
        **({"sessionId": session_id} if session_id is not None else {}),
        "payload": payload,
    }


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())