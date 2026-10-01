"""Microphone tap that feeds the WebAudio bridge.

``VoicePEFrameTap`` attaches to a Voice PE device's ``AudioIngress`` (see
``AudioIngress.attach_sink``). Its callbacks run on the Voice PE manager
loop, so they only append to a bounded deque and bump counters; the actual
conversion (float32 lane output -> PCM16LE) and framing happen in the bridge
session writer task. No audio processing ever runs inside the network
callback.

The tap is deliberately single-consumer: the bridge serves one client at a
time and the writer task is its only drainer. When no client is connected,
frames still pass through the bounded queue and are dropped oldest-first
with a counter, so the sequence space stays honest across reconnects.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Callable, List, Optional, Tuple

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]

from .protocol import (
    STREAM_STATE_IDLE,
    STREAM_STATE_STREAMING,
)

#: Payload types the tap accepts from the ingress.
_BYTES_TYPES = (bytes, bytearray)


def samples_to_pcm16(samples: Any) -> bytes:
    """Normalise one delivered block to PCM16LE bytes.

    The ``device_enhanced`` path already delivers PCM16LE bytes and is passed
    through untouched; the host AEC lane delivers float32 frames, which are
    converted once here, at the defined boundary. This is the only conversion
    on the bridge path.
    """
    if isinstance(samples, _BYTES_TYPES):
        return bytes(samples)
    if np is not None and hasattr(samples, "dtype") and hasattr(samples, "tobytes"):
        arr = np.asarray(samples)
        if arr.dtype == np.float32:
            scaled = np.clip(arr, -1.0, 1.0)
            return (scaled * 32767.0).astype(np.int16).tobytes(order="C")
        if arr.dtype == np.int16:
            return arr.astype(np.int16).tobytes(order="C")
        raise TypeError(f"unsupported float32-lane dtype: {arr.dtype}")
    raise TypeError(f"unsupported sample payload: {type(samples).__name__}")


class VoicePEFrameTap:
    """Bounded, thread-safe tap on the delivered Voice PE microphone signal.

    Callbacks (``on_frame``, ``on_stream_start``, ``on_stream_end``) are
    invoked from the Voice PE event loop and must stay append-only. The
    writer task drains with ``drain()`` and wakes through ``notify``.
    """

    def __init__(
        self,
        max_frames: int = 200,
        notify: Optional[Callable[[], None]] = None,
    ) -> None:
        self._max_frames = max(1, int(max_frames))
        self._notify = notify
        self._lock = threading.Lock()
        self._items: deque = deque()
        self._base = time.monotonic()
        self._seq = 0
        self._dropped_frames = 0
        self._frames_in = 0
        self._frames_queued = 0
        self._last_seq: Optional[int] = None
        self._last_ts_ms: int = 0
        self._streaming = False
        self._session_generation = 0
        self._device_id: Optional[str] = None
        self._device_name: str = ""

    # -- AudioIngress sink protocol (Voice PE loop thread) ------------------

    def on_frame(self, frame) -> None:
        """Append one delivered block; drop the oldest on overflow."""
        payload = getattr(frame, "samples", None)
        if payload is None:
            return
        stream = getattr(frame, "stream", None)
        device_id = ""
        if stream is not None:
            try:
                device_id = str(stream.device_id or "")
            except (AttributeError, TypeError, ValueError):
                device_id = ""
        if not device_id:
            return  # local microphone identity; never bridged
        with self._lock:
            seq = self._seq
            self._seq = (self._seq + 1) & 0xFFFFFFFF
            ts_ms = int((time.monotonic() - self._base) * 1000.0)
            self._frames_in += 1
            if len(self._items) >= self._max_frames:
                self._items.popleft()
                self._dropped_frames += 1
            self._items.append((seq, ts_ms, payload))
            self._last_seq = seq
            self._last_ts_ms = ts_ms
            self._frames_queued = len(self._items)
        self._wake()

    def on_stream_start(self, stream) -> None:
        device_id, generation = self._stream_identity(stream)
        if not device_id:
            return
        with self._lock:
            self._streaming = True
            self._session_generation = generation
            self._device_id = device_id
        self._wake()

    def on_stream_end(self, stream) -> None:
        device_id, _ = self._stream_identity(stream)
        if not device_id:
            return
        with self._lock:
            self._streaming = False
        self._wake()

    # -- writer side ---------------------------------------------------------

    def drain(self, limit: int = 4096) -> List[Tuple[int, int, Any]]:
        """Pop up to ``limit`` queued blocks as ``(seq, ts_ms, payload)``."""
        with self._lock:
            out: list = []
            while self._items and len(out) < limit:
                out.append(self._items.popleft())
            self._frames_queued = len(self._items)
        return out

    def state(self) -> Tuple[str, int, Optional[str]]:
        """``(stream_state, session_generation, device_id)`` snapshot."""
        with self._lock:
            state = (
                STREAM_STATE_STREAMING if self._streaming else STREAM_STATE_IDLE
            )
            return state, self._session_generation, self._device_id

    def drop_count(self) -> int:
        """Total frames dropped oldest-first by the bounded queue."""
        with self._lock:
            return self._dropped_frames

    def depth(self) -> int:
        with self._lock:
            return len(self._items)

    def metrics(self) -> dict:
        """Diagnostics snapshot: counts, depth, latest seq/timestamp."""
        with self._lock:
            return {
                "frames_in": self._frames_in,
                "frames_queued": self._frames_queued,
                "dropped_frames": self._dropped_frames,
                "max_frames": self._max_frames,
                "last_seq": self._last_seq,
                "last_ts_ms": self._last_ts_ms,
                "streaming": self._streaming,
                "session_generation": self._session_generation,
                "device_id": self._device_id,
            }

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _stream_identity(stream) -> Tuple[str, int]:
        device_id = ""
        generation = 0
        if stream is None:
            return device_id, generation
        try:
            device_id = str(getattr(stream, "device_id", "") or "")
        except (AttributeError, TypeError, ValueError):
            device_id = ""
        try:
            generation = int(getattr(stream, "session_generation", 0) or 0)
        except (AttributeError, TypeError, ValueError):
            generation = 0
        return device_id, generation

    def _wake(self) -> None:
        callback = self._notify
        if callback is not None:
            try:
                callback()
            except Exception:  # pragma: no cover - defensive
                pass

    def set_notify(self, notify: Optional[Callable[[], None]]) -> None:
        """Swap the wake callback (called from the bridge loop thread)."""
        self._notify = notify