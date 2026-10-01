"""Deterministic world runtime. Directors propose; this executes."""

from __future__ import annotations

import random
import time

from .actions import ActionEngine, MainToasterSoul
from .animation import AnimationDirector, compose_pose
from .appliances import ApplianceWorld
from .audio import AudioDirector
from .behavior import BehaviorEngine, Transition
from .chaos import ChaosDirector
from .comedy import ComedyBeat, ComedyDirector
from .config import WorldConfig, load_world_config
from .dirty import DirtyAccumulator
from .doomscroll import DoomscrollLayer
from .ecology import PopulationController, living_toasts
from .entities import Entity, Particle, spawn_entity
from .events import EventBus
from .keyboard_games import KeyboardGames
from .llm_director import LlmDirector, _log
from .monitors import MonitorTopology
from .narrative import NarrativeDirector
from .observability import WorldMetrics
from .performance import SceneBeat, ScenePerformance
from .rare_events import RareEventDirector
from .physics import gravity
from .safety import SafetyArbiter
from .signals import SignalSampler, UserSignals
from .state_machine import RITUAL_STATES
from .types import (
    ApplianceKind,
    BehaviorLayer,
    ChaosMode,
    ClipId,
    EntityKind,
    LAYER_PRIORITY,
    Rect,
    ActivityState,
    OverrideReason,
    SceneId,
    SceneIntent,
    SafetyZone,
    SlotAnchor,
    ToasterAction,
    ToastState,
    Vec2,
    WorldEvent,
)


def _now() -> float:
    return time.monotonic()


_WORLD: ToasterWorld | None = None

POPULATION_PRESETS = {
    "quiet": {"spawn_on_start": 0, "max_mini_toasts_normal": 2, "population_growth_rate": 0.02},
    "cozy": {"spawn_on_start": 1, "max_mini_toasts_normal": 4, "population_growth_rate": 0.05},
    "alive": {"spawn_on_start": 2, "max_mini_toasts_normal": 8, "population_growth_rate": 0.08},
    "busy": {"spawn_on_start": 4, "max_mini_toasts_normal": 10, "population_growth_rate": 0.12},
    "chaos": {"spawn_on_start": 6, "max_mini_toasts_normal": 12, "population_growth_rate": 0.2},
}
COMEDY_PRESETS = {"calm": 180.0, "balanced": 90.0, "unhinged": 24.0}
CHATTER_PRESETS = {"off": 1e9, "low": 280.0, "normal": 180.0, "high": 60.0}
RARE_PRESETS = {"rare": 0.00006, "occasional": 0.0003, "frequent": 0.0006}
WORK_PRESETS = {"minimal": 0.45, "adaptive": 0.68, "normal": 0.82}

# Per-kind ambient lifespan scales, applied as scale × cfg.ambient_lifespan_sec.
# Ambient noise-makers (kernels, rice spirits, echoes, butter) are ephemeral so
# they never accumulate into permanent clutter; mini toasts are NOT in this map.
AMBIENT_LIFESPAN: dict[EntityKind, float] = {
    EntityKind.POPCORN_KERNEL: 0.75,
    EntityKind.RICE_SPIRIT: 1.0,
    EntityKind.PORTAL_ECHO: 1.0,
    EntityKind.BUTTER_BLOB: 0.9,
}


def get_world(cfg: WorldConfig | None = None) -> ToasterWorld:
    global _WORLD
    if _WORLD is None:
        _WORLD = ToasterWorld(cfg)
    return _WORLD


class ToasterWorld:
    def __init__(self, cfg: WorldConfig | None = None, seed: int = 4) -> None:
        self.cfg = (cfg or load_world_config()).clamp()
        self.rng = random.Random(seed)
        self.world_id = f"world-{seed}"
        self.session_id = f"session-{seed}"
        self.bus = EventBus(world_id=self.world_id, session_id=self.session_id)
        self.metrics = WorldMetrics(world_id=self.world_id, session_id=self.session_id)
        self.signals = SignalSampler(self.cfg, self.bus)
        self.safety = SafetyArbiter(self.cfg)
        self.behavior = BehaviorEngine(self.cfg, self.bus, self.safety, self.rng)
        self.animation = AnimationDirector(self.bus)
        self.actions = ActionEngine(self.bus, self.animation)
        self.comedy = ComedyDirector(self.cfg, self.rng)
        self.narrative = NarrativeDirector(self.cfg)
        self.llm = LlmDirector(self.cfg)
        self.population = PopulationController(self.cfg, self.rng)
        self.keyboard = KeyboardGames(self.cfg)
        self.doom = DoomscrollLayer(self.cfg)
        self.appliances = ApplianceWorld()
        self.rare = RareEventDirector(self.cfg, self.rng)
        self.chaos = ChaosDirector(self.rng)
        self.audio = AudioDirector(self.cfg)
        self.topology = MonitorTopology()
        self.dirty = DirtyAccumulator(self.cfg)
        self.entities: list[Entity] = []
        self.particles: list[Particle] = []
        self.slots: list[SlotAnchor] = [SlotAnchor(0, Vec2()), SlotAnchor(1, Vec2())]
        self.desktop = Rect(0, 0, 1920, 1080)
        self.toaster = Vec2()
        self.anchors_ready = False
        self.soul = MainToasterSoul()
        self.started = _now()
        self.last_t = self.started
        self.clock = 0.0
        self.caption = ""
        self.caption_until = 0.0
        self.last_beat: ComedyBeat | None = None
        self.seq = 0
        self._scene_applied: SceneId | None = None
        self.pending_director_payload: dict | None = None
        self.director_accepted: SceneIntent | None = None
        self.performance = ScenePerformance()
        self._last_population = 0
        self._weather_emitted = False
        self._night_emitted = False
        self.night_mode = False
        self.last_layer: str = BehaviorLayer.L4_CHARACTER_GRAPH.value
        self._dt_accum = 0.0
        self._fixed_dt = 1.0 / 30.0
        self.home_anchor = Vec2()
        self._user_lock_until = 0.0
        self._dragging = False
        self._drag_offset = Vec2()
        self._pre_drag = Vec2()
        self.character_form = self.cfg.character_form
        self.bus.on_any(self._on_bus)
        self.apply_user_presets()

    def _on_bus(self, event) -> None:
        self.audio.on_event(event.name, self.signals.signals, t=event.t)
        self.metrics.record(
            "event",
            t=event.t,
            name=event.name.value,
            source=event.source,
            entity=event.entity_id,
            event_id=event.event_id,
            world_id=event.payload.get("world_id"),
            session_id=event.payload.get("session_id"),
        )

    def set_desktop(self, rect: Rect) -> None:
        self.desktop = rect

    def set_toaster(self, pos: Vec2, slot_a: Vec2, slot_b: Vec2) -> None:
        self.toaster = pos
        self.slots[0].pos = slot_a
        self.slots[1].pos = slot_b
        self.anchors_ready = True
        self.behavior.toaster_pos = pos
        if self.home_anchor.length() < 1.0 and self.cfg.home_anchor_x < 0:
            self.home_anchor = Vec2(pos.x, pos.y)
        self.appliances.ensure_layout(pos)

    def apply_user_presets(self) -> None:
        pop = POPULATION_PRESETS.get(self.cfg.population_level, POPULATION_PRESETS["cozy"])
        for key, value in pop.items():
            setattr(self.cfg, key, value)
        self.cfg.comedy_cooldown_sec = COMEDY_PRESETS.get(self.cfg.comedy_level, 90.0)
        self.cfg.voice_cooldown_sec = CHATTER_PRESETS.get(self.cfg.chatter_level, 180.0)
        self.cfg.rare_event_base_rate = RARE_PRESETS.get(self.cfg.rare_event_frequency, 0.00018)
        self.cfg.focus_threshold = WORK_PRESETS.get(self.cfg.work_activity, 0.68)
        self.cfg.audio_enabled = bool(self.cfg.world_sounds)
        self.cfg.audio_volume = self.cfg.voice_volume if self.cfg.character_voice else 0.0
        if not self.cfg.ambient_sounds:
            self.cfg.audio_volume = min(self.cfg.audio_volume, 0.12)
        self.character_form = self.cfg.character_form
        if self.cfg.home_anchor_x >= 0 and self.cfg.home_anchor_y >= 0:
            self.home_anchor = Vec2(self.cfg.home_anchor_x, self.cfg.home_anchor_y)
        self.cfg.clamp()

    def apply_settings(self, updates: dict) -> dict:
        previous = self.cfg.apply_override(**updates)
        self.apply_user_presets()
        return previous

    def set_character_form(self, form_id: str) -> str:
        from .character_forms import get_form, is_registered

        if not is_registered(form_id):
            return self.character_form
        profile = get_form(form_id)
        self.character_form = profile.id
        self.cfg.character_form = profile.id
        self.soul.last_action = f"form:{profile.id}"
        self._persist_world_keys(character_form=profile.id)
        try:
            from desktop_app.face_widget import get_jarvis_state

            get_jarvis_state().state_changed.emit(get_jarvis_state().state)
        except Exception:
            pass
        return profile.id

    def get_character_form(self) -> str:
        return self.character_form

    def begin_drag(self, cursor: Vec2) -> None:
        self._dragging = True
        self._pre_drag = Vec2(self.toaster.x, self.toaster.y)
        self._drag_offset = Vec2(cursor.x - self.toaster.x, cursor.y - self.toaster.y)
        self.last_layer = BehaviorLayer.L0_USER_INTENT.value
        self.soul.jelly = 0.35

    def update_drag(self, cursor: Vec2) -> Vec2:
        if not self._dragging:
            return self.toaster
        pos = Vec2(cursor.x - self._drag_offset.x, cursor.y - self._drag_offset.y)
        self.toaster = pos
        self.behavior.toaster_pos = pos
        return pos

    def end_drag(self, persist: bool = True) -> Vec2:
        self._dragging = False
        self.home_anchor = Vec2(self.toaster.x, self.toaster.y)
        self._user_lock_until = self.clock + 8.0
        self.cfg.home_anchor_x = self.home_anchor.x
        self.cfg.home_anchor_y = self.home_anchor.y
        if persist:
            self._persist_world_keys(
                home_anchor_x=self.home_anchor.x,
                home_anchor_y=self.home_anchor.y,
            )
        return self.toaster

    def cancel_drag(self) -> Vec2:
        self._dragging = False
        self.toaster = self._pre_drag
        self.behavior.toaster_pos = self.toaster
        return self.toaster

    def reset_position(self) -> None:
        self.cfg.home_anchor_x = -1.0
        self.cfg.home_anchor_y = -1.0
        self.home_anchor = Vec2()
        self._persist_world_keys(home_anchor_x=-1.0, home_anchor_y=-1.0)

    def is_dragging(self) -> bool:
        return self._dragging

    def _persist_world_keys(self, **values) -> None:
        try:
            from jarvis.config import default_config_path, _load_json, _save_json

            path = default_config_path()
            data = _load_json(path) or {}
            for key, value in values.items():
                data[f"toaster_universe_{key}"] = value
            _save_json(path, data)
        except Exception:
            pass

    def bootstrap(self) -> None:
        if self.entities or not self.anchors_ready:
            return
        if not self.cfg.autonomous_characters:
            return
        n = max(0, int(self.cfg.spawn_on_start))
        for i in range(n):
            self.spawn_toast(self.toaster + Vec2(-80 - i * 28, 40 + i * 12))

    def spawn_toast(self, pos: Vec2, kind: EntityKind = EntityKind.MINI_TOAST) -> Entity:
        if not self.anchors_ready:
            raise RuntimeError("WORLD_WAITING_FOR_ANCHORS")
        if not self.cfg.appliances_enabled and kind is EntityKind.APPLIANCE:
            kind = EntityKind.MINI_TOAST
        living = len(living_toasts(self.entities))
        physics_n = len(self.entities)
        if physics_n >= self.cfg.max_physics_entities:
            if self.entities:
                return self.entities[-1]
            return spawn_entity(kind, pos, self.rng, cfg=self.cfg)
        entity = spawn_entity(kind, pos, self.rng, cfg=self.cfg)
        self.entities.append(entity)
        if kind in {EntityKind.MINI_TOAST, EntityKind.BURNT_TOAST} and living <= 1:
            self.bus.emit(WorldEvent.POPULATION_LOW, self.clock, entity_id=entity.id)
        self.metrics.record("entity_spawned", t=self.clock, id=entity.id, species=kind.value)
        self.metrics.record(
            "population_change",
            t=self.clock,
            before=living,
            after=len(living_toasts(self.entities)),
            reason="spawn",
        )
        return entity

    def ingest_director_json(self, raw: str | dict) -> bool:
        self.metrics.record("llm_director_call", t=self.clock)
        decision = self.llm.parse(raw)
        if decision.intent is None:
            self.metrics.record("llm_director_reject", t=self.clock, reason=decision.rejected)
            return False
        return self.apply_director_intent(decision.intent)

    def apply_director_intent(self, intent: SceneIntent) -> bool:
        if not self.llm.admissible(self, intent):
            self.metrics.record("llm_director_reject", t=self.clock, reason="safety")
            return False
        if intent.novelty_budget < self.rare.novelty * 0.15 and intent.rarity_class in {"rare", "legendary", "very_rare"}:
            self.metrics.record("llm_director_reject", t=self.clock, reason="novelty")
            return False
        if intent.allowed_entities:
            allowed = {k.lower() for k in intent.allowed_entities}
            for entity in self.entities:
                if entity.kind.value not in allowed and entity.state not in RITUAL_STATES:
                    entity.proposed = ToastState.HIDE
        if getattr(intent, "content_key", ""):
            self.force_comedy(intent.content_key, speech_ok=not intent.visual_only)
        self.narrative.force(self.clock, intent)
        self._scene_applied = None
        self.director_accepted = intent
        self.pending_director_payload = {
            "scene": intent.scene.value,
            "tone": intent.tone.value,
            "participants": intent.participants,
            "callback": intent.callback_key,
            "line_intent": intent.line_intent[:80] if intent.line_intent else "",
            "novelty_budget": intent.novelty_budget,
            "visual_only": intent.visual_only,
            "rarity_class": intent.rarity_class,
            "content_key": getattr(intent, "content_key", ""),
        }
        if intent.tone:
            self.comedy.style = intent.tone
        if intent.line_intent and not intent.visual_only:
            self.caption = intent.line_intent[:96]
            self.caption_until = self.clock + 4.0
            self.soul.line = self.caption
            self.soul.line_until = self.caption_until
        payoff = ""
        try:
            from .performance import SCRIPTS

            script = SCRIPTS.get(intent.scene)
            payoff = script.payoff if script else intent.scene.value
        except Exception:
            payoff = intent.scene.value
        self.llm.session.note_accept(intent, self.clock, payoff)
        self.metrics.record("llm_director_accept", t=self.clock, scene=intent.scene.value)
        self.llm.session.note_setup(intent.scene)
        _log(
            f"apply scene={intent.scene.value} hide_entities={len(intent.allowed_entities)} "
            f"comedy={getattr(intent, 'content_key', '') or '-'} caption='{(intent.line_intent or '')[:48]}'"
        )
        return True

    def force_director_request(self, *, opportunity: str = "debug") :
        return self.llm.request(self, force=True, opportunity=opportunity)

    def force_rare_event(self, event_id: str, *, honor_safety: bool = True):
        from .types import RareEventId

        event = RareEventId(event_id)
        fire = self.rare.force(event, self.clock, self.signals.signals, honor_safety=honor_safety)
        if fire is None:
            self.metrics.record("rare_event_suppressed", event=event_id)
            return None
        self.narrative.force(self.clock, self.rare.as_intent(fire))
        self._scene_applied = None
        if fire.chaos is not ChaosMode.NONE:
            self.chaos.start(fire.chaos, self.clock)
            self.metrics.record("rare_event", t=self.clock, event=fire.event.value, forced=True)
        self.bus.emit(WorldEvent.RARE_EVENT_TICK, self.clock, payload={"event": fire.event.value, "forced": True})
        return fire

    def force_comedy(self, category: str, *, speech_ok: bool = True):
        from .catalog import entries_for
        from .comedy import ComedyBeat

        used = set(self.comedy.last_by_key)
        pool = [e for e in entries_for(category) if e.key not in used] or list(entries_for(category))
        if not pool:
            return None
        entry = self.rng.choice(pool)
        beat = ComedyBeat(
            entry,
            self.comedy.style_for(self.signals.signals.activity, self.narrative.current()),
            self.clock,
            entry.visual_only or not speech_ok,
        )
        self.comedy.last_by_key[entry.key] = self.clock
        self.comedy.last_by_category[entry.category] = self.clock
        if speech_ok and not entry.visual_only:
            self.comedy.last_line_t = self.clock
        self.last_beat = beat
        self.metrics.record("comedy_gag_used", t=self.clock, key=beat.entry.key, forced=True)
        if not beat.visual_only:
            self.caption = beat.entry.text
            self.caption_until = self.clock + 4.2
            self.soul.line = beat.entry.text
            self.soul.line_until = self.clock + 4.2
        return beat

    def note_toaster_click(self) -> None:
        self.bus.emit(WorldEvent.TOASTER_CLICK, self.clock, source="main_toaster")
        self.play_action(ToasterAction.JELLY_WARP)
        self.play_action(ToasterAction.CRUMB_BURST)
        self.soul.jelly = 1.0
        self.soul.crumbs = 1.0

    def note_toaster_hover(self, hovering: bool) -> None:
        if hovering:
            self.bus.emit(WorldEvent.TOASTER_HOVER, self.clock, source="main_toaster")
            self.animation.play(ClipId.HOVER_SCALE, self.clock, entity_id="main_toaster")

    def play_action(self, action: ToasterAction, *, look_target: Vec2 | None = None) -> None:
        if self.clock - self.soul.last_action_t < 0.08 and action is ToasterAction.BLINK:
            return
        if action is ToasterAction.CRUMB_BURST:
            self._burst(self.toaster + Vec2(0, 18), "crumb", 6)
            self.soul.crumbs = 1.0
        if action is ToasterAction.LOOK_AT_CURSOR:
            self.soul.look_mode = "cursor"
        self.actions.play(action, self.clock, look_target=look_target)
        self.soul.last_action = action.value
        self.soul.last_action_t = self.clock

    def tick(self, dt: float | None = None) -> dict:
        t0 = time.perf_counter()
        physics_t0 = t0
        wall = _now()
        if dt is None:
            raw = wall - self.last_t
            if raw > 2.0:
                self.bus.emit(WorldEvent.SYSTEM_SLEEP, self.clock, source="os")
                self.bus.emit(WorldEvent.SYSTEM_WAKE, self.clock + 0.01, source="os")
                dt = 0.05
            else:
                dt = min(0.05, max(0.008, raw))
        self.last_t = wall
        # Semi-fixed accumulator: consume clamped frame time in 1/30s substeps.
        self._dt_accum += dt
        steps = 0
        while self._dt_accum >= self._fixed_dt and steps < 4:
            self._dt_accum -= self._fixed_dt
            steps += 1
        if steps:
            dt = self._fixed_dt * steps
        self.clock += dt
        t = self.clock
        self.metrics.ticks += 1
        self.rare.recover(dt)
        signals = self.signals.tick(t)
        signals.browning_comedy = max((e.browning for e in self.entities if e.kind in {EntityKind.MINI_TOAST, EntityKind.BURNT_TOAST}), default=0.0)
        if getattr(signals, "work_rect", None) is not None:
            self.safety.set_work_rect(signals.work_rect)
        habitat = self.topology.habitat_at(signals.cursor)
        if habitat is not None:
            signals.active_monitor = habitat.index
        if signals.recent_click_t > 0 and t - signals.recent_click_t < 0.2:
            self.safety.attention.note_click(signals.recent_click, t)
        self.safety.refresh(signals, t)
        self.topology.refresh_from_qt()
        if self.topology.habitats:
            self.desktop = self.topology.virtual
        self.bootstrap()
        if self._dragging or signals.dragging or signals.selecting or signals.scrolling or signals.focus_score >= self.cfg.focus_threshold:
            self.last_layer = BehaviorLayer.L0_USER_INTENT.value
        else:
            self.last_layer = LAYER_PRIORITY[0].value
        if not self._dragging and self.home_anchor.length() > 1.0:
            if self.cfg.lock_character_position or t < self._user_lock_until:
                self.toaster = self.home_anchor
                self.behavior.toaster_pos = self.toaster
        self._update_soul(dt, t, signals)
        self._emit_ambient_events(t, signals)
        self._consume_events(t)
        calm = self.narrative.maybe_calm(t, signals)
        if calm:
            scene = calm
        else:
            scene = self.narrative.propose(t, signals, len(living_toasts(self.entities)), any(e.burnt for e in self.entities))
        fire = self.rare.maybe_fire(t, signals, len(living_toasts(self.entities)), any(e.burnt for e in self.entities))
        if fire:
            scene = self.narrative.force(t, self.rare.as_intent(fire))
            if fire.chaos is not None:
                self.chaos.start(fire.chaos, t)
            self.metrics.record("rare_event", t=t, event=fire.event.value)
            self.bus.emit(WorldEvent.RARE_EVENT_TICK, t, payload={"event": fire.event.value})
            if fire.event.value in {"MIDNIGHT_PORTAL", "SLOT_WITH_NO_BOTTOM", "HQ_VAE_CEREMONY"}:
                self.bus.emit(WorldEvent.PORTAL_OPEN, t)
                self.appliances.unlock(ApplianceKind.SECRET_CHAMBER)
            if fire.event.value == "THE_OTHER_MONITOR_RETURN":
                other = next((h for h in self.topology.habitats if not h.primary), None)
                if other is not None and living_toasts(self.entities):
                    living_toasts(self.entities)[0].pos = other.playable.center
                    living_toasts(self.entities)[0].monitor_index = other.index
            if fire.event.value == "THE_ONE_TOAST_CHOIR" and living_toasts(self.entities):
                for entity in living_toasts(self.entities)[:4]:
                    self.behavior.enter(entity, ToastState.DANCE, t, "scene")
            if fire.event.value == "WINDOW_EDGE_CAMP" and living_toasts(self.entities):
                living_toasts(self.entities)[0].target = Vec2(self.desktop.x + 12, self.desktop.y + 24)
            if fire.event.value == "TASKBAR_PARADE":
                self.chaos.start(ChaosMode.PARADE, t)
            if fire.event.value == "FOURTH_TERMINAL_ORACLE":
                for entity in living_toasts(self.entities)[:2]:
                    self.behavior.enter(entity, ToastState.TERMINAL_AWE, t, "scene")
            if fire.event.value == "FLAT_WHITE_ALIGNMENT":
                self.appliances.unlock(ApplianceKind.ESPRESSO)
            if fire.event.value == "CRUMB_KING":
                king = self.spawn_toast(self.toaster + Vec2(20, 30), EntityKind.CRUMB)
                king.local_intent = "crumb_king"
                self.chaos.start(ChaosMode.CRUMB_STORM, t)
            if fire.scene is SceneId.MICROWAVE_ANOMALY:
                self.appliances.unlock(ApplianceKind.MICROWAVE)
            if fire.scene is SceneId.AIRFRYER_VORTEX:
                self.appliances.unlock(ApplianceKind.AIR_FRYER)
            if fire.scene is SceneId.RICE_ZEN:
                self.appliances.unlock(ApplianceKind.RICE_COOKER)
            if fire.scene is SceneId.SHAREWARE_APOCALYPSE:
                self.dirty.force_full()
            if fire.event.value == "POPCORN_STORM":
                self.chaos.start(ChaosMode.CRUMB_STORM, t)
                self.spawn_toast(self.toaster + Vec2(self.rng.uniform(-40, 40), 40), EntityKind.POPCORN_KERNEL)
                self.audio.play("popcorn", signals, t=t)
            if not self.caption:
                self.caption = "Vzácný jev. Žádný meeting."
                self.caption_until = t + 3.2
            if fire.event.value == "BUTTER_ECLIPSE" and not any(e.kind is EntityKind.BUTTER_BLOB for e in self.entities):
                self.spawn_toast(self.toaster + Vec2(-20, 10), EntityKind.BUTTER_BLOB)
        self.appliances.tick(dt)
        self.chaos.tick(t, self.entities, self.desktop, signals)
        if signals.dragging or signals.selecting or signals.scrolling or signals.focus_score >= self.cfg.focus_threshold or signals.typing_rate > 4.0:
            if self.chaos.mode is not ChaosMode.NONE:
                self.chaos.collapse()
        self.doom.tick(t, signals, self.entities)
        for kind_pos in self.population.tick(dt, self.entities, signals.activity):
            kind = EntityKind.MINI_TOAST if kind_pos[0] == "mini_toast" else EntityKind.CRUMB
            self.spawn_toast(kind_pos[1] if kind_pos[1].length() > 1 else self.toaster + Vec2(-90, 30), kind)
        living_n = len(living_toasts(self.entities))
        if living_n != self._last_population:
            self.metrics.record("population_change", t=t, before=self._last_population, after=living_n, reason="ecology")
            self.bus.emit(WorldEvent.POPULATION_CHANGE, t, payload={"before": self._last_population, "after": living_n})
            self._last_population = living_n
        pop_event = self.population.event_name(living_n, t)
        if pop_event:
            self.bus.emit(pop_event, t)
        for ev, entity in self.keyboard.tick(t, signals, living_toasts(self.entities)):
            self.bus.emit(ev, t, entity_id=entity.id)
        speech_ok = (
            signals.focus_score < self.cfg.focus_threshold
            and not signals.dragging
            and not signals.selecting
            and not signals.scrolling
        )
        if scene.visual_only:
            speech_ok = False
        if self.llm.session.want_silence(t, signals.activity, signals.focus_score, self.rare.novelty, 0.0):
            speech_ok = False
        penalties_before = self.comedy.penalties
        force_cat = "butter" if any(e.kind is EntityKind.BUTTER_BLOB and not e.hidden for e in self.entities) else None
        beat = self.comedy.pick(
            t,
            signals,
            scene.scene,
            speech_ok=speech_ok and scene.speech_budget > 0,
            force_category=force_cat,
        )
        if self.comedy.penalties > penalties_before:
            self.metrics.record("gag_repetition_penalty", t=t, count=self.comedy.penalties - penalties_before)
        if beat:
            self.last_beat = beat
            self.metrics.record("comedy_gag_used", t=t, key=beat.entry.key, visual=beat.entry.visual)
            clip_name = beat.entry.visual
            try:
                clip = ClipId(clip_name)
                self.animation.play(clip, t, entity_id="main_toaster")
            except Exception:
                visual_alias = {
                    "slot_glow": ClipId.TOAST_BROWN,
                    "smoke": ClipId.TOAST_SMOKE,
                    "deadpan_stare": ClipId.DEADPAN_STARE,
                    "popcorn": ClipId.POPCORN_BURST,
                    "heart_burst": ClipId.HEART_BURST,
                    "sleep_melt": ClipId.SLEEP_MELT,
                    "portal_warp": ClipId.PORTAL_WARP,
                    "crowd_wave": ClipId.CROWD_WAVE,
                    "funeral_procession": ClipId.FUNERAL_PROCESSION,
                    "steam": ClipId.TOAST_SMOKE,
                    "swarm": ClipId.CROWD_WAVE,
                    "star_spin": ClipId.SPIN,
                    "vortex": ClipId.WIGGLE,
                    "rice_spirit": ClipId.IDLE_BREATHE,
                    "espresso_puff": ClipId.TOAST_SMOKE,
                    "crumb_burst": ClipId.TOAST_CRUMBS,
                    "quiet_audience": ClipId.IDLE_BREATHE,
                    "terminal_awe": ClipId.FACE_TURN,
                }.get(clip_name)
                if visual_alias is not None:
                    self.animation.play(visual_alias, t, entity_id="main_toaster")
            if not beat.visual_only:
                living_n_now = len(living_toasts(self.entities))
                allow_speech = living_n_now < 8 or self.rng.random() <= max(0.15, 1.2 / max(1, living_n_now))
                if allow_speech:
                    self.caption = beat.entry.text
                    self.caption_until = t + 4.2
                    self.soul.line = beat.entry.text
                    self.soul.line_until = t + 4.2
                    self.bus.emit(WorldEvent.VOICE_SPEAK_START, t, source="comedy")
        if self.soul.line and t > self.soul.line_until:
            last_end = self.bus.last(WorldEvent.VOICE_SPEAK_END)
            if last_end is None or t - last_end.t > 0.4:
                self.bus.emit(WorldEvent.VOICE_SPEAK_END, t, source="comedy")
                self.soul.line = ""
        accepted = self.llm.tick(self)
        if accepted is not None:
            scene = self.narrative.active.intent if self.narrative.active else scene
            self.metrics.record("llm_director_call", t=t, stage="accepted", scene=accepted.scene.value)
        elif self.cfg.llm_director_enabled:
            self.llm.request(self)
        self._apply_scene(scene.scene, t)
        if signals.dragging or signals.selecting or signals.scrolling or signals.focus_score >= self.cfg.focus_threshold:
            if self.performance.state.beat not in {SceneBeat.COMPLETED, SceneBeat.INTERRUPTED}:
                self.performance.interrupt(t)
                self.chaos.collapse()
                self.llm.session.note_interrupt(scene.scene)
                self.metrics.record("scene_interrupted", t=t, scene=scene.scene.value)
                self.metrics.record("scene_finished", t=t, scene=scene.scene.value, how="interrupted")
        elif self.performance.state.beat not in {SceneBeat.COMPLETED, SceneBeat.INTERRUPTED}:
            prev = self.performance.state.beat
            self.performance.tick(self, dt, t)
            if self.performance.state.beat is not prev:
                self.metrics.record("scene_beat", t=t, scene=scene.scene.value, beat=self.performance.state.beat.value)
            if self.performance.state.payoff_reached and prev is not SceneBeat.PAYOFF:
                self.metrics.record("scene_payoff", t=t, scene=scene.scene.value)
            if self.performance.state.completed and prev is not SceneBeat.COMPLETED:
                self.metrics.record("scene_completed", t=t, scene=scene.scene.value)
                self.metrics.record("scene_finished", t=t, scene=scene.scene.value, how="completed")
        if scene.scene is SceneId.SHAREWARE_APOCALYPSE:
            self.dirty.force_full()
        self._collect_prev_dirty()
        self._tick_entities(dt, t, signals)
        self._tick_particles(dt)
        self._collect_curr_dirty()
        if t > self.caption_until:
            self.caption = ""
        self.metrics.population = len(living_toasts(self.entities))
        self.metrics.entities = len(self.entities)
        self.metrics.scene = scene.scene.value
        self.metrics.activity = signals.activity.value
        self.metrics.novelty = self.rare.novelty
        physics_ms = (time.perf_counter() - physics_t0) * 1000.0
        frame_ms = (time.perf_counter() - t0) * 1000.0 + self.metrics.paint_ms
        self.metrics.physics_ms = physics_ms
        self.metrics.frame_ms = frame_ms
        self.metrics.record("physics_time", t=t, ms=round(physics_ms, 3))
        self.metrics.record("frame_time", t=t, ms=round(frame_ms, 3), paint_ms=round(self.metrics.paint_ms, 3))
        if self.metrics.paint_ms:
            self.metrics.record("paint_time", t=t, ms=round(self.metrics.paint_ms, 3))
        self.metrics.note_spike(frame_ms, scene.scene.value, self.metrics.entities)
        return self.snapshot()

    def _apply_scene(self, scene: SceneId, t: float) -> None:
        if scene is self._scene_applied:
            return
        self._scene_applied = scene
        esc = self.narrative.active.intent.escalation if self.narrative.active else 0.2
        self.performance.start(scene, t, esc)
        self.metrics.record("scene_started", t=t, scene=scene.value)
        if self.narrative.active:
            self.llm.session.note_setup(scene)
            if scene is SceneId.SHAREWARE_APOCALYPSE and self.chaos.mode is ChaosMode.NONE:
                self.chaos.start(ChaosMode.HAMMER_FAKEOUT, t, duration=2.4)
        living = living_toasts(self.entities)
        cast = 4
        if self.narrative.active:
            cast = max(1, min(self.narrative.active.intent.participants, 8))
        safe_living = [e for e in living if e.state not in {
            ToastState.AVOID_CURSOR, ToastState.AVOID_READING_ZONE, ToastState.PANIC_RUN,
            ToastState.HIDE, ToastState.WORK_QUIET_MODE, ToastState.TOASTING,
            ToastState.JUMP_INTO_SLOT, ToastState.OVERTOASTING,
        }]
        if scene is SceneId.TOAST_WARMUP_RITUAL:
            for entity in safe_living[:min(2, cast)]:
                if entity.state in {ToastState.IDLE, ToastState.WANDER}:
                    self.behavior.enter(entity, ToastState.APPROACH_TOASTER, t, "scene")
        elif scene is SceneId.TOAST_INFESTATION:
            self.population.infestation = True
            if safe_living and len(living) < self.cfg.max_mini_toasts_infestation and self.population.can_multiply(t, True):
                self.behavior.enter(safe_living[0], ToastState.MULTIPLY, t, "scene")
        elif scene is SceneId.KEYBOARD_FEEDING_FRENZY:
            self.keyboard.state.combo = max(self.keyboard.state.combo, 1)
            for entity in safe_living[:min(4, cast)]:
                self.behavior.enter(entity, ToastState.FOLLOW_CURSOR_AT_DISTANCE, t, "scene")
        elif scene is SceneId.POPCORN_BRAIN:
            for entity in safe_living[:min(6, cast)]:
                self.behavior.enter(entity, ToastState.POPCORN_AUDIENCE, t, "scene")
            if len([e for e in self.entities if e.kind is EntityKind.POPCORN_KERNEL]) < 4:
                self.spawn_toast(self.toaster + Vec2(self.rng.uniform(-80, 80), 50), EntityKind.POPCORN_KERNEL)
        elif scene is SceneId.KPOP_HEART_MODE:
            for entity in safe_living[:min(5, cast)]:
                self.behavior.enter(entity, ToastState.HEART_MODE, t, "scene")
        elif scene is SceneId.LATE_NIGHT_SLEEP:
            fridge = self.appliances.unlock(ApplianceKind.MINI_FRIDGE)
            fridge.heat = max(fridge.heat, 0.15)
            for entity in safe_living:
                self.behavior.enter(entity, ToastState.SLEEP, t, "scene")
                if (entity.pos - fridge.pos).length() < 160:
                    entity.target = fridge.pos + Vec2(0, 24)
        elif scene is SceneId.FUNERAL_RITUAL:
            for entity in safe_living:
                if entity.burnt:
                    self.behavior.enter(entity, ToastState.RITUAL_FUNERAL, t, "scene")
        elif scene is SceneId.PORTAL_GLITCH:
            chamber = self.appliances.unlock(ApplianceKind.SECRET_CHAMBER)
            if safe_living:
                safe_living[0].appliance = chamber.kind.value
                self.behavior.enter(safe_living[0], ToastState.PORTAL_CURIOUS, t, "scene")
        elif scene is SceneId.RICE_ZEN:
            self.appliances.unlock(ApplianceKind.RICE_COOKER)
            self.audio.play("rice_steam", self.signals.signals, t=t)
            if not any(e.kind is EntityKind.RICE_SPIRIT for e in self.entities):
                self.spawn_toast(self.toaster + Vec2(40, 70), EntityKind.RICE_SPIRIT)
        elif scene is SceneId.AIRFRYER_VORTEX:
            fryer = self.appliances.unlock(ApplianceKind.AIR_FRYER)
            if safe_living:
                safe_living[0].appliance = fryer.kind.value
                self.behavior.enter(safe_living[0], ToastState.PORTAL_CURIOUS, t, "scene")
        elif scene is SceneId.MICROWAVE_ANOMALY:
            ovenish = self.appliances.unlock(ApplianceKind.MICROWAVE)
            self.appliances.unlock(ApplianceKind.OVEN)
            self.audio.play("microwave_ping", self.signals.signals, t=t)
            if safe_living:
                safe_living[0].appliance = ovenish.kind.value
                self.behavior.enter(safe_living[0], ToastState.PORTAL_CURIOUS, t, "scene")
            if len([e for e in self.entities if e.kind is EntityKind.POPCORN_KERNEL]) < 3:
                kernel = self.spawn_toast(ovenish.pos + Vec2(0, 18), EntityKind.POPCORN_KERNEL)
                kernel.local_intent = "microwave_spawn"
                kernel.appliance = ovenish.kind.value
        elif scene is SceneId.SHAREWARE_APOCALYPSE:
            self.population.event_swarm = True
            if self.chaos.mode is ChaosMode.NONE:
                self.chaos.start(ChaosMode.TOAST_SWARM, t)
        elif scene is SceneId.HR_ROAST_SKIT:
            self.appliances.unlock(ApplianceKind.ESPRESSO)
        elif scene is SceneId.BURN_RECOVERY:
            for entity in living:
                if entity.burnt:
                    self.behavior.enter(entity, ToastState.RECOVER, t, "scene")
        elif scene is SceneId.SECRET_CHAMBER:
            chamber = self.appliances.unlock(ApplianceKind.SECRET_CHAMBER)
            chamber.impossible = True
            dest_kind = self.appliances.unlock(ApplianceKind.MICROWAVE)
            if safe_living:
                dest = dest_kind if dest_kind.kind is not ApplianceKind.SECRET_CHAMBER else self.appliances.other_exit(ApplianceKind.SECRET_CHAMBER)
                safe_living[0].appliance = chamber.kind.value
                self.behavior.enter(safe_living[0], ToastState.PORTAL_CURIOUS, t, "scene")
                if dest is not None:
                    self.appliances.open_passage(ApplianceKind.SECRET_CHAMBER, dest.kind, t, safe_living[0].id)
            if not any(e.kind is EntityKind.PORTAL_ECHO for e in self.entities):
                self.spawn_toast(self.toaster + Vec2(0, -80), EntityKind.PORTAL_ECHO)
            if self.narrative.active and self.narrative.active.intent.callback_key == "BUTTER_ECLIPSE":
                if not any(e.kind is EntityKind.BUTTER_BLOB for e in self.entities):
                    self.spawn_toast(self.toaster + Vec2(-30, 20), EntityKind.BUTTER_BLOB)
        elif scene is SceneId.RETURN_TO_CALM:
            self.population.event_swarm = False
            self.chaos.collapse()

    def _emit_ambient_events(self, t: float, signals: UserSignals) -> None:
        hour = __import__("time").localtime().tm_hour
        if hour >= 23 or hour < 5 or signals.activity is ActivityState.LATE_NIGHT:
            if not self._night_emitted:
                self.bus.emit(WorldEvent.NIGHT_THEME, t, source="signals")
                self._night_emitted = True
            self.night_mode = True
        else:
            self._night_emitted = False
            self.night_mode = False
        if signals.novelty_seeking_proxy < 0.12 and signals.mouse_idle_time > 20.0:
            if not self._weather_emitted:
                self.bus.emit(WorldEvent.WEATHER_GLOOMY_SIGNAL, t, source="signals")
                self._weather_emitted = True
        elif signals.mouse_idle_time < 4.0:
            self._weather_emitted = False
        if getattr(signals, "system_sleep", False):
            self.bus.emit(WorldEvent.SYSTEM_SLEEP, t, source="signals")

    def _consume_events(self, t: float) -> None:
        """Event consumers propose; BehaviorEngine.enter still owns the commit."""
        recent = self.bus.recent(limit=8)
        living = living_toasts(self.entities)
        if not living or not recent:
            return
        for event in recent:
            if event.event_id and any(e.last_event_id == event.event_id for e in living):
                continue
            dest = self.behavior.propose_from_event(living[0], event.name)
            if dest is None:
                continue
            for entity in living[:3]:
                if entity.proposed is None and entity.state not in RITUAL_STATES and entity.state not in {
                    ToastState.APPROACH_TOASTER,
                    ToastState.QUEUE_FOR_SLOT,
                    ToastState.PORTAL_CURIOUS,
                    ToastState.PORTAL_ENTER,
                    ToastState.PORTAL_RETURN,
                    ToastState.INSIDE_APPLIANCE,
                }:
                    entity.proposed = dest
                    entity.last_event_id = event.event_id

    def _update_soul(self, dt: float, t: float, signals: UserSignals) -> None:
        pets = living_toasts(self.entities)
        if self.doom.toaster_watching and pets:
            self.soul.look_mode = "pet"
            self.soul.pet_target = pets[0].pos
            self.soul.jelly = max(self.soul.jelly, 0.35)
        if getattr(self.doom, "comment", "") and not self.caption and self.doom.phase.value == "DEADPAN_COMMENT":
            self.caption = self.doom.comment
            self.caption_until = t + 3.0
            self.soul.line = self.doom.comment
            self.soul.line_until = t + 3.0
        if self.soul.look_mode == "pet" and pets:
            target = self.soul.pet_target or pets[0].pos
            look = target - self.toaster
        else:
            look = signals.cursor - self.toaster
            self.soul.look_mode = "cursor"
        self.soul.look = Vec2(max(-1.0, min(1.0, look.x / 240.0)), max(-1.0, min(1.0, look.y / 240.0)))
        self.soul.jelly = max(0.0, self.soul.jelly - dt * 2.4)
        self.soul.crumbs = max(0.0, self.soul.crumbs - dt * 1.1)
        self.soul.slot_glow = max(0.0, self.soul.slot_glow - dt * 1.4)
        self.soul.steam = max(0.0, self.soul.steam - dt * 1.2)
        occupied = any(s.occupied_by for s in self.slots)
        self.soul.heat += ((0.85 if occupied else 0.32) - self.soul.heat) * min(1.0, dt * 2.0)
        self.soul.shimmer = 0.25 + 0.2 * abs(__import__("math").sin(t * 3.1)) * self.soul.heat
        if occupied and self.soul.slot_glow < 0.2 and t - self.soul.last_action_t > 0.6:
            self.play_action(ToasterAction.SLOT_GLOW)
            self.audio.play("heat_hum", signals, t=t)
        if self.soul.heat > 0.7 and t - self.soul.last_action_t > 2.4:
            self.play_action(ToasterAction.HEAT_SHIMMER)
            self.audio.play("heat_hum", signals, t=t)
        blink = 0.5 + 0.5 * __import__("math").sin(t * 0.85)
        if blink > 0.985 and t - self.soul.last_action_t > 1.6:
            self.play_action(ToasterAction.DOUBLE_BLINK if self.rng.random() < 0.18 else ToasterAction.BLINK)
        self.soul.blink = max(self.soul.blink * 0.6, 1.0 if blink > 0.96 else 0.0)
        self.actions.tick(self.soul, t)
        if t > self.soul.line_until:
            self.soul.line = ""
        if self.soul.line and t - self.soul.voice_ready_t < self.cfg.voice_cooldown_sec:
            pass
        elif self.soul.line:
            self.soul.voice_ready_t = t

    def _tick_entities(self, dt: float, t: float, signals: UserSignals) -> None:
        keep: list[Entity] = []
        self.last_layer = BehaviorLayer.L1_ATTENTION_AVOIDANCE.value
        for entity in self.entities:
            lifespan_scale = AMBIENT_LIFESPAN.get(entity.kind)
            if (
                lifespan_scale is not None
                and entity.state is not ToastState.DESPAWN
                and entity.age > lifespan_scale * self.cfg.ambient_lifespan_sec
            ):
                # Ambient noise-makers are ephemeral: they pop (kernel burst +
                # sound) and despawn instead of accumulating permanent clutter.
                if entity.kind is EntityKind.POPCORN_KERNEL:
                    self._burst(entity.pos, "crumb", 5)
                    self.audio.play("popcorn", signals, t=t)
                trans = self.behavior.enter(entity, ToastState.DESPAWN, t, "lifecycle")
                if trans:
                    self._log_transition(trans)
                self.metrics.record("entity_despawned", t=t, id=entity.id)
                self.metrics.record("population_change", t=t, delta=-1, reason="lifespan", id=entity.id)
                continue
            field = self.safety.evaluate(entity.pos, entity.radius, signals, self.desktop)
            ritual = entity.state in RITUAL_STATES
            # Ritual may keep semantic occupancy, but visual yield stays on.
            trans = self.behavior.apply_safety(entity, field, t)
            if trans:
                self._log_transition(trans)
                self.metrics.record("attention_retreat", t=t, entity=entity.id, reason=field.reason.value)
                self.metrics.record("safety_override", t=t, entity=entity.id, reason=field.reason.value)
                self.bus.emit(WorldEvent.SAFETY_YIELD, t, entity_id=entity.id, payload={"reason": field.reason.value})
            elif field.must_yield and ritual:
                self.metrics.record("safety_override", t=t, entity=entity.id, reason=field.reason.value, visual=True)
            proposed = entity.proposed
            entity.proposed = None
            if proposed is not None and (entity.state in {
                ToastState.AVOID_CURSOR,
                ToastState.AVOID_READING_ZONE,
                ToastState.PANIC_RUN,
                ToastState.HIDE,
                ToastState.WORK_QUIET_MODE,
                ToastState.TOASTING,
                ToastState.OVERTOASTING,
                ToastState.JUMP_INTO_SLOT,
            } or field.must_yield):
                proposed = None
            nxt = None
            reason = "graph"
            if not field.must_yield or ritual:
                nxt = self.behavior.choose(entity, t, signals, self.slots, self.entities)
                if nxt is not None:
                    reason = "lifecycle" if nxt is ToastState.DESPAWN else "graph"
                elif proposed is not None and entity.state not in {
                    ToastState.PORTAL_CURIOUS,
                    ToastState.PORTAL_ENTER,
                    ToastState.PORTAL_RETURN,
                    ToastState.INSIDE_APPLIANCE,
                }:
                    nxt = proposed
                    reason = "lifecycle" if proposed is ToastState.DESPAWN else "event"
            if nxt:
                if nxt is ToastState.PORTAL_ENTER and entity.appliance:
                    item = None
                    try:
                        item = self.appliances.by_kind(ApplianceKind(entity.appliance))
                    except Exception:
                        item = None
                    cooldown = self.cfg.portal_cooldown_hours * 3600.0
                    if item is not None and item.last_used > 0 and t - item.last_used < cooldown:
                        nxt = ToastState.WANDER
                    else:
                        try:
                            self.appliances.admit(ApplianceKind(entity.appliance), t)
                        except Exception:
                            pass
                if entity.state is ToastState.INSIDE_APPLIANCE and nxt is ToastState.PORTAL_RETURN and entity.appliance:
                    try:
                        self.appliances.release(ApplianceKind(entity.appliance))
                    except Exception:
                        pass
                trans = self.behavior.enter(entity, nxt, t, reason)
                if trans:
                    self._log_transition(trans)
            if entity.state is ToastState.DESPAWN:
                self.metrics.record("entity_despawned", t=t, id=entity.id)
                self.metrics.record("population_change", t=t, delta=-1, reason="despawn", id=entity.id)
                continue
            habitat = self.topology.habitat_at(entity.pos)
            if habitat is not None:
                entity.monitor_index = habitat.index
                nudge = self.safety.habitat_nudge(entity.pos, habitat)
                if nudge.length() > 0:
                    entity.vel = entity.vel + nudge * 18.0
                    if not habitat.primary:
                        field.zone = SafetyZone.SECONDARY_MONITOR_PLAY_ZONE
            self.last_layer = BehaviorLayer.L3_WORLD_PHYSICS.value
            self.behavior.integrate(entity, dt, t, signals, habitat.playable if habitat else self.desktop, self.slots, self.entities, self.toaster)
            if field.must_yield:
                safe_target, fail = self.safety.low_attention_target(entity.pos, self.desktop, signals, habitat)
                if fail is OverrideReason.NO_SAFE_TARGET and entity.state not in RITUAL_STATES:
                    field.reason = OverrideReason.NO_SAFE_TARGET
                    entity.opacity = min(entity.opacity, 0.12)
                    trans_fail = self.behavior.enter(entity, ToastState.DESPAWN, t, "safety")
                    if trans_fail:
                        self._log_transition(trans_fail)
                        continue
                elif safe_target is not None:
                    entity.target = safe_target
                steered = self.safety.steer(entity.pos, entity.vel, field, self.cfg.panic_speed)
                entity.vel = steered
                entity.pos = entity.pos + steered * dt
                entity.last_override = field.reason.value
                entity.opacity = min(entity.opacity, field.opacity_scale)
                entity.safety_offset = field.retreat * 18.0
                if (entity.pos - signals.cursor).length() < entity.radius + 28:
                    last = self.bus.last(WorldEvent.MOUSE_APPROACH_ENTITY)
                    if last is None or t - last.t > 0.25:
                        self.bus.emit(WorldEvent.MOUSE_APPROACH_ENTITY, t, entity_id=entity.id)
            elif (entity.pos - signals.cursor).length() < entity.radius + 36:
                last = self.bus.last(WorldEvent.TOAST_HOVER)
                if last is None or t - last.t > 0.4:
                    self.bus.emit(WorldEvent.TOAST_HOVER, t, entity_id=entity.id)
                if signals.buttons_down and t - signals.recent_click_t < 0.12:
                    last_click = self.bus.last(WorldEvent.TOAST_CLICK)
                    if last_click is None or t - last_click.t > 0.25:
                        self.bus.emit(WorldEvent.TOAST_CLICK, t, entity_id=entity.id)
            compose_pose(entity, t, signals, self.animation)
            if nxt is ToastState.PORTAL_RETURN:
                other = next((h for h in self.topology.habitats if h.index != entity.monitor_index), None)
                emerge = None
                if entity.appliance:
                    try:
                        emerge = self.appliances.other_exit(ApplianceKind(entity.appliance))
                    except Exception:
                        emerge = None
                if other is not None:
                    entity.pos = other.playable.center
                    entity.monitor_index = other.index
                elif emerge is not None:
                    entity.pos = emerge.pos + Vec2(self.rng.uniform(-16, 16), 18)
                    entity.appliance = emerge.kind.value
                else:
                    entity.pos = self.toaster + Vec2(self.rng.uniform(-40, 40), -90)
                    self.animation.play(ClipId.TELEPORT_GLITCH, t, entity_id=entity.id)
            if entity.state in {ToastState.WANDER, ToastState.FOLLOW_CURSOR_AT_DISTANCE} and entity.hop_phase < dt * 4:
                self.audio.play("tiny_step", signals, t=t)
            if entity.state is ToastState.OVERTOASTING:
                self.audio.play("steam_hiss", signals, t=t)
            if entity.state is ToastState.POP_OUT:
                self._burst(entity.pos, "crumb", 6)
                self.audio.play("crumb_tick", signals, t=t)
                if entity.previous_state in {ToastState.TOASTING, ToastState.OVERTOASTING, ToastState.JUMP_INTO_SLOT}:
                    if not any(e.kind is EntityKind.CRUMB and (e.pos - entity.pos).length() < 8 for e in self.entities):
                        self.spawn_toast(entity.pos + Vec2(self.rng.uniform(-10, 10), 8), EntityKind.CRUMB)
            if entity.state is ToastState.HEART_MODE:
                self._burst(entity.pos + Vec2(0, -12), "heart", 1)
            keep.append(entity)
        self.entities = keep
        for kind, pos in self.behavior.pending_spawns:
            if len(living_toasts(self.entities)) < self.cfg.max_mini_toasts_event:
                self.spawn_toast(pos, kind)
                self.metrics.record("population_change", t=t, delta=1, reason="spawn")
        self.behavior.pending_spawns.clear()

    def _burst(self, pos: Vec2, kind: str, n: int) -> None:
        room = self.cfg.max_particles - len(self.particles)
        for _ in range(min(n, max(0, room))):
            self.particles.append(
                Particle(
                    pos=pos,
                    vel=Vec2(self.rng.uniform(-60, 60), self.rng.uniform(-90, -10)),
                    life=self.rng.uniform(0.35, 0.9),
                    kind=kind,
                )
            )

    def _tick_particles(self, dt: float) -> None:
        live: list[Particle] = []
        for particle in self.particles:
            particle.life -= dt
            particle.vel = gravity(particle.vel, dt, g=180.0)
            particle.pos = particle.pos + particle.vel * dt
            if particle.life > 0:
                live.append(particle)
        self.particles = live

    def _aabb(self, pos: Vec2, radius: float, extra: float = 0.0) -> Rect:
        span = radius + extra
        return Rect(pos.x - span, pos.y - span, span * 2, span * 2)

    def _collect_prev_dirty(self) -> None:
        for entity in self.entities:
            if entity.prev_aabb is not None:
                self.dirty.add(entity.prev_aabb)
            else:
                self.dirty.add(self._aabb(entity.pos, entity.radius, abs(entity.pose.hop) + 8))
        for particle in self.particles:
            self.dirty.add(self._aabb(particle.pos, 6.0, 4.0), pad=8)

    def _collect_curr_dirty(self) -> None:
        for entity in self.entities:
            box = self._aabb(entity.pos, entity.radius, abs(entity.pose.hop) + abs(entity.pose.smear) * 20 + 10)
            entity.prev_aabb = box
            self.dirty.add(box)
        for particle in self.particles:
            self.dirty.add(self._aabb(particle.pos, 6.0, 4.0), pad=8)
        for appliance in self.appliances.items:
            if appliance.open:
                self.dirty.add(self._aabb(appliance.pos, 22.0, appliance.interior_scale * 4))
        if self.soul.jelly > 0.02 or self.soul.crumbs > 0.02:
            self.dirty.add(self._aabb(self.toaster, 90.0, 20.0))
        if self.caption:
            self.dirty.add(Rect(self.desktop.x + 8, self.desktop.y + 8, 640, 36), pad=4)
        if self.metrics.paint_ms:
            self.metrics.frame_ms = self.metrics.physics_ms + self.metrics.paint_ms

    def consume_dirty(self) -> tuple[bool, list[Rect]]:
        full, rects = self.dirty.consume(self.desktop)
        self.metrics.dirty_count = self.dirty.last_count
        self.metrics.dirty_pixels = self.dirty.last_pixels
        self.metrics.dirty_ratio = self.dirty.last_ratio
        self.metrics.dirty_full = full
        return full, rects

    def _log_transition(self, trans: Transition) -> None:
        self.metrics.record(
            "transition",
            t=trans.t,
            entity=trans.entity_id,
            src=trans.src.value,
            dst=trans.dst.value,
            reason=trans.reason,
            trigger=getattr(trans, "trigger", ""),
            override=getattr(trans, "override_reason", ""),
            layer=getattr(trans, "layer", ""),
        )

    def debug_snapshot(self) -> dict:
        return {
            "anchors_ready": self.anchors_ready,
            "scene": self.narrative.current().value,
            "performance": self.performance.snapshot(),
            "director": self.llm.metrics.snapshot(),
            "session": self.llm.session.snapshot(),
            "attention": self.safety.field.snapshot(),
            "monitors": self.topology.snapshot(),
            "population": self.metrics.population,
            "novelty": round(self.rare.novelty, 3),
            "config": self.cfg.snapshot(),
            "layer": self.last_layer,
            "world_id": self.world_id,
            "session_id": self.session_id,
            "soul_action": self.soul.last_action,
            "night_mode": self.night_mode,
            "narrative_callbacks": sorted(self.narrative.callbacks_seen),
            "novelty_cooldown_until": self.narrative.novelty_cooldown_until,
        }

    def snapshot(self) -> dict:
        return {
            "enabled": self.cfg.enabled,
            "scene": self.narrative.current().value,
            "activity": self.signals.signals.activity.value,
            "population": len(living_toasts(self.entities)),
            "caption": self.caption,
            "keyboard": self.keyboard.state.mode.value,
            "doom": self.doom.phase.value,
            "chaos": self.chaos.mode.value,
            "novelty": round(self.rare.novelty, 3),
            "entities": [e.snapshot() for e in self.entities],
            "appliances": [a.snapshot() for a in self.appliances.items],
            "metrics": self.metrics.snapshot(),
            "signals": self.signals.signals.snapshot(),
            "soul": {
                "look": (round(self.soul.look.x, 3), round(self.soul.look.y, 3)),
                "jelly": round(self.soul.jelly, 3),
                "heat": round(self.soul.heat, 3),
                "line": self.soul.line,
            },
            "anchors_ready": self.anchors_ready,
            "monitors": self.topology.snapshot(),
            "attention": self.safety.field.snapshot(),
            "director": self.llm.metrics.snapshot(),
            "performance": self.performance.snapshot(),
            "config": self.cfg.snapshot(),
            "layer": self.last_layer,
            "night_mode": self.night_mode,
            "character_form": self.character_form,
            "home_anchor": (round(self.home_anchor.x, 1), round(self.home_anchor.y, 1)),
            "dragging": self._dragging,
        }
