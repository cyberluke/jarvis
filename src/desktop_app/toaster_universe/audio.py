"""Sparse event audio. Bounded mixer. Work quiets the kitchen."""

from __future__ import annotations

import math
import struct
import tempfile
import wave
from pathlib import Path

from .config import WorldConfig
from .signals import UserSignals
from .types import ActivityState, WorldEvent


SOUND_FOR_EVENT = {
    WorldEvent.TOAST_POP: "toast_pop",
    WorldEvent.TOAST_BURNT: "steam_hiss",
    WorldEvent.SLOT_OCCUPIED: "slot_click",
    WorldEvent.SLOT_FREE: "soft_boing",
    WorldEvent.PORTAL_OPEN: "portal_chime",
    WorldEvent.PORTAL_CLOSE: "portal_close",
    WorldEvent.KEYBOARD_SLICE: "crumb_tick",
    WorldEvent.POPULATION_INFESTATION: "crowd_murmur",
    WorldEvent.TOASTER_CLICK: "tiny_hop",
    WorldEvent.VOICE_SPEAK_START: "deadpan_voice",
    WorldEvent.SYSTEM_WAKE: "wake_chime",
    WorldEvent.SAFETY_YIELD: "tiny_fall",
}

NAMED_CUES = (
    "toast_pop",
    "tiny_hop",
    "crumb_tick",
    "steam_hiss",
    "portal_chime",
    "portal_close",
    "popcorn",
    "popcorn_pop",
    "microwave_ping",
    "wake_chime",
    "deadpan_voice",
    "slot_click",
    "crowd_murmur",
    "heart_plink",
    "soft_boing",
    "heat_hum",
    "rice_steam",
    "tiny_step",
    "tiny_fall",
)

CUE_SPECS = {
    "toast_pop": (620, 90, 0.18),
    "tiny_hop": (440, 50, 0.14),
    "crumb_tick": (980, 28, 0.12),
    "steam_hiss": (180, 160, 0.08),
    "portal_chime": (523, 180, 0.16),
    "portal_close": (392, 160, 0.14),
    "popcorn": (310, 70, 0.12),
    "popcorn_pop": (310, 70, 0.12),
    "microwave_ping": (880, 140, 0.16),
    "wake_chime": (659, 120, 0.14),
    "deadpan_voice": (196, 90, 0.08),
    "slot_click": (210, 40, 0.12),
    "crowd_murmur": (140, 220, 0.06),
    "heart_plink": (784, 70, 0.12),
    "soft_boing": (330, 70, 0.12),
    "heat_hum": (92, 240, 0.06),
    "rice_steam": (210, 180, 0.07),
    "tiny_step": (520, 22, 0.08),
    "tiny_fall": (180, 80, 0.12),
}

POOL_SIZE = 4


def _tone_wav(path: Path, freq: float, ms: int, volume: float = 0.22, decay: bool = True) -> None:
    rate = 22050
    n = int(rate * ms / 1000)
    frames = bytearray()
    for i in range(n):
        t = i / rate
        env = math.exp(-t * 8.0) if decay else max(0.0, 1.0 - t / (ms / 1000))
        # Two partials so cues are not pure sine stubs.
        sample = int(
            32767
            * volume
            * env
            * (
                0.72 * math.sin(2 * math.pi * freq * t)
                + 0.18 * math.sin(2 * math.pi * freq * 2.01 * t)
                + 0.10 * math.sin(2 * math.pi * freq * 0.5 * t)
            )
        )
        frames += struct.pack("<h", max(-32767, min(32767, sample)))
    with wave.open(str(path), "w") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(frames)


class AudioDirector:
    def __init__(self, cfg: WorldConfig | None = None) -> None:
        self.cfg = cfg or WorldConfig()
        self.last = ""
        self.queue: list[str] = []
        self.played: list[str] = []
        self.enabled = self.cfg.audio_enabled
        self._effects: dict[str, Path] = {}
        self._players: list = []
        self._cursor = 0
        self._ready = False
        self._last_cue_t: dict[str, float] = {}
        self._last_any_t = -1e9
        self.voice_ready_t = -1e9

    def prepare(self) -> None:
        if self._ready:
            return
        root = Path(tempfile.gettempdir()) / "toaster_universe_sfx"
        root.mkdir(parents=True, exist_ok=True)
        for name, (freq, ms, vol) in CUE_SPECS.items():
            path = root / f"{name}.wav"
            if not path.exists():
                _tone_wav(path, freq, ms, volume=vol, decay=name != "crowd_murmur")
            self._effects[name] = path
        self._ready = True

    def _gain(self, signals: UserSignals | None) -> float:
        if not self.enabled or not self.cfg.audio_enabled:
            return 0.0
        gain = self.cfg.audio_volume
        if signals is None:
            return gain
        if signals.activity in {ActivityState.CODING_FLOW, ActivityState.FOCUSED, ActivityState.FRANTIC, ActivityState.TERMINAL_HEAVY}:
            return 0.0 if signals.focus_score >= 0.55 else gain * 0.15
        if signals.dragging or signals.selecting or signals.scrolling:
            return 0.0
        return gain

    def on_event(self, name: WorldEvent, signals: UserSignals | None = None, t: float = 0.0) -> str | None:
        cue = SOUND_FOR_EVENT.get(name)
        if cue:
            self.play(cue, signals, t=t)
        return cue

    def play(self, cue: str, signals: UserSignals | None = None, t: float = 0.0) -> bool:
        if cue == "popcorn_pop":
            cue = "popcorn"
        if cue not in NAMED_CUES:
            return False
        gain = self._gain(signals)
        if gain <= 0.0:
            return False
        if t - self._last_any_t < 0.18:
            return False
        last_same = self._last_cue_t.get(cue, -1e9)
        if t - last_same < 0.45:
            return False
        if cue == "deadpan_voice" and t - self.voice_ready_t < self.cfg.voice_cooldown_sec:
            return False
        if cue in {"popcorn", "popcorn_pop"} and t - last_same < 4.0:
            return False
        while len(self.queue) >= 2:
            self.queue.pop(0)
        self.last = cue
        self.queue.append(cue)
        self._last_cue_t[cue] = t
        self._last_any_t = t
        if cue == "deadpan_voice":
            self.voice_ready_t = t
        self.prepare()
        path = self._effects.get(cue)
        if path is None:
            return False
        try:
            from PyQt6.QtMultimedia import QSoundEffect
            from PyQt6.QtCore import QUrl

            if len(self._players) < POOL_SIZE:
                self._players.append(QSoundEffect())
            player = self._players[self._cursor % len(self._players)]
            self._cursor += 1
            player.setSource(QUrl.fromLocalFile(str(path)))
            player.setVolume(gain)
            player.play()
            self.played.append(cue)
            return True
        except Exception:
            self.played.append(f"{cue}:queued")
            return False

    def pop(self) -> str | None:
        return self.queue.pop(0) if self.queue else None
