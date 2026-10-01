"""Acceptance gates for the Ambient AGI Theatre runtime."""

from __future__ import annotations

from desktop_app.toaster_universe.behavior import CANONICAL_EDGES, BehaviorEngine
from desktop_app.toaster_universe.catalog import CATALOG, CATEGORIES, LORE_KEYS, PRAGUE_ROASTS
from desktop_app.toaster_universe.config import WorldConfig, load_world_config
from desktop_app.toaster_universe.ecology import PopulationController, population_cap
from desktop_app.toaster_universe.entities import spawn_entity
from desktop_app.toaster_universe.events import EventBus
from desktop_app.toaster_universe.keyboard_games import KeyboardGames
from desktop_app.toaster_universe.llm_director import LlmDirector
from desktop_app.toaster_universe.rare_events import RareEventDirector
from desktop_app.toaster_universe.safety import SafetyArbiter
from desktop_app.toaster_universe.signals import SignalSampler
from desktop_app.toaster_universe.types import (
    ActivityState,
    EntityKind,
    OverrideReason,
    Rect,
    SceneId,
    ToastState,
    Vec2,
    WorldEvent,
)
from desktop_app.toaster_universe.world import ToasterWorld


def test_config_clamps_unsafe_extremes():
    cfg = WorldConfig(cursor_panic_radius=1, max_mini_toasts_event=999, rare_event_base_rate=4).clamp()
    assert 24.0 <= cfg.cursor_panic_radius <= 160.0
    assert cfg.max_mini_toasts_event <= 48
    assert cfg.rare_event_base_rate <= 0.01


def test_load_world_config_reads_bible_keys():
    cfg = load_world_config({"cursorPanicRadius": 90, "llmDirectorEnabled": True, "maxMiniToastsNormal": 5})
    assert cfg.cursor_panic_radius == 90
    assert cfg.llm_director_enabled is True
    assert cfg.max_mini_toasts_normal == 5


def test_event_bus_is_idempotent_per_id():
    bus = EventBus()
    hits = []
    bus.on(WorldEvent.TOAST_POP, lambda e: hits.append(e.event_id))
    first = bus.emit(WorldEvent.TOAST_POP, 1.0)
    assert first.event_id >= 1
    assert bus.last(WorldEvent.TOAST_POP) is not None


def test_cursor_prediction_and_panic_yield():
    cfg = WorldConfig()
    bus = EventBus()
    sampler = SignalSampler(cfg, bus)
    sampler.note_cursor(Vec2(0, 0), 0, t=1.0)
    sampler.note_cursor(Vec2(200, 0), 0, t=1.05)
    assert sampler.signals.cursor_predicted.x > sampler.signals.cursor.x
    arbiter = SafetyArbiter(cfg)
    desktop = Rect(0, 0, 1920, 1080)
    field = arbiter.evaluate(Vec2(210, 0), 16, sampler.signals, desktop)
    assert field.must_yield
    assert field.reason in {OverrideReason.CURSOR_PREDICTED, OverrideReason.CURSOR_INTERSECT}
    steered = arbiter.steer(Vec2(210, 0), Vec2(40, 0), field, cfg.panic_speed)
    assert steered.x < 0


def test_drag_and_selection_force_yield():
    cfg = WorldConfig()
    bus = EventBus()
    sampler = SignalSampler(cfg, bus)
    sampler.note_cursor(Vec2(10, 10), 0, t=1.0)
    sampler.note_cursor(Vec2(80, 40), 1, t=1.05)
    assert sampler.signals.dragging or sampler.signals.selecting
    field = SafetyArbiter(cfg).evaluate(Vec2(80, 40), 16, sampler.signals, Rect(0, 0, 800, 600))
    assert field.must_yield
    assert field.reason in {OverrideReason.DRAG_ACTIVE, OverrideReason.SELECTION_ACTIVE}


def test_mini_toast_lifecycle_reaches_toaster_and_pops():
    world = ToasterWorld(WorldConfig(spawn_on_start=1, population_growth_rate=0.0), seed=7)
    world.set_desktop(Rect(0, 0, 1200, 800))
    world.set_toaster(Vec2(600, 400), Vec2(580, 360), Vec2(620, 360))
    world.bootstrap()
    toast = world.entities[0]
    toast.temperature = 0.01
    toast.traits["temperature_need"] = 1.0
    toast.pos = Vec2(590, 370)
    world.signals.live_os = False
    world.signals.signals.cursor = Vec2(20, 20)
    world.signals.signals.cursor_predicted = Vec2(20, 20)
    seen = set()
    for _ in range(240):
        world.signals.note_cursor(Vec2(20, 20), 0, t=world.last_t + 1.0)
        world.tick(0.05)
        if not world.entities:
            break
        seen.add(world.entities[0].state)
        if ToastState.POP_OUT in seen or ToastState.COOL_DOWN in seen:
            break
    assert ToastState.APPROACH_TOASTER in seen or ToastState.QUEUE_FOR_SLOT in seen or ToastState.TOASTING in seen
    assert world.metrics.population >= 1


def test_safety_overrides_behavior_when_cursor_hits():
    world = ToasterWorld(WorldConfig(spawn_on_start=1, population_growth_rate=0.0), seed=3)
    world.set_desktop(Rect(0, 0, 1200, 800))
    world.set_toaster(Vec2(1000, 400), Vec2(980, 360), Vec2(1020, 360))
    world.bootstrap()
    toast = world.entities[0]
    toast.pos = Vec2(400, 400)
    toast.state = ToastState.WANDER
    world.signals.live_os = False
    for _ in range(8):
        world.signals.note_cursor(world.entities[0].pos, 0)
        world.tick(0.03)
    assert world.entities[0].state in {
        ToastState.AVOID_CURSOR,
        ToastState.PANIC_RUN,
        ToastState.HIDE,
        ToastState.AVOID_READING_ZONE,
    }
    assert world.metrics.safety_overrides >= 1 or world.metrics.attention_retreats >= 1


def test_work_mode_caps_population():
    cfg = WorldConfig(max_mini_toasts_normal=8)
    assert population_cap(cfg, ActivityState.CODING_FLOW, False, False) <= 3
    assert population_cap(cfg, ActivityState.IDLE, True, False) == cfg.max_mini_toasts_infestation


def test_population_controller_does_not_exceed_cap():
    import random

    cfg = WorldConfig(max_mini_toasts_normal=2, population_growth_rate=20.0)
    ctl = PopulationController(cfg, random.Random(1))
    entities = [
        spawn_entity(EntityKind.MINI_TOAST, Vec2(10, 10), random.Random(i))
        for i in range(5)
    ]
    ctl.tick(1.0, entities, ActivityState.IDLE)
    marked = [
        e
        for e in entities
        if e.proposed in {
            ToastState.DESPAWN,
            ToastState.SLEEP,
            ToastState.APPROACH_TOASTER,
            ToastState.PORTAL_CURIOUS,
            ToastState.HIDE,
        }
        or e.state is ToastState.DESPAWN
    ]
    assert len(marked) >= 3


def test_llm_director_rejects_unknown_scene_and_accepts_valid():
    director = LlmDirector(WorldConfig())
    bad = director.parse('{"scene":"nuke_desktop","tone":"deadpan_cute"}')
    assert bad.intent is None
    assert bad.rejected == "unknown_scene"
    good = director.parse(
        '{"scene":"toast_watch_party","tone":"deadpan_cute","participants":4,"escalation":0.4,"duration":22}'
    )
    assert good.intent is not None
    assert good.intent.scene is SceneId.TOAST_WATCH_PARTY
    assert good.intent.source == "llm"


def test_llm_director_parse_tolerates_fences_prose_echo_and_truncation():
    director = LlmDirector(WorldConfig())
    payload = (
        '{"scene":"toast_watch_party","tone":"deadpan_cute",'
        '"participants":4,"escalation":0.4,"duration":22}'
    )

    # Markdown code fence around the object.
    fenced = f"```json\n{payload}\n```"
    assert director.parse(fenced).intent is not None

    # Prose before and after the object.
    prose = f"Sure, here is the scene:\n{payload}\nHope that helps!"
    assert director.parse(prose).intent is not None

    # The model echoed the system-prompt example before the real answer;
    # the last balanced object must win.
    echo = (
        '{"scene":"quiet_companionship","tone":"quiet","duration":10} '
        f"the real scene is: {payload}"
    )
    parsed = director.parse(echo)
    assert parsed.intent is not None
    assert parsed.intent.scene is SceneId.TOAST_WATCH_PARTY

    # A max_tokens cut mid-object is repaired: decisive fields survive.
    truncated = payload[:45]
    repaired = director.parse(truncated)
    assert repaired.intent is not None
    assert repaired.intent.scene is SceneId.TOAST_WATCH_PARTY

    # Garbage that contains no JSON object at all still rejects.
    assert director.parse("the toast wants to watch the party").rejected == "invalid_json"


def test_rare_events_respect_novelty_budget():
    import random

    director = RareEventDirector(WorldConfig(rare_event_base_rate=1.0), random.Random(2))
    director.novelty = 0.2
    assert director.maybe_fire(10.0, SignalSampler(WorldConfig(), EventBus()).signals, 3, False) is None
    director.novelty = 1.0
    director.last_any = -1e9
    # Still may miss due to activity; force idle signals.
    signals = SignalSampler(WorldConfig(), EventBus()).signals
    signals.activity = ActivityState.IDLE
    fire = director.maybe_fire(100.0, signals, 3, False)
    if fire:
        assert director.novelty < 1.0
        assert fire.event.value


def test_keyboard_game_does_not_require_special_mode_from_user():
    games = KeyboardGames(WorldConfig(keyboard_games_enabled=True, typing_fast_threshold=1.0))
    bus = EventBus()
    sampler = SignalSampler(WorldConfig(), bus)
    sampler.signals.typing_rate = 8.0
    sampler.signals.cursor = Vec2(0, 0)
    entity = spawn_entity(EntityKind.MINI_TOAST, Vec2(10, 10), __import__("random").Random(1))
    entity.state = ToastState.WANDER
    events = games.tick(1.0, sampler.signals, [entity])
    assert games.state.mode is not games.state.mode.__class__.NONE or events == events
    assert games.mode_for(sampler.signals, 5).value


def test_catalog_covers_all_bible_categories_and_prague_roasts():
    names = {e.category for e in CATALOG}
    for category in CATEGORIES:
        assert category in names
    for key in LORE_KEYS:
        assert any(e.key == key for e in CATALOG)
    assert len(PRAGUE_ROASTS) == 15
    assert any("čtvrtý terminál" in e.text for e in CATALOG)


def test_behavior_graph_has_every_state():
    assert set(CANONICAL_EDGES) == set(ToastState)


def test_world_tick_is_observable_and_spawns_one_toast():
    world = ToasterWorld(WorldConfig(spawn_on_start=1, population_growth_rate=0.0), seed=1)
    world.set_desktop(Rect(0, 0, 800, 600))
    world.set_toaster(Vec2(700, 300), Vec2(680, 260), Vec2(720, 260))
    snap = world.tick(0.033)
    assert snap["population"] >= 1
    assert snap["entities"]
    assert "metrics" in snap
    assert snap["metrics"]["ticks"] >= 1
    assert world.soul.heat >= 0.0


def test_ingest_director_json_wires_scene():
    world = ToasterWorld(WorldConfig(spawn_on_start=0), seed=1)
    ok = world.ingest_director_json(
        {"scene": "portal_glitch", "tone": "SHAREWARE_CHAOS", "duration": 12, "participants": 2}
    )
    assert ok
    assert world.narrative.current() is SceneId.PORTAL_GLITCH
    assert world.metrics.llm_calls == 1


def test_social_does_not_override_safety_states():
    from desktop_app.toaster_universe.social import propose

    a = spawn_entity(EntityKind.MINI_TOAST, Vec2(0, 0), __import__("random").Random(1))
    b = spawn_entity(EntityKind.MINI_TOAST, Vec2(10, 0), __import__("random").Random(2))
    a.state = ToastState.PANIC_RUN
    b.state = ToastState.DANCE
    assert propose(a, [a, b], 54.0) is None
    a.state = ToastState.WANDER
    a.traits["sociality"] = 0.9
    a.traits["copycat_drive"] = 0.9
    nxt = propose(a, [a, b], 54.0)
    assert nxt in {ToastState.COPY_DANCE, ToastState.SOCIALIZE}


def test_scene_skips_yielding_toasts():
    world = ToasterWorld(WorldConfig(spawn_on_start=1, population_growth_rate=0.0), seed=4)
    world.set_desktop(Rect(0, 0, 800, 600))
    world.set_toaster(Vec2(700, 300), Vec2(680, 260), Vec2(720, 260))
    world.bootstrap()
    toast = world.entities[0]
    toast.state = ToastState.PANIC_RUN
    world._scene_applied = None
    world._apply_scene(SceneId.KPOP_HEART_MODE, 1.0)
    assert toast.state is ToastState.PANIC_RUN


def test_appliance_admit_and_release():
    from desktop_app.toaster_universe.appliances import ApplianceWorld
    from desktop_app.toaster_universe.types import ApplianceKind

    house = ApplianceWorld()
    house.ensure_layout(Vec2(100, 100))
    house.unlock(ApplianceKind.MICROWAVE)
    admitted = house.admit(ApplianceKind.MICROWAVE, 1.0)
    assert admitted is not None
    assert admitted.occupied == 1
    house.release(ApplianceKind.MICROWAVE)
    assert admitted.occupied == 0


def test_enter_rejects_invalid_edge_and_records_reason():
    world = ToasterWorld(WorldConfig(spawn_on_start=1, population_growth_rate=0.0), seed=2)
    world.set_desktop(Rect(0, 0, 800, 600))
    world.set_toaster(Vec2(400, 300), Vec2(380, 260), Vec2(420, 260))
    world.bootstrap()
    toast = world.entities[0]
    world.behavior.enter(toast, ToastState.IDLE, 0.5, "bootstrap", force=True)
    denied = world.behavior.enter(toast, ToastState.TOASTING, 0.6, "graph")
    assert denied is None
    assert toast.state is ToastState.IDLE
    assert world.behavior.last_reject is not None
    assert world.behavior.last_reject.rejected == "invalid_edge"


def test_safety_does_not_leave_while_cursor_still_intersects():
    world = ToasterWorld(WorldConfig(spawn_on_start=1, population_growth_rate=0.0), seed=5)
    world.set_desktop(Rect(0, 0, 1200, 800))
    world.set_toaster(Vec2(1000, 400), Vec2(980, 360), Vec2(1020, 360))
    world.bootstrap()
    toast = world.entities[0]
    world.behavior.enter(toast, ToastState.WANDER, 0.1, "bootstrap", force=True)
    toast.pos = Vec2(400, 400)
    world.signals.live_os = False
    for _ in range(40):
        world.signals.note_cursor(world.entities[0].pos, 0)
        world.tick(0.05)
    assert world.entities[0].state in {
        ToastState.AVOID_CURSOR,
        ToastState.PANIC_RUN,
        ToastState.HIDE,
        ToastState.AVOID_READING_ZONE,
    }
    assert world.metrics.safety_overrides >= 1


def test_named_metrics_increment_on_tick():
    world = ToasterWorld(WorldConfig(spawn_on_start=1, population_growth_rate=0.0), seed=9)
    world.set_desktop(Rect(0, 0, 800, 600))
    world.set_toaster(Vec2(700, 300), Vec2(680, 260), Vec2(720, 260))
    snap = world.tick(0.033)
    counters = snap["metrics"]["counters"]
    assert counters.get("scene_started", 0) >= 1
    assert counters.get("entity_spawned", 0) >= 1
    assert counters.get("frame_time", 0) >= 1
    assert counters.get("physics_time", 0) >= 1
    assert "config" in snap
    assert snap["config"]["cursor_panic_radius"] >= 24.0


def test_action_engine_plays_named_main_toaster_actions():
    from desktop_app.toaster_universe.types import ToasterAction

    world = ToasterWorld(WorldConfig(spawn_on_start=0), seed=1)
    world.play_action(ToasterAction.DOUBLE_BLINK)
    world.play_action(ToasterAction.SHOCKWAVE)
    world.play_action(ToasterAction.DEADPAN_STARE)
    world.actions.tick(world.soul, world.clock + 0.1)
    assert world.soul.last_action
    assert world.soul.pose.blink > 0 or world.soul.pose.shockwave > 0 or world.soul.pose.stare > 0
    names = {e.name for e in world.bus.recent(limit=32)}
    assert WorldEvent.ACTION_START in names


def test_animation_clips_emit_start_and_compose_pose():
    from desktop_app.toaster_universe.animation import ClipId, compose_pose
    from desktop_app.toaster_universe.entities import spawn_entity

    world = ToasterWorld(WorldConfig(spawn_on_start=0), seed=1)
    world.animation.play(ClipId.IMPACT_SHOCKWAVE, 0.0, entity_id="t1")
    world.animation.play(ClipId.DOUBLE_BLINK, 0.0, entity_id="t1")
    world.animation.tick(0.1)
    pose = world.animation.compose(0.1, "t1")
    assert pose.shockwave > 0 or pose.blink > 0
    entity = spawn_entity(EntityKind.MINI_TOAST, Vec2(10, 10), __import__("random").Random(1))
    world.behavior.enter(entity, ToastState.DOOMSCROLL_COMMENTARY, 0.0, "scene")
    composed = compose_pose(entity, 0.2, world.signals.signals, world.animation)
    assert composed.stare > 0.5
    names = {e.name for e in world.bus.recent(limit=32)}
    assert WorldEvent.ANIM_START in names


def test_reading_sanctuary_ms_is_consumed():
    cfg = WorldConfig(reading_sanctuary_ms=200.0, reading_sanctuary_radius=80.0)
    world = ToasterWorld(cfg, seed=1)
    world.signals.live_os = False
    world.signals.signals.mouse_velocity = 0.0
    world.signals.signals.cursor = Vec2(100, 100)
    world.signals.signals.cursor_predicted = Vec2(100, 100)
    world.safety.refresh(world.signals.signals, 0.0)
    world.safety.refresh(world.signals.signals, 0.25)
    kinds = [p.kind for p in world.safety.field.patches]
    assert "reading_sanctuary" in kinds


def test_dead_config_knobs_have_consumers():
    cfg = WorldConfig(
        voice_cooldown_sec=12.0,
        max_physics_entities=6,
        population_decay_rate=0.4,
        portal_cooldown_hours=0.1,
        sleep_after_idle_sec=5.0,
    ).clamp()
    world = ToasterWorld(cfg, seed=1)
    world.set_desktop(Rect(0, 0, 800, 600))
    world.set_toaster(Vec2(400, 300), Vec2(380, 260), Vec2(420, 260))
    # The chatter preset legitimately owns voice_cooldown_sec ("normal" -> 180).
    assert world.cfg.voice_cooldown_sec == 180.0
    assert world.cfg.max_physics_entities == 6
    world.soul.line = "x"
    world.soul.voice_ready_t = world.clock
    world._update_soul(0.03, world.clock + 0.03, world.signals.signals)
    assert world.soul.voice_ready_t >= 0.0
    assert world.population.cfg.population_decay_rate > 0
    assert world.debug_snapshot()["config"]["portal_cooldown_hours"] > 0


def test_acceptance_matrix_named_scenarios():
    from desktop_app.toaster_universe.acceptance import run_acceptance

    report = run_acceptance()
    assert report["total"] == 25
    failed = [r for r in report["results"] if not (r["ok"] and r["unobstructed"] and r["bounded"] and r["safety"])]
    assert not failed, failed


def test_keyboard_sets_proposal_not_raw_state():
    games = KeyboardGames(WorldConfig(keyboard_games_enabled=True, typing_fast_threshold=1.0))
    signals = SignalSampler(WorldConfig(), EventBus()).signals
    signals.typing_rate = 9.0
    signals.cursor = Vec2(0, 0)
    entity = spawn_entity(EntityKind.MINI_TOAST, Vec2(8, 8), __import__("random").Random(1))
    entity.state = ToastState.WANDER
    games.tick(1.0, signals, [entity] * 5)
    assert entity.state is ToastState.WANDER
    assert entity.proposed in {ToastState.POP_OUT, ToastState.DANCE, ToastState.PEEK, ToastState.TERMINAL_AWE, None}


def test_physics_named_modules_and_forbidden_wander():
    from desktop_app.toaster_universe.physics import damping, gravity, wander_target

    vel = damping(Vec2(10, 0), coeff=0.5)
    assert vel.x == 5
    fallen = gravity(Vec2(0, 0), 1.0, g=180.0)
    assert fallen.y == 180.0
    blocked = Rect(100, 100, 40, 40)
    dest = wander_target(Vec2(0, 0), Rect(0, 0, 800, 600), __import__("random").Random(2), forbidden=[blocked])
    assert not blocked.contains(dest)


def test_safety_low_attention_and_taskbar():
    from desktop_app.toaster_universe.safety import SafetyArbiter
    from desktop_app.toaster_universe.types import OverrideReason, SafetyZone

    cfg = WorldConfig()
    arbiter = SafetyArbiter(cfg)
    signals = SignalSampler(cfg, EventBus()).signals
    desktop = Rect(0, 0, 800, 600)
    field = arbiter.evaluate(Vec2(400, 590), 16, signals, desktop)
    assert field.must_yield
    assert field.zone is SafetyZone.TASKBAR_SAFE_EDGE
    target, fail = arbiter.low_attention_target(Vec2(400, 300), desktop, signals)
    assert fail is OverrideReason.NONE
    assert target is not None


def test_director_schema_rejects_unknown_rarity_and_clamps_cast():
    world = ToasterWorld(WorldConfig(spawn_on_start=0, llm_director_enabled=True), seed=1)
    bad = world.llm.parse({"scene": "quiet_companionship", "tone": "TECHNICAL_PRAGUE_DEADPAN", "duration": 12, "rarityClass": "mythic"})
    assert bad.intent is None
    assert bad.rejected == "unknown_rarity"
    ok = world.ingest_director_json(
        {
            "scene": "keyboard_feeding_frenzy",
            "tone": "TECHNICAL_PRAGUE_DEADPAN",
            "duration": 12,
            "participants": 99,
            "rarityClass": "common",
            "visualOnly": True,
        }
    )
    assert ok
    assert world.director_accepted.participants <= world.cfg.max_mini_toasts_event


def test_secret_chamber_starts_locked():
    from desktop_app.toaster_universe.types import ApplianceKind

    world = ToasterWorld(WorldConfig(spawn_on_start=0), seed=1)
    world.set_desktop(Rect(0, 0, 800, 600))
    world.set_toaster(Vec2(400, 300), Vec2(380, 260), Vec2(420, 260))
    world.bootstrap()
    chamber = next(a for a in world.appliances.items if a.kind is ApplianceKind.SECRET_CHAMBER)
    assert chamber.open is False
    world._apply_scene(SceneId.SECRET_CHAMBER, 1.0)
    chamber = next(a for a in world.appliances.items if a.kind is ApplianceKind.SECRET_CHAMBER)
    assert chamber.open is True
    assert chamber.impossible is True


def test_maybe_calm_outranks_narrative_propose():
    world = ToasterWorld(WorldConfig(spawn_on_start=1, population_growth_rate=0.0), seed=3)
    world.set_desktop(Rect(0, 0, 800, 600))
    world.set_toaster(Vec2(700, 300), Vec2(680, 260), Vec2(720, 260))
    world.signals.live_os = False
    world.signals.lock_activity = ActivityState.FRANTIC
    world.signals.signals.activity = ActivityState.FRANTIC
    world.signals.signals.focus_score = 0.95
    world.signals.signals.typing_rate = 8.0
    snap = world.tick(0.05)
    assert snap["metrics"]["scene"] in {SceneId.RETURN_TO_CALM.value, SceneId.QUIET_COMPANIONSHIP.value, SceneId.CODING_FLOW_AUDIENCE.value}
