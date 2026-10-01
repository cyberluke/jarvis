"""Wire protocol of the Voice PE WebAudio bridge (``v271-webaudio/1``).

One binary frame is one decoded microphone block:

    offset  size  field
    0       4     sequence number, unsigned 32-bit little endian
    4       4     timestamp in milliseconds since bridge start, u32 LE
    8       1     flags (see ``FRAME_FLAG_*``)
    9       3     reserved, zero

The PCM payload follows the header: PCM16LE, 16 kHz, mono, in the device's
native block size (512 samples / 1024 bytes on the retail Voice PE).

Control messages are JSON text frames and are listed in the spec
(``voice_pe_bridge.spec.md``): ``hello``, ``state``, ``discontinuity``,
``resync``, ``error``, plus the client's ``subscribe``.

Everything here is pure data plumbing: encode/decode/validate, no I/O.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

PROTOCOL_NAME = "v271-webaudio/1"
PROTOCOL_VERSION = 1

#: Bytes of the fixed frame header.
FRAME_HEADER_BYTES = 12
#: Flags of one audio frame.
FRAME_FLAG_DISCONTINUITY = 0x01  # this frame follows a gap or a reconnect
FRAME_FLAG_END_OF_STREAM = 0x02  # final frame of a microphone stream
FRAME_FLAG_RESYNC = 0x04  # sequence restarted at a reconnect anchor

#: WebSocket close codes of the bridge (application range 4000-4999).
CLOSE_UNAUTHORISED = 4001  # missing or wrong bearer token
CLOSE_ORIGIN_REJECTED = 4003  # Origin not on the allow list
CLOSE_BUSY = 4004  # another client holds the stream
CLOSE_PROTOCOL_ERROR = 4005  # malformed message or unsupported protocol

#: Named states of the satellite microphone stream.
STREAM_STATE_IDLE = "idle"
STREAM_STATE_STREAMING = "streaming"
STREAM_STATES = (STREAM_STATE_IDLE, STREAM_STATE_STREAMING)

#: Discontinuity reasons the server can announce.
DISCONTINUITY_GAP = "gap"  # a frame sequence was skipped
DISCONTINUITY_DROPPED = "dropped"  # backpressure dropped buffered frames
DISCONTINUITY_RESYNC = "resync"  # first frames after a (re)connect
DISCONTINUITY_DEVICE_CHANGE = "device_change"  # the source satellite changed
DISCONTINUITY_REASONS = (
    DISCONTINUITY_GAP,
    DISCONTINUITY_DROPPED,
    DISCONTINUITY_RESYNC,
    DISCONTINUITY_DEVICE_CHANGE,
)

#: Error codes the server can send in an ``error`` message.
ERROR_NO_DEVICE = "no_device"  # no satellite is attached/ready yet
ERROR_UNSUPPORTED = "unsupported"  # protocol/format mismatch
ERROR_BUSY = "busy"  # another client holds the stream
ERROR_INTERNAL = "internal"


@dataclass(frozen=True)
class AudioFrame:
    """One decoded wire frame, before the PCM payload."""

    seq: int
    ts_ms: int
    flags: int
    payload: bytes

    @property
    def discontinuity(self) -> bool:
        return bool(self.flags & FRAME_FLAG_DISCONTINUITY)

    @property
    def end_of_stream(self) -> bool:
        return bool(self.flags & FRAME_FLAG_END_OF_STREAM)

    @property
    def resync(self) -> bool:
        return bool(self.flags & FRAME_FLAG_RESYNC)


def pack_frame(seq: int, ts_ms: int, payload: bytes, flags: int = 0) -> bytes:
    """Encode one audio frame: 12-byte header followed by the PCM payload."""
    if not isinstance(payload, (bytes, bytearray)):
        raise TypeError("payload must be bytes")
    header = (
        int(seq).to_bytes(4, "little")
        + int(ts_ms).to_bytes(4, "little")
        + bytes((int(flags) & 0xFF, 0, 0, 0))
    )
    return header + bytes(payload)


def unpack_frame(data: bytes) -> AudioFrame:
    """Decode one audio frame; raises ``ValueError`` on a bad header."""
    if len(data) < FRAME_HEADER_BYTES:
        raise ValueError(
            f"frame too short: {len(data)} < {FRAME_HEADER_BYTES} header bytes"
        )
    seq = int.from_bytes(data[0:4], "little")
    ts_ms = int.from_bytes(data[4:8], "little")
    flags = int(data[8])
    return AudioFrame(seq=seq, ts_ms=ts_ms, flags=flags, payload=data[12:])


def encode_message(payload: Dict[str, Any]) -> str:
    """Serialise one JSON control message."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def decode_message(text: str) -> Optional[Dict[str, Any]]:
    """Parse one JSON control message; ``None`` for a non-object payload."""
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def hello_message(
    session_id: str,
    device: Optional[Dict[str, Any]],
    stream_state: str,
    ts_ms: int,
) -> Dict[str, Any]:
    """The server's first message on a successful handshake."""
    return {
        "type": "hello",
        "protocol": PROTOCOL_NAME,
        "sessionId": session_id,
        "device": device,
        "format": {
            "sampleRate": 16000,
            "channels": 1,
            "sampleType": "pcm16le",
            "endianness": "little",
            "frameSamples": 512,
            "frameDurationMs": 32,
        },
        "streamState": stream_state,
        "tsMs": int(ts_ms),
    }


def state_message(stream_state: str, session_generation: int = 0) -> Dict[str, Any]:
    """A satellite microphone stream state change."""
    return {
        "type": "state",
        "streamState": stream_state,
        "sessionGeneration": int(session_generation),
    }


def discontinuity_message(
    reason: str,
    from_seq: Optional[int],
    to_seq: Optional[int],
    dropped_frames: int = 0,
) -> Dict[str, Any]:
    """A continuity break in the frame sequence."""
    return {
        "type": "discontinuity",
        "reason": reason,
        "fromSeq": from_seq,
        "toSeq": to_seq,
        "droppedFrames": int(dropped_frames),
    }


def resync_message(from_seq: Optional[int], to_seq: Optional[int]) -> Dict[str, Any]:
    """Anchor message sent right after ``hello`` on a (re)connect."""
    return {
        "type": "resync",
        "fromSeq": from_seq,
        "toSeq": to_seq,
    }


def error_message(code: str, message: str) -> Dict[str, Any]:
    """One protocol error, followed by a close in the fatal cases."""
    return {"type": "error", "code": code, "message": message}