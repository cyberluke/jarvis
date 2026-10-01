"""Windows 9x shareware chaos. Bounded, reversible, click-through."""

from __future__ import annotations

import math
import random

from .entities import Entity
from .types import ChaosMode, EntityKind, Rect, SAFETY_STATES, ToastState, Vec2


class ChaosDirector:
    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.mode = ChaosMode.NONE
        self.until = 0.0

    def start(self, mode: ChaosMode, t: float, duration: float = 16.0) -> None:
        self.mode = mode
        self.until = t + duration

    def collapse(self) -> None:
        self.mode = ChaosMode.NONE
        self.until = 0.0

    def tick(self, t: float, entities: list[Entity], desktop: Rect, signals=None) -> None:
        if self.mode is ChaosMode.NONE:
            return
        if signals is not None:
            if signals.dragging or signals.selecting or signals.scrolling or signals.focus_score >= 0.55 or signals.typing_rate > 2.0:
                self.collapse()
                return
        if t >= self.until:
            self.collapse()
            return
        playable = [e for e in entities if e.state not in SAFETY_STATES and not e.hidden]
        if self.mode is ChaosMode.EDGE_BOUNCE:
            for entity in playable:
                if entity.pos.x < desktop.x + entity.radius or entity.pos.x > desktop.right - entity.radius:
                    entity.vel = Vec2(-entity.vel.x, entity.vel.y)
                if entity.pos.y < desktop.y + entity.radius or entity.pos.y > desktop.bottom - 60:
                    entity.vel = Vec2(entity.vel.x, -entity.vel.y)
        elif self.mode is ChaosMode.PARADE:
            for i, entity in enumerate(playable):
                entity.target = Vec2(desktop.x + 40 + (t * 70 + i * 36) % max(80, desktop.w - 80), desktop.bottom - 90)
                entity.proposed = ToastState.DANCE
        elif self.mode is ChaosMode.TOAST_SWARM:
            cx, cy = desktop.center.x, desktop.center.y
            for i, entity in enumerate(playable):
                ang = t * 1.8 + i
                entity.target = Vec2(cx + math.cos(ang) * 160, cy + math.sin(ang) * 90)
        elif self.mode is ChaosMode.SCREEN_SAVER_HORDE:
            for entity in playable:
                entity.vel = Vec2(self.rng.uniform(-180, 180), self.rng.uniform(-140, 140))
        elif self.mode is ChaosMode.MULTI_MONITOR_MIGRATION:
            for entity in playable:
                entity.target = Vec2(desktop.x - 40, entity.pos.y)
        elif self.mode is ChaosMode.HAMMER_FAKEOUT:
            for entity in playable[:1]:
                entity.pose.squash_y = 0.4
                entity.pose.squash_x = 1.5
        elif self.mode is ChaosMode.CRUMB_STORM:
            for entity in playable:
                entity.vel = entity.vel + Vec2(self.rng.uniform(-40, 40), self.rng.uniform(-30, 10))
                if entity.kind is EntityKind.MINI_TOAST:
                    entity.proposed = entity.proposed or ToastState.CARRY_CRUMB
        elif self.mode is ChaosMode.DOOM_MONSTER_HOMAGE:
            for i, entity in enumerate(playable):
                # Homage only: squat silhouette + lateral hunt. No Doom assets.
                entity.pose.squash_y = 0.62
                entity.pose.squash_x = 1.28
                entity.pose.glow = 0.2
                hunt = Vec2(desktop.center.x + math.sin(t * 2.2 + i) * 180, desktop.center.y + 40)
                entity.target = hunt
                entity.proposed = entity.proposed or ToastState.PANIC_RUN
