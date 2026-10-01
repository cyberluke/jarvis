"""Speech-aware post normalizer for the delivered Voice PE signal.

Sits on the float32 frames in the ingress pump, after the single
bytes->float32 conversion and before the listener queue (see
``audio_pipeline.spec.md``).

Behaviour:

```text
silence -> do not learn gain (noise-floor EMA continues, gain frozen)
speech  -> estimate level
           slowly adjust gain toward target (bounded rate)
           clamp maximum gain
           soft limiter at the configured ceiling
```

Never blind permanent gain: the gain only moves while speech is present and
is clamped in magnitude and rate.
"""

from __future__ import annotations

from typing import Optional

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]

from .audio_settings import clamp, db_to_linear, linear_to_db

#: A frame is speech when it clears the noise floor by this margin.
SPEECH_OVER_FLOOR_DB = 6.0
#: Absolute floor below which a frame is never speech.
ABSOLUTE_FLOOR_DB = -55.0
#: Noise-floor EMA coefficient per frame (slow).
NOISE_FLOOR_ALPHA = 0.01
#: Speech-level EMA coefficient per frame.
SPEECH_LEVEL_ALPHA = 0.05


class SpeechAwareNormalizer:
    """Per-device gain stage with silence-aware adaptation and a limiter.

    Stateless across frames apart from the adaptive state; frame-in,
    frame-out with the same shape.
    """

    def __init__(
        self,
        *,
        target_db: float = -28.0,
        max_gain_db: float = 9.0,
        attack_db_per_s: float = 3.0,
        limiter_db: float = -1.0,
        enabled: bool = True,
        sample_rate: int = 16000,
        frame_samples: int = 512,
    ) -> None:
        self.target_db = float(target_db)
        self.max_gain_db = float(max_gain_db)
        self.attack_db_per_s = float(attack_db_per_s)
        self.limiter_db = float(limiter_db)
        self.enabled = bool(enabled)
        self._sample_rate = max(1000, int(sample_rate))
        self._frame_samples = max(1, int(frame_samples))
        self._dt_s = self._frame_samples / self._sample_rate
        self._gain_db = 0.0
        self._noise_floor_db: Optional[float] = None
        self._speech_level_db: Optional[float] = None
        self._stats = {
            "frames_processed": 0,
            "speech_frames": 0,
            "silence_frames": 0,
            "clipped_samples": 0,
            "peak_db": -120.0,
            "last_rms_db": -120.0,
        }

    # -- runtime --------------------------------------------------------------

    def process(self, samples) -> "object":
        """Apply gain + limiter to one float32 frame; returns the same shape.

        When disabled (or numpy unavailable) the frame passes through
        untouched and no state changes.
        """
        if not self.enabled or np is None:
            return samples
        arr = np.asarray(samples, dtype=np.float32)
        if arr.size == 0:
            return samples
        rms = float(np.sqrt(np.mean(np.square(arr))))
        rms_db = linear_to_db(rms)
        peak = float(np.max(np.abs(arr))) if arr.size else 0.0
        self._stats["last_rms_db"] = rms_db
        self._stats["peak_db"] = max(float(self._stats["peak_db"]), linear_to_db(peak))
        self._stats["frames_processed"] = int(self._stats["frames_processed"]) + 1

        floor = self._noise_floor_db
        is_speech = (
            rms_db > ABSOLUTE_FLOOR_DB
            and (floor is None or rms_db > floor + SPEECH_OVER_FLOOR_DB)
        )
        if is_speech:
            self._stats["speech_frames"] = int(self._stats["speech_frames"]) + 1
            # Speech level EMA for diagnostics and calibration.
            if self._speech_level_db is None:
                self._speech_level_db = rms_db
            else:
                self._speech_level_db += SPEECH_LEVEL_ALPHA * (
                    rms_db - self._speech_level_db
                )
            # Slowly adjust gain toward the target, rate-bounded.
            target_gain = clamp(self.target_db - rms_db, 0.0, self.max_gain_db)
            max_step = self.attack_db_per_s * self._dt_s
            step = clamp(target_gain - self._gain_db, -max_step, max_step)
            self._gain_db = clamp(self._gain_db + step, 0.0, self.max_gain_db)
        else:
            # Silence: the gain is frozen (never learned from silence), only
            # the noise floor keeps adapting.
            self._stats["silence_frames"] = int(self._stats["silence_frames"]) + 1
            if floor is None:
                self._noise_floor_db = rms_db
            else:
                self._noise_floor_db = floor + NOISE_FLOOR_ALPHA * (rms_db - floor)

        if self._gain_db > 0.0:
            arr = arr * db_to_linear(self._gain_db)
        if self.limiter_db > -120.0 and self.limiter_db < 0.0:
            ceiling = db_to_linear(self.limiter_db)
            if ceiling > 0.0:
                arr = np.tanh(arr / ceiling) * ceiling
        clipped = int(np.count_nonzero(np.abs(arr) >= 0.999))
        if clipped:
            self._stats["clipped_samples"] = (
                int(self._stats["clipped_samples"]) + clipped
            )
        return arr

    def reset(self) -> None:
        """Forget the adaptive state (used on stream/generation changes)."""
        self._gain_db = 0.0
        self._noise_floor_db = None
        self._speech_level_db = None

    def apply_view(self, **overrides) -> None:
        """Apply a resolved settings view in place (values keyed by metadata key)."""
        mapping = {
            "normalizer_target_db": "target_db",
            "normalizer_max_gain_db": "max_gain_db",
            "normalizer_attack_db_per_s": "attack_db_per_s",
            "normalizer_limiter_db": "limiter_db",
            "normalizer_enabled": "enabled",
        }
        for meta_key, attr in mapping.items():
            if meta_key in overrides:
                setattr(self, attr, float(overrides[meta_key]))
        self.enabled = bool(overrides.get("normalizer_enabled", self.enabled))

    # -- diagnostics ----------------------------------------------------------

    def metrics(self) -> dict:
        """Live state: effective gain, floor, levels, clipping, counts."""
        total = int(self._stats["frames_processed"])
        return {
            "enabled": self.enabled,
            "gain_db": round(self._gain_db, 3),
            "target_db": self.target_db,
            "max_gain_db": self.max_gain_db,
            "limiter_db": self.limiter_db,
            "noise_floor_db": (
                None if self._noise_floor_db is None else round(self._noise_floor_db, 2)
            ),
            "speech_level_db": (
                None if self._speech_level_db is None else round(self._speech_level_db, 2)
            ),
            "last_rms_db": round(self._stats["last_rms_db"], 2),
            "peak_db": round(self._stats["peak_db"], 2),
            "clipping_ratio": (
                round(
                    int(self._stats["clipped_samples"])
                    / max(1, total * self._frame_samples),
                    6,
                )
                if total
                else 0.0
            ),
            "speech_frames": self._stats["speech_frames"],
            "silence_frames": self._stats["silence_frames"],
            "frames_processed": total,
        }