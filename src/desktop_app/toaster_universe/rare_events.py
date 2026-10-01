"""Rare events stay rare. Novelty budget is the immune system."""

from __future__ import annotations

import random
from dataclasses import dataclass

from .config import WorldConfig
from .signals import UserSignals
from .types import ChaosMode, RareEventId, SceneId, SceneIntent, ActivityState


@dataclass
class RareFire:
    event: RareEventId
    scene: SceneId
    chaos: ChaosMode
    t: float


EVENT_SCENE = {
    RareEventId.THE_OTHER_MONITOR_RETURN: (SceneId.SHAREWARE_APOCALYPSE, ChaosMode.MULTI_MONITOR_MIGRATION),
    RareEventId.THE_BLACK_TOAST_FUNERAL: (SceneId.FUNERAL_RITUAL, ChaosMode.NONE),
    RareEventId.THE_ONE_TOAST_CHOIR: (SceneId.QUIET_COMPANIONSHIP, ChaosMode.PARADE),
    RareEventId.MIDNIGHT_PORTAL: (SceneId.PORTAL_GLITCH, ChaosMode.NONE),
    RareEventId.MICROWAVE_STAR: (SceneId.MICROWAVE_ANOMALY, ChaosMode.NONE),
    RareEventId.RICE_SPIRIT_VISIT: (SceneId.RICE_ZEN, ChaosMode.NONE),
    RareEventId.AIRFRYER_TORNADO: (SceneId.AIRFRYER_VORTEX, ChaosMode.NONE),
    RareEventId.POPCORN_STORM: (SceneId.POPCORN_BRAIN, ChaosMode.CRUMB_STORM),
    RareEventId.SILENT_HEART: (SceneId.KPOP_HEART_MODE, ChaosMode.NONE),
    RareEventId.TOAST_CLONE_GLITCH: (SceneId.TOAST_INFESTATION, ChaosMode.TOAST_SWARM),
    RareEventId.SLOT_WITH_NO_BOTTOM: (SceneId.SECRET_CHAMBER, ChaosMode.NONE),
    RareEventId.WINDOW_EDGE_CAMP: (SceneId.SHAREWARE_APOCALYPSE, ChaosMode.EDGE_BOUNCE),
    RareEventId.TASKBAR_PARADE: (SceneId.SHAREWARE_APOCALYPSE, ChaosMode.PARADE),
    RareEventId.FOURTH_TERMINAL_ORACLE: (SceneId.CODING_FLOW_AUDIENCE, ChaosMode.NONE),
    RareEventId.HQ_VAE_CEREMONY: (SceneId.SECRET_CHAMBER, ChaosMode.NONE),
    RareEventId.FLAT_WHITE_ALIGNMENT: (SceneId.HR_ROAST_SKIT, ChaosMode.NONE),
    RareEventId.SHAREWARE_INVASION: (SceneId.SHAREWARE_APOCALYPSE, ChaosMode.SCREEN_SAVER_HORDE),
    RareEventId.CRUMB_KING: (SceneId.TOAST_INFESTATION, ChaosMode.CRUMB_STORM),
    RareEventId.BUTTER_ECLIPSE: (SceneId.SECRET_CHAMBER, ChaosMode.NONE),
}


class RareEventDirector:
    def __init__(self, cfg: WorldConfig, rng: random.Random) -> None:
        self.cfg = cfg
        self.rng = rng
        self.last_any = -1e9
        self.last_by_event: dict[RareEventId, float] = {}
        self.novelty = 1.0

    def recover(self, dt: float) -> None:
        self.novelty = min(1.0, self.novelty + dt * self.cfg.novelty_budget_recovery)

    def maybe_fire(self, t: float, signals: UserSignals, population: int, burnt: bool) -> RareFire | None:
        if not self.cfg.rare_events_enabled:
            return None
        if t - self.last_any < 45 * 60:
            return None
        if self.novelty < 0.55:
            return None
        if signals.activity in {
            ActivityState.CODING_FLOW,
            ActivityState.FRANTIC,
            ActivityState.FOCUSED,
            ActivityState.TERMINAL_HEAVY,
        } and signals.focus_score > 0.72:
            return None
        if signals.dragging or signals.selecting or signals.scrolling:
            return None
        chance = self.cfg.rare_event_base_rate
        if signals.activity in {
            ActivityState.CODING_FLOW,
            ActivityState.FRANTIC,
            ActivityState.FOCUSED,
            ActivityState.TERMINAL_HEAVY,
        } and signals.focus_score > 0.45:
            # Coding lull trickle: scale the rate down smoothly as focus climbs
            # from 0.45 to the hard wall at 0.72, so long sessions still get the
            # occasional rare event during lulls without interrupting deep flow.
            chance *= max(0.0, 1.0 - (signals.focus_score - 0.45) / 0.27)
        if signals.activity is ActivityState.LATE_NIGHT:
            chance *= 2.2
        if population >= 8:
            chance *= 1.4
        if self.rng.random() > chance:
            return None
        weights = {event: 1.0 for event in RareEventId}
        if burnt:
            weights[RareEventId.THE_BLACK_TOAST_FUNERAL] += 4.0
        if signals.activity is ActivityState.TERMINAL_HEAVY:
            weights[RareEventId.FOURTH_TERMINAL_ORACLE] += 3.5
        if signals.activity is ActivityState.LATE_NIGHT:
            weights[RareEventId.MIDNIGHT_PORTAL] += 3.0
            weights[RareEventId.SLOT_WITH_NO_BOTTOM] += 2.0
        if signals.activity is ActivityState.DOOMSCROLLING:
            weights[RareEventId.POPCORN_STORM] += 2.5
        pool = list(weights)
        event = self.rng.choices(pool, weights=[weights[e] for e in pool], k=1)[0]
        if t - self.last_by_event.get(event, -1e9) < 6 * 3600:
            return None
        scene, chaos = EVENT_SCENE[event]
        self.last_any = t
        self.last_by_event[event] = t
        self.novelty = max(0.0, self.novelty - 0.55)
        return RareFire(event, scene, chaos, t)

    def force(self, event: RareEventId, t: float, signals: UserSignals, *, honor_safety: bool = True) -> RareFire | None:
        """Developer trigger. Still fail-closed on focus/drag when honor_safety."""
        if honor_safety:
            if signals.dragging or signals.selecting or signals.scrolling:
                return None
            if signals.activity in {
                ActivityState.CODING_FLOW,
                ActivityState.FRANTIC,
                ActivityState.FOCUSED,
                ActivityState.TERMINAL_HEAVY,
            } and signals.focus_score > 0.72:
                return None
        scene, chaos = EVENT_SCENE[event]
        self.last_any = t
        self.last_by_event[event] = t
        self.novelty = max(0.0, self.novelty - 0.55)
        return RareFire(event, scene, chaos, t)

    def as_intent(self, fire: RareFire) -> SceneIntent:
        return SceneIntent(
            scene=fire.scene,
            duration=22.0,
            escalation=0.85,
            rarity_class="legendary",
            visual_only=True,
            callback_key=fire.event.value,
            source="rare",
        )
