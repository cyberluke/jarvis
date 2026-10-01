"""Population ecology. Intimate, alive, infestation, event-only swarm."""

from __future__ import annotations

import random

from .config import WorldConfig
from .entities import Entity
from .types import ActivityState, EntityKind, PopulationMode, ToastState, Vec2, WorldEvent


def population_cap(cfg: WorldConfig, activity: ActivityState, infestation: bool, event_swarm: bool) -> int:
    if event_swarm:
        return cfg.max_mini_toasts_event
    if infestation or activity is ActivityState.DOOMSCROLLING:
        return cfg.max_mini_toasts_infestation
    if activity in {ActivityState.CODING_FLOW, ActivityState.FOCUSED, ActivityState.FRANTIC}:
        return min(3, cfg.max_mini_toasts_normal)
    if activity is ActivityState.LATE_NIGHT:
        return min(2, cfg.max_mini_toasts_normal)
    return cfg.max_mini_toasts_normal


def living_toasts(entities: list[Entity]) -> list[Entity]:
    return [
        e
        for e in entities
        if e.kind in {EntityKind.MINI_TOAST, EntityKind.BURNT_TOAST} and e.state is not ToastState.DESPAWN
    ]


def mode_for(n: int, event_swarm: bool) -> PopulationMode:
    if event_swarm or n >= 21:
        return PopulationMode.SWARM_EVENT
    if n <= 0:
        return PopulationMode.EMPTY
    if n <= 3:
        return PopulationMode.INTIMATE
    if n <= 8:
        return PopulationMode.ALIVE
    return PopulationMode.INFESTATION


class PopulationController:
    def __init__(self, cfg: WorldConfig, rng: random.Random) -> None:
        self.cfg = cfg
        self.rng = rng
        self.infestation = False
        self.event_swarm = False
        self.growth_acc = 0.0
        self.decay_acc = 0.0
        self.mode = PopulationMode.EMPTY
        self.multiply_ready_t = -1e9
        self._last_infestation_emit = -1e9
        self._last_pop_emit = -1e9

    def tick(self, dt: float, entities: list[Entity], activity: ActivityState) -> list[tuple[str, Vec2]]:
        living = living_toasts(entities)
        n = len(living)
        if not self.event_swarm and n < 9:
            self.infestation = False
        if n >= 9:
            self.infestation = True
        if n >= 21:
            self.event_swarm = True
        self.mode = mode_for(n, self.event_swarm)
        cap = population_cap(self.cfg, activity, self.infestation, self.event_swarm)
        if self.mode is PopulationMode.SWARM_EVENT and not self.event_swarm:
            cap = min(cap, self.cfg.max_mini_toasts_infestation)
        spawns: list[tuple[str, Vec2]] = []
        if n < cap and activity not in {ActivityState.CODING_FLOW, ActivityState.FRANTIC} and self.mode is not PopulationMode.SWARM_EVENT:
            self.growth_acc += dt * self.cfg.population_growth_rate * (1.0 if n else 1.6)
            if self.growth_acc >= 1.0:
                self.growth_acc -= 1.0
                anchor = living[0].pos if living else Vec2()
                spawns.append(("mini_toast", anchor + Vec2(self.rng.uniform(-40, 40), self.rng.uniform(-10, 20))))
        self.decay_acc += dt * self.cfg.population_decay_rate
        over = n > cap
        decay = self.decay_acc >= 1.0 and n > 1 and activity in {ActivityState.CODING_FLOW, ActivityState.FOCUSED, ActivityState.FRANTIC}
        if over or decay:
            if self.decay_acc >= 1.0:
                self.decay_acc -= 1.0
            keep = cap if over else max(1, n - 1)
            extras = sorted(living, key=lambda e: e.age)[keep:]
            for entity in extras:
                if entity.state not in {ToastState.TOASTING, ToastState.JUMP_INTO_SLOT, ToastState.OVERTOASTING}:
                    entity.proposed = self._diegetic_exit(entity, n)
        return spawns

    def _diegetic_exit(self, entity: Entity, n: int) -> ToastState:
        if entity.appliance:
            return ToastState.PORTAL_CURIOUS
        if entity.temperature < 0.28:
            return ToastState.APPROACH_TOASTER
        if n >= 6 and entity.trait("sleepiness") > 0.4:
            return ToastState.SLEEP
        if entity.pos.x < 80 or entity.trait("shyness") > 0.7:
            return ToastState.HIDE
        return ToastState.DESPAWN

    def can_multiply(self, t: float, scene_ok: bool) -> bool:
        if self.mode is PopulationMode.SWARM_EVENT and not self.event_swarm:
            return False
        if not scene_ok:
            return False
        if t - self.multiply_ready_t < 8.0:
            return False
        self.multiply_ready_t = t
        return True

    def event_name(self, n: int, t: float = 0.0) -> WorldEvent | None:
        if n <= 0:
            return None
        if n >= 9:
            if t - self._last_infestation_emit < 8.0:
                return None
            self._last_infestation_emit = t
            return WorldEvent.POPULATION_INFESTATION
        # Debounce LOW/HIGH: they used to fire every tick at population < 4,
        # letting POPULATION_LOW -> SOCIALIZE hijack toasts out of the slot queue.
        if t - self._last_pop_emit < 2.5:
            return None
        self._last_pop_emit = t
        if n >= 4:
            return WorldEvent.POPULATION_HIGH
        return WorldEvent.POPULATION_LOW
