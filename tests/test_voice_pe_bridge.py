"""Tests for the Voice PE WebAudio bridge (v271-webaudio/1).

Behaviour-level: the wire contract (handshake, auth, origin, framing,
discontinuity, backpressure) is exercised over a real loopback WebSocket;
the tap is verified against the real ``AudioIngress`` delivery path with a
stubbed listener. No live Voice PE device and no network beyond loopback.
"""

import asyncio
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from jarvis.integrations.voice_pe import config as pe_config
from jarvis.integrations.voice_pe.models import (
    AUDIO_CHANNEL_ENHANCED,
    StreamId,
)
from jarvis.integrations.voice_pe.voice_transport import AudioIngress
from jarvis.voice_pe_bridge.protocol import (
    CLOSE_BUSY,
    CLOSE_ORIGIN_REJECTED,
    CLOSE_UNAUTHORISED,
    FRAME_FLAG_DISCONTINUITY,
    FRAME_FLAG_END_OF_STREAM,
    FRAME_FLAG_RESYNC,
    PROTOCOL_NAME,
    decode_message,
    encode_message,
    pack_frame,
    unpack_frame,
)
from jarvis.voice_pe_bridge.server import VoicePEBridgeServer, origin_allowed
from jarvis.voice_pe_bridge.session import BridgeSession
from jarvis.voice_pe_bridge.sink import VoicePEFrameTap, samples_to_pcm16

TOKEN = "test-bridge-token"
ORIGIN = "https://v271.cz"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class FakeListener:
    def __init__(self):
        self._audio_q = queue.Queue(maxsize=64)

    def pad_until_endpoint(self, source, generation):
        pass


def _ingress_config(**overrides):
    base = {
        "enabled": True,
        "prefer_api_audio": True,
        "audio_queue_ms": 300,
        "audio_channel": "enhanced",
        "dsp_mode": "device_enhanced",
    }
    base.update(overrides)
    return pe_config.from_settings(
        SimpleNamespace(**{f"voice_pe_{k}": v for k, v in base.items()})
    )


def _ingress(**overrides):
    listener = FakeListener()
    ingress = AudioIngress(listener, _ingress_config(**overrides), {})
    return ingress, listener


def _stream(gen: int = 1) -> StreamId:
    return StreamId("aa:bb:cc:dd:ee:ff", 1, gen)


def _bridge_config(**overrides):
    base = {
        "voice_pe_bridge_enabled": True,
        "voice_pe_bridge_port": 0,
        "voice_pe_bridge_token": TOKEN,
        "voice_pe_bridge_allowed_origins": [ORIGIN],
        "voice_pe_bridge_max_clients": 1,
        "voice_pe_bridge_buffer_frames": 200,
        "voice_pe_bridge_device": "",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class FakeManager:
    """Manager stand-in: one fake device with identity + ingress wiring."""

    def __init__(self, device=None, attach_ok: bool = True):
        self._device = device
        self._attach_ok = attach_ok
        self.attach_calls = 0
        self.detach_calls = 0

    def device(self, key: str = ""):
        return self._device

    def attach_audio_sink(self, sink, key: str = "") -> bool:
        self.attach_calls += 1
        if not self._attach_ok or self._device is None:
            return False
        return True

    def detach_audio_sink(self, sink, key: str = "") -> bool:
        self.detach_calls += 1
        return self._device is not None


def _fake_device(identity=None):
    return SimpleNamespace(
        identity=identity
        or {
            "node_name": "living-room-pe",
            "friendly_name": "Living Room Voice PE",
            "mac_address": "AA:BB:CC:DD:EE:FF",
        },
        state="ready",
        _host="192.168.1.50",
    )


async def _connect(uri, *, token=TOKEN, origin=ORIGIN, headers=None):
    from websockets.asyncio.client import connect

    kwargs = {}
    if origin is not None:
        kwargs["origin"] = origin
    headers = dict(headers or {})
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if headers:
        kwargs["additional_headers"] = headers
    return await connect(uri, **kwargs)


async def _recv_json(ws, timeout: float = 2.0):
    text = await asyncio.wait_for(ws.recv(), timeout)
    assert isinstance(text, str), f"expected a JSON message, got {type(text)}"
    return decode_message(text)


async def _recv_frame(ws, timeout: float = 2.0):
    data = await asyncio.wait_for(ws.recv(), timeout)
    assert isinstance(data, (bytes, bytearray)), f"expected a binary frame, got {type(data)}"
    return unpack_frame(bytes(data))


class RecordingWS:
    """Captures everything the session writer sends."""

    def __init__(self):
        self.sent: list = []

    async def send(self, message) -> None:
        self.sent.append(message)

    async def close(self, code=1000, reason=""):
        pass


class FakeTap:
    """Deterministic tap stand-in for writer-level tests."""

    def __init__(self, items, drop_count=0, state=("streaming", 1, "dev")):
        self._items = list(items)
        self._drop_count = drop_count
        self._state = state

    def drain(self, limit=512):
        out, self._items = self._items[:limit], self._items[limit:]
        return out

    def state(self):
        return self._state

    def drop_count(self):
        return self._drop_count


def _fake_server(tap):
    return SimpleNamespace(tap=tap, wake=asyncio.Event())


def _session(ws, tap, **kwargs):
    return BridgeSession(
        ws,
        _fake_server(tap),
        resolve_device=kwargs.get("resolve_device", lambda d: None),
        device_info=kwargs.get("device_info", lambda d: None),
    )


# ---------------------------------------------------------------------------
# writer: continuity, resync, dropped reporting (deterministic, no server)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSessionWriter:
    def _first_frame(self, session, seq, payload=b"\x00\x00" * 4):
        # drive the private frame path directly; ``_first_frame`` anchors
        return session._send_frame(seq, 100, payload)

    async def test_first_frame_anchors_with_resync(self):
        ws = RecordingWS()
        session = _session(ws, FakeTap([]))
        await session._send_frame(0, 100, b"\x00\x00")
        texts = [m for m in ws.sent if isinstance(m, str)]
        frames = [m for m in ws.sent if isinstance(m, bytes)]
        assert any('"resync"' in t for t in texts)
        assert len(frames) == 1
        frame = unpack_frame(frames[0])
        assert frame.seq == 0
        assert frame.resync and frame.discontinuity

    async def test_gap_triggers_discontinuity_and_flag(self):
        ws = RecordingWS()
        session = _session(ws, FakeTap([]))
        await session._send_frame(5, 100, b"\x00\x00")  # anchors at 5
        ws.sent.clear()
        await session._send_frame(9, 120, b"\x01\x01")  # 6,7,8 skipped
        texts = " ".join(t for t in ws.sent if isinstance(t, str))
        frames = [m for m in ws.sent if isinstance(m, bytes)]
        assert '"gap"' in texts and '"fromSeq":5' in texts and '"toSeq":9' in texts
        assert len(frames) == 1
        assert unpack_frame(frames[0]).discontinuity

    async def test_dropped_delta_is_reported_once(self):
        ws = RecordingWS()
        tap = FakeTap([], drop_count=5)  # 5 frames were dropped before/now
        session = _session(ws, tap)
        await session._send_frame(0, 100, b"\x00\x00")  # resync anchor
        ws.sent.clear()
        await session._send_frame(1, 120, b"\x01\x01")
        texts = " ".join(t for t in ws.sent if isinstance(t, str))
        assert '"dropped"' in texts and '"droppedFrames":5' in texts

    async def test_dropped_reported_only_on_delta(self):
        ws = RecordingWS()
        tap = FakeTap([], drop_count=5)
        session = _session(ws, tap)
        await session._send_frame(0, 100, b"\x00\x00")
        await session._send_frame(1, 120, b"\x01\x01")  # reports the 5
        ws.sent.clear()
        await session._send_frame(2, 140, b"\x02\x02")  # no new drops
        texts = " ".join(t for t in ws.sent if isinstance(t, str))
        assert '"dropped"' not in texts

    async def test_idle_frames_are_dropped_not_replayed(self):
        ws = RecordingWS()
        tap = FakeTap([], state=("idle", 1, "dev"))
        session = _session(ws, tap)
        await session._send_frame(3, 100, b"\x03\x03")
        assert ws.sent == []  # nothing reaches the wire

    async def test_state_transition_emits_state_and_eos(self):
        ws = RecordingWS()
        tap = FakeTap([], state=("idle", 1, "dev"))
        session = _session(ws, tap)
        # constructed while streaming; simulate the writer sync on idle
        session._sent_state = "streaming"
        await session._sync_state()
        texts = " ".join(t for t in ws.sent if isinstance(t, str))
        assert '"streamState":"idle"' in texts
        frames = [m for m in ws.sent if isinstance(m, bytes)]
        assert len(frames) == 1
        assert unpack_frame(frames[0]).end_of_stream
        assert unpack_frame(frames[0]).payload == b""


# ---------------------------------------------------------------------------
# protocol codec
# ---------------------------------------------------------------------------


class TestFrameCodec:
    def test_roundtrip(self):
        payload = b"\x00\x01\x02\x03" * 256
        wire = pack_frame(7, 1234, payload, FRAME_FLAG_DISCONTINUITY)
        frame = unpack_frame(wire)
        assert frame.seq == 7
        assert frame.ts_ms == 1234
        assert frame.payload == payload
        assert frame.discontinuity
        assert not frame.end_of_stream
        assert not frame.resync

    def test_resync_flag(self):
        frame = unpack_frame(pack_frame(0, 0, b"", FRAME_FLAG_RESYNC))
        assert frame.resync
        assert not frame.discontinuity

    def test_too_short_raises(self):
        with pytest.raises(ValueError):
            unpack_frame(b"\x00" * 4)

    def test_non_bytes_payload_rejected(self):
        with pytest.raises(TypeError):
            pack_frame(0, 0, "not bytes")  # type: ignore[arg-type]

    def test_message_roundtrip(self):
        message = {"type": "state", "streamState": "streaming", "sessionGeneration": 3}
        assert decode_message(encode_message(message)) == message

    def test_invalid_message_returns_none(self):
        assert decode_message("not json") is None
        assert decode_message('["a", 1]') is None  # not an object


# ---------------------------------------------------------------------------
# origin matching
# ---------------------------------------------------------------------------


class TestOriginAllowed:
    def test_exact_match(self):
        assert origin_allowed("https://v271.cz", ["https://v271.cz"])

    def test_scheme_and_host_with_any_port(self):
        assert origin_allowed("http://localhost:1420", ["http://localhost:*"])
        assert origin_allowed("http://localhost:9999", ["http://localhost:*"])

    def test_port_mismatch_rejected(self):
        assert not origin_allowed("https://other.cz", ["https://v271.cz"])

    def test_wildcard_entry(self):
        assert origin_allowed("https://anything.example", ["*"])

    def test_missing_origin_never_allowed(self):
        assert not origin_allowed(None, ["https://v271.cz"])
        assert not origin_allowed("", ["*"])


# ---------------------------------------------------------------------------
# conversion
# ---------------------------------------------------------------------------


class TestSamplesToPcm16:
    def test_bytes_passthrough(self):
        data = b"\x00\x10\x00\x20"
        assert samples_to_pcm16(data) == data

    def test_float32_converted_once(self):
        import numpy as np

        values = np.array([0.0, 0.5, -0.5, 1.0], dtype=np.float32)
        out = samples_to_pcm16(values)
        assert len(out) == 8  # 4 samples * 2 bytes
        assert out[0:2] == b"\x00\x00"  # 0.0
        assert out[2:4] == b"\xff\x3f"  # 0.5 -> 16383 (0x3FFF, int16 max scaled)
        assert out[4:6] == b"\x01\xc0"  # -0.5 -> -16383 (0xC001)
        assert out[6:8] == b"\xff\x7f"  # 1.0 -> 32767 (0x7FFF)

    def test_int16_array_passthrough(self):
        import numpy as np

        values = np.array([1, -2, 3], dtype=np.int16)
        assert samples_to_pcm16(values) == values.tobytes()

    def test_unsupported_type_raises(self):
        with pytest.raises(TypeError):
            samples_to_pcm16(12345)


# ---------------------------------------------------------------------------
# tap
# ---------------------------------------------------------------------------


class TestVoicePEFrameTap:
    def test_frames_are_sequenced_and_timestamped(self):
        tap = VoicePEFrameTap(max_frames=10)
        stream = _stream(1)
        frames = []
        for i in range(3):
            payload = b"\x00\x00" * 256
            tap.on_frame(SimpleNamespace(stream=stream, samples=payload))
            frames.append(tap.drain(10)[-1])
        seqs = [f[0] for f in frames]
        assert seqs == [0, 1, 2]
        assert all(f[2] == b"\x00\x00" * 256 for f in frames)
        # timestamps are monotonic ms
        assert frames[0][1] <= frames[2][1]

    def test_local_stream_is_never_bridged(self):
        tap = VoicePEFrameTap(max_frames=10)
        tap.on_frame(SimpleNamespace(stream=StreamId("", 0, 0), samples=b"\x00" * 4))
        assert tap.drain(10) == []
        assert tap.metrics()["frames_in"] == 0

    def test_bounded_queue_drops_oldest(self):
        tap = VoicePEFrameTap(max_frames=3)
        stream = _stream(1)
        for i in range(5):
            tap.on_frame(SimpleNamespace(stream=stream, samples=bytes([i]) * 4))
        items = tap.drain(10)
        assert [f[0] for f in items] == [2, 3, 4]  # oldest two dropped
        assert tap.drop_count() == 2
        assert tap.depth() == 0
        assert tap.metrics()["dropped_frames"] == 2
        assert tap.metrics()["frames_in"] == 5
        assert tap.metrics()["max_frames"] == 3

    def test_stream_state_transitions(self):
        tap = VoicePEFrameTap(max_frames=10)
        assert tap.state()[0] == "idle"
        tap.on_stream_start(_stream(4))
        assert tap.state()[0] == "streaming"
        assert tap.state()[1] == 4
        tap.on_stream_end(_stream(4))
        assert tap.state()[0] == "idle"

    def test_ignore_empty_stream_identity(self):
        tap = VoicePEFrameTap(max_frames=10)
        tap.on_stream_start(StreamId("", 0, 0))
        assert tap.state()[0] == "idle"

    def test_metrics_shape(self):
        tap = VoicePEFrameTap(max_frames=5)
        tap.on_frame(SimpleNamespace(stream=_stream(1), samples=b"\x00" * 4))
        metrics = tap.metrics()
        assert metrics["frames_in"] == 1
        assert metrics["frames_queued"] == 1
        assert metrics["last_seq"] == 0
        assert metrics["streaming"] is False

    def test_thread_safety(self):
        tap = VoicePEFrameTap(max_frames=1000)
        stream = _stream(1)
        errors = []

        def push(offset):
            try:
                for i in range(200):
                    tap.on_frame(SimpleNamespace(stream=stream, samples=bytes([i]) * 4))
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=push, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert tap.metrics()["frames_in"] == 800
        assert len(tap.drain(10_000)) == 800


# ---------------------------------------------------------------------------
# AudioIngress integration: the tap sees the delivered satellite signal
# ---------------------------------------------------------------------------


class TestIngressTap:
    def test_delivered_frames_reach_the_tap(self):
        ingress, _ = _ingress()
        tap = VoicePEFrameTap(max_frames=100)
        ingress.attach_sink(tap)
        stream = _stream(1)
        ingress.set_stream(stream)
        # fixed ``enhanced`` channel: the first block locks and delivers
        ingress.put(b"\x00\x00" * 512, None, stream)
        ingress.put(b"\x01\x01" * 512, None, stream)
        items = tap.drain(10)
        assert len(items) == 2
        assert items[0][2] == b"\x00\x00" * 512
        assert items[1][2] == b"\x01\x01" * 512
        # stream start was announced
        assert tap.state()[0] == "streaming"

    def test_stream_end_announced(self):
        ingress, _ = _ingress()
        tap = VoicePEFrameTap(max_frames=100)
        ingress.attach_sink(tap)
        stream = _stream(1)
        ingress.set_stream(stream)
        ingress.put(b"\x00\x00" * 512, None, stream)
        ingress.mark_end_of_stream(stream)
        assert tap.state()[0] == "idle"

    def test_detach_stops_the_tap(self):
        ingress, _ = _ingress()
        tap = VoicePEFrameTap(max_frames=100)
        ingress.attach_sink(tap)
        ingress.detach_sink(tap)
        stream = _stream(1)
        ingress.set_stream(stream)
        ingress.put(b"\x00\x00" * 512, None, stream)
        assert tap.drain(10) == []
        assert tap.state()[0] == "idle"

    def test_attach_is_idempotent_and_zero_cost_without_sink(self):
        ingress, listener = _ingress()
        tap = VoicePEFrameTap(max_frames=100)
        ingress.attach_sink(tap)
        ingress.attach_sink(tap)  # same instance twice: no duplicate
        stream = _stream(1)
        ingress.set_stream(stream)
        ingress.put(b"\x00\x00" * 512, None, stream)
        assert len(tap.drain(10)) == 1
        # without any sink the delivery path still works
        tap2 = VoicePEFrameTap(max_frames=100)
        ingress.detach_sink(tap)
        ingress.put(b"\x00\x00" * 512, None, stream)
        assert len(tap2.drain(10)) == 0
        assert ingress.depth_ms() > 0  # frames still queued for the pump

    def test_replay_frames_are_tapped(self):
        ingress, _ = _ingress(audio_channel="enhanced")
        tap = VoicePEFrameTap(max_frames=1000)
        ingress.attach_sink(tap)
        stream = _stream(1)
        ingress.set_stream(stream)
        # fixed ``enhanced``: the first block locks and replays the buffer
        # (1 replayed frame), the next two are delivered directly.
        for i in range(3):
            ingress.put(bytes([i]) * 512, None, stream)
        items = tap.drain(1000)
        assert [f[0] for f in items] == [0, 1, 2]
        assert items[0][2] == b"\x00" * 512
        assert items[2][2] == b"\x02" * 512


# ---------------------------------------------------------------------------
# server: handshake, auth, origin, slots
# ---------------------------------------------------------------------------


class _ClosedDuringHandshake(Exception):
    """Raised when the server closes a connection during the handshake."""


async def _expect_close(ws, code: int) -> None:
    """The server closes right after the handshake; assert on first recv."""
    with pytest.raises(Exception) as excinfo:
        await asyncio.wait_for(ws.recv(), 2.0)
    assert str(code) in str(excinfo.value)


@pytest.mark.asyncio
class TestBridgeServerAuth:
    async def test_start_requires_token(self):
        server = VoicePEBridgeServer(_bridge_config(voice_pe_bridge_token=""), FakeManager())
        assert not server.start()

    async def test_missing_token_rejected(self):
        server = VoicePEBridgeServer(_bridge_config(), FakeManager(_fake_device()))
        assert server.start()
        try:
            uri = f"ws://127.0.0.1:{server.port}/voice-pe/v1"
            ws = await _connect(uri, token=None)
            await _expect_close(ws, CLOSE_UNAUTHORISED)
        finally:
            server.stop()

    async def test_wrong_token_rejected(self):
        server = VoicePEBridgeServer(_bridge_config(), FakeManager(_fake_device()))
        assert server.start()
        try:
            uri = f"ws://127.0.0.1:{server.port}/voice-pe/v1"
            ws = await _connect(uri, token="wrong")
            await _expect_close(ws, CLOSE_UNAUTHORISED)
        finally:
            server.stop()

    async def test_bad_origin_rejected(self):
        server = VoicePEBridgeServer(_bridge_config(), FakeManager(_fake_device()))
        assert server.start()
        try:
            uri = f"ws://127.0.0.1:{server.port}/voice-pe/v1"
            ws = await _connect(uri, origin="https://evil.example")
            await _expect_close(ws, CLOSE_ORIGIN_REJECTED)
        finally:
            server.stop()

    async def test_no_origin_rejected(self):
        server = VoicePEBridgeServer(_bridge_config(), FakeManager(_fake_device()))
        assert server.start()
        try:
            uri = f"ws://127.0.0.1:{server.port}/voice-pe/v1"
            ws = await _connect(uri, origin=None)
            await _expect_close(ws, CLOSE_ORIGIN_REJECTED)
        finally:
            server.stop()

    async def test_second_client_is_busy(self):
        server = VoicePEBridgeServer(_bridge_config(), FakeManager(_fake_device()))
        assert server.start()
        try:
            uri = f"ws://127.0.0.1:{server.port}/voice-pe/v1"
            first = await _connect(uri)
            hello = await _recv_json(first)
            assert hello["type"] == "hello"
            second = await _connect(uri)
            await _expect_close(second, CLOSE_BUSY)
            await first.close()
        finally:
            server.stop()


# ---------------------------------------------------------------------------
# server: hello, frames, stream lifecycle, discontinuity, reconnect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestBridgeServerStream:
    async def _server_with_ingress(self, **overrides):
        server = VoicePEBridgeServer(
            _bridge_config(**overrides), FakeManager(_fake_device())
        )
        assert server.start()
        ingress, _ = _ingress()
        ingress.attach_sink(server.tap)
        return server, ingress

    async def test_hello_contract(self):
        server, ingress = await self._server_with_ingress()
        try:
            uri = f"ws://127.0.0.1:{server.port}/voice-pe/v1"
            ws = await _connect(uri)
            hello = await _recv_json(ws)
            assert hello["type"] == "hello"
            assert hello["protocol"] == PROTOCOL_NAME
            assert hello["sessionId"]
            assert hello["format"] == {
                "sampleRate": 16000,
                "channels": 1,
                "sampleType": "pcm16le",
                "endianness": "little",
                "frameSamples": 512,
                "frameDurationMs": 32,
            }
            assert hello["streamState"] == "idle"
            assert hello["device"]["deviceId"] == "aabbccddeeff"
            assert hello["device"]["name"] == "living-room-pe"
            await ws.close()
        finally:
            server.stop()

    async def test_frames_flow_with_sequence_and_payload(self):
        server, ingress = await self._server_with_ingress()
        try:
            uri = f"ws://127.0.0.1:{server.port}/voice-pe/v1"
            ws = await _connect(uri)
            await _recv_json(ws)  # hello
            stream = _stream(1)
            ingress.set_stream(stream)
            ingress.put(b"\x00\x00" * 512, None, stream)
            ingress.put(b"\x01\x01" * 512, None, stream)
            # state streaming
            state = await _recv_json(ws)
            assert state["type"] == "state"
            assert state["streamState"] == "streaming"
            assert state["sessionGeneration"] == 1
            # resync anchor, then two frames
            resync = await _recv_json(ws)
            assert resync["type"] == "resync"
            first = await _recv_frame(ws)
            second = await _recv_frame(ws)
            assert first.seq == 0
            assert second.seq == 1
            assert first.resync and first.discontinuity
            assert not second.resync
            assert first.payload == b"\x00\x00" * 512
            assert second.payload == b"\x01\x01" * 512
            await ws.close()
        finally:
            server.stop()

    async def test_stream_end_emits_state_and_eos_frame(self):
        server, ingress = await self._server_with_ingress()
        try:
            uri = f"ws://127.0.0.1:{server.port}/voice-pe/v1"
            ws = await _connect(uri)
            await _recv_json(ws)  # hello
            stream = _stream(1)
            ingress.set_stream(stream)
            ingress.put(b"\x00\x00" * 512, None, stream)
            await _recv_json(ws)  # state streaming
            await _recv_json(ws)  # resync
            await _recv_frame(ws)  # frame 0
            ingress.mark_end_of_stream(stream)
            state = await _recv_json(ws)
            assert state["type"] == "state"
            assert state["streamState"] == "idle"
            eos = await _recv_frame(ws)
            assert eos.end_of_stream
            assert eos.payload == b""
            # a later stream re-anchors
            stream2 = _stream(2)
            ingress.set_stream(stream2)
            ingress.put(b"\x02\x02" * 512, None, stream2)
            state2 = await _recv_json(ws)
            assert state2["streamState"] == "streaming"
            assert state2["sessionGeneration"] == 2
            resync2 = await _recv_json(ws)
            assert resync2["type"] == "resync"
            await ws.close()
        finally:
            server.stop()

    async def test_subscribe_without_device_reports_no_device(self):
        server = VoicePEBridgeServer(_bridge_config(), FakeManager(None))
        assert server.start()
        try:
            uri = f"ws://127.0.0.1:{server.port}/voice-pe/v1"
            ws = await _connect(uri)
            hello = await _recv_json(ws)
            assert hello["device"] is None
            await ws.send(encode_message({"type": "subscribe", "deviceId": ""}))
            error = await _recv_json(ws)
            assert error["type"] == "error"
            assert error["code"] == "no_device"
            await ws.close()
        finally:
            server.stop()

    async def test_reconnect_gets_fresh_session_and_resync(self):
        server, ingress = await self._server_with_ingress()
        try:
            uri = f"ws://127.0.0.1:{server.port}/voice-pe/v1"
            stream = _stream(1)
            first = await _connect(uri)
            hello1 = await _recv_json(first)
            await first.close()
            second = await _connect(uri)
            hello2 = await _recv_json(second)
            assert hello1["sessionId"] != hello2["sessionId"]
            # audio continues after the reconnect: the stream opens under
            # the second connection, so the order is state, resync, frame.
            ingress.set_stream(stream)
            ingress.put(b"\x00\x00" * 512, None, stream)
            state = await _recv_json(second)
            assert state["type"] == "state"
            assert state["streamState"] == "streaming"
            resync = await _recv_json(second)
            assert resync["type"] == "resync"
            frame = await _recv_frame(second)
            assert frame.resync and frame.discontinuity
            assert frame.payload == b"\x00\x00" * 512
            await second.close()
        finally:
            server.stop()

    async def test_metrics_and_health(self):
        server, ingress = await self._server_with_ingress()
        try:
            uri = f"ws://127.0.0.1:{server.port}/voice-pe/v1"
            ws = await _connect(uri)
            await _recv_json(ws)  # hello
            stream = _stream(1)
            ingress.set_stream(stream)
            ingress.put(b"\x00\x00" * 512, None, stream)
            await _recv_json(ws)  # state
            await _recv_json(ws)  # resync
            await _recv_frame(ws)
            await asyncio.sleep(0.1)
            health = server.health()
            assert health["running"] is True
            assert health["clients"] == 1
            assert health["attached"] is True
            metrics = server.metrics()
            assert metrics["clients"] == 1
            assert metrics["tap"]["frames_in"] >= 1
            assert metrics["sessions"][0]["frames_sent"] >= 1
            assert metrics["sessions"][0]["bytes_sent"] >= 1024
            await ws.close()
        finally:
            server.stop()

    async def test_health_reports_token_and_origins(self):
        server = VoicePEBridgeServer(_bridge_config(), FakeManager(None))
        assert server.start()
        try:
            health = server.health()
            assert health["token"] is True
            assert ORIGIN in health["allowed_origins"]
            assert health["host"] == "127.0.0.1"
        finally:
            server.stop()# ---------------------------------------------------------------------------
# config defaults + daemon wiring (full-dep env only)
# ---------------------------------------------------------------------------


class TestConfigDefaults:
    def test_bridge_defaults_present(self, tmp_path, monkeypatch):
        import os

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))
        from jarvis.config import load_settings

        settings = load_settings()
        assert settings.voice_pe_bridge_enabled is False
        assert settings.voice_pe_bridge_port == 27123
        assert settings.voice_pe_bridge_token == ""
        assert settings.voice_pe_bridge_allowed_origins == ["https://v271.cz"]
        assert settings.voice_pe_bridge_max_clients == 1
        assert settings.voice_pe_bridge_buffer_frames == 200
        assert settings.voice_pe_bridge_device == ""

    def test_bridge_token_from_environment(self, tmp_path, monkeypatch):
        import os

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))
        monkeypatch.setenv("JARVIS_VOICE_PE_BRIDGE_TOKEN", "env-token")
        from jarvis.config import load_settings

        settings = load_settings()
        assert settings.voice_pe_bridge_token == "env-token"


class TestDaemonWiring:
    def test_daemon_starts_and_stops_voice_pe_bridge(self):
        """``daemon.main(smoke_test=True)`` boots and stops the bridge."""
        pytest.importorskip("huggingface_hub")  # full daemon dep set
        from unittest.mock import MagicMock, patch
        import io

        cfg = MagicMock()
        cfg.voice_pe_bridge_enabled = True
        cfg.voice_pe_bridge_port = 0
        cfg.voice_pe_bridge_token = "daemon-token"
        cfg.voice_pe_bridge_allowed_origins = ["https://v271.cz"]
        cfg.voice_pe_bridge_max_clients = 1
        cfg.voice_pe_bridge_buffer_frames = 200
        cfg.voice_pe_bridge_device = ""
        with patch("jarvis.daemon.load_settings", return_value=cfg), \
             patch("jarvis.daemon.Database"), \
             patch("jarvis.daemon.initialize_mcp_tools", return_value=({}, {})), \
             patch("jarvis.daemon.DialogueMemory"), \
             patch("jarvis.daemon.get_location_context", return_value="Location: Test"), \
             patch("jarvis.daemon.create_tts_engine") as mock_tts, \
             patch("jarvis.daemon.VoiceListener"), \
             patch("jarvis.memory.graph.GraphMemoryStore"):
            mock_tts_instance = MagicMock()
            mock_tts_instance.enabled = False
            mock_tts.return_value = mock_tts_instance
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                from jarvis.daemon import main

                main(smoke_test=True)

            output = captured.getvalue()
            assert "V271 PWA WebAudio bridge: ws://127.0.0.1:" in output
            from jarvis import daemon

            assert daemon.get_voice_pe_bridge() is None

    def test_daemon_bridge_disabled_prints_disabled(self):
        pytest.importorskip("huggingface_hub")
        from unittest.mock import MagicMock, patch
        import io

        cfg = MagicMock()
        cfg.voice_pe_bridge_enabled = False
        with patch("jarvis.daemon.load_settings", return_value=cfg), \
             patch("jarvis.daemon.Database"), \
             patch("jarvis.daemon.initialize_mcp_tools", return_value=({}, {})), \
             patch("jarvis.daemon.DialogueMemory"), \
             patch("jarvis.daemon.get_location_context", return_value="Location: Test"), \
             patch("jarvis.daemon.create_tts_engine") as mock_tts, \
             patch("jarvis.daemon.VoiceListener"), \
             patch("jarvis.memory.graph.GraphMemoryStore"):
            mock_tts_instance = MagicMock()
            mock_tts_instance.enabled = False
            mock_tts.return_value = mock_tts_instance
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                from jarvis.daemon import main

                main(smoke_test=True)
            assert "V271 PWA WebAudio bridge disabled" in captured.getvalue()
