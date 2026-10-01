"""Live Qt runtime proof for the toaster universe.

Not a unit-test stand-in. Instantiates FaceWindow + WorldOverlay + ToasterWorld
on a real QApplication, forces paints, injects cursor/keys, and dumps evidence.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("QT_LOGGING_RULES", "*=false")


def _cfg(**over):
    from desktop_app.toaster_universe.config import WorldConfig

    cfg = WorldConfig(
        enabled=True,
        spawn_on_start=1,
        population_growth_rate=0.0,
        llm_director_enabled=False,
        rare_events_enabled=True,
        keyboard_games_enabled=True,
        appliances_enabled=True,
        personal_lore_enabled=True,
        max_mini_toasts_normal=3,
        max_mini_toasts_infestation=8,
        max_mini_toasts_event=12,
        comedy_cooldown_sec=0.2,
    )
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg.clamp()


def _paint(widget):
    pix = widget.grab()
    return pix.width(), pix.height(), not pix.isNull()


def main() -> int:
    evidence: dict = {
        "platform": sys.platform,
        "qt_platform": "",
        "errors": [],
        "proven": {},
        "wired_unproven": [],
        "failed": [],
        "performance": {},
        "traces": {},
    }
    try:
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtCore import Qt, QEvent
        from PyQt6.QtGui import QMouseEvent

        app = QApplication.instance() or QApplication(["toaster-universe-live-proof"])
        evidence["qt_platform"] = app.platformName()
        evidence["qt_screens"] = [
            {
                "name": s.name(),
                "geo": [s.geometry().x(), s.geometry().y(), s.geometry().width(), s.geometry().height()],
            }
            for s in app.screens()
        ]

        from desktop_app.face_widget import FaceWindow, JarvisState, get_jarvis_state
        from desktop_app.toaster_universe.overlay import WorldOverlay
        from desktop_app.toaster_universe.types import (
            ActivityState,
            ApplianceKind,
            EntityKind,
            OverrideReason,
            RareEventId,
            SceneId,
            SceneIntent,
            ToastState,
            Vec2,
            WorldEvent,
        )
        from desktop_app.toaster_universe.world import ToasterWorld
        from desktop_app.toaster_universe.ecology import living_toasts, population_cap

        world = ToasterWorld(_cfg(), seed=11)
        world.signals.live_os = False
        world.signals.lock_activity = ActivityState.IDLE
        face = FaceWindow()
        face.show()
        app.processEvents()
        face.face.sync_world_anchors(world)
        fw, fh, fnull = _paint(face)
        evidence["proven"]["face_window_painted"] = {"w": fw, "h": fh, "ok": fnull}

        overlay = WorldOverlay(world)
        overlay.show()
        app.processEvents()
        ow, oh, onull = _paint(overlay)
        evidence["proven"]["overlay_painted"] = {"w": ow, "h": oh, "ok": onull, "visible": overlay.isVisible()}
        flags = int(overlay.windowFlags().value)
        evidence["proven"]["overlay_click_through_flags"] = {
            "WindowTransparentForInput": bool(overlay.windowFlags() & Qt.WindowType.WindowTransparentForInput),
            "WA_TransparentForMouseEvents": overlay.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents),
            "WA_ShowWithoutActivating": overlay.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating),
            "Tool": bool(overlay.windowFlags() & Qt.WindowType.Tool),
        }
        native = {}
        try:
            import ctypes
            from ctypes import wintypes

            hwnd = int(overlay.winId())
            style = ctypes.windll.user32.GetWindowLongW(wintypes.HWND(hwnd), -20)
            native = {
                "hwnd": hwnd,
                "WS_EX_TRANSPARENT": bool(style & 0x00000020),
                "WS_EX_NOACTIVATE": bool(style & 0x08000000),
                "WS_EX_LAYERED": bool(style & 0x00080000),
                "exstyle": hex(style & 0xFFFFFFFF),
            }
        except Exception as exc:
            native = {"error": str(exc)}
        evidence["proven"]["overlay_native_exstyle"] = native

        # --- 1 Main Toaster soul via live paint ---
        world.signals.teleport_cursor(Vec2(face.x() + 20, face.y() + 20), 0, t=world.clock)
        world.tick(0.033)
        face.face.sync_world_anchors(world)
        look_left = (world.soul.look.x, world.soul.look.y)
        world.signals.teleport_cursor(Vec2(face.x() + face.width() - 10, face.y() + 10), 0, t=world.clock + 0.05)
        world.tick(0.033)
        look_right = (world.soul.look.x, world.soul.look.y)
        world.note_toaster_click()
        jelly = world.soul.jelly
        crumbs = world.soul.crumbs
        world.soul.heat = 0.95
        world._update_soul(0.033, world.clock, world.signals.signals)
        shimmer = world.soul.shimmer
        _paint(face)
        evidence["proven"]["main_toaster"] = {
            "look_left": look_left,
            "look_right": look_right,
            "look_changed": look_right[0] != look_left[0],
            "jelly_after_click": jelly,
            "crumbs_after_click": crumbs,
            "shimmer_hot": shimmer,
            "toaster_anchor": (world.toaster.x, world.toaster.y),
            "slots": [(s.pos.x, s.pos.y) for s in world.slots],
        }
        if not evidence["proven"]["main_toaster"]["look_changed"]:
            evidence["failed"].append("main_toaster.look_at_cursor_did_not_change")
        if jelly < 0.9 or crumbs < 0.9:
            evidence["failed"].append("main_toaster.jelly_or_crumbs_missing")
        if shimmer <= 0.25:
            evidence["failed"].append("main_toaster.heat_shimmer_flat")

        if not world.anchors_ready or world.slots[0].pos.length() < 1:
            evidence["failed"].append("startup_used_default_slot_anchors")
        evidence["proven"]["startup_anchors"] = {
            "ready": world.anchors_ready,
            "toaster": (world.toaster.x, world.toaster.y),
            "slots": [(s.pos.x, s.pos.y) for s in world.slots],
        }

        def _run_lifecycle(*, burn: bool) -> list[str]:
            world.entities.clear()
            toast = world.spawn_toast(Vec2(world.slots[0].pos.x - 12, world.slots[0].pos.y + 8))
            toast.temperature = 0.01
            toast.traits["temperature_need"] = 1.0
            toast.traits["work_respect"] = 0.1
            toast.traits["burn_tolerance"] = 0.05 if burn else 0.99
            park = Vec2(20, 20)
            world.signals.teleport_cursor(park, 0, t=world.clock)
            world.signals.lock_activity = ActivityState.IDLE
            world.signals.signals.focus_score = 0.0
            states = [toast.state.value]
            hops = []
            for _ in range(360):
                world.signals.teleport_cursor(park, 0, t=world.clock + 1.0)
                world.signals.signals.focus_score = 0.0
                if toast.state is ToastState.TOASTING:
                    toast.browning = min(1.0, toast.browning + (0.12 if burn else 0.02))
                if toast.state is ToastState.OVERTOASTING:
                    toast.browning = min(1.0, toast.browning + 0.08)
                world.tick(0.05)
                if not world.entities:
                    break
                toast = next((e for e in world.entities if e.kind is EntityKind.MINI_TOAST), world.entities[0])
                states.append(toast.state.value)
                hops.append(toast.pose.hop)
                if burn and toast.state is ToastState.RECOVER:
                    break
                if not burn and toast.state is ToastState.COOL_DOWN:
                    break
            unique = []
            for s in states:
                if not unique or unique[-1] != s:
                    unique.append(s)
            return unique, hops, toast

        burn_life, burn_hops, burnt_toast = _run_lifecycle(burn=True)
        pop_life, pop_hops, cool_toast = _run_lifecycle(burn=False)
        evidence["traces"]["mini_toast_lifecycle_burn"] = burn_life
        evidence["traces"]["mini_toast_lifecycle_pop"] = pop_life
        evidence["proven"]["mini_toast_lifecycle"] = {
            "burn": burn_life,
            "pop": pop_life,
            "burnt": burnt_toast.burnt,
            "pop_cool": cool_toast.state.value,
            "hop_max": max((burn_hops or [0]) + (pop_hops or [0])),
        }
        burn_need = {"APPROACH_TOASTER", "QUEUE_FOR_SLOT", "JUMP_INTO_SLOT", "TOASTING", "OVERTOASTING", "POP_OUT", "BURNT", "RECOVER"}
        pop_need = {"APPROACH_TOASTER", "QUEUE_FOR_SLOT", "JUMP_INTO_SLOT", "TOASTING", "POP_OUT", "COOL_DOWN"}
        missing_burn = sorted(burn_need - set(burn_life))
        missing_pop = sorted(pop_need - set(pop_life))
        if missing_burn:
            evidence["failed"].append(f"burn_lifecycle_missing:{missing_burn}")
        if missing_pop:
            evidence["failed"].append(f"pop_lifecycle_missing:{missing_pop}")

        # --- 3 Safety ---
        world.entities.clear()
        prey = world.spawn_toast(Vec2(400, 400))
        prey.state = ToastState.WANDER
        world.signals.lock_activity = ActivityState.IDLE
        world.signals.teleport_cursor(Vec2(10, 400), 0, t=world.clock)
        world.signals.note_cursor(Vec2(380, 400), 0, t=world.clock + 0.04)
        pred = world.signals.signals.cursor_predicted
        field = world.safety.evaluate(prey.pos, prey.radius, world.signals.signals, world.desktop)
        world.tick(0.03)
        safety_state = world.entities[0].state.value if world.entities else None
        world.signals.teleport_cursor(Vec2(400, 400), 0, t=world.clock)
        world.signals.lock_activity = ActivityState.CODING_FLOW
        world.signals.signals.activity = ActivityState.CODING_FLOW
        world.signals.signals.focus_score = 0.8
        world.safety.refresh(world.signals.signals, world.clock)
        sanctuary = world.safety.evaluate(Vec2(620, 400), 16, world.signals.signals, world.desktop)
        world.signals.lock_activity = ActivityState.IDLE
        world.signals.signals.focus_score = 0.0
        world.signals.teleport_cursor(Vec2(100, 100), 0, t=world.clock)
        world.signals.note_cursor(Vec2(180, 160), 1, t=world.clock + 0.05)
        drag_field = world.safety.evaluate(Vec2(180, 160), 16, world.signals.signals, world.desktop)
        world.signals.teleport_cursor(Vec2(200, 200), 0, t=world.clock + 1.0)
        world.signals.signals.buttons_down = 0
        world.signals.signals.dragging = False
        world.signals.signals.selecting = False
        world.signals.signals.scrolling = True
        scroll_field = world.safety.evaluate(Vec2(200, 200), 16, world.signals.signals, world.desktop)
        world.signals.signals.scrolling = False
        evidence["traces"]["safety"] = {
            "predicted": (pred.x, pred.y),
            "approach_field": {"yield": field.must_yield, "reason": field.reason.value, "zone": field.zone.value if field.zone else None},
            "state_after_approach": safety_state,
            "sanctuary": {"yield": sanctuary.must_yield, "reason": sanctuary.reason.value},
            "drag": {"yield": drag_field.must_yield, "reason": drag_field.reason.value},
            "scroll": {"yield": scroll_field.must_yield, "reason": scroll_field.reason.value},
        }
        evidence["proven"]["safety"] = evidence["traces"]["safety"]
        if not field.must_yield:
            evidence["failed"].append("predictive_avoidance_did_not_yield")
        if sanctuary.reason is not OverrideReason.READING_ZONE:
            evidence["failed"].append("reading_sanctuary_not_triggered")
        if drag_field.reason not in {OverrideReason.DRAG_ACTIVE, OverrideReason.SELECTION_ACTIVE}:
            evidence["failed"].append("drag_select_did_not_yield")
        if scroll_field.reason is not OverrideReason.SCROLL_ACTIVE:
            evidence["failed"].append(f"scroll_reason_was_{scroll_field.reason.value}")

        # --- 4 Social ---
        world.entities.clear()
        a = world.spawn_toast(Vec2(300, 300))
        b = world.spawn_toast(Vec2(318, 300))
        a.state = ToastState.WANDER
        b.state = ToastState.DANCE
        a.traits["copycat_drive"] = 0.99
        a.traits["sociality"] = 0.99
        a.traits["boldness"] = 0.99
        social_seen = set()
        world.signals.lock_activity = ActivityState.IDLE
        for _ in range(20):
            world.signals.teleport_cursor(Vec2(20, 20), 0, t=world.clock + 1)
            world.tick(0.1)
            social_seen.update(e.state.value for e in world.entities)
        world.entities = [e for e in world.entities if e.kind is EntityKind.MINI_TOAST][:1]
        a = world.entities[0]
        crumb = world.spawn_toast(Vec2(a.pos.x + 8, a.pos.y), EntityKind.CRUMB)
        a.traits["food_drive"] = 0.99
        a.temperature = 0.9
        a.state = ToastState.WANDER
        a.proposed = None
        carry_seen = False
        for _ in range(40):
            world.signals.teleport_cursor(Vec2(20, 20), 0, t=world.clock + 1)
            world.tick(0.08)
            if any(e.state is ToastState.CARRY_CRUMB or e.state is ToastState.EAT_CRUMB for e in world.entities):
                carry_seen = True
                break
        evidence["proven"]["social"] = {
            "states": sorted(social_seen),
            "copy_or_social": bool({"COPY_DANCE", "SOCIALIZE", "FIGHT_PLAYFULLY"} & social_seen),
            "carry_or_eat": carry_seen,
        }
        if not evidence["proven"]["social"]["copy_or_social"]:
            evidence["failed"].append("social_copy_or_pair_not_observed")
        if not carry_seen:
            evidence["wired_unproven"].append("crumb_carry_not_reached_in_live_window")

        # --- 5 Population ---
        world.entities.clear()
        world.cfg.max_mini_toasts_normal = 3
        world.population.infestation = False
        world.population.event_swarm = False
        world.population.growth_acc = 0.0
        for i in range(3):
            world.spawn_toast(Vec2(200 + i * 20, 200))
        world.signals.lock_activity = ActivityState.IDLE
        for _ in range(5):
            world.signals.teleport_cursor(Vec2(20, 20), 0, t=world.clock + 1)
            world.tick(0.2)
        idle_count = len(living_toasts(world.entities))
        world.population.infestation = True
        world.cfg.population_growth_rate = 8.0
        for _ in range(40):
            world.signals.teleport_cursor(Vec2(20, 20), 0, t=world.clock + 1)
            world.tick(0.2)
        inf_count = len(living_toasts(world.entities))
        cap = population_cap(world.cfg, ActivityState.IDLE, True, False)
        # swarm vs safety
        if world.entities:
            victim = world.entities[0]
            victim.state = ToastState.WANDER
            world.signals.note_cursor(Vec2(victim.pos.x - 80, victim.pos.y), 0, t=world.clock)
            world.signals.note_cursor(victim.pos, 0, t=world.clock + 0.04)
            world.tick(0.03)
            swarm_yield = world.entities[0].state.value if world.entities else None
        else:
            swarm_yield = None
        evidence["proven"]["population"] = {
            "idle_count": idle_count,
            "infestation_count": inf_count,
            "cap": cap,
            "exceeded": inf_count > cap,
            "safety_during_swarm": swarm_yield,
        }
        evidence["performance"]["idle_entity_count"] = idle_count
        evidence["performance"]["infestation_entity_count"] = inf_count
        if inf_count > cap:
            evidence["failed"].append("population_exceeded_cap")
        if swarm_yield not in {"AVOID_CURSOR", "PANIC_RUN", "HIDE", "AVOID_READING_ZONE"}:
            evidence["failed"].append(f"swarm_did_not_yield:{swarm_yield}")

        # --- 6 Keyboard ---
        world.entities.clear()
        k = world.spawn_toast(Vec2(250, 250))
        k.state = ToastState.WANDER
        world.signals.signals.typing_rate = 9.0
        world.signals.signals.cursor = Vec2(250, 250)
        world.keyboard.tick(world.clock + 1, world.signals.signals, [k] * 5)
        proposed_ok = k.proposed
        k.state = ToastState.PANIC_RUN
        k.proposed = ToastState.POP_OUT
        world.signals.signals.cursor = Vec2(20, 20)
        world.signals.signals.cursor_predicted = Vec2(20, 20)
        world._tick_entities(0.03, world.clock, world.signals.signals)
        key_payloads = [e.payload for e in world.bus.recent(WorldEvent.USER_TYPING_START, 8)]
        content_leaks = [p for p in key_payloads if any(x in p for x in ("key", "char", "text", "vk"))]
        evidence["proven"]["keyboard"] = {
            "mode": world.keyboard.state.mode.value,
            "proposal_on_wander": proposed_ok.value if proposed_ok else None,
            "state_after_safety_block": k.state.value,
            "payloads": key_payloads,
            "content_leaks": content_leaks,
        }
        if k.state is ToastState.POP_OUT:
            evidence["failed"].append("keyboard_proposal_overrode_safety")
        if content_leaks:
            evidence["failed"].append("keyboard_logged_key_content")

        # --- 7 Appliances ---
        world.appliances.items.clear()
        world.appliances.unlocked.clear()
        world.appliances.ensure_layout(world.toaster)
        exercised = {}
        for kind in ApplianceKind:
            item = world.appliances.unlock(kind)
            admitted = world.appliances.admit(kind, world.clock)
            world.appliances.release(kind)
            exercised[kind.value] = {
                "open": item.open,
                "admitted": admitted is not None,
                "occupied_after_release": item.occupied,
                "portal": item.portal,
            }
        world.entities.clear()
        portalist = world.spawn_toast(Vec2(world.toaster.x, world.toaster.y - 40))
        portalist.appliance = ApplianceKind.SECRET_CHAMBER.value
        portalist.traits["copycat_drive"] = 0.0
        portalist.traits["sociality"] = 0.0
        world._scene_applied = None
        world.narrative.force(world.clock, SceneIntent(scene=SceneId.PORTAL_GLITCH, duration=8))
        world._scene_applied = None
        world._apply_scene(SceneId.PORTAL_GLITCH, world.clock)
        for _ in range(80):
            world.signals.teleport_cursor(Vec2(20, 20), 0, t=world.clock + 1)
            world.tick(0.08)
            if portalist.state in {ToastState.PORTAL_ENTER, ToastState.INSIDE_APPLIANCE, ToastState.PORTAL_RETURN}:
                break
        rice = world.spawn_toast(world.toaster + Vec2(40, 70), EntityKind.RICE_SPIRIT)
        pop = world.spawn_toast(world.toaster + Vec2(10, 40), EntityKind.POPCORN_KERNEL)
        butter = world.spawn_toast(world.toaster + Vec2(-30, 20), EntityKind.BUTTER_BLOB)
        echo = world.spawn_toast(world.toaster + Vec2(0, -80), EntityKind.PORTAL_ECHO)
        _paint(overlay)
        kinds_live = sorted({e.kind.value for e in world.entities})
        evidence["proven"]["appliances"] = {
            "exercised": exercised,
            "portal_state": portalist.state.value,
            "species": kinds_live,
        }
        if portalist.state.value not in {"PORTAL_CURIOUS", "PORTAL_ENTER", "INSIDE_APPLIANCE"}:
            evidence["wired_unproven"].append(f"portal_transfer_only_entered_{portalist.state.value}")
        for species in ("rice_spirit", "popcorn_kernel", "butter_blob", "portal_echo"):
            if species not in kinds_live:
                evidence["failed"].append(f"missing_species:{species}")

        # --- 8 Rare events ---
        world.signals.lock_activity = ActivityState.IDLE
        world.signals.signals.focus_score = 0.0
        world.signals.signals.dragging = False
        world.rare.novelty = 1.0
        world.rare.last_any = -1e9
        fired = []
        for ev in (
            RareEventId.MIDNIGHT_PORTAL,
            RareEventId.THE_BLACK_TOAST_FUNERAL,
            RareEventId.SHAREWARE_INVASION,
            RareEventId.RICE_SPIRIT_VISIT,
        ):
            world.rare.novelty = 1.0
            fire = world.force_rare_event(ev.value, honor_safety=True)
            fired.append({"event": ev.value, "ok": fire is not None, "novelty": world.rare.novelty, "last_any": world.rare.last_any})
        world.signals.signals.dragging = True
        suppressed = world.force_rare_event(RareEventId.POPCORN_STORM.value, honor_safety=True)
        world.signals.signals.dragging = False
        world.signals.lock_activity = ActivityState.CODING_FLOW
        world.signals.signals.activity = ActivityState.CODING_FLOW
        world.signals.signals.focus_score = 0.9
        focus_suppressed = world.force_rare_event(RareEventId.SILENT_HEART.value, honor_safety=True)
        world.signals.lock_activity = ActivityState.IDLE
        world.signals.signals.focus_score = 0.0
        evidence["traces"]["rare"] = {"fired": fired, "drag_suppressed": suppressed is None, "focus_suppressed": focus_suppressed is None}
        evidence["proven"]["rare"] = evidence["traces"]["rare"]
        if not all(x["ok"] for x in fired):
            evidence["failed"].append("rare_force_failed")
        if suppressed is not None:
            evidence["failed"].append("rare_not_suppressed_on_drag")
        if focus_suppressed is not None:
            evidence["failed"].append("rare_not_suppressed_on_focus")
        if fired and fired[0]["novelty"] >= 1.0:
            evidence["failed"].append("novelty_budget_not_consumed")

        # --- 9 Comedy ---
        world.signals.lock_activity = ActivityState.CODING_FLOW
        world.signals.signals.focus_score = 0.2
        lines = []
        keys = []
        for _ in range(6):
            beat = world.force_comedy("corporate_roast", speech_ok=True)
            if beat:
                lines.append(beat.entry.text)
                keys.append(beat.entry.key)
        world.comedy.last_line_t = world.clock
        blocked_by_global = world.comedy.pick(world.clock, world.signals.signals, world.narrative.current(), speech_ok=True)
        evidence["proven"]["comedy"] = {
            "lines": lines,
            "keys": keys,
            "unique_keys": len(set(keys)),
            "repeat_blocked_or_cooled": blocked_by_global is None,
        }
        if len(lines) < 3:
            evidence["failed"].append(f"comedy_too_few_lines:{len(lines)}")
        if len(set(keys)) < 2 and keys:
            evidence["failed"].append("comedy_no_variety")

        # --- 10 LLM director ---
        disabled = world.cfg.llm_director_enabled
        payload = world.llm.maybe_request_payload(world.clock + 100, world.signals.signals, 1.0)
        ok = world.ingest_director_json(
            {"scene": "toast_watch_party", "tone": "deadpan_cute", "participants": 3, "duration": 10}
        )
        bad = world.ingest_director_json({"scene": "nuke_desktop", "tone": "deadpan_cute"})
        evidence["proven"]["llm"] = {
            "enabled_flag": disabled,
            "request_payload_when_disabled": payload,
            "valid_ingest": ok,
            "scene_after_valid": world.narrative.current().value,
            "invalid_ingest": bad,
            "rejects": world.metrics.llm_rejects,
        }
        if disabled is not False:
            evidence["failed"].append("llm_not_explicitly_disabled")
        if payload is not None:
            evidence["failed"].append("llm_requested_while_disabled")
        if not ok or bad:
            evidence["failed"].append("llm_ingest_path_wrong")

        # --- 11 traces already collected ---
        evidence["traces"]["metrics_tail"] = list(world.metrics.ring)[-24:]
        evidence["traces"]["snapshot"] = {
            "scene": world.narrative.current().value,
            "population": len(living_toasts(world.entities)),
            "novelty": world.rare.novelty,
            "llm_calls": world.metrics.llm_calls,
            "llm_rejects": world.metrics.llm_rejects,
            "attention_retreats": world.metrics.attention_retreats,
        }

        # --- 12 Performance / dirty / audio / topology ---
        world.entities.clear()
        for i in range(8):
            world.spawn_toast(Vec2(world.toaster.x - 80 + i * 18, world.toaster.y + 40))
        times = []
        dirty_counts = []
        dirty_ratios = []
        full_flags = []
        t0 = time.perf_counter()
        for _ in range(45):
            s = time.perf_counter()
            world.tick(0.033)
            times.append((time.perf_counter() - s) * 1000)
            full, rects = world.consume_dirty()
            dirty_counts.append(len(rects))
            dirty_ratios.append(world.metrics.dirty_ratio)
            full_flags.append(full)
            overlay.apply_dirty()
            app.processEvents()
        elapsed = time.perf_counter() - t0
        world.audio.enabled = True
        world.cfg.audio_enabled = True
        world.signals.lock_activity = ActivityState.IDLE
        world.signals.signals.focus_score = 0.0
        world.audio.play("toast_pop", world.signals.signals)
        world.audio.play("tiny_hop", world.signals.signals)
        world.audio.play("crumb_tick", world.signals.signals)
        world.audio.play("steam_hiss", world.signals.signals)
        world.audio.play("portal_chime", world.signals.signals)
        world.audio.play("popcorn", world.signals.signals)
        world.audio.play("microwave_ping", world.signals.signals)
        world.signals.lock_activity = ActivityState.CODING_FLOW
        world.signals.signals.activity = ActivityState.CODING_FLOW
        world.signals.signals.focus_score = 0.9
        muted = world.audio._gain(world.signals.signals)
        evidence["proven"]["audio"] = {
            "played": world.audio.played,
            "work_gain": muted,
            "enabled": world.audio.enabled,
        }
        if muted != 0.0:
            evidence["failed"].append("audio_not_suppressed_in_focus")
        if len(world.audio.played) < 5:
            evidence["failed"].append("audio_cues_missing")
        evidence["proven"]["monitors"] = world.topology.snapshot()
        evidence["proven"]["attention"] = world.safety.field.snapshot()
        if any(full_flags) and max(dirty_ratios or [1]) >= 0.99:
            evidence["failed"].append("normal_frames_still_full_overlay")
        evidence["performance"].update({
            "tick_ms_avg": round(sum(times) / len(times), 3),
            "tick_ms_max": round(max(times), 3),
            "metrics_paint_ms": world.metrics.paint_ms,
            "metrics_physics_ms": world.metrics.physics_ms,
            "forced_loop_hz": round(45 / elapsed, 2),
            "overlay_timer_ms": overlay._timer.interval(),
            "dirty_count_avg": round(sum(dirty_counts) / max(1, len(dirty_counts)), 2),
            "dirty_ratio_avg": round(sum(dirty_ratios) / max(1, len(dirty_ratios)), 4),
            "dirty_ratio_max": round(max(dirty_ratios or [0]), 4),
            "dirty_full_frames": sum(1 for x in full_flags if x),
            "pathological_full_desktop_repaint": bool(all(full_flags)),
            "note": "Normal frames use dirty-region update(); full paint only if ratio>0.55 or shareware scene.",
        })

        face.hide()
        overlay.hide()
        face.close()
        overlay.close()
        app.processEvents()
    except Exception:
        evidence["errors"].append(traceback.format_exc())
        evidence["failed"].append("harness_exception")

    out = ROOT / "tmp" / "toaster_universe_live_proof.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "out": str(out),
        "qt": evidence.get("qt_platform"),
        "failed": evidence.get("failed"),
        "wired_unproven": evidence.get("wired_unproven"),
        "lifecycle": evidence.get("traces", {}).get("mini_toast_lifecycle"),
        "performance": evidence.get("performance"),
    }, ensure_ascii=False, indent=2))
    return 0 if not evidence.get("errors") else 1


if __name__ == "__main__":
    raise SystemExit(main())
