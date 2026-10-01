"""Passive keyboard games. The user keeps working; cadence is the controller."""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import WorldConfig
from .entities import Entity
from .signals import UserSignals
from .types import KeyboardMode, SAFETY_STATES, ToastState, Vec2, WorldEvent


@dataclass
class KeyboardGameState:
    mode: KeyboardMode = KeyboardMode.NONE
    combo: int = 0
    sliced: int = 0
    last_slice_t: float = -1e9


class KeyboardGames:
    def __init__(self, cfg: WorldConfig) -> None:
        self.cfg = cfg
        self.state = KeyboardGameState()
        self._burst_run = 0
        self._last_keyish = False

    def mode_for(self, signals: UserSignals, population: int) -> KeyboardMode:
        if not self.cfg.keyboard_games_enabled:
            return KeyboardMode.NONE
        if signals.selecting or (getattr(signals, "mouse_velocity", 99) < 8.0 and signals.focus_score >= 0.55):
            return KeyboardMode.NONE
        if signals.typing_rate >= self.cfg.typing_fast_threshold and population >= 4:
            return KeyboardMode.FRUIT_NINJA_FEED
        if signals.typing_regularity > 0.62 and signals.typing_rate > 3.0:
            return KeyboardMode.GUITAR_HERO_RHYTHM
        if signals.terminal_activity > 0.7:
            return KeyboardMode.TERMINAL_FEEDING_FRENZY
        if signals.typing_burstiness > 0.7:
            return KeyboardMode.BURST_YEET
        if 0.0 < signals.typing_rate < 2.0:
            return KeyboardMode.QUIET_TAP
        if population >= 8:
            return KeyboardMode.COMBO_CLEANUP
        return KeyboardMode.NONE

    def tick(self, t: float, signals: UserSignals, entities: list[Entity]) -> list[tuple[WorldEvent, Entity]]:
        self.state.mode = self.mode_for(signals, len(entities))
        events: list[tuple[WorldEvent, Entity]] = []
        if signals.dragging or signals.selecting or signals.scrolling or signals.focus_score >= self.cfg.focus_threshold:
            self.state.mode = KeyboardMode.NONE
            self.state.combo = 0
            self._burst_run = 0
            return events
        typing = signals.typing_rate >= 0.4
        if typing:
            self._burst_run += 1
        else:
            self._burst_run = 0
        if self.state.mode is KeyboardMode.NONE or signals.typing_rate < 0.4:
            self.state.combo = max(0, self.state.combo - 1)
            return events
        if t - self.state.last_slice_t < 0.16:
            return events
        target = None
        best = 1e9
        for entity in entities:
            if entity.hidden or entity.state in SAFETY_STATES or entity.state in {
                ToastState.TOASTING,
                ToastState.JUMP_INTO_SLOT,
                ToastState.OVERTOASTING,
                ToastState.INSIDE_APPLIANCE,
            }:
                continue
            dist = (entity.pos - signals.cursor).length()
            if dist < 90:
                continue
            if dist < best and dist < 280:
                best = dist
                target = entity
        if target is None:
            return events
        self.state.last_slice_t = t
        rate_w = min(1.0, signals.typing_rate / max(1.0, self.cfg.typing_fast_threshold))
        regular_w = max(0.0, signals.typing_regularity)
        burst_w = min(1.0, self._burst_run / 8.0)
        energy = 1 + int(rate_w * 2 + regular_w * 2 + burst_w * 3)
        self.state.combo += energy
        self.state.sliced += 1
        if self.state.mode in {KeyboardMode.FRUIT_NINJA_FEED, KeyboardMode.COMBO_CLEANUP, KeyboardMode.BURST_YEET}:
            target.vel = Vec2(target.vel.x * 0.2, -220)
            target.proposed = ToastState.POP_OUT
            events.append((WorldEvent.KEYBOARD_SLICE, target))
        elif self.state.mode is KeyboardMode.GUITAR_HERO_RHYTHM:
            target.proposed = ToastState.DANCE
        elif self.state.mode is KeyboardMode.QUIET_TAP:
            target.proposed = ToastState.PEEK
        elif self.state.mode is KeyboardMode.TERMINAL_FEEDING_FRENZY:
            target.proposed = ToastState.TERMINAL_AWE
        return events
