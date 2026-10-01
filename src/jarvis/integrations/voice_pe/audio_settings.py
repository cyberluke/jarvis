"""One typed settings model for the Voice PE audio pipeline.

Every host-side knob that Toastovač can actually apply lives here with its
metadata (unit, range, default, source layer, restart requirement, profile
scope). Device-side DSP fields (noise suppression, auto gain, volume
multiplier) are normalised from the ``audio_settings`` object the satellite
sends on every pipeline start and are shown read-only.

Profiles (``AUTO/DESK/ROOM/FAR_FIELD/MEETING``) are presets over the
profile-scoped fields. Resolution: per-device override > ``voice_pe_profile``
> ``AUTO`` (base config). See ``audio_pipeline.spec.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

#: Layer that consumes a setting. ``device`` fields are read-only reports.
LAYER_HOST = "host"
LAYER_LISTENER = "listener"
LAYER_WHISPER = "whisper"
LAYER_DEVICE = "device"
LAYERS = (LAYER_HOST, LAYER_LISTENER, LAYER_WHISPER, LAYER_DEVICE)

#: Profile names, in priority order.
PROFILE_AUTO = "auto"
PROFILE_DESK = "desk"
PROFILE_ROOM = "room"
PROFILE_FAR_FIELD = "far_field"
PROFILE_MEETING = "meeting"
PROFILES = (
    PROFILE_AUTO,
    PROFILE_DESK,
    PROFILE_ROOM,
    PROFILE_FAR_FIELD,
    PROFILE_MEETING,
)
PROFILE_LABELS = {
    PROFILE_AUTO: "Auto",
    PROFILE_DESK: "Desk",
    PROFILE_ROOM: "Room",
    PROFILE_FAR_FIELD: "Far field",
    PROFILE_MEETING: "Meeting",
}


@dataclass(frozen=True)
class SettingMeta:
    """Metadata of one profile-scoped host setting."""

    key: str
    label: str
    unit: str = ""
    min_val: Optional[float] = None
    max_val: Optional[float] = None
    default: Any = None
    source_layer: str = LAYER_HOST
    restart_required: bool = False
    profile_scope: bool = True


#: Profile-scoped knobs, in display order. Every key is consumed at runtime:
#: ``vad_*`` by the listener (WebRTC VAD + pre-roll), ``whisper_*`` by the
#: decode gates, ``normalizer_*`` by the per-device normalizer in the ingress
#: pump. ``whisper_language`` is not profile-scoped (per-utterance pair
#: transcription already adapts) and stays on the base config.
SETTING_METADATA: List[SettingMeta] = [
    SettingMeta("vad_aggressiveness", "VAD aggressiveness", "",
                0, 3, 2, LAYER_LISTENER),
    SettingMeta("vad_pre_roll_ms", "VAD pre-roll", "ms",
                0, 2000, 240, LAYER_LISTENER),
    SettingMeta("whisper_post_roll_ms", "Whisper post-roll", "ms",
                0, 3000, 400, LAYER_WHISPER),
    SettingMeta("whisper_min_avg_logprob", "Whisper min avg logprob", "",
                -2.0, 0.0, -0.7, LAYER_WHISPER),
    SettingMeta("whisper_no_speech_threshold", "Whisper no-speech threshold", "",
                0.0, 1.0, 0.5, LAYER_WHISPER),
    SettingMeta("normalizer_enabled", "Speech-aware normalizer", "",
                None, None, False, LAYER_HOST),
    SettingMeta("normalizer_target_db", "Normalizer target level", "dBFS",
                -40.0, -12.0, -28.0, LAYER_HOST),
    SettingMeta("normalizer_max_gain_db", "Normalizer max gain", "dB",
                0.0, 18.0, 9.0, LAYER_HOST),
    SettingMeta("normalizer_attack_db_per_s", "Normalizer gain rate", "dB/s",
                0.5, 12.0, 3.0, LAYER_HOST),
    SettingMeta("normalizer_limiter_db", "Normalizer limiter", "dBFS",
                -6.0, 0.0, -1.0, LAYER_HOST),
]

#: Profile presets. ``AUTO`` inherits the base config (empty overrides).
PROFILE_PRESETS: Dict[str, Dict[str, Any]] = {
    PROFILE_AUTO: {},
    PROFILE_DESK: {
        "vad_aggressiveness": 2,
        "vad_pre_roll_ms": 240,
        "whisper_post_roll_ms": 400,
        "normalizer_enabled": True,
        "normalizer_target_db": -26.0,
        "normalizer_max_gain_db": 6.0,
        "normalizer_attack_db_per_s": 3.0,
        "normalizer_limiter_db": -1.0,
    },
    PROFILE_ROOM: {
        "vad_aggressiveness": 1,
        "vad_pre_roll_ms": 320,
        "whisper_post_roll_ms": 600,
        "normalizer_enabled": True,
        "normalizer_target_db": -28.0,
        "normalizer_max_gain_db": 9.0,
        "normalizer_attack_db_per_s": 2.5,
        "normalizer_limiter_db": -1.0,
    },
    PROFILE_FAR_FIELD: {
        "vad_aggressiveness": 0,
        "vad_pre_roll_ms": 400,
        "whisper_post_roll_ms": 900,
        "normalizer_enabled": True,
        "normalizer_target_db": -32.0,
        "normalizer_max_gain_db": 12.0,
        "normalizer_attack_db_per_s": 2.0,
        "normalizer_limiter_db": -1.5,
    },
    PROFILE_MEETING: {
        "vad_aggressiveness": 0,
        "vad_pre_roll_ms": 480,
        "whisper_post_roll_ms": 1400,
        "whisper_min_avg_logprob": -1.1,
        "whisper_no_speech_threshold": 0.6,
        "normalizer_enabled": True,
        "normalizer_target_db": -30.0,
        "normalizer_max_gain_db": 9.0,
        "normalizer_attack_db_per_s": 2.0,
        "normalizer_limiter_db": -1.5,
    },
}

#: Keys a calibration can tune (a subset of the profile-scoped fields).
TUNEABLE_KEYS = (
    "vad_aggressiveness",
    "vad_pre_roll_ms",
    "whisper_post_roll_ms",
    "whisper_min_avg_logprob",
    "normalizer_enabled",
    "normalizer_target_db",
    "normalizer_max_gain_db",
    "normalizer_attack_db_per_s",
    "normalizer_limiter_db",
)

#: On-device noise suppression levels (ESPHome ``VoiceAssistantAudioSettings``).
NOISE_SUPPRESSION_NAMES = {
    0: "none",
    1: "low",
    2: "medium",
    3: "high",
}


@dataclass
class VoicePEAudioSettings:
    """Runtime view of the host-side audio knobs for one device.

    Values are resolved from the base settings + active profile + per-device
    calibration overrides. ``unsupported`` lists knobs a backend rejected
    (e.g. WebRTC VAD unavailable), with reasons.
    """

    profile: str = PROFILE_AUTO
    values: Dict[str, Any] = field(default_factory=dict)
    unsupported: Dict[str, str] = field(default_factory=dict)
    source: str = "base"  # "base" | "profile:<name>" | "calibration:<mac>"

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def view(self) -> Dict[str, Any]:
        """Flat view for UI/diagnostics: metadata + value + layer + state."""
        out: Dict[str, Any] = {"profile": self.profile, "settings": []}
        for meta in SETTING_METADATA:
            row = {
                "key": meta.key,
                "label": meta.label,
                "unit": meta.unit,
                "min": meta.min_val,
                "max": meta.max_val,
                "default": meta.default,
                "layer": meta.source_layer,
                "value": self.values.get(meta.key, meta.default),
                "state": "unsupported" if meta.key in self.unsupported else "active",
                "reason": self.unsupported.get(meta.key),
            }
            out["settings"].append(row)
        out["unsupported"] = dict(self.unsupported)
        return out


def normalize_profile_name(name: Any) -> str:
    """Lowercase and validate a profile name; unknown names fall back to AUTO."""
    raw = str(name or "").strip().lower().replace("-", "_")
    return raw if raw in PROFILES else PROFILE_AUTO


def profile_preset(profile: str) -> Dict[str, Any]:
    """The preset of one profile (``{}`` for AUTO)."""
    return dict(PROFILE_PRESETS.get(normalize_profile_name(profile), {}))


def resolve_audio_settings(
    base: Dict[str, Any],
    profile: str = PROFILE_AUTO,
    calibration: Optional[Dict[str, Any]] = None,
) -> VoicePEAudioSettings:
    """Merge base config + profile preset + calibration overrides.

    ``base`` carries the configured defaults for every metadata key
    (``voice_pe_*`` for normalizer knobs, plain keys for the listener/whisper
    knobs, as the shared settings object spells them).
    """
    profile = normalize_profile_name(profile)
    values: Dict[str, Any] = {}
    for meta in SETTING_METADATA:
        values[meta.key] = base.get(meta.key, meta.default)
    for key, value in profile_preset(profile).items():
        values[key] = value
    source = f"profile:{profile}" if profile != PROFILE_AUTO else "base"
    for key, value in dict(calibration or {}).items():
        if key in SETTING_KEYS() and key in TUNEABLE_KEYS:
            values[key] = value
            source = f"calibration:{key}" if source == "base" else source
    return VoicePEAudioSettings(profile=profile, values=values, source=source)


def SETTING_KEYS() -> set:
    return {meta.key for meta in SETTING_METADATA}


def normalize_device_settings(audio_settings: Any) -> Dict[str, Any]:
    """Normalise the satellite-reported ``VoiceAssistantAudioSettings``.

    Read-only: the Native API has no setter for these fields, the device
    YAML owns them. Every field is defensive: when the payload lacks it, the
    row says ``not reported`` instead of inventing a value.
    """
    raw = audio_settings if audio_settings is not None else None
    out: Dict[str, Any] = {
        "reported": raw is not None,
        "controllable": False,
        "layer": LAYER_DEVICE,
    }
    level = getattr(raw, "noise_suppression_level", None) if raw is not None else None
    if level is None:
        out["noise_suppression_level"] = {"value": None, "display": "not reported"}
    else:
        try:
            level = int(level)
            display = NOISE_SUPPRESSION_NAMES.get(level, f"unknown({level})")
        except (TypeError, ValueError):
            display = "not reported"
        out["noise_suppression_level"] = {"value": level, "display": display}
    gain = getattr(raw, "auto_gain", None) if raw is not None else None
    if gain is None:
        out["auto_gain"] = {"value": None, "display": "not reported"}
    else:
        try:
            gain = int(gain)
            display = "disabled" if gain < 0 else f"{gain} dBFS"
        except (TypeError, ValueError):
            display = "not reported"
        out["auto_gain"] = {"value": gain, "display": display}
    multiplier = (
        getattr(raw, "volume_multiplier", None) if raw is not None else None
    )
    if multiplier is None:
        out["volume_multiplier"] = {"value": None, "display": "not reported"}
    else:
        try:
            multiplier = float(multiplier)
            display = f"{multiplier:.2f}x"
        except (TypeError, ValueError):
            display = "not reported"
        out["volume_multiplier"] = {"value": multiplier, "display": display}
    return out


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def db_to_linear(db: float) -> float:
    return 10.0 ** (float(db) / 20.0)


def linear_to_db(value: float) -> float:
    if value is None or value <= 0.0:
        return -120.0
    return 20.0 * __import__("math").log10(float(value))