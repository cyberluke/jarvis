"""Short-video dopamine layer. Observe → hearts → audience → popcorn → deadpan."""

from __future__ import annotations

from enum import Enum

from .config import WorldConfig
from .entities import Entity
from .signals import UserSignals
from .types import ActivityState, ToastState, Vec2


class DoomPhase(str, Enum):
    OBSERVE = "OBSERVE"
    HEARTS = "HEARTS"
    AUDIENCE_GATHERS = "AUDIENCE_GATHERS"
    CHAIRS_APPEAR = "CHAIRS_APPEAR"
    POPCORN_STARTS = "POPCORN_STARTS"
    MAIN_TOASTER_JOINS = "MAIN_TOASTER_JOINS"
    DEADPAN_COMMENT = "DEADPAN_COMMENT"
    COOLDOWN = "COOLDOWN"


class DoomscrollLayer:
    def __init__(self, cfg: WorldConfig) -> None:
        self.cfg = cfg
        self.phase = DoomPhase.OBSERVE
        self.entered = 0.0
        self.comment = ""
        self.chairs: list[Vec2] = []
        self.toaster_watching = False

    def tick(self, t: float, signals: UserSignals, entities: list[Entity]) -> DoomPhase:
        if not self.cfg.doomscroll_layer_enabled:
            self.phase = DoomPhase.OBSERVE
            return self.phase
        active = signals.activity is ActivityState.DOOMSCROLLING
        elapsed = t - self.entered if self.entered else 0.0
        if not active:
            if self.phase is not DoomPhase.OBSERVE:
                self.phase = DoomPhase.COOLDOWN
                if elapsed > 2.0 or self.phase is DoomPhase.DEADPAN_COMMENT:
                    self.phase = DoomPhase.OBSERVE
                    self.entered = t
                    self.comment = ""
                    self.toaster_watching = False
            return self.phase
        budget = signals.focus_score < 0.55 and not signals.dragging and not signals.selecting
        if not budget:
            self.phase = DoomPhase.OBSERVE
            self.entered = t
            return self.phase
        ladders = (
            (
                DoomPhase.OBSERVE,
                DoomPhase.AUDIENCE_GATHERS,
                DoomPhase.CHAIRS_APPEAR,
                DoomPhase.POPCORN_STARTS,
                DoomPhase.HEARTS,
                DoomPhase.MAIN_TOASTER_JOINS,
                DoomPhase.DEADPAN_COMMENT,
            ),
            (
                DoomPhase.OBSERVE,
                DoomPhase.POPCORN_STARTS,
                DoomPhase.AUDIENCE_GATHERS,
                DoomPhase.HEARTS,
                DoomPhase.CHAIRS_APPEAR,
                DoomPhase.MAIN_TOASTER_JOINS,
                DoomPhase.DEADPAN_COMMENT,
            ),
        )
        order = ladders[int(t * 3) % 2]
        if self.phase not in order:
            self.phase = DoomPhase.OBSERVE
            self.entered = t or 0.001
        if self.entered <= 0:
            self.entered = t or 0.001
        elapsed = t - self.entered
        idx = order.index(self.phase)
        if elapsed > 3.4 and idx + 1 < len(order):
            self.phase = order[idx + 1]
            self.entered = t
        if self.phase is DoomPhase.CHAIRS_APPEAR and not self.chairs:
            self.chairs = [Vec2(entity.pos.x, entity.pos.y + 14) for entity in entities[:6]]
        playable = [e for e in entities if e.state not in {ToastState.TOASTING, ToastState.JUMP_INTO_SLOT, ToastState.HIDE, ToastState.PANIC_RUN}]
        if self.phase in {DoomPhase.AUDIENCE_GATHERS, DoomPhase.CHAIRS_APPEAR, DoomPhase.POPCORN_STARTS}:
            dest = ToastState.POPCORN_AUDIENCE if self.phase is DoomPhase.POPCORN_STARTS else ToastState.WATCH_VIDEO
            for entity in playable[:8]:
                if entity.state not in {ToastState.WATCH_VIDEO, ToastState.POPCORN_AUDIENCE, ToastState.HEART_MODE}:
                    entity.proposed = dest
        if self.phase is DoomPhase.HEARTS:
            for entity in playable[:4]:
                entity.proposed = ToastState.HEART_MODE
        if self.phase is DoomPhase.MAIN_TOASTER_JOINS:
            self.toaster_watching = True
        if self.phase is DoomPhase.DEADPAN_COMMENT:
            self.comment = "Krátké video. Dlouhý večer."
            for entity in playable[:2]:
                entity.proposed = ToastState.DOOMSCROLL_COMMENTARY
        if self.phase is DoomPhase.OBSERVE:
            self.chairs = []
            self.toaster_watching = False
        return self.phase
