"""Tests for the Voice PE full audio pipeline and auto-calibration.

Behaviour-level: settings model/profiles, the speech-aware normalizer
(silence no-learn, rate-bound adaptation, clamps, limiter), calibration
measurements and tuning, ingress integration, device/manager wiring and the
CLI surface. No live device, no network.
"""

import asyncio
import queue
import sys
from types import SimpleNamespace

import pytest

import numpy as np

from jarvis.integrations.voice_pe import config as pe_config
from jarvis.integrations.voice_pe.audio_settings import (
    PROFILE_DESK,
    PROFILE_FAR_FIELD,
    PROFILE_MEETING,
    PROFILES,
    SETTING_METADATA,
    SettingMeta,
    VoicePEAudioSettings,
    normalize_device_settings,
    normalize_profile_name,
    profile_preset,
    resolve_audio_settings,
)
from jarvis.integrations.voice_pe.calibration import (
    CalibrationSession,
    CalibrationWizard,
    _SinkCollector,
    rms_db,
    tune_settings,
)
from jarvis.integrations.voice_pe.models import StreamId
from jarvis.integrations.voice_pe.normalizer import SpeechAwareNormalizer
from jarvis.integrations.voice_pe.voice_transport import AudioIngress


def _base_settings():
    return {
        "vad_aggressiveness": 2,
        "vad_pre_roll_ms": 240,
        "whisper_post_roll_ms": 400,
        "whisper_min_avg_logprob": -0.7,
        "whisper_no_speech_threshold": 0.5,
        "normalizer_enabled": False,
        "normalizer_target_db": -28.0,
        "normalizer_max_gain_db": 9.0,
        "normalizer_attack_db_per_s": 3.0,
        "normalizer_limiter_db": -1.0,
    }


class FakeListener:
    def __init__(self):
        self._audio_q = queue.Queue(maxsize=256)
        self.vad_level = None

    def pad_until_endpoint(self, source, generation):
        pass

    def set_vad_aggressiveness(self, level: int) -> bool:
        self.vad_level = int(level)
        return True


def _ingress(**overrides):
    listener = FakeListener()
    base = {
        "enabled": True,
        "prefer_api_audio": True,
        "audio_queue_ms": 300,
        "audio_channel": "enhanced",
        "dsp_mode": "device_enhanced",
    }
    base.update(overrides)
    cfg = pe_config.from_settings(
        SimpleNamespace(**{f"voice_pe_{k}": v for k, v in base.items()})
    )
    return AudioIngress(listener, cfg, {}), listener


def _frames(count: int, amplitude: float = 0.01, seed: int = 0):
    rng = np.random.default_rng(seed)
    return [
        (rng.standard_normal(512).astype(np.float32) * amplitude)
        for _ in range(count)
    ]


# ---------------------------------------------------------------------------
# settings model + profiles
# ---------------------------------------------------------------------------


class TestAudioSettingsModel:
    def test_metadata_covers_all_layers(self):
        keys = [m.key for m in SETTING_METADATA]
        assert "vad_aggressiveness" in keys
        assert "normalizer_limiter_db" in keys
        for meta in SETTING_METADATA:
            assert isinstance(meta, SettingMeta)
            assert meta.source_layer in ("host", "listener", "whisper", "device")

    def test_profiles_differ(self):
        desk = profile_preset(PROFILE_DESK)
        meeting = profile_preset(PROFILE_MEETING)
        far = profile_preset(PROFILE_FAR_FIELD)
        assert desk["whisper_post_roll_ms"] != meeting["whisper_post_roll_ms"]
        assert desk["vad_aggressiveness"] != meeting["vad_aggressiveness"]
        assert far["normalizer_max_gain_db"] > desk["normalizer_max_gain_db"]
        assert profile_preset("auto") == {}

    def test_auto_inherits_base(self):
        resolved = resolve_audio_settings(_base_settings(), "auto")
        assert resolved.profile == "auto"
        assert resolved.values == _base_settings()
        assert resolved.source == "base"

    def test_profile_merge_and_calibration_override(self):
        resolved = resolve_audio_settings(_base_settings(), "meeting")
        assert resolved.values["vad_aggressiveness"] == 0
        assert resolved.values["whisper_post_roll_ms"] == 1400
        assert resolved.values["normalizer_enabled"] is True
        calibrated = resolve_audio_settings(
            _base_settings(),
            "meeting",
            calibration={"whisper_post_roll_ms": 900},
        )
        assert calibrated.values["whisper_post_roll_ms"] == 900
        # non-tuneable keys in a calibration are ignored
        assert calibrated.values["vad_pre_roll_ms"] == 480

    def test_profile_name_normalisation(self):
        assert normalize_profile_name("FAR-FIELD") == "far_field"
        assert normalize_profile_name("bogus") == "auto"
        assert normalize_profile_name(None) == "auto"

    def test_view_shape(self):
        resolved = resolve_audio_settings(_base_settings(), "desk")
        view = resolved.view()
        assert view["profile"] == "desk"
        rows = {row["key"]: row for row in view["settings"]}
        assert rows["vad_aggressiveness"]["value"] == 2
        assert rows["vad_aggressiveness"]["layer"] == "listener"
        assert rows["normalizer_limiter_db"]["unit"] == "dBFS"
        assert rows["normalizer_target_db"]["state"] == "active"


class TestDeviceSettingsNormalisation:
    def test_full_report(self):
        device = SimpleNamespace(
            noise_suppression_level=2, auto_gain=-1, volume_multiplier=1.0
        )
        out = normalize_device_settings(device)
        assert out["reported"] is True
        assert out["controllable"] is False
        assert out["noise_suppression_level"]["display"] == "medium"
        assert out["auto_gain"]["display"] == "disabled"
        assert out["volume_multiplier"]["display"] == "1.00x"

    def test_partial_report_is_not_reported(self):
        out = normalize_device_settings(SimpleNamespace(volume_multiplier=0.5))
        assert out["noise_suppression_level"]["display"] == "not reported"
        assert out["volume_multiplier"]["value"] == 0.5

    def test_none_is_not_reported(self):
        out = normalize_device_settings(None)
        assert out["reported"] is False
        assert out["noise_suppression_level"]["display"] == "not reported"


# ---------------------------------------------------------------------------
# speech-aware normalizer
# ---------------------------------------------------------------------------


class TestSpeechAwareNormalizer:
    def test_disabled_passthrough(self):
        normalizer = SpeechAwareNormalizer(enabled=False)
        data = np.zeros(512, dtype=np.float32)
        assert normalizer.process(data) is data
        assert normalizer.metrics()["gain_db"] == 0.0

    def test_silence_never_learns_gain(self):
        normalizer = SpeechAwareNormalizer(target_db=-28.0, attack_db_per_s=6.0)
        silence = np.zeros(512, dtype=np.float32)
        for _ in range(200):
            normalizer.process(silence)
        metrics = normalizer.metrics()
        assert metrics["gain_db"] == 0.0
        assert metrics["noise_floor_db"] is not None  # floor still adapts

    def test_speech_adapts_toward_target_rate_bounded(self):
        normalizer = SpeechAwareNormalizer(
            target_db=-28.0, attack_db_per_s=3.0, max_gain_db=9.0
        )
        # train the floor with silence, then feed steady quiet speech
        for _ in range(50):
            normalizer.process(np.zeros(512, dtype=np.float32))
        quiet = _frames(20, amplitude=0.004)  # ~ -48 dBFS
        for frame in quiet:
            normalizer.process(frame)
        metrics = normalizer.metrics()
        assert 0.0 < metrics["gain_db"] <= 9.0
        # rate bound: 3 dB/s * (512 / 16000 s per frame) = 0.096 dB/frame;
        # 20 frames can move at most 1.92 dB
        max_step = 3.0 * (512 / 16000)
        assert metrics["gain_db"] <= 20 * max_step + 1e-6

    def test_max_gain_clamped(self):
        normalizer = SpeechAwareNormalizer(
            target_db=-28.0, attack_db_per_s=12.0, max_gain_db=3.0
        )
        for _ in range(50):
            normalizer.process(np.zeros(512, dtype=np.float32))
        for frame in _frames(400, amplitude=0.0005):  # very quiet
            normalizer.process(frame)
        assert normalizer.metrics()["gain_db"] <= 3.0 + 1e-6

    def test_silence_freezes_gain(self):
        normalizer = SpeechAwareNormalizer(target_db=-28.0, attack_db_per_s=12.0)
        for _ in range(50):
            normalizer.process(np.zeros(512, dtype=np.float32))
        for frame in _frames(300, amplitude=0.004):
            normalizer.process(frame)
        gain_before = normalizer.metrics()["gain_db"]
        assert gain_before > 0.0
        for _ in range(200):
            normalizer.process(np.zeros(512, dtype=np.float32))
        assert normalizer.metrics()["gain_db"] == gain_before

    def test_limiter_caps_peaks(self):
        normalizer = SpeechAwareNormalizer(
            target_db=-12.0, attack_db_per_s=12.0, limiter_db=-3.0
        )
        for _ in range(50):
            normalizer.process(np.zeros(512, dtype=np.float32))
        loud = _frames(400, amplitude=0.9)
        ceiling = 10 ** (-3.0 / 20.0)
        for frame in loud:
            out = normalizer.process(frame)
            assert float(np.max(np.abs(out))) <= ceiling + 1e-4

    def test_metrics_and_reset(self):
        normalizer = SpeechAwareNormalizer(target_db=-28.0)
        for frame in _frames(10, amplitude=0.01):
            normalizer.process(frame)
        metrics = normalizer.metrics()
        assert metrics["frames_processed"] == 10
        assert metrics["clipping_ratio"] >= 0.0
        assert "gain_db" in metrics and "noise_floor_db" in metrics
        normalizer.reset()
        assert normalizer.metrics()["gain_db"] == 0.0
        assert normalizer.metrics()["noise_floor_db"] is None


# ---------------------------------------------------------------------------
# calibration: measurements + tuning
# ---------------------------------------------------------------------------


class TestCalibrationSession:
    def test_silence_window_is_honest(self):
        session = CalibrationSession("silence", is_voiced=lambda arr: False)
        for i, frame in enumerate(_frames(50, amplitude=0.0001)):
            session.add_block(frame, at=i * 0.01)
        snap = session.snapshot()
        assert snap["rms_db"] is not None and snap["rms_db"] < -40.0
        assert snap["voiced_ratio"] == 0.0
        assert snap["clipping_ratio"] == 0.0
        assert snap["duration_s"] > 0.4 and snap["duration_s"] <= 0.5

    def test_speech_window_metrics(self):
        session = CalibrationSession("speech", is_voiced=lambda arr: True)
        for frame in _frames(50, amplitude=0.1):
            session.add_block(frame)
        snap = session.snapshot()
        assert snap["rms_db"] > -30.0
        assert snap["voiced_ratio"] == 1.0
        assert snap["peak_db"] > snap["rms_db"]

    def test_empty_window(self):
        snap = CalibrationSession("x").snapshot()
        assert snap["rms_db"] is None
        assert snap["frames"] == 0

    def test_collector_converts_bytes(self):
        session = CalibrationSession("x")
        collector = _SinkCollector(session)
        pcm = (np.ones(512, dtype=np.float32) * 0.5).astype(np.int16).tobytes()
        collector.on_frame(SimpleNamespace(samples=pcm))
        assert session.snapshot()["frames"] == 1


class TestTuneSettings:
    def test_far_field_degradation_raises_max_gain(self):
        measurements = {
            "noise_floor_db": -50.0,
            "speech_rms_db": -25.0,
            "far_rms_db": -40.0,
            "false_vad_rate": 0.0,
            "far_voiced_ratio": 0.5,
        }
        tuned = tune_settings(measurements, _base_settings())
        # degradation 15 dB + 6 headroom = 21 -> clamped to the 12 dB cap
        assert tuned["normalizer_max_gain_db"] == pytest.approx(12.0)
        assert tuned["normalizer_enabled"] is True
        assert tuned["normalizer_target_db"] == pytest.approx(-32.0)  # floor+18, < speech+4

    def test_false_vad_raises_aggressiveness(self):
        measurements = {
            "noise_floor_db": -50.0,
            "false_vad_rate": 0.2,
            "far_voiced_ratio": 0.5,
        }
        tuned = tune_settings(measurements, _base_settings())
        assert tuned["vad_aggressiveness"] == 3

    def test_far_voiced_loss_extends_post_roll(self):
        measurements = {
            "noise_floor_db": -50.0,
            "far_voiced_ratio": 0.2,
            "false_vad_rate": 0.0,
        }
        tuned = tune_settings(measurements, _base_settings())
        assert tuned["whisper_post_roll_ms"] == 1000  # 400+300+300
        assert tuned["vad_aggressiveness"] == 1

    def test_confidence_relaxation_is_bounded(self):
        measurements = {
            "noise_floor_db": -50.0,
            "whisper_avg_logprob": -1.4,
            "far_voiced_ratio": 0.5,
        }
        tuned = tune_settings(measurements, _base_settings())
        assert tuned["whisper_min_avg_logprob"] == pytest.approx(-1.5)  # floored

    def test_no_measurements_keeps_base(self):
        tuned = tune_settings({}, _base_settings())
        assert tuned["vad_aggressiveness"] == 2
        assert tuned["whisper_post_roll_ms"] == 400


# ---------------------------------------------------------------------------
# ingress integration: normalizer + diagnostics
# ---------------------------------------------------------------------------


class TestIngressAudioPipeline:
    async def _pump_one(self, ingress, frames, stream):
        ingress.set_stream(stream)
        task = asyncio.ensure_future(ingress.pump())
        # Push in small batches and yield, so the pump drains between them;
        # the ingress queue is bounded (300 ms) and drops oldest on overflow.
        for start in range(0, len(frames), 8):
            for payload in frames[start:start + 8]:
                ingress.put(payload, None, stream)
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.02)
        metrics = (
            ingress.normalizer.metrics()
            if ingress.normalizer is not None else None
        )
        ingress.close()
        try:
            await task
        except Exception:
            pass
        return metrics

    def test_pump_delivers_unchanged_without_normalizer(self):
        ingress, listener = _ingress()
        stream = StreamId("aa:bb:cc:dd:ee:ff", 1, 1)
        pcm = (np.ones(512, dtype=np.float32) * 0.1 * 32768)
        payload = pcm.astype(np.int16).tobytes()
        asyncio.run(self._pump_one(ingress, [payload] * 3, stream))
        delivered = []
        while not listener._audio_q.empty():
            delivered.append(listener._audio_q.get())
        assert len(delivered) == 3
        first = getattr(delivered[0], "samples", None)
        assert np.allclose(first, 0.1, atol=1e-3)

    def test_pump_applies_normalizer_gain(self):
        ingress, listener = _ingress()
        from jarvis.integrations.voice_pe.normalizer import SpeechAwareNormalizer

        ingress.normalizer = SpeechAwareNormalizer(
            target_db=-20.0, attack_db_per_s=12.0, max_gain_db=9.0, enabled=True
        )
        stream = StreamId("aa:bb:cc:dd:ee:ff", 1, 1)
        quiet = 0.004  # ~ -48 dBFS: below the -20 dBFS target, gain should rise
        pcm = (np.ones(512, dtype=np.float32) * quiet * 32768)
        payload = pcm.astype(np.int16).tobytes()
        metrics = asyncio.run(self._pump_one(ingress, [payload] * 60, stream))
        delivered = []
        while not listener._audio_q.empty():
            delivered.append(listener._audio_q.get())
        assert len(delivered) == 60
        samples = np.concatenate(
            [np.asarray(getattr(f, "samples", [])) for f in delivered]
        )
        assert float(np.sqrt(np.mean(np.square(samples)))) > quiet * 2
        assert metrics["gain_db"] > 0.0
        assert metrics["gain_db"] <= 9.0

    def test_diagnostics_shape(self):
        ingress, _ = _ingress()
        stream = StreamId("aa:bb:cc:dd:ee:ff", 1, 1)
        ingress.set_stream(stream)
        ingress.put(b"\x00\x01" * 512, None, stream)
        diag = ingress.diagnostics()
        for key in (
            "input_rms_db", "input_peak_db", "noise_floor_db", "snr_db",
            "queue_depth_ms", "dropped_chunks", "aec", "normalizer",
        ):
            assert key in diag


# ---------------------------------------------------------------------------
# device wiring
# ---------------------------------------------------------------------------


class TestDeviceAudioPipeline:
    def _real_device(self):
        """A real VoicePEDevice wired like the existing voice_pe tests."""
        import test_voice_pe as vp_helpers

        return vp_helpers._device()

    def test_pipeline_start_captures_device_settings(self):
        import test_voice_pe as vp_helpers

        device = self._real_device()
        audio_settings = SimpleNamespace(
            noise_suppression_level=1, auto_gain=12, volume_multiplier=0.9
        )

        async def _run():
            await device.handle_pipeline_start(
                "conv-1", 0, audio_settings, None
            )
            return dict(device.device_audio_settings)

        reported = vp_helpers._run_loop(_run, device)
        assert reported["reported"] is True
        assert reported["noise_suppression_level"]["display"] == "low"
        assert reported["auto_gain"]["display"] == "12 dBFS"

    def test_pipeline_start_reports_none_settings(self):
        import test_voice_pe as vp_helpers

        device = self._real_device()

        async def _run():
            await device.handle_pipeline_start("conv-2", 0, None, None)
            return dict(device.device_audio_settings)

        reported = vp_helpers._run_loop(_run, device)
        assert reported["reported"] is False
        assert reported["noise_suppression_level"]["display"] == "not reported"

    def test_apply_audio_profile_updates_normalizer(self):
        import test_voice_pe as vp_helpers

        device = vp_helpers._device()
        device._attach_normalizer()
        resolved = resolve_audio_settings(_base_settings(), "far_field")
        device.apply_audio_profile(resolved)
        assert device.active_profile == "far_field"
        assert device._ingress.normalizer is not None
        assert device._ingress.normalizer.target_db == -32.0
        assert device._ingress.normalizer.enabled is True

    def test_health_snapshot_includes_pipeline(self):
        device = self._real_device()
        device.device_audio_settings = normalize_device_settings(None)
        device.active_profile = "auto"
        snapshot = device.health_snapshot()
        assert "audio_settings" in snapshot
        assert snapshot["profile"] == "auto"
        assert "diagnostics" in snapshot
        assert "normalizer" in snapshot["diagnostics"]


async def _noop_await():
    return None


# ---------------------------------------------------------------------------
# manager wiring: profiles, meeting mode, calibration
# ---------------------------------------------------------------------------


class _FakeDevice:
    def __init__(self, ingress=None):
        self.identity = {
            "mac_address": "AA:BB:CC:DD:EE:FF",
            "node_name": "living-room-pe",
            "friendly_name": "Living Room",
        }
        self._host = "192.168.1.50"
        self.device_id = "aabbccddeeff"
        self.audio_ingress = ingress
        self.device_audio_settings = {}
        self.active_profile = "auto"
        self.audio_settings_view = None
        self.metrics = {}
        self.config = None
        self.capabilities = SimpleNamespace(multi_channel_audio=False)

    def apply_audio_profile(self, settings):
        self.active_profile = str(getattr(settings, "profile", "auto"))
        self.audio_settings_view = settings
        # mirror the real device: the per-device normalizer follows the view
        if self.audio_ingress is not None and self.audio_ingress.normalizer is not None:
            self.audio_ingress.normalizer.apply_view(
                **getattr(settings, "values", {})
            )

    def health_snapshot(self):
        return {
            "device_state": "ready",
            "audio_settings": dict(self.device_audio_settings),
            "profile": self.active_profile,
            "diagnostics": {
                "input_rms_db": -30.0,
                "noise_floor_db": -50.0,
                "normalizer": {"gain_db": 3.0, "enabled": True},
            },
            "metrics": {"last_segment": {"avg_logprob": -0.5}},
        }


def _manager(device=None, listener=None):
    from jarvis.integrations.voice_pe.manager import VoicePEManager

    settings = SimpleNamespace(
        vad_aggressiveness=2,
        vad_pre_roll_ms=240,
        whisper_post_roll_ms=400,
        whisper_min_avg_logprob=-0.7,
        whisper_no_speech_threshold=0.5,
        voice_pe_normalizer_enabled=False,
        voice_pe_normalizer_target_db=-28.0,
        voice_pe_normalizer_max_gain_db=9.0,
        voice_pe_normalizer_attack_db_per_s=3.0,
        voice_pe_normalizer_limiter_db=-1.0,
        voice_pe_profile="auto",
        voice_pe_audio_queue_ms=300,
        voice_pe_enabled=True,
        voice_pe_prefer_api_audio=True,
        voice_pe_audio_channel="enhanced",
        voice_pe_dsp_mode="device_enhanced",
    )
    manager = VoicePEManager(settings, listener or FakeListener(), None)
    if device is not None:
        manager._devices = [device]
    return manager


class TestManagerAudioPipeline:
    def test_apply_profile_mutates_listener_and_normalizer(self):
        ingress, _ = _ingress()
        listener = FakeListener()
        device = _FakeDevice(ingress=ingress)
        manager = _manager(device, listener)
        from jarvis.integrations.voice_pe.normalizer import SpeechAwareNormalizer

        ingress.normalizer = SpeechAwareNormalizer()
        result = manager.apply_profile(device.device_id, profile="meeting")
        assert result["applied"] is True
        assert result["profile"] == "meeting"
        assert listener.vad_level == 0
        assert manager.settings.whisper_post_roll_ms == 1400
        assert ingress.normalizer.enabled is True
        assert ingress.normalizer.target_db == -30.0
        assert result["unsupported"] == {}

    def test_apply_profile_reports_unsupported_vad(self):
        device = _FakeDevice(ingress=None)

        class NoVadListener:
            def set_vad_aggressiveness(self, level):
                return False

        manager = _manager(device, NoVadListener())
        result = manager.apply_profile(device.device_id, profile="desk")
        assert "vad_aggressiveness" in result["unsupported"]
        assert "WebRTC VAD" in result["unsupported"]["vad_aggressiveness"]

    def test_set_profile_persists_and_applies(self, monkeypatch):
        device = _FakeDevice()
        manager = _manager(device)
        calls = {}

        def fake_save(mac, meta):
            calls["mac"] = mac
            calls["meta"] = meta
            return True

        monkeypatch.setattr(
            "jarvis.integrations.voice_pe.manager.pe_config.save_device_metadata",
            fake_save,
        )
        result = manager.set_profile(device.device_id, "far_field")
        assert result["applied"] is True
        assert result["profile"] == "far_field"
        assert result["persisted"] is True
        assert calls["meta"]["profile"] == "far_field"

    def test_set_meeting_mode_switches_and_restores(self):
        ingress, _ = _ingress()
        device = _FakeDevice(ingress=ingress)
        manager = _manager(device)
        from jarvis.integrations.voice_pe.normalizer import SpeechAwareNormalizer

        ingress.normalizer = SpeechAwareNormalizer()
        on = manager.set_meeting_mode(True)
        assert on["meeting_mode"] is True
        assert device.active_profile == "meeting"
        off = manager.set_meeting_mode(False)
        assert off["meeting_mode"] is False
        assert device.active_profile == "auto"

    def test_audio_settings_view_and_diag(self):
        device = _FakeDevice()
        manager = _manager(device)
        view = manager.audio_settings_view(device.device_id)
        assert view["profile"] == "auto"
        assert view["settings"]["settings"]
        diag = manager.diag(device.device_id)
        assert diag["diagnostics"]["noise_floor_db"] == -50.0
        assert diag["profile"] == "auto"

    def test_calibrate_persists_and_applies(self, monkeypatch):
        device = _FakeDevice(ingress=None)
        manager = _manager(device)

        class FakeWizard:
            def __init__(self, *args, **kwargs):
                pass

            async def run(self, timeout_s=120.0):
                return {
                    "measurements": {
                        "noise_floor_db": -50.0,
                        "speech_rms_db": -25.0,
                        "far_rms_db": -38.0,
                        "false_vad_rate": 0.0,
                        "far_voiced_ratio": 0.6,
                        "whisper_avg_logprob": -0.8,
                    },
                    "tuned": {"normalizer_enabled": True, "vad_aggressiveness": 2},
                }

        saved = {}

        def fake_save(mac, payload):
            saved[mac] = payload
            return True

        monkeypatch.setattr(
            "jarvis.integrations.voice_pe.manager.pe_config.save_calibration",
            fake_save,
        )
        monkeypatch.setattr(
            "jarvis.integrations.voice_pe.calibration.CalibrationWizard", FakeWizard
        )
        result = asyncio.run(
            manager.calibrate(device.device_id, "desk", timeout_s=10.0)
        )
        assert result["applied"] is True
        assert result["profile"] == "desk"
        assert saved[device.identity["mac_address"]]["tuned"]["vad_aggressiveness"] == 2
        assert saved[device.identity["mac_address"]]["profile"] == "desk"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class _CliManager:
    """Minimal manager stand-in for CLI tests (wraps a real manager)."""

    def __init__(self, manager):
        self._m = manager
        self.config = manager.config

    def device(self, key):
        return self._m.device(key)

    def set_profile(self, key, profile):
        return self._m.set_profile(key, profile)

    def audio_settings_view(self, key):
        return self._m.audio_settings_view(key)

    def diag(self, key):
        return self._m.diag(key)

    async def calibrate(self, key, profile=None, **kwargs):
        return await self._m.calibrate(key, profile, **kwargs)

    @property
    def devices(self):
        return self._m.devices


class TestCliAudioPipeline:
    def test_profile_read_and_write(self, capsys, monkeypatch):
        from jarvis.integrations.voice_pe import cli

        device = _FakeDevice()
        manager = _CliManager(_manager(device))
        monkeypatch.setattr(
            "jarvis.integrations.voice_pe.manager.pe_config.save_device_metadata",
            lambda mac, meta: True,
        )
        assert cli.handle(["profile", "living-room-pe", "meeting"], None, manager) == 0
        out = capsys.readouterr().out
        assert "meeting" in out
        assert cli.handle(["profile", "living-room-pe"], None, manager) == 0
        out = capsys.readouterr().out
        assert "Profile: meeting" in out

    def test_audio_settings_command(self, capsys):
        from jarvis.integrations.voice_pe import cli

        manager = _CliManager(_manager(_FakeDevice()))
        assert cli.handle(["audio-settings", ""], None, manager) == 0
        out = capsys.readouterr().out
        assert "profile auto" in out
        assert "VAD aggressiveness" in out
        assert "device-controlled" in out  # read-only device fields

    def test_diag_command(self, capsys):
        from jarvis.integrations.voice_pe import cli

        manager = _CliManager(_manager(_FakeDevice()))
        assert cli.handle(["diag", ""], None, manager) == 0
        out = capsys.readouterr().out
        assert "Diagnostics" in out
        assert "noise floor" in out

    def test_calibrate_command(self, capsys, monkeypatch):
        from jarvis.integrations.voice_pe import cli

        device = _FakeDevice()
        manager = _CliManager(_manager(device))

        class FakeWizard:
            def __init__(self, *args, **kwargs):
                pass

            async def run(self, timeout_s=120.0):
                return {
                    "measurements": {
                        "noise_floor_db": -50.0,
                        "speech_rms_db": -25.0,
                        "snr_db": 25.0,
                        "far_rms_db": -38.0,
                        "far_degradation_db": 13.0,
                        "false_vad_rate": 0.0,
                        "whisper_avg_logprob": -0.8,
                        "echo_leakage_db": 4.0,
                    },
                    "tuned": {
                        "normalizer_enabled": True,
                        "vad_aggressiveness": 2,
                        "whisper_post_roll_ms": 700,
                    },
                }

        monkeypatch.setattr(
            "jarvis.integrations.voice_pe.calibration.CalibrationWizard", FakeWizard
        )
        monkeypatch.setattr(
            "jarvis.integrations.voice_pe.manager.pe_config.save_calibration",
            lambda mac, payload: True,
        )
        assert (
            cli.handle(["calibrate", "living-room-pe", "--profile", "desk"],
                       None, manager)
            == 0
        )
        out = capsys.readouterr().out
        assert "noise floor: -50.0" in out
        assert "echo leakage: 4.0" in out
        assert "Tuned settings" in out


# ---------------------------------------------------------------------------
# coach meeting-mode wiring
# ---------------------------------------------------------------------------


class TestMeetingModeWiring:
    def test_coach_events_switch_meeting_mode(self, monkeypatch):
        import types as _types

        from jarvis.everywhere import broker as broker_mod

        device = _FakeDevice()
        manager = _manager(device)
        fake_daemon = _types.ModuleType("jarvis.daemon")
        fake_daemon.get_voice_pe_manager = lambda: manager
        monkeypatch.setitem(sys.modules, "jarvis.daemon", fake_daemon)
        b = broker_mod.EverywhereBroker(SimpleNamespace())
        b._queue_coach_event({"type": "coach_session_started"})
        assert device.active_profile == "meeting"
        b._queue_coach_event({"type": "coach_session_ended"})
        assert device.active_profile == "auto"
        # unrelated coach events do not touch the profile
        b._queue_coach_event({"type": "transcript", "speaker": "me", "text": "x"})
        assert device.active_profile == "auto"