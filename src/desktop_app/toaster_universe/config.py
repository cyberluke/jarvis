"""Tunable world parameters. Every disruptive knob is bounded and rollback-safe."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any, Mapping


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


@dataclass
class WorldConfig:
    enabled: bool = True
    cursor_panic_radius: float = 72.0
    cursor_strong_radius: float = 140.0
    cursor_soft_radius: float = 220.0
    cursor_predict_ms: float = 120.0
    reading_sanctuary_ms: float = 900.0
    reading_sanctuary_radius: float = 260.0
    click_sanctuary_ms: float = 700.0
    click_sanctuary_radius: float = 90.0
    typing_fast_threshold: float = 7.5
    typing_burst_threshold: float = 12.0
    doomscroll_threshold: float = 0.62
    tab_switch_threshold: float = 0.35
    focus_threshold: float = 0.68
    max_mini_toasts_normal: int = 8
    max_mini_toasts_infestation: int = 20
    max_mini_toasts_event: int = 36
    rare_event_base_rate: float = 0.0003
    comedy_cooldown_sec: float = 90.0
    voice_cooldown_sec: float = 180.0
    max_scene_duration_sec: float = 28.0
    max_particles: int = 80
    max_physics_entities: int = 48
    animation_fps_target: float = 30.0
    toasting_browning_rate: float = 0.22
    overtoast_browning_rate: float = 0.30
    ambient_lifespan_sec: float = 75.0
    llm_director_enabled: bool = False
    llm_director_timeout_ms: float = 8000.0
    llm_director_cooldown_sec: float = 45.0
    llm_director_max_calls_per_hour: int = 24
    llm_director_ttl_ambient_sec: float = 45.0
    llm_director_ttl_long_sec: float = 90.0
    llm_director_prewarm: bool = True
    novelty_budget_recovery: float = 0.012
    population_growth_rate: float = 0.08
    population_decay_rate: float = 0.05
    portal_cooldown_hours: float = 3.0
    wander_speed: float = 46.0
    hop_speed: float = 78.0
    panic_speed: float = 210.0
    social_radius: float = 54.0
    toaster_approach_radius: float = 86.0
    toasting_sec: float = 3.4
    overtoast_sec: float = 2.2
    cool_down_sec: float = 2.6
    sleep_after_idle_sec: float = 38.0
    temperature_loss_per_sec: float = 0.035
    temperature_gain_in_slot: float = 0.28
    burn_threshold: float = 0.92
    work_quiet_opacity: float = 0.28
    click_through_pets: bool = True
    spawn_on_start: int = 1
    shareware_chaos_enabled: bool = True
    keyboard_games_enabled: bool = True
    doomscroll_layer_enabled: bool = True
    appliances_enabled: bool = True
    rare_events_enabled: bool = True
    personal_lore_enabled: bool = True
    audio_enabled: bool = True
    audio_volume: float = 0.32
    dirty_padding: float = 18.0
    dirty_merge_gap: float = 28.0
    character_form: str = "classic_toaster"
    population_level: str = "cozy"
    comedy_level: str = "balanced"
    chatter_level: str = "normal"
    work_activity: str = "adaptive"
    lock_character_position: bool = False
    rare_event_frequency: str = "occasional"
    world_sounds: bool = True
    character_voice: bool = True
    ambient_sounds: bool = True
    voice_volume: float = 0.32
    autonomous_characters: bool = True
    home_anchor_x: float = -1.0
    home_anchor_y: float = -1.0
    home_monitor_key: str = ""

    def clamp(self) -> "WorldConfig":
        self.cursor_panic_radius = _clamp(self.cursor_panic_radius, 24.0, 160.0)
        self.cursor_strong_radius = _clamp(self.cursor_strong_radius, 60.0, 280.0)
        self.cursor_soft_radius = _clamp(self.cursor_soft_radius, 100.0, 420.0)
        self.cursor_predict_ms = _clamp(self.cursor_predict_ms, 40.0, 240.0)
        self.reading_sanctuary_ms = _clamp(self.reading_sanctuary_ms, 120.0, 4000.0)
        self.reading_sanctuary_radius = _clamp(self.reading_sanctuary_radius, 40.0, 520.0)
        self.click_sanctuary_ms = _clamp(self.click_sanctuary_ms, 80.0, 4000.0)
        self.click_sanctuary_radius = _clamp(self.click_sanctuary_radius, 24.0, 240.0)
        self.typing_fast_threshold = _clamp(self.typing_fast_threshold, 1.0, 24.0)
        self.typing_burst_threshold = _clamp(self.typing_burst_threshold, 2.0, 40.0)
        self.doomscroll_threshold = _clamp(self.doomscroll_threshold, 0.15, 0.95)
        self.tab_switch_threshold = _clamp(self.tab_switch_threshold, 0.05, 2.0)
        self.focus_threshold = _clamp(self.focus_threshold, 0.2, 0.95)
        self.max_mini_toasts_normal = int(_clamp(self.max_mini_toasts_normal, 0, 12))
        self.max_mini_toasts_infestation = int(_clamp(self.max_mini_toasts_infestation, 1, 28))
        self.max_mini_toasts_event = int(_clamp(self.max_mini_toasts_event, 4, 48))
        self.rare_event_base_rate = _clamp(self.rare_event_base_rate, 0.0, 0.01)
        self.comedy_cooldown_sec = _clamp(self.comedy_cooldown_sec, 8.0, 600.0)
        self.voice_cooldown_sec = _clamp(self.voice_cooldown_sec, 8.0, 900.0)
        self.max_scene_duration_sec = _clamp(self.max_scene_duration_sec, 4.0, 60.0)
        self.max_particles = int(_clamp(self.max_particles, 8, 240))
        self.max_physics_entities = int(_clamp(self.max_physics_entities, 4, 96))
        self.animation_fps_target = _clamp(self.animation_fps_target, 12.0, 60.0)
        self.llm_director_timeout_ms = _clamp(self.llm_director_timeout_ms, 250.0, 30000.0)
        self.llm_director_cooldown_sec = _clamp(self.llm_director_cooldown_sec, 4.0, 600.0)
        self.novelty_budget_recovery = _clamp(self.novelty_budget_recovery, 0.0, 0.2)
        self.population_growth_rate = _clamp(self.population_growth_rate, 0.0, 2.0)
        self.population_decay_rate = _clamp(self.population_decay_rate, 0.0, 2.0)
        self.portal_cooldown_hours = _clamp(self.portal_cooldown_hours, 0.05, 72.0)
        self.wander_speed = _clamp(self.wander_speed, 8.0, 180.0)
        self.hop_speed = _clamp(self.hop_speed, 12.0, 240.0)
        self.panic_speed = _clamp(self.panic_speed, 40.0, 420.0)
        self.sleep_after_idle_sec = _clamp(self.sleep_after_idle_sec, 4.0, 180.0)
        self.audio_volume = _clamp(self.audio_volume, 0.0, 1.0)
        self.spawn_on_start = int(_clamp(self.spawn_on_start, 0, 8))
        self.llm_director_max_calls_per_hour = int(_clamp(self.llm_director_max_calls_per_hour, 1, 120))
        self.llm_director_ttl_ambient_sec = _clamp(self.llm_director_ttl_ambient_sec, 8.0, 180.0)
        self.llm_director_ttl_long_sec = _clamp(self.llm_director_ttl_long_sec, 12.0, 240.0)
        self.toasting_browning_rate = _clamp(self.toasting_browning_rate, 0.02, 0.6)
        self.overtoast_browning_rate = _clamp(self.overtoast_browning_rate, 0.05, 0.9)
        self.ambient_lifespan_sec = _clamp(self.ambient_lifespan_sec, 4.0, 300.0)
        self.toasting_sec = _clamp(self.toasting_sec, 0.8, 12.0)
        self.overtoast_sec = _clamp(self.overtoast_sec, 0.4, 10.0)
        self.cool_down_sec = _clamp(self.cool_down_sec, 0.4, 12.0)
        self.temperature_loss_per_sec = _clamp(self.temperature_loss_per_sec, 0.0, 0.4)
        self.temperature_gain_in_slot = _clamp(self.temperature_gain_in_slot, 0.0, 1.0)
        self.burn_threshold = _clamp(self.burn_threshold, 0.4, 1.0)
        self.work_quiet_opacity = _clamp(self.work_quiet_opacity, 0.05, 1.0)
        self.social_radius = _clamp(self.social_radius, 16.0, 160.0)
        self.toaster_approach_radius = _clamp(self.toaster_approach_radius, 24.0, 220.0)
        self.dirty_padding = _clamp(self.dirty_padding, 4.0, 64.0)
        self.dirty_merge_gap = _clamp(self.dirty_merge_gap, 4.0, 96.0)
        self.voice_volume = _clamp(self.voice_volume, 0.0, 1.0)
        if self.population_level not in {"quiet", "cozy", "alive", "busy", "chaos"}:
            self.population_level = "cozy"
        if self.comedy_level not in {"calm", "balanced", "unhinged"}:
            self.comedy_level = "balanced"
        if self.chatter_level not in {"off", "low", "normal", "high"}:
            self.chatter_level = "normal"
        if self.work_activity not in {"minimal", "adaptive", "normal"}:
            self.work_activity = "adaptive"
        if self.rare_event_frequency not in {"rare", "occasional", "frequent"}:
            self.rare_event_frequency = "occasional"
        if self.character_form not in {
            "classic_toaster",
            "rice_cooker_zen",
            "microwave",
            "air_fryer",
            "espresso",
            "oven",
            "mini_fridge",
        }:
            self.character_form = "classic_toaster"
        return self

    def apply_override(self, **values: Any) -> dict[str, Any]:
        """Runtime canary override. Previous values are returned for rollback."""
        previous: dict[str, Any] = {}
        for name, value in values.items():
            if not hasattr(self, name):
                continue
            previous[name] = getattr(self, name)
            setattr(self, name, type(getattr(self, name))(value))
        self.clamp()
        return previous

    def rollback(self, previous: Mapping[str, Any]) -> None:
        for name, value in previous.items():
            if hasattr(self, name):
                setattr(self, name, value)
        self.clamp()

    def snapshot(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


_KEY_MAP = {
    "toaster_universe_enabled": "enabled",
    "cursorPanicRadius": "cursor_panic_radius",
    "cursorStrongRadius": "cursor_strong_radius",
    "cursorSoftRadius": "cursor_soft_radius",
    "readingSanctuaryMs": "reading_sanctuary_ms",
    "readingSanctuaryRadius": "reading_sanctuary_radius",
    "typingFastThreshold": "typing_fast_threshold",
    "typingBurstThreshold": "typing_burst_threshold",
    "doomscrollThreshold": "doomscroll_threshold",
    "tabSwitchThreshold": "tab_switch_threshold",
    "focusThreshold": "focus_threshold",
    "maxMiniToastsNormal": "max_mini_toasts_normal",
    "maxMiniToastsInfestation": "max_mini_toasts_infestation",
    "rareEventBaseRate": "rare_event_base_rate",
    "comedyCooldownSec": "comedy_cooldown_sec",
    "voiceCooldownSec": "voice_cooldown_sec",
    "maxSceneDurationSec": "max_scene_duration_sec",
    "maxParticles": "max_particles",
    "maxPhysicsEntities": "max_physics_entities",
    "animationFpsTarget": "animation_fps_target",
    "llmDirectorEnabled": "llm_director_enabled",
    "llmDirectorTimeoutMs": "llm_director_timeout_ms",
    "llmDirectorCooldownSec": "llm_director_cooldown_sec",
    "llmDirectorMaxCallsPerHour": "llm_director_max_calls_per_hour",
    "llmDirectorPrewarm": "llm_director_prewarm",
    "toastingBrowningRate": "toasting_browning_rate",
    "overtoastBrowningRate": "overtoast_browning_rate",
    "ambientLifespanSec": "ambient_lifespan_sec",
    "noveltyBudgetRecovery": "novelty_budget_recovery",
    "populationGrowthRate": "population_growth_rate",
    "populationDecayRate": "population_decay_rate",
    "portalCooldownHours": "portal_cooldown_hours",
    "toaster_universe_character_form": "character_form",
    "toaster_universe_population_level": "population_level",
    "toaster_universe_comedy_level": "comedy_level",
    "toaster_universe_chatter_level": "chatter_level",
    "toaster_universe_work_activity": "work_activity",
    "toaster_universe_lock_character_position": "lock_character_position",
    "toaster_universe_rare_event_frequency": "rare_event_frequency",
    "toaster_universe_world_sounds": "world_sounds",
    "toaster_universe_character_voice": "character_voice",
    "toaster_universe_ambient_sounds": "ambient_sounds",
    "toaster_universe_voice_volume": "voice_volume",
    "toaster_universe_autonomous_characters": "autonomous_characters",
    "toaster_universe_home_anchor_x": "home_anchor_x",
    "toaster_universe_home_anchor_y": "home_anchor_y",
    "toaster_universe_home_monitor_key": "home_monitor_key",
}


def load_world_config(raw: Mapping[str, Any] | None = None) -> WorldConfig:
    cfg = WorldConfig()
    if raw is None:
        try:
            from jarvis.config import load_config

            raw = load_config()
        except Exception:
            raw = {}
    block = raw.get("toaster_universe") if isinstance(raw.get("toaster_universe"), dict) else {}
    merged: dict[str, Any] = dict(raw)
    merged.update(block)
    for src, dest in _KEY_MAP.items():
        if src in merged and merged[src] is not None:
            setattr(cfg, dest, type(getattr(cfg, dest))(merged[src]))
    for field in fields(cfg):
        snake = f"toaster_universe_{field.name}"
        if snake in merged and merged[snake] is not None:
            setattr(cfg, field.name, type(getattr(cfg, field.name))(merged[snake]))
    return cfg.clamp()
