"""Live visual proof: every catalog scene has a distinct performance script."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

REQUIRED = (
    "quiet_companionship",
    "toast_watch_party",
    "toast_warmup_ritual",
    "coding_flow_audience",
    "hr_roast_skit",
    "rice_zen",
    "late_night_sleep",
    "popcorn_brain",
    "portal_glitch",
    "secret_chamber",
    "microwave_anomaly",
    "kpop_heart_mode",
    "airfryer_vortex",
    "toast_infestation",
    "burn_recovery",
    "funeral_ritual",
    "shareware_apocalypse",
)


def _world():
    from desktop_app.toaster_universe.config import WorldConfig
    from desktop_app.toaster_universe.types import Rect, Vec2
    from desktop_app.toaster_universe.world import ToasterWorld

    cfg = WorldConfig(
        enabled=True,
        spawn_on_start=3,
        population_growth_rate=0.0,
        llm_director_enabled=False,
        rare_events_enabled=False,
        max_scene_duration_sec=20.0,
    )
    world = ToasterWorld(cfg, seed=9)
    world.signals.live_os = False
    world.set_desktop(Rect(0, 0, 1400, 900))
    world.set_toaster(Vec2(1100, 420), Vec2(1080, 380), Vec2(1120, 380))
    world.bootstrap()
    return world


def main() -> int:
    from desktop_app.toaster_universe.performance import SCRIPTS, SceneBeat
    from desktop_app.toaster_universe.types import ActivityState, SceneId, SceneIntent, ToastState, Vec2

    failed = []
    tested = []
    completed = []
    interrupted = []
    payoffs = []
    transitions = 0
    durations = []
    rows = []

    missing_scripts = [sid for sid in REQUIRED if SceneId(sid) not in SCRIPTS]
    if missing_scripts:
        failed.append(f"missing_scripts:{missing_scripts}")

    visuals = {}
    for sid in REQUIRED:
        script = SCRIPTS[SceneId(sid)]
        visuals[sid] = (script.setup, script.beat1, script.beat2, script.payoff)
    unique = len(set(visuals.values()))
    if unique < len(REQUIRED):
        failed.append(f"staging_not_distinct:{unique}/{len(REQUIRED)}")

    world = _world()
    world.signals.lock_activity = ActivityState.IDLE
    world.signals.signals.focus_score = 0.0
    world.signals.teleport_cursor(Vec2(40, 40), 0, t=0)

    try:
        from PyQt6.QtWidgets import QApplication
        from desktop_app.toaster_universe.overlay import WorldOverlay

        app = QApplication.instance() or QApplication(["scene-performance-proof"])
        overlay = WorldOverlay(world)
        overlay.show()
        painted = True
    except Exception as exc:
        app = None
        overlay = None
        painted = False
        rows.append({"paint": str(exc)})

    for sid in REQUIRED:
        scene = SceneId(sid)
        world.signals.signals.dragging = False
        world.signals.signals.selecting = False
        world.signals.signals.scrolling = False
        world.signals.signals.focus_score = 0.0
        if scene in {SceneId.BURN_RECOVERY, SceneId.FUNERAL_RITUAL} and world.entities:
            world.entities[0].burnt = True
            world.entities[0].browning = 0.96
        intent = SceneIntent(scene=scene, duration=18.0, escalation=0.45 if scene is not SceneId.SHAREWARE_APOCALYPSE else 0.9, visual_only=True)
        world.narrative.force(world.clock, intent)
        world._scene_applied = None
        beats = []
        payoff = False
        done = False
        for _ in range(360):
            world.tick(0.05)
            if app is not None:
                overlay.apply_dirty()
                app.processEvents()
            beat = world.performance.state.beat.value
            if not beats or beats[-1] != beat:
                beats.append(beat)
            if world.performance.state.payoff_reached:
                payoff = True
            if world.performance.state.completed:
                done = True
                break
        tested.append(sid)
        transitions += max(0, len(beats) - 1)
        durations.append(world.performance.state.duration)
        if payoff:
            payoffs.append(sid)
        if done:
            completed.append(sid)
        else:
            failed.append(f"not_completed:{sid}:{beats}")
        needed = {"setup", "beat1", "beat2", "payoff"}
        if not needed.issubset(set(beats)):
            failed.append(f"beats_missing:{sid}:{beats}")
        rows.append({"scene": sid, "beats": beats, "duration": round(world.performance.state.duration, 3), "payoff": payoff, "completed": done, "visual": world.performance.state.visual})

    # interrupt proof on a mid-scene
    world.narrative.force(world.clock, SceneIntent(scene=SceneId.TOAST_WATCH_PARTY, duration=18.0, escalation=0.5))
    world._scene_applied = None
    for _ in range(20):
        world.tick(0.05)
    world.signals.signals.dragging = True
    world.tick(0.05)
    if world.performance.state.beat is not SceneBeat.INTERRUPTED:
        failed.append(f"interrupt_failed:{world.performance.state.beat.value}")
    else:
        interrupted.append("toast_watch_party")
    world.signals.signals.dragging = False

    # settle after interrupt: entities still exist and safety still works
    if not world.entities:
        failed.append("world_empty_after_interrupt")
    prey = world.entities[0]
    prey.state = ToastState.WANDER
    world.signals.note_cursor(Vec2(prey.pos.x - 70, prey.pos.y), 0, t=world.clock)
    world.signals.note_cursor(prey.pos, 0, t=world.clock + 0.04)
    world.tick(0.03)
    if world.entities[0].state.value not in {"AVOID_CURSOR", "PANIC_RUN", "HIDE", "AVOID_READING_ZONE"}:
        failed.append(f"safety_after_scene:{world.entities[0].state.value}")

    snap = world.performance.snapshot()
    evidence = {
        "SCENES_TESTED": tested,
        "SCENES_COMPLETED": completed,
        "SCENES_INTERRUPTED": interrupted,
        "PAYOFFS_REACHED": payoffs,
        "BEAT_TRANSITIONS": transitions,
        "AVG_DURATION": round(sum(durations) / max(1, len(durations)), 3),
        "MAX_DURATION": round(max(durations or [0]), 3),
        "PAINTED": painted,
        "DISTINCT_STAGING": unique,
        "ROWS": rows,
        "PERFORMANCE": snap,
        "FAILED": failed,
        "STATUS": "PASS" if not failed else "FAIL",
    }
    out = ROOT / "tmp" / "toaster_universe_scene_performance_proof.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"out": str(out), "failed": failed, "completed": len(completed), "payoffs": len(payoffs), "interrupted": interrupted}, indent=2))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
