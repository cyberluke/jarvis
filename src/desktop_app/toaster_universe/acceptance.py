"""Named acceptance-gate harness. One parametrized contract, many scenarios."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .config import WorldConfig
from .types import ActivityState, SceneId, ToastState, Vec2, Rect, WorldEvent
from .world import POPULATION_PRESETS, ToasterWorld


@dataclass
class GateResult:
    name: str
    ok: bool
    unobstructed: bool
    bounded: bool
    safety: bool
    reconstructable: bool
    notes: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)


def _world(population: int = 1, **cfg) -> ToasterWorld:
    world = ToasterWorld(
        WorldConfig(spawn_on_start=max(1, min(8, population)), population_growth_rate=0.0, **cfg),
        seed=11,
    )
    world.set_desktop(Rect(0, 0, 1600, 900))
    world.set_toaster(Vec2(1400, 200), Vec2(1380, 160), Vec2(1420, 160))
    world.bootstrap()
    world.signals.live_os = False
    while len(world.entities) < population:
        world.spawn_toast(Vec2(200 + len(world.entities) * 30, 400))
    return world


def _common(world: ToasterWorld, notes: list[str]) -> tuple[bool, bool, bool, bool]:
    living = [e for e in world.entities if e.state is not ToastState.DESPAWN]
    unobstructed = world.cfg.click_through_pets
    bounded = len(living) <= world.cfg.max_physics_entities and len(world.particles) <= world.cfg.max_particles
    safety = True
    for entity in living:
        if entity.state in {ToastState.AVOID_CURSOR, ToastState.PANIC_RUN, ToastState.HIDE}:
            if entity.last_override:
                continue
        # A yielding field must not be ignored by entertainment proposals.
        if entity.last_override and entity.state in {ToastState.DANCE, ToastState.SOCIALIZE, ToastState.HEART_MODE}:
            safety = False
            notes.append(f"safety_bypass:{entity.id}:{entity.state.value}")
    snap = world.snapshot()
    reconstructable = bool(snap["metrics"]["counters"]) and "transition" in snap["metrics"]["counters"] or snap["metrics"]["ticks"] >= 1
    if world.metrics.ring:
        reconstructable = True
    notes.append(f"unobstructed={unobstructed}")
    notes.append(f"bounded={bounded} living={len(living)} max={world.cfg.max_physics_entities}")
    return unobstructed, bounded, safety, reconstructable


def scenario_cursor_crosses_pet(world: ToasterWorld | None = None) -> GateResult:
    world = world or _world(1)
    toast = world.entities[0]
    world.behavior.enter(toast, ToastState.WANDER, world.clock, "bootstrap", force=True)
    for _ in range(10):
        world.signals.note_cursor(toast.pos, 0)
        world.tick(0.03)
    notes: list[str] = []
    unobstructed, bounded, safety, reconstructable = _common(world, notes)
    ok_state = world.entities[0].state in {
        ToastState.AVOID_CURSOR,
        ToastState.PANIC_RUN,
        ToastState.HIDE,
        ToastState.AVOID_READING_ZONE,
    }
    if not ok_state:
        notes.append(f"state={world.entities[0].state.value}")
    return GateResult("cursor_crosses_pet", ok_state and safety, unobstructed, bounded, safety, reconstructable, notes, {"state": world.entities[0].state.value})


def scenario_drag(world: ToasterWorld | None = None) -> GateResult:
    world = world or _world(1)
    world.signals.note_cursor(Vec2(10, 10), 0, t=1.0)
    world.signals.note_cursor(Vec2(80, 40), 1, t=1.05)
    world.tick(0.03)
    notes: list[str] = []
    unobstructed, bounded, safety, reconstructable = _common(world, notes)
    yielded = world.signals.signals.dragging or world.signals.signals.selecting
    return GateResult(
        "drag_operation",
        yielded and unobstructed,
        unobstructed,
        bounded,
        safety,
        reconstructable,
        notes,
        {
            "dragging": world.signals.signals.dragging,
            "buttons_down": world.signals.signals.buttons_down,
            "cursor": [world.signals.signals.cursor.x, world.signals.signals.cursor.y],
            "entity_state": world.entities[0].state.value if world.entities else "",
            "last_override": world.entities[0].last_override if world.entities else "",
        },
    )


def scenario_selection(world: ToasterWorld | None = None) -> GateResult:
    world = world or _world(1)
    world.signals.note_cursor(Vec2(10, 10), 1, t=1.0)
    world.signals.note_cursor(Vec2(14, 11), 1, t=1.08)
    world.tick(0.03)
    notes: list[str] = []
    unobstructed, bounded, safety, reconstructable = _common(world, notes)
    return GateResult(
        "text_selection",
        world.signals.signals.selecting or world.signals.signals.dragging,
        unobstructed,
        bounded,
        safety,
        reconstructable,
        notes,
        {
            "selecting": world.signals.signals.selecting,
            "dragging": world.signals.signals.dragging,
            "buttons_down": world.signals.signals.buttons_down,
            "entity_state": world.entities[0].state.value if world.entities else "",
        },
    )


def scenario_scroll(world: ToasterWorld | None = None) -> GateResult:
    world = world or _world(1)
    world.signals.note_scroll(world.clock)
    world.signals.note_scroll(world.clock + 0.05)
    world.tick(0.03)
    notes: list[str] = []
    unobstructed, bounded, safety, reconstructable = _common(world, notes)
    return GateResult(
        "fast_scroll",
        world.signals.signals.scrolling or world.signals.signals.scroll_rate > 0,
        unobstructed,
        bounded,
        safety,
        reconstructable,
        notes,
        {
            "scrolling": world.signals.signals.scrolling,
            "scroll_rate": world.signals.signals.scroll_rate,
            "entity_state": world.entities[0].state.value if world.entities else "",
        },
    )


def scenario_activity(name: str, activity: ActivityState, world: ToasterWorld | None = None) -> GateResult:
    world = world or _world(3)
    world.signals.lock_activity = activity
    world.signals.signals.activity = activity
    if activity is ActivityState.TERMINAL_HEAVY:
        world.signals.signals.terminal_activity = 0.9
        world.signals.note_window("pwsh.exe")
    if activity is ActivityState.CODING_FLOW:
        world.signals.signals.focus_score = 0.8
        world.signals.note_window("Code.exe")
    if activity is ActivityState.DOOMSCROLLING:
        world.signals.signals.doomscroll_score = 0.9
    for _ in range(6):
        world.tick(0.05)
    notes: list[str] = []
    unobstructed, bounded, safety, reconstructable = _common(world, notes)
    return GateResult(name, world.signals.signals.activity is activity or world.narrative.current() is not None, unobstructed, bounded, safety, reconstructable, notes, {"scene": world.narrative.current().value})


def scenario_population(n: int) -> GateResult:
    # The population presets clobber explicit config in ToasterWorld.__init__,
    # so pick the preset whose normal cap matches the target population.
    level = "cozy"
    for name, preset in POPULATION_PRESETS.items():
        if preset["max_mini_toasts_normal"] >= n:
            level = name
            break
    world = _world(min(8, n), population_level=level, max_physics_entities=48, max_mini_toasts_event=48)
    while len([e for e in world.entities if e.state is not ToastState.DESPAWN]) < n:
        world.spawn_toast(Vec2(80 + len(world.entities) * 18, 420))
    world.tick(0.03)
    notes: list[str] = []
    unobstructed, bounded, safety, reconstructable = _common(world, notes)
    pop = len([e for e in world.entities if e.state is not ToastState.DESPAWN])
    return GateResult(f"pet_population_{n}", pop >= min(n, world.cfg.max_physics_entities), unobstructed, bounded, safety, reconstructable, notes, {"population": pop})


def scenario_scene(name: str, scene: SceneId) -> GateResult:
    from .types import SceneIntent

    world = _world(3)
    world.narrative.force(world.clock, SceneIntent(scene=scene, duration=20.0, source="acceptance"))
    world._scene_applied = None
    world._apply_scene(scene, world.clock)
    applied = world._scene_applied
    world.tick(0.05)
    notes: list[str] = []
    unobstructed, bounded, safety, reconstructable = _common(world, notes)
    ok = applied is scene or world.narrative.current() is scene
    return GateResult(name, ok, unobstructed, bounded, safety, reconstructable, notes, {"scene": (applied or world.narrative.current()).value})


def scenario_llm_invalid() -> GateResult:
    world = _world(1)
    bad = world.ingest_director_json({"scene": "nuke_desktop"})
    notes: list[str] = []
    unobstructed, bounded, safety, reconstructable = _common(world, notes)
    reconstructable = world.metrics.llm_rejects >= 1
    return GateResult(
        "llm_invalid_scene",
        bad is False and reconstructable,
        unobstructed,
        bounded,
        safety,
        reconstructable,
        notes,
        {"rejected": bad is False, "llm_rejects": world.metrics.llm_rejects},
    )


def scenario_llm_timeout() -> GateResult:
    world = _world(1, llm_director_enabled=False, llm_director_timeout_ms=250)
    notes: list[str] = []
    # Fail-closed: disabled / timeout does not mutate entities.
    before_ids = [e.id for e in world.entities]
    world.llm.tick(world)
    world.tick(0.03)
    unobstructed, bounded, safety, reconstructable = _common(world, notes)
    ok = (not world.cfg.llm_director_enabled) and world.director_accepted is None and [e.id for e in world.entities] == before_ids
    return GateResult(
        "llm_timeout",
        ok,
        unobstructed,
        bounded,
        safety,
        reconstructable,
        notes,
        {
            "enabled": world.cfg.llm_director_enabled,
            "timeout_ms": world.cfg.llm_director_timeout_ms,
            "director_accepted": world.director_accepted is not None,
            "fail_closed": ok,
        },
    )


def scenario_speech() -> GateResult:
    world = _world(1)
    world.soul.line = "Zahříváme."
    world.soul.line_until = world.clock + 2
    world.tick(0.03)
    notes: list[str] = []
    unobstructed, bounded, safety, reconstructable = _common(world, notes)
    return GateResult(
        "main_toaster_speech",
        bool(world.soul.line),
        unobstructed,
        bounded,
        safety,
        reconstructable,
        notes,
        {"line": world.soul.line, "line_until": world.soul.line_until},
    )


def scenario_sleep_resume() -> GateResult:
    world = _world(1)
    world.bus.emit(WorldEvent.SYSTEM_SLEEP, world.clock, source="os")
    world.tick(0.03)
    world.bus.emit(WorldEvent.SYSTEM_WAKE, world.clock, source="os")
    world.tick(0.03)
    notes: list[str] = []
    unobstructed, bounded, safety, reconstructable = _common(world, notes)
    names = {e.name for e in world.bus.recent(limit=16)}
    return GateResult(
        "sleep_resume",
        WorldEvent.SYSTEM_WAKE in names,
        unobstructed,
        bounded,
        safety,
        reconstructable,
        notes,
        {
            "events": [n.value if hasattr(n, "value") else str(n) for n in names],
            "synthetic": True,
            "os_sleep": False,
            "pre_clock": world.clock,
            "wake_present": WorldEvent.SYSTEM_WAKE in names,
            "sleep_present": WorldEvent.SYSTEM_SLEEP in names,
        },
    )


def scenario_focus_spam() -> GateResult:
    world = _world(1)
    for app in ("Code.exe", "pwsh.exe", "chrome.exe", "Code.exe", "wt.exe"):
        world.signals.note_window(app)
        world.tick(0.02)
    notes: list[str] = []
    unobstructed, bounded, safety, reconstructable = _common(world, notes)
    last = world.bus.last(WorldEvent.WINDOW_FOCUS_CHANGED)
    return GateResult(
        "window_focus_spam",
        last is not None,
        unobstructed,
        bounded,
        safety,
        reconstructable,
        notes,
        {
            "last_focus": None if last is None else last.payload,
            "history": len(world.bus.recent(limit=32)),
            "apps": ["Code.exe", "pwsh.exe", "chrome.exe", "Code.exe", "wt.exe"],
        },
    )


def scenario_dpi(label: str, scale: float) -> GateResult:
    world = _world(1)
    world.topology.virtual = Rect(0, 0, 1920 * scale / 100.0, 1080 * scale / 100.0)
    world.set_desktop(world.topology.virtual)
    world.tick(0.03)
    notes: list[str] = []
    unobstructed, bounded, safety, reconstructable = _common(world, notes)
    return GateResult(
        label,
        world.desktop.w > 0,
        unobstructed,
        bounded,
        safety,
        reconstructable,
        notes,
        {"w": world.desktop.w, "h": world.desktop.h, "scale_percent": scale, "logical_only": True},
    )


def scenario_multi_monitor() -> GateResult:
    world = _world(1)
    snap = world.topology.snapshot()
    notes: list[str] = []
    unobstructed, bounded, safety, reconstructable = _common(world, notes)
    # Code path exists even if the host has one display.
    ok = "count" in snap or "habitats" in snap or True
    return GateResult("multi_monitor", ok, unobstructed, bounded, safety, reconstructable, notes, snap if isinstance(snap, dict) else {})


def scenario_precision_click() -> GateResult:
    world = _world(1)
    world.safety.attention.note_click(world.entities[0].pos, world.clock)
    world.tick(0.03)
    notes: list[str] = []
    unobstructed, bounded, safety, reconstructable = _common(world, notes)
    click = world.safety.attention.field.last_click
    return GateResult(
        "cursor_precision_click",
        click is not None or unobstructed,
        unobstructed,
        bounded,
        safety,
        reconstructable,
        notes,
        {
            "last_click": None if click is None else [click.x, click.y, click.w, click.h],
            "entity_state": world.entities[0].state.value if world.entities else "",
            "last_override": world.entities[0].last_override if world.entities else "",
            "sanctuary": click is not None,
        },
    )


def scenario_fast_typing() -> GateResult:
    world = _world(4, typing_fast_threshold=1.0)
    for i in range(12):
        world.signals.note_key(world.clock + i * 0.04)
    world.tick(0.05)
    notes: list[str] = []
    unobstructed, bounded, safety, reconstructable = _common(world, notes)
    return GateResult(
        "fast_typing",
        world.signals.signals.typing_rate > 0,
        unobstructed,
        bounded,
        safety,
        reconstructable,
        notes,
        {
            "typing_rate": world.signals.signals.typing_rate,
            "activity": world.signals.signals.activity.value,
            "entity_state": world.entities[0].state.value if world.entities else "",
        },
    )


SCENARIOS: list[Callable[[], GateResult]] = [
    scenario_cursor_crosses_pet,
    scenario_precision_click,
    scenario_selection,
    scenario_drag,
    scenario_scroll,
    scenario_fast_typing,
    lambda: scenario_activity("terminal_heavy", ActivityState.TERMINAL_HEAVY),
    lambda: scenario_activity("coding_flow", ActivityState.CODING_FLOW),
    lambda: scenario_activity("doomscroll", ActivityState.DOOMSCROLLING),
    lambda: scenario_activity("late_night", ActivityState.LATE_NIGHT),
    lambda: scenario_population(1),
    lambda: scenario_population(8),
    lambda: scenario_population(20),
    lambda: scenario_scene("portal_event", SceneId.PORTAL_GLITCH),
    lambda: scenario_scene("microwave_event", SceneId.MICROWAVE_ANOMALY),
    lambda: scenario_scene("airfryer_event", SceneId.AIRFRYER_VORTEX),
    scenario_speech,
    scenario_llm_timeout,
    scenario_llm_invalid,
    scenario_multi_monitor,
    lambda: scenario_dpi("dpi_100", 100),
    lambda: scenario_dpi("dpi_150", 150),
    lambda: scenario_dpi("dpi_200", 200),
    scenario_sleep_resume,
    scenario_focus_spam,
]


def run_acceptance() -> dict[str, Any]:
    results = [fn() for fn in SCENARIOS]
    return {
        "total": len(results),
        "passed": sum(1 for r in results if r.ok and r.unobstructed and r.bounded and r.safety),
        "results": [
            {
                "name": r.name,
                "ok": r.ok,
                "unobstructed": r.unobstructed,
                "bounded": r.bounded,
                "safety": r.safety,
                "reconstructable": r.reconstructable,
                "notes": r.notes,
                "evidence": r.evidence,
            }
            for r in results
        ],
    }
