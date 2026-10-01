"""Auto-calibration of the Voice PE audio pipeline.

Measurement core (``CalibrationSession``) is pure and testable: it consumes
delivered float32 blocks and accumulates RMS/peak/clipping/voiced ratios.
The wizard (``CalibrationWizard``) orchestrates the five steps against a
live device: it attaches a temporary sink to the device's ``AudioIngress``,
instructs the user, collects windows, reads the listener's ``last_segment``
for Whisper confidence, plays the echo sample through the announce path and
tunes conservative settings (``tune_settings``). See
``audio_pipeline.spec.md``.
"""

from __future__ import annotations

import math
import time
from typing import Any, Callable, Dict, Optional

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]

from .audio_settings import (
    TUNEABLE_KEYS,
    clamp,
    linear_to_db,
)

#: Sample phrase for the echo step (played through the device speaker).
ECHO_SAMPLE_TEXT = "Jedna, dva, tři. Kalibrační tón."

#: A frame is "voiced" for the false-VAD rate when it clears the floor by this.
VOICED_OVER_FLOOR_DB = 6.0


def rms_db(samples) -> float:
    """RMS level of one block in dBFS (``-120`` for silence)."""
    if np is None or samples is None or getattr(samples, "size", 0) == 0:
        return -120.0
    arr = np.asarray(samples, dtype=np.float32)
    rms = float(np.sqrt(np.mean(np.square(arr))))
    return linear_to_db(rms)


def peak_db(samples) -> float:
    if np is None or samples is None or getattr(samples, "size", 0) == 0:
        return -120.0
    arr = np.asarray(samples, dtype=np.float32)
    return linear_to_db(float(np.max(np.abs(arr))))


class CalibrationSession:
    """Accumulates one measurement window from delivered blocks.

    ``is_voiced`` is a pluggable per-block speech classifier (WebRTC VAD in
    the wizard, a fake in tests); ``None`` falls back to an energy gate over
    the running noise floor.
    """

    def __init__(
        self,
        label: str,
        is_voiced: Optional[Callable[[Any], bool]] = None,
    ) -> None:
        self.label = label
        self._is_voiced = is_voiced
        self._rms_sum = 0.0
        self._rms_count = 0
        self._peak = -120.0
        self._clipped_samples = 0
        self._total_samples = 0
        self._voiced_frames = 0
        self._frames = 0
        self._noise_floor_db: Optional[float] = None
        self._first_at = 0.0
        self._last_at = 0.0

    def add_block(self, samples, at: Optional[float] = None) -> None:
        if np is None or getattr(samples, "size", 0) == 0:
            return
        arr = np.asarray(samples, dtype=np.float32)
        now = at if at is not None else time.monotonic()
        if self._first_at == 0.0:
            self._first_at = now
        self._last_at = now
        level = rms_db(arr)
        self._rms_sum += level
        self._rms_count += 1
        self._peak = max(self._peak, peak_db(arr))
        total = int(arr.size)
        self._total_samples += total
        self._clipped_samples += int(np.count_nonzero(np.abs(arr) >= 0.999))
        voiced = False
        if self._is_voiced is not None:
            voiced = bool(self._is_voiced(arr))
        else:
            floor = self._noise_floor_db
            voiced = (
                level > -55.0
                and (floor is None or level > floor + VOICED_OVER_FLOOR_DB)
            )
            if not voiced:
                self._noise_floor_db = (
                    level if floor is None else floor + 0.05 * (level - floor)
                )
        if voiced:
            self._voiced_frames += 1
        self._frames += 1

    def snapshot(self) -> Dict[str, Any]:
        """Window metrics; all None/0.0 fields stay honest for empty windows."""
        frames = self._frames
        return {
            "label": self.label,
            "duration_s": round(self._last_at - self._first_at, 2) if frames else 0.0,
            "frames": frames,
            "rms_db": round(self._rms_sum / frames, 2) if frames else None,
            "peak_db": round(self._peak, 2) if frames else None,
            "clipping_ratio": (
                round(self._clipped_samples / max(1, self._total_samples), 6)
                if frames
                else 0.0
            ),
            "voiced_ratio": (
                round(self._voiced_frames / frames, 4) if frames else 0.0
            ),
            "noise_floor_db": (
                None if self._noise_floor_db is None else round(self._noise_floor_db, 2)
            ),
        }


def _transcript_metrics(device) -> Dict[str, Any]:
    """Whisper confidence of the device's last utterance, when available."""
    last = getattr(device, "metrics", {}).get("last_segment") or {}
    if not last:
        return {"avg_logprob": None, "no_speech_prob": None, "text": None}
    return {
        "avg_logprob": last.get("avg_logprob"),
        "no_speech_prob": last.get("no_speech_prob"),
        "text": last.get("text"),
        "voiced_ratio": (
            round(
                int(last.get("voiced_frame_count") or 0)
                / max(1, int(last.get("total_frame_count") or 0)),
                4,
            )
            if last.get("total_frame_count")
            else None
        ),
    }


def tune_settings(
    measurements: Dict[str, Any],
    base: Dict[str, Any],
) -> Dict[str, Any]:
    """Conservative auto-tune from the five-step measurements.

    Rules (all bounded by the metadata ranges):

    - normalizer target sits 18 dB above the noise floor, never above the
      observed speech level + 4 dB, clamped to [-40, -20];
    - max gain covers the far-field degradation plus 6 dB headroom,
      clamped to [3, 12];
    - VAD aggressiveness rises (stricter) when the false-VAD rate is high
      and falls (more sensitive) when far-field voiced ratio collapses;
    - post-roll grows with far-field voiced-ratio loss, capped at 1500 ms;
    - ``whisper_min_avg_logprob`` is relaxed only to the observed confidence
      minus headroom, floored at -1.5;
    - the normalizer is enabled when far-field speech sits below the target.
    """
    noise_floor = measurements.get("noise_floor_db")
    speech = measurements.get("speech_rms_db")
    far = measurements.get("far_rms_db")
    false_vad = measurements.get("false_vad_rate", 0.0)
    far_voiced = measurements.get("far_voiced_ratio", 0.5)
    avg_logprob = measurements.get("whisper_avg_logprob")

    target_db = float(base.get("normalizer_target_db", -28.0))
    max_gain = float(base.get("normalizer_max_gain_db", 9.0))
    aggressiveness = int(base.get("vad_aggressiveness", 2))
    post_roll = int(base.get("whisper_post_roll_ms", 400))
    min_logprob = float(base.get("whisper_min_avg_logprob", -0.7))
    normalizer_on = bool(base.get("normalizer_enabled", False))

    if noise_floor is not None:
        target_db = clamp(noise_floor + 18.0, -40.0, -20.0)
    if speech is not None:
        target_db = min(target_db, speech + 4.0)
    if far is not None and speech is not None:
        degradation = speech - far  # positive = quieter at distance
        max_gain = clamp(degradation + 6.0, 3.0, 12.0)
        if far < target_db - 6.0:
            normalizer_on = True
    if false_vad >= 0.05:
        aggressiveness = min(3, aggressiveness + 1)
    if far_voiced < 0.4:
        aggressiveness = max(0, aggressiveness - 1)
        post_roll = min(1500, post_roll + 300)
    if far_voiced < 0.25:
        post_roll = min(1500, post_roll + 300)
    if avg_logprob is not None:
        relaxed = float(avg_logprob) - 0.3
        if relaxed < min_logprob:
            min_logprob = clamp(relaxed, -1.5, 0.0)

    tuned: Dict[str, Any] = {
        "normalizer_enabled": normalizer_on,
        "normalizer_target_db": round(clamp(target_db, -40.0, -12.0), 1),
        "normalizer_max_gain_db": round(clamp(max_gain, 0.0, 18.0), 1),
        "vad_aggressiveness": int(clamp(aggressiveness, 0, 3)),
        "vad_pre_roll_ms": int(clamp(post_roll // 2, 0, 2000)),
        "whisper_post_roll_ms": int(clamp(post_roll, 0, 3000)),
        "whisper_min_avg_logprob": round(clamp(min_logprob, -2.0, 0.0), 3),
    }
    return {k: v for k, v in tuned.items() if k in TUNEABLE_KEYS}


class CalibrationWizard:
    """Five-step semi-automated wizard against one live device."""

    def __init__(
        self,
        manager: Any,
        device: Any,
        *,
        window_s: float = 5.0,
        prompt: Optional[Callable[[str], None]] = None,
        is_voiced: Optional[Callable[[Any], bool]] = None,
        echo_text: str = ECHO_SAMPLE_TEXT,
    ) -> None:
        self._manager = manager
        self._device = device
        self._window_s = max(1.0, float(window_s))
        self._prompt = prompt or (lambda text: print(text, flush=True))
        self._is_voiced = is_voiced
        self._echo_text = echo_text
        self._ingress = getattr(device, "audio_ingress", None)

    # -- wizard --------------------------------------------------------------

    async def run(self, timeout_s: float = 120.0) -> Dict[str, Any]:
        """Run the five steps and return measurements + tuned settings."""
        self._prompt("🎙️ Calibration: press the centre button for each step.")
        silence = await self._collect("silence", self._window_s, timeout_s)
        speech = await self._collect("speech", self._window_s, timeout_s)
        far = await self._collect("far", self._window_s, timeout_s)
        echo = await self._echo_check(timeout_s)
        transcript = _transcript_metrics(self._device)

        noise_floor = silence.get("noise_floor_db") or silence.get("rms_db")
        snr = (
            round(speech["rms_db"] - noise_floor, 2)
            if speech.get("rms_db") is not None and noise_floor is not None
            else None
        )
        far_degradation = (
            round(speech["rms_db"] - far["rms_db"], 2)
            if speech.get("rms_db") is not None and far.get("rms_db") is not None
            else None
        )
        measurements: Dict[str, Any] = {
            "noise_floor_db": noise_floor,
            "peak_noise_db": silence.get("peak_db"),
            "false_vad_rate": silence.get("voiced_ratio", 0.0),
            "silence_frames": silence.get("frames"),
            "speech_rms_db": speech.get("rms_db"),
            "speech_peak_db": speech.get("peak_db"),
            "speech_voiced_ratio": speech.get("voiced_ratio"),
            "speech_clipping_ratio": speech.get("clipping_ratio"),
            "snr_db": snr,
            "far_rms_db": far.get("rms_db"),
            "far_peak_db": far.get("peak_db"),
            "far_voiced_ratio": far.get("voiced_ratio"),
            "far_clipping_ratio": far.get("clipping_ratio"),
            "far_degradation_db": far_degradation,
            "whisper_avg_logprob": transcript.get("avg_logprob"),
            "whisper_no_speech_prob": transcript.get("no_speech_prob"),
            "transcript_text": transcript.get("text"),
            "transcript_voiced_ratio": transcript.get("voiced_ratio"),
            "echo_leakage_db": echo.get("leakage_db"),
            "echo_aec_state": echo.get("aec_state"),
            "echo_reference_active": echo.get("reference_active"),
        }
        base = {
            "normalizer_target_db": -28.0,
            "normalizer_max_gain_db": 9.0,
            "vad_aggressiveness": 2,
            "whisper_post_roll_ms": 400,
            "whisper_min_avg_logprob": -0.7,
            "normalizer_enabled": False,
        }
        tuned = tune_settings(measurements, base)
        return {
            "measurements": measurements,
            "tuned": tuned,
            "profile": "",
            "applied": False,
        }

    # -- steps ----------------------------------------------------------------

    async def _collect(
        self, label: str, window_s: float, timeout_s: float
    ) -> Dict[str, Any]:
        """One button-press window: collect until the window elapses."""
        if self._ingress is None:
            return CalibrationSession(label).snapshot()
        instructions = {
            "silence": "🔇 Press the button and keep quiet for "
            f"{int(window_s)} seconds.",
            "speech": f"🎤 Press the button and speak normally for {int(window_s)} seconds.",
            "far": f"📏 Press the button and speak from a distance (far field) for "
            f"{int(window_s)} seconds.",
        }
        self._prompt(instructions.get(label, label))
        session = CalibrationSession(label, is_voiced=self._is_voiced)
        collector = _SinkCollector(session)
        self._ingress.attach_sink(collector)
        try:
            deadline = time.monotonic() + max(timeout_s, window_s + 5.0)
            while time.monotonic() < deadline:
                if session.snapshot()["duration_s"] >= window_s:
                    break
                await _sleep(0.05)
        finally:
            self._ingress.detach_sink(collector)
        return session.snapshot()

    async def _echo_check(self, timeout_s: float) -> Dict[str, Any]:
        """Play the sample, then measure mic level after playback + AEC lane.

        Stock firmware serialises microphone and TTS, so the mic cannot
        stream while the sample plays; the check therefore measures the
        post-playback residue against the noise floor and reports the host
        AEC lane state (``aec_state``/``reference_active``). When the device
        cannot play, the step reports ``unavailable``.
        """
        out: Dict[str, Any] = {"leakage_db": None, "aec_state": None,
                               "reference_active": None}
        try:
            played = await self._manager.announce(
                self._device.device_id, self._echo_text, start_conversation=False
            )
        except Exception as exc:  # pragma: no cover - device dependent
            out["unavailable"] = f"announce failed: {exc}"
            return out
        if not played:
            out["unavailable"] = "no speaker/media path on the device"
            return out
        # Post-playback window: residual leakage above the noise floor.
        session = CalibrationSession("echo", is_voiced=self._is_voiced)
        collector = _SinkCollector(session)
        if self._ingress is not None:
            self._ingress.attach_sink(collector)
        try:
            deadline = time.monotonic() + max(10.0, timeout_s)
            while time.monotonic() < deadline:
                if session.snapshot()["duration_s"] >= min(3.0, self._window_s):
                    break
                await _sleep(0.05)
        finally:
            if self._ingress is not None:
                self._ingress.detach_sink(collector)
        snap = session.snapshot()
        floor = getattr(self._ingress, "normalizer", None)
        floor_metrics = floor.metrics() if floor is not None else {}
        noise_floor = floor_metrics.get("noise_floor_db")
        if snap.get("rms_db") is not None and noise_floor is not None:
            out["leakage_db"] = round(snap["rms_db"] - noise_floor, 2)
        status = self._ingress.source_status(None) if self._ingress is not None else {}
        out["aec_state"] = status.get("aec_state")
        out["reference_active"] = status.get("reference_active")
        return out


class _SinkCollector:
    """Temporary ingress sink feeding one CalibrationSession."""

    def __init__(self, session: CalibrationSession) -> None:
        self._session = session

    def on_frame(self, frame) -> None:
        payload = getattr(frame, "samples", None)
        if payload is None:
            return
        if isinstance(payload, (bytes, bytearray)):
            if np is None:  # pragma: no cover
                return
            arr = np.frombuffer(payload, dtype=np.int16).astype(np.float32) / 32768.0
        else:
            arr = payload
        self._session.add_block(arr)

    def on_stream_start(self, stream) -> None:
        pass

    def on_stream_end(self, stream) -> None:
        pass


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)