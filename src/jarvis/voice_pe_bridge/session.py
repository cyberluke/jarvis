"""One client session of the Voice PE WebAudio bridge.

The session owns the WebSocket connection: it announces the negotiated
format in ``hello``, answers ``subscribe``, and runs the writer task that
drains the shared tap, applies the single conversion, detects continuity
breaks and streams binary frames. Authorisation and origin checks happen in
the server before a session is created.

No audio processing happens on the Voice PE callback path; the writer task
is the only consumer of the tap's bounded queue.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Callable, Dict, Optional

from .protocol import (
    ERROR_NO_DEVICE,
    FRAME_FLAG_DISCONTINUITY,
    FRAME_FLAG_END_OF_STREAM,
    FRAME_FLAG_RESYNC,
    STREAM_STATE_IDLE,
    STREAM_STATE_STREAMING,
    decode_message,
    discontinuity_message,
    encode_message,
    error_message,
    hello_message,
    pack_frame,
    resync_message,
    state_message,
)
from .sink import samples_to_pcm16

#: One drain cycle consumes at most this many frames before re-waking.
DRAIN_LIMIT = 512
#: Sentinel for "no previous sequence" (fresh connection or stream end).
_NO_SEQ = object()


def _seq_is_next(previous: int, current: int) -> bool:
    """Whether ``current`` directly follows ``previous`` modulo 2^32."""
    return ((current - previous) & 0xFFFFFFFF) == 1


class BridgeSession:
    """One authenticated client connection on ``/voice-pe/v1``.

    The server passes itself so the session can reach the shared tap and
    wake event; ``resolve_device`` maps a client ``deviceId`` to a device
    snapshot and ``device_info`` renders one for the hello/state messages.
    """

    def __init__(
        self,
        ws,
        server: Any,
        resolve_device: Callable[[str], Optional[Dict[str, Any]]],
        device_info: Callable[[Optional[str]], Optional[Dict[str, Any]]],
    ) -> None:
        self._ws = ws
        self._server = server
        self._resolve_device = resolve_device
        self._device_info = device_info
        self.session_id = uuid.uuid4().hex
        self._tap = server.tap
        self._wake = server.wake
        self._writer: Optional[asyncio.Task] = None
        state, generation, device_id = self._tap.state()
        self._sent_state: Optional[str] = state
        self._sent_generation: Optional[int] = generation
        self._sent_device: Optional[str] = device_id
        self._last_seq: Any = _NO_SEQ
        self._drop_baseline = 0
        self._first_frame = True
        self._closed = False
        self._metrics: Dict[str, Any] = {
            "session_id": self.session_id,
            "frames_sent": 0,
            "bytes_sent": 0,
            "messages_sent": 0,
            "gaps": 0,
            "dropped_reported": 0,
            "last_activity_ms": 0,
        }

    # -- lifecycle -----------------------------------------------------------

    async def run(self) -> None:
        """Serve one authenticated connection: hello, writer, message loop."""
        self._touch()
        try:
            await self._send(hello_message(
                self.session_id,
                self._device_info(None),
                self._sent_state or STREAM_STATE_IDLE,
                self._now_ms(),
            ))
        except Exception:
            return
        self._writer = asyncio.ensure_future(self._writer_loop())
        try:
            async for message in self._ws:
                if isinstance(message, str):
                    await self._on_message(message)
                # Binary frames from the client are ignored: the stream is
                # one-way (Voice PE -> PWA).
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            self._closed = True
            if self._writer is not None:
                self._writer.cancel()
                try:
                    await self._writer
                except (asyncio.CancelledError, Exception):
                    pass

    async def _on_message(self, text: str) -> None:
        message = decode_message(text)
        if message is None:
            return
        kind = message.get("type")
        if kind == "subscribe":
            device = self._resolve_device(str(message.get("deviceId") or ""))
            if device is None:
                await self._send(error_message(
                    ERROR_NO_DEVICE, "no satellite attached"
                ))
                return
            self._sent_device = str(device.get("deviceId") or "")
            # A fresh subscribe is a stream anchor: never a silent gap.
            self._last_seq = _NO_SEQ
            state, generation, _ = self._tap.state()
            await self._send(state_message(state, generation))
        elif kind in ("ping", "status"):
            self._touch()

    # -- writer --------------------------------------------------------------

    async def _writer_loop(self) -> None:
        """Drain the tap, convert once, frame, and stream to the client."""
        try:
            while not self._closed:
                await self._wake.wait()
                self._wake.clear()
                items = self._tap.drain(DRAIN_LIMIT)
                await self._sync_state()
                for seq, ts_ms, payload in items:
                    await self._send_frame(seq, ts_ms, payload)
                if items:
                    self._touch()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _send_frame(self, seq: int, ts_ms: int, payload) -> None:
        state, _, _ = self._tap.state()
        if state != STREAM_STATE_STREAMING:
            # Late frames of a finished run are dropped, never replayed.
            return
        flags = 0
        if self._first_frame:
            self._first_frame = False
            self._last_seq = _NO_SEQ
        if self._last_seq is _NO_SEQ:
            # Connect or stream (re)start: anchor, never a silent gap.
            flags |= FRAME_FLAG_RESYNC | FRAME_FLAG_DISCONTINUITY
            await self._send(resync_message(None, seq))
        else:
            if not _seq_is_next(int(self._last_seq), int(seq)):
                self._metrics["gaps"] = int(self._metrics["gaps"]) + 1
                await self._send(discontinuity_message(
                    "gap", int(self._last_seq), int(seq), 0
                ))
                flags |= FRAME_FLAG_DISCONTINUITY
            dropped = self._tap.drop_count()
            if dropped > self._drop_baseline:
                delta = dropped - self._drop_baseline
                self._metrics["dropped_reported"] = (
                    int(self._metrics["dropped_reported"]) + delta
                )
                await self._send(discontinuity_message(
                    "dropped",
                    int(self._last_seq) if self._last_seq is not _NO_SEQ else None,
                    int(seq),
                    delta,
                ))
                flags |= FRAME_FLAG_DISCONTINUITY
                self._drop_baseline = dropped
        self._last_seq = int(seq)
        pcm = samples_to_pcm16(payload)
        self._metrics["frames_sent"] = int(self._metrics["frames_sent"]) + 1
        self._metrics["bytes_sent"] = int(self._metrics["bytes_sent"]) + len(pcm)
        await self._ws.send(pack_frame(seq, ts_ms, pcm, flags))

    async def _sync_state(self) -> None:
        """Emit ``state`` transitions and the EOS frame at stream end."""
        state, generation, device_id = self._tap.state()
        changed = (
            self._sent_state != state
            or self._sent_generation != generation
            or (device_id is not None and self._sent_device != device_id)
        )
        if not changed:
            return
        was_streaming = self._sent_state == STREAM_STATE_STREAMING
        self._sent_state = state
        self._sent_generation = generation
        if device_id is not None:
            self._sent_device = device_id
        message: Dict[str, Any] = {
            "type": "state",
            "streamState": state,
            "sessionGeneration": int(generation),
        }
        if device_id is not None:
            info = self._device_info(str(device_id))
            if info is not None:
                message["device"] = info
        await self._send(message)
        if was_streaming and state == STREAM_STATE_IDLE:
            # Clean terminator: one zero-payload EOS frame at stream end.
            seq = 0 if self._last_seq is _NO_SEQ else int(self._last_seq)
            self._last_seq = _NO_SEQ
            await self._ws.send(pack_frame(seq, self._now_ms(), b"", FRAME_FLAG_END_OF_STREAM))

    async def announce_device(self, info: Optional[Dict[str, Any]]) -> None:
        """Tell the client a satellite became available (server-driven)."""
        if self._closed or info is None:
            return
        self._sent_device = str(info.get("deviceId") or "")
        state, generation, _ = self._tap.state()
        message: Dict[str, Any] = {
            "type": "state",
            "streamState": state,
            "sessionGeneration": int(generation),
            "device": info,
        }
        await self._send(message)

    async def disconnect(self, code: int = 1001, reason: str = "closed") -> None:
        """Close the connection (used by the server on shutdown)."""
        self._closed = True
        try:
            await self._ws.close(code=code, reason=reason)
        except Exception:
            pass

    # -- helpers -------------------------------------------------------------

    async def _send(self, message: Dict[str, Any]) -> None:
        self._metrics["messages_sent"] = int(self._metrics["messages_sent"]) + 1
        self._touch()
        await self._ws.send(encode_message(message))

    def _touch(self) -> None:
        self._metrics["last_activity_ms"] = self._now_ms()

    def _now_ms(self) -> int:
        return int(time.monotonic() * 1000.0)

    def metrics(self) -> Dict[str, Any]:
        return dict(self._metrics)