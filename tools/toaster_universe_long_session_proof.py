"""Long-session retention proof. Simulated hours, mixed activity, no live LLM."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


ACTIVITY_SCRIPT = (
    # hour-scale mixed workday + night. Each tuple: (minutes, activity, focus, pop, novelty, burnt, interrupt)
    (40, "CODING_FLOW", 0.82, 2, 0.35, False, False),
    (15, "IDLE", 0.15, 3, 0.55, False, False),
    (25, "TERMINAL_HEAVY", 0.70, 2, 0.40, False, False),
    (20, "BROWSING", 0.25, 3, 0.50, False, False),
    (12, "DOOMSCROLLING", 0.18, 5, 0.72, False, False),
    (30, "CODING_FLOW", 0.88, 2, 0.30, False, True),
    (18, "IDLE", 0.10, 4, 0.62, False, False),
    (10, "FRANTIC", 0.95, 1, 0.20, False, True),
    (22, "FOCUSED", 0.75, 2, 0.28, False, False),
    (35, "IDLE", 0.12, 3, 0.80, False, False),
    (15, "LATE_NIGHT", 0.20, 2, 0.45, False, False),
    (25, "CODING_FLOW", 0.80, 2, 0.33, False, False),
    (14, "BROWSING", 0.22, 4, 0.58, False, False),
    (8, "DOOMSCROLLING", 0.16, 6, 0.85, False, False),
    (40, "IDLE", 0.10, 3, 0.70, False, False),
    (20, "LATE_NIGHT", 0.15, 2, 0.40, True, False),
    (30, "CODING_FLOW", 0.84, 2, 0.32, False, False),
    (16, "IDLE", 0.12, 5, 0.90, False, False),
    (12, "TERMINAL_HEAVY", 0.68, 2, 0.42, False, False),
    (50, "FOCUSED", 0.78, 2, 0.25, False, True),
    (20, "IDLE", 0.14, 3, 0.60, False, False),
    (18, "BROWSING", 0.20, 3, 0.52, False, False),
    (10, "LATE_NIGHT", 0.18, 2, 0.38, False, False),
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
        llm_director_cooldown_sec=0.0,
    )
    world = ToasterWorld(cfg, seed=21)
    world.signals.live_os = False
    world.set_desktop(Rect(0, 0, 1600, 900))
    world.set_toaster(Vec2(1300, 420), Vec2(1280, 380), Vec2(1320, 380))
    world.bootstrap()
    return world


def _pick(world, activity, focus, pop, novelty, burnt):
    from desktop_app.toaster_universe.session import RARE_SCENES
    from desktop_app.toaster_universe.types import ActivityState, ComedyStyle, SceneId, SceneIntent

    world.signals.lock_activity = ActivityState(activity)
    world.signals.signals.activity = ActivityState(activity)
    world.signals.signals.focus_score = focus
    world.signals.signals.idle_duration = 20.0 if activity == "IDLE" else 0.5
    world.metrics.population = pop
    world.rare.novelty = novelty
    if burnt and world.entities:
        world.entities[0].burnt = True
    elif world.entities:
        world.entities[0].burnt = False
    sess = world.llm.session
    sess.update_arc(world.clock, world.signals.signals.activity, novelty, burnt)
    sess.note_attention(world.signals.signals.activity, focus)
    eligible = world.llm.eligible_scenes(world)
    fat = min((sess.fatigue(s, world.clock) for s in eligible), default=1.0) if eligible else 1.0
    if sess.want_silence(world.clock, world.signals.signals.activity, focus, novelty, fat):
        sess.note_silence(world.clock)
        return None
    if not eligible:
        sess.note_silence(world.clock)
        return None
    scene = eligible[0]
    if scene in RARE_SCENES and not sess.rare_ok(scene, world.clock, novelty):
        scene = next((s for s in eligible if s not in RARE_SCENES), eligible[0])
    matured = sess.mature_callbacks()
    cb = matured[0] if matured and sess.callback_ok(matured[0], world.clock) else ""
    tone_by_scene = {
        "hr_roast_skit": ComedyStyle.CORPORATE_ROAST,
        "rice_zen": ComedyStyle.ZEN_IRONY,
        "late_night_sleep": ComedyStyle.EXISTENTIAL_CRUMB,
        "popcorn_brain": ComedyStyle.ABSURD_PROFESSIONAL,
        "kpop_heart_mode": ComedyStyle.ANIME_GAG,
        "portal_glitch": ComedyStyle.META_AI,
        "coding_flow_audience": ComedyStyle.TECHNICAL_PRAGUE_DEADPAN,
        "quiet_companionship": ComedyStyle.QUIET_VISUAL_ONLY,
        "toast_watch_party": ComedyStyle.OFFICE_SATIRE,
        "funeral_ritual": ComedyStyle.DARK_TOAST,
        "shareware_apocalypse": ComedyStyle.SHAREWARE_CHAOS,
    }
    intent = SceneIntent(
        scene=scene,
        tone=tone_by_scene.get(scene.value, ComedyStyle.TECHNICAL_PRAGUE_DEADPAN),
        participants=min(pop, 4),
        escalation=0.15 if focus > 0.5 else 0.35,
        visual_only=True,
        callback_key=cb,
        source="session_sim",
    )
    if not world.llm.admissible(world, intent):
        sess.note_silence(world.clock)
        return None
    world.apply_director_intent(intent)
    return intent


def simulate(hours: float) -> dict:
    from desktop_app.toaster_universe.types import ActivityState

    world = _world()
    target = hours * 3600.0
    script = ACTIVITY_SCRIPT
    idx = 0
    elapsed_in_block = 0.0
    step = 45.0
    while world.clock < target:
        minutes, activity, focus, pop, novelty, burnt, interrupt = script[idx % len(script)]
        block = minutes * 60.0
        _pick(world, activity, focus, pop, novelty, burnt)
        if interrupt and world.director_accepted:
            world.llm.session.note_interrupt(world.director_accepted.scene)
        world.clock += step
        world.rare.novelty = min(1.0, world.rare.novelty + step * 0.012)
        elapsed_in_block += step
        if elapsed_in_block >= block:
            elapsed_in_block = 0.0
            idx += 1
    snap = world.llm.session.snapshot(hours)
    failed = []
    if snap["TOTAL_SCENES"] < 8:
        failed.append(f"too_few_scenes:{snap['TOTAL_SCENES']}")
    if snap["SILENCE_DECISIONS"] < 1:
        failed.append("no_silence")
    if snap["TOP_SCENE_SHARE"] > 0.62:
        failed.append(f"scene_dominates:{snap['TOP_SCENE_SHARE']}")
    if snap["TOP_TONE_SHARE"] > 0.85:
        failed.append(f"tone_dominates:{snap['TOP_TONE_SHARE']}")
    rare_total = sum(snap["RARE_SCENE_COUNTS"].values()) if snap["RARE_SCENE_COUNTS"] else 0
    if rare_total > max(6, int(hours * 2.5)):
        failed.append(f"rares_not_rare:{rare_total}")
    if snap["CONSECUTIVE_REPEAT_RATE"] > 0.35:
        failed.append(f"repeat_spam:{snap['CONSECUTIVE_REPEAT_RATE']}")
    reuse = snap["CALLBACK_REUSE"]
    if any(v > 4 for v in reuse.values()):
        failed.append(f"callback_spam:{reuse}")
    if snap["FOCUS_SUPPRESSIONS"] < 1:
        failed.append("focus_never_suppressed")
    snap["FAILED"] = failed
    snap["STATUS"] = "PASS" if not failed else "FAIL"
    return snap


def main() -> int:
    one = simulate(1)
    four = simulate(4)
    eight = simulate(8)
    failed = one["FAILED"] + [f"4h:{x}" for x in four["FAILED"]] + [f"8h:{x}" for x in eight["FAILED"]]
    evidence = {
        "SIMULATED_HOURS": [1, 4, 8],
        "HOURS": {"1": one, "4": four, "8": eight},
        "TOTAL_SCENES": eight["TOTAL_SCENES"],
        "SILENCE_DECISIONS": eight["SILENCE_DECISIONS"],
        "SCENES_PER_HOUR": eight["SCENES_PER_HOUR"],
        "SCENE_DISTRIBUTION": eight["SCENE_DISTRIBUTION"],
        "TONE_DISTRIBUTION": eight["TONE_DISTRIBUTION"],
        "TOP_SCENE_SHARE": eight["TOP_SCENE_SHARE"],
        "TOP_TONE_SHARE": eight["TOP_TONE_SHARE"],
        "RARE_SCENE_COUNTS": eight["RARE_SCENE_COUNTS"],
        "THEATRICAL_INTERVAL_AVG": eight["THEATRICAL_INTERVAL_AVG"],
        "RARE_INTERVAL_AVG": eight["RARE_INTERVAL_AVG"],
        "CONSECUTIVE_REPEAT_RATE": eight["CONSECUTIVE_REPEAT_RATE"],
        "PAYOFF_REPEAT_RATE": eight["PAYOFF_REPEAT_RATE"],
        "PARTICIPANT_REPEAT_RATE": eight["PARTICIPANT_REPEAT_RATE"],
        "CALLBACK_REUSE": eight["CALLBACK_REUSE"],
        "FATIGUE_SUPPRESSIONS": eight["FATIGUE_SUPPRESSIONS"],
        "NOVELTY_SUPPRESSIONS": eight["NOVELTY_SUPPRESSIONS"],
        "FOCUS_SUPPRESSIONS": eight["FOCUS_SUPPRESSIONS"],
        "INTERRUPTION_RATE": eight["INTERRUPTION_RATE"],
        "FAILED": failed,
        "STATUS": "PASS" if not failed else "FAIL",
    }
    out = ROOT / "tmp" / "toaster_universe_long_session_proof.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"out": str(out), "failed": failed, "scenes_8h": eight["TOTAL_SCENES"], "silence": eight["SILENCE_DECISIONS"], "top": eight["TOP_SCENE_SHARE"]}, indent=2))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
