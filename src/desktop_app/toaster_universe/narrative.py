"""Narrative director. Scene grammar, escalation, return to calm."""

from __future__ import annotations

from dataclasses import dataclass

from .config import WorldConfig
from .signals import UserSignals
from .types import ActivityState, ChaosMode, SceneId, SceneIntent, ComedyStyle


SCENE_BY_ACTIVITY = {
    ActivityState.IDLE: SceneId.TOAST_WARMUP_RITUAL,
    ActivityState.FOCUSED: SceneId.QUIET_COMPANIONSHIP,
    ActivityState.CODING_FLOW: SceneId.CODING_FLOW_AUDIENCE,
    ActivityState.FRANTIC: SceneId.RETURN_TO_CALM,
    ActivityState.BROWSING: SceneId.TOAST_WATCH_PARTY,
    ActivityState.DOOMSCROLLING: SceneId.POPCORN_BRAIN,
    ActivityState.TERMINAL_HEAVY: SceneId.HR_ROAST_SKIT,
    ActivityState.LATE_NIGHT: SceneId.LATE_NIGHT_SLEEP,
}


@dataclass
class ActiveScene:
    intent: SceneIntent
    started: float
    ends: float


class NarrativeDirector:
    def __init__(self, cfg: WorldConfig) -> None:
        self.cfg = cfg
        self.active: ActiveScene | None = None
        self.chaos = ChaosMode.NONE
        self.last_scene: SceneId = SceneId.QUIET_COMPANIONSHIP
        self.callbacks_seen: set[str] = set()
        self.last_callback: str = ""
        self.novelty_cooldown_until: float = -1e9

    def current(self) -> SceneId:
        return self.active.intent.scene if self.active else self.last_scene

    def propose(self, t: float, signals: UserSignals, population: int, burnt: bool) -> SceneIntent:
        if self.active and t < self.active.ends and self.active.intent.source != "probe":
            return self.active.intent
        if t < self.novelty_cooldown_until:
            return self.force(t, SceneIntent(scene=SceneId.RETURN_TO_CALM, duration=8.0, visual_only=True, source="narrative"))
        scene = SCENE_BY_ACTIVITY.get(signals.activity, SceneId.QUIET_COMPANIONSHIP)
        if signals.activity is ActivityState.FRANTIC and signals.focus_score >= 0.55:
            scene = SceneId.RETURN_TO_CALM
        if burnt:
            scene = SceneId.BURN_RECOVERY
        elif population >= 9:
            scene = SceneId.TOAST_INFESTATION
        elif signals.activity is ActivityState.DOOMSCROLLING and signals.doomscroll_score > 0.8:
            scene = SceneId.KPOP_HEART_MODE if signals.novelty_seeking_proxy > 0.7 else SceneId.POPCORN_BRAIN
        duration = min(self.cfg.max_scene_duration_sec, 10.0 + population * 0.6)
        from .comedy import ComedyDirector

        tone = ComedyDirector(self.cfg, __import__("random").Random(1)).style_for(signals.activity, scene)
        focus_allows = signals.focus_score < self.cfg.focus_threshold and signals.activity not in {
            ActivityState.CODING_FLOW,
            ActivityState.FOCUSED,
            ActivityState.FRANTIC,
        }
        escalation = min(1.0, population / 20.0 + signals.doomscroll_score * 0.3) if focus_allows else 0.0
        budget = max(0, 3 - int(signals.focus_score * 4) - int(signals.dragging) - int(signals.selecting))
        intent = SceneIntent(
            scene=scene,
            tone=tone,
            participants=max(1, min(population, max(1, budget + 1))),
            escalation=escalation,
            duration=duration,
            visual_only=signals.activity in {ActivityState.CODING_FLOW, ActivityState.FOCUSED, ActivityState.FRANTIC} or not focus_allows,
            speech_budget=0 if not focus_allows else 1,
            callback_key=self.last_callback,
            source="narrative",
        )
        self.active = ActiveScene(intent, t, t + duration)
        self.last_scene = scene
        return intent

    def force(self, t: float, intent: SceneIntent) -> SceneIntent:
        duration = min(self.cfg.max_scene_duration_sec, max(4.0, intent.duration))
        intent.duration = duration
        self.active = ActiveScene(intent, t, t + duration)
        self.last_scene = intent.scene
        return intent

    def maybe_calm(self, t: float, signals: UserSignals) -> SceneIntent | None:
        if signals.activity in {ActivityState.FRANTIC, ActivityState.CODING_FLOW, ActivityState.FOCUSED} and signals.focus_score >= 0.7:
            return self.force(
                t,
                SceneIntent(scene=SceneId.RETURN_TO_CALM, duration=8.0, visual_only=True, source="narrative"),
            )
        if self.active and t >= self.active.ends and self.active.intent.scene is not SceneId.RETURN_TO_CALM:
            leaving = self.active.intent.scene
            if self.active.intent.callback_key:
                self.note_callback(self.active.intent.callback_key)
            if leaving in {SceneId.SHAREWARE_APOCALYPSE, SceneId.TOAST_INFESTATION, SceneId.FUNERAL_RITUAL, SceneId.PORTAL_GLITCH}:
                self.novelty_cooldown_until = t + 45.0
            return self.force(
                t,
                SceneIntent(scene=SceneId.RETURN_TO_CALM, duration=8.0, visual_only=True, source="narrative"),
            )
        return None

    def note_callback(self, key: str) -> None:
        if not key:
            return
        self.callbacks_seen.add(key)
        self.last_callback = key
