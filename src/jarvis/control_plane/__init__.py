"""Toastovač desktop control plane (v271-local/1).

See ``control_plane.spec.md`` for the protocol contract and scope.
"""

from .protocol import (
    ErrorCode,
    PROTOCOL_VERSION,
    ProtocolError,
    make_envelope,
    make_error,
    make_result,
    parse_envelope,
)
from .service import ControlPlane

__all__ = [
    "ControlPlane",
    "ErrorCode",
    "PROTOCOL_VERSION",
    "ProtocolError",
    "make_envelope",
    "make_error",
    "make_result",
    "parse_envelope",
]