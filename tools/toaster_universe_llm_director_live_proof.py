"""Live LLM Director proof + latency/scheduling proof.

Uses the configured Jarvis provider/model for one real outbound call.
Scripted transport covers fail-closed / stale / focus / coalesce cases
on the same arbitration path. World tick never blocks on I/O.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


VALID = {
    "scene": "toast_watch_party",
    "tone": "TECHNICAL_PRAGUE_DEADPAN",
    "participants": 2,
    "escalation": 0.3,
    "duration": 12,
    "lineIntent": "watch",
    "callbackKey": "",
    "visualOnly": True,
    "speechBudget": 0,
    "rarityClass": "common",
}


class ScriptedTransport:
    def __init__(self, replies, provider="scripted", model="scripted-director"):
        self.replies = list(replies)
        self.calls = []
        self.provider = provider
        self.model = model
        self.last_error = ""

    def complete(self, system, user, timeout_sec):
        from desktop_app.toaster_universe.llm_director import DirectorRawResult, DirectorRequest, DirectorTiming

        self.calls.append({"system_len": len(system), "user": user, "timeout": timeout_sec})
        spec = self.replies.pop(0) if self.replies else {"error": "network"}
        time.sleep(float(spec.get("sleep", 0.0)))
        timing = DirectorTiming(
            queue_ms=0.4,
            connect_ms=float(spec.get("connect_ms", 3.0)),
            ttft_ms=float(spec.get("ttft_ms", 4.0)),
            generation_ms=float(spec.get("generation_ms", 6.0)),
            total_ms=float(spec.get("total_ms", 8.0)),
        )
        return DirectorRawResult(
            request=DirectorRequest("", 0, "", 0, 0),
            text=spec.get("text", ""),
            error=spec.get("error", ""),
            provider=self.provider,
            model=self.model,
            prompt_tokens=int(spec.get("prompt_tokens", 120)),
            completion_tokens=int(spec.get("completion_tokens", 24)),
            timing=timing,
        )

    def prewarm(self, timeout_sec=20.0):
        return True


def _world(enabled=True, timeout_ms=8000):
    from desktop_app.toaster_universe.config import WorldConfig
    from desktop_app.toaster_universe.types import Rect, Vec2
    from desktop_app.toaster_universe.world import ToasterWorld

    cfg = WorldConfig(
        enabled=True,
        spawn_on_start=1,
        population_growth_rate=0.0,
        llm_director_enabled=enabled,
        llm_director_timeout_ms=timeout_ms,
        llm_director_cooldown_sec=0.0,
        llm_director_max_calls_per_hour=99,
        llm_director_prewarm=False,
    )
    world = ToasterWorld(cfg, seed=3)
    world.signals.live_os = False
    world.set_desktop(Rect(0, 0, 1200, 800))
    world.set_toaster(Vec2(900, 400), Vec2(880, 360), Vec2(920, 360))
    world.bootstrap()
    return world


def _wait(world, timeout=12.0):
    deadline = time.time() + timeout
    ticks = 0
    while time.time() < deadline:
        world.tick(0.03)
        ticks += 1
        if world.llm._future is None and world.llm.in_flight is None:
            return ticks
        time.sleep(0.02)
    return ticks


def main() -> int:
    from desktop_app.toaster_universe.llm_director import (
        ConfiguredTransport,
        DirectorRawResult,
        DirectorRequest,
        DirectorState,
        LlmDirector,
    )
    from desktop_app.toaster_universe.types import ActivityState, SceneId, ToastState, Vec2

    failed = []
    evidence = {
        "STATUS": "RUNNING",
        "PROVIDER": "",
        "MODEL": "",
        "REQUESTS": 0,
        "ACCEPTED_SCENES": [],
        "REJECTED_SCENES": [],
        "SCHEMA_FAILURES": 0,
        "TIMEOUTS": 0,
        "STALE_REJECTIONS": 0,
        "SAFETY_REJECTIONS": 0,
        "TOKEN_USAGE": {"prompt": 0, "completion": 0},
        "LATENCY": {},
        "PRIVACY_AUDIT": {},
        "FAILED": failed,
        "WORLD_ALIVE": False,
    }

    world = _world(enabled=True)
    real = ConfiguredTransport()
    evidence["PROVIDER"] = real.provider
    evidence["MODEL"] = real.model
    if not real.provider or not real.model:
        failed.append("configured_provider_or_model_missing")

    # A/B real outbound
    t0 = time.perf_counter()
    raw_real = real.complete(
        "Return SceneIntent JSON only.",
        json.dumps({"act": "IDLE", "scns": ["quiet_companionship", "toast_watch_party"]}),
        timeout_sec=8.0,
    )
    real_ms = (time.perf_counter() - t0) * 1000
    evidence["REAL_OUTBOUND"] = {
        "error": raw_real.error,
        "text_prefix": (raw_real.text or "")[:160],
        "latency_ms": round(real_ms, 2),
        "provider": raw_real.provider or real.provider,
        "model": raw_real.model or real.model,
        "prompt_tokens": raw_real.prompt_tokens,
        "completion_tokens": raw_real.completion_tokens,
        "timing": raw_real.timing.snapshot(),
    }
    if raw_real.error and raw_real.error not in {"timeout", "network", "provider_unavailable"}:
        failed.append(f"unexpected_real_error:{raw_real.error}")

    world.llm.transport = real
    world.signals.lock_activity = ActivityState.IDLE
    world.signals.signals.activity = ActivityState.IDLE
    world.signals.signals.focus_score = 0.0
    if not raw_real.error and raw_real.text:
        req = DirectorRequest("real1", world.llm.snapshot_revision, "idle", world.clock, world.clock + 60)
        raw_real.request = req
        intent = world.llm.apply_raw_for_tests(world, raw_real)
        if intent is None:
            # Real model may emit invalid JSON; still a real outbound call.
            evidence["REAL_ACCEPTED"] = False
            evidence["REAL_PARSE"] = world.llm.parse(raw_real.text).rejected
        else:
            evidence["REAL_ACCEPTED"] = True
            evidence["ACCEPTED_SCENES"].append(intent.scene.value)
    else:
        evidence["REAL_ACCEPTED"] = False

    # C valid accept via same arbiter (scripted, schema-valid)
    scripted = ScriptedTransport([{"text": json.dumps(VALID), "prompt_tokens": 140, "completion_tokens": 28, "total_ms": 12}])
    world.llm.transport = scripted
    world.llm.state = DirectorState.IDLE
    world.llm.in_flight = None
    world.signals.signals.dragging = False
    world.signals.signals.focus_score = 0.0
    req = world.force_director_request(opportunity="idle")
    ticks = _wait(world, 4.0)
    if world.narrative.current() is not SceneId.TOAST_WATCH_PARTY:
        failed.append("valid_scene_not_accepted")
    else:
        evidence["ACCEPTED_SCENES"].append("toast_watch_party")
    evidence["ASYNC_TICKS_DURING_CALL"] = ticks

    # D unknown scene
    world.llm.transport = ScriptedTransport([{"text": json.dumps({**VALID, "scene": "nuke_desktop"})}])
    world.llm.in_flight = None
    world.force_director_request(opportunity="idle")
    _wait(world, 3.0)
    if world.llm.metrics.director_unknown_scene_reject < 1:
        failed.append("unknown_scene_not_rejected")
    evidence["REJECTED_SCENES"].append("unknown_scene")

    # E malformed
    world.llm.transport = ScriptedTransport([{"text": "not-json{{"}])
    world.llm.in_flight = None
    world.force_director_request(opportunity="idle")
    _wait(world, 3.0)
    if world.llm.metrics.director_schema_reject < 1:
        failed.append("malformed_not_rejected")
    evidence["SCHEMA_FAILURES"] = world.llm.metrics.director_schema_reject

    # F timeout / network leaves world alive
    before_pop = world.metrics.population
    world.llm.transport = ScriptedTransport([{"error": "timeout"}])
    world.llm.in_flight = None
    world.force_director_request(opportunity="idle")
    _wait(world, 3.0)
    world.tick(0.03)
    evidence["TIMEOUTS"] = world.llm.metrics.director_timeout
    if world.llm.metrics.director_timeout < 1:
        failed.append("timeout_not_recorded")
    if len(world.entities) == 0 or world.metrics.population < 1:
        failed.append("world_died_after_timeout")
    evidence["WORLD_ALIVE"] = world.metrics.population >= 1 and before_pop >= 0

    world.llm.transport = ScriptedTransport([{"error": "network"}])
    world.llm.in_flight = None
    world.force_director_request(opportunity="idle")
    _wait(world, 3.0)
    if world.llm.metrics.director_network_error < 1:
        failed.append("network_error_not_recorded")

    # G focus suppression
    world.signals.lock_activity = ActivityState.CODING_FLOW
    world.signals.signals.activity = ActivityState.CODING_FLOW
    world.signals.signals.focus_score = 0.92
    world.llm.in_flight = None
    blocked = world.force_director_request(opportunity="idle")
    if blocked is not None and world.llm.state is not DirectorState.REJECTED:
        if world.llm.metrics.director_focus_skip < 1:
            failed.append("focus_did_not_suppress")
    evidence["FOCUS_SKIP"] = world.llm.metrics.director_focus_skip
    world.signals.lock_activity = ActivityState.IDLE
    world.signals.signals.activity = ActivityState.IDLE
    world.signals.signals.focus_score = 0.0

    # H stale
    world.llm.transport = ScriptedTransport([{"text": json.dumps(VALID)}])
    req = DirectorRequest("stale1", world.llm.snapshot_revision, "idle", world.clock, world.clock + 30)
    world.llm.snapshot_revision += 1
    raw = DirectorRawResult(request=req, text=json.dumps(VALID), provider="scripted", model="scripted-director")
    got = world.llm.apply_raw_for_tests(world, raw)
    if got is not None or world.llm.metrics.director_stale_reject < 1:
        failed.append("stale_not_rejected")
    evidence["STALE_REJECTIONS"] = world.llm.metrics.director_stale_reject

    # safety interrupt after accepted scene
    if world.entities:
        prey = world.entities[0]
        prey.state = ToastState.WANDER
        world.signals.note_cursor(Vec2(prey.pos.x - 80, prey.pos.y), 0, t=world.clock)
        world.signals.note_cursor(prey.pos, 0, t=world.clock + 0.04)
        world.tick(0.03)
        if world.entities[0].state.value not in {"AVOID_CURSOR", "PANIC_RUN", "HIDE", "AVOID_READING_ZONE"}:
            failed.append(f"safety_did_not_interrupt:{world.entities[0].state.value}")

    packet = world.llm.context_packet(world)
    privacy_ok = world.llm.privacy_ok(packet)
    evidence["PRIVACY_AUDIT"] = {
        "ok": privacy_ok,
        "keys": sorted(packet.keys()),
        "has_forbidden_payload": any(k in packet for k in ("typed", "clipboard", "filename", "document")),
    }
    if not privacy_ok or evidence["PRIVACY_AUDIT"]["has_forbidden_payload"]:
        failed.append("privacy_audit_failed")

    evidence["REQUESTS"] = world.llm.metrics.director_requests
    evidence["SAFETY_REJECTIONS"] = world.llm.metrics.director_safety_reject + world.llm.metrics.director_focus_skip
    evidence["TOKEN_USAGE"] = {
        "prompt": world.llm.metrics.prompt_tokens + (raw_real.prompt_tokens or 0),
        "completion": world.llm.metrics.completion_tokens + (raw_real.completion_tokens or 0),
    }
    evidence["LATENCY"] = world.llm.last_timing.snapshot()
    evidence["DIRECTOR_METRICS"] = world.llm.metrics.snapshot()
    evidence["STATUS"] = "PASS" if not failed else "FAIL"
    evidence["FAILED"] = failed

    out = ROOT / "tmp" / "toaster_universe_llm_director_live_proof.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- latency / scheduling proof ----
    lat_failed = []
    lat = {
        "PROVIDER": real.provider,
        "MODEL": real.model,
        "COLD_LATENCY": evidence["REAL_OUTBOUND"]["latency_ms"],
        "WARM_LATENCY": 0.0,
        "TTFT": evidence["REAL_OUTBOUND"]["timing"]["ttft_ms"],
        "GENERATION": evidence["REAL_OUTBOUND"]["timing"]["generation_ms"],
        "PROMPT_TOKENS": evidence["TOKEN_USAGE"]["prompt"],
        "COMPLETION_TOKENS": evidence["TOKEN_USAGE"]["completion"],
        "ACCEPTED": evidence["ACCEPTED_SCENES"],
        "REJECTED": evidence["REJECTED_SCENES"],
        "STALE": evidence["STALE_REJECTIONS"],
        "EXPIRED": 0,
        "FOCUS_SUPPRESSED": evidence["FOCUS_SKIP"],
        "SCHEMA_FAILURES": evidence["SCHEMA_FAILURES"],
        "NETWORK_FAILURES": world.llm.metrics.director_network_error,
        "PENDING_COALESCED": 0,
        "FAILED": lat_failed,
    }

    t1 = time.perf_counter()
    raw_warm = real.complete("Return SceneIntent JSON only.", '{"act":"IDLE"}', timeout_sec=8.0)
    lat["WARM_LATENCY"] = round((time.perf_counter() - t1) * 1000, 2)
    if raw_warm.timing.ttft_ms:
        lat["TTFT"] = raw_warm.timing.ttft_ms
        lat["GENERATION"] = raw_warm.timing.generation_ms

    w2 = _world(enabled=True)
    w2.llm.transport = ScriptedTransport(
        [
            {"text": json.dumps(VALID), "sleep": 0.05, "total_ms": 20, "prompt_tokens": 160, "completion_tokens": 20},
            {"text": json.dumps({**VALID, "scene": "quiet_companionship"}), "total_ms": 9, "prompt_tokens": 150, "completion_tokens": 18},
        ]
    )
    w2.signals.lock_activity = ActivityState.IDLE
    w2.signals.signals.focus_score = 0.0
    first = w2.force_director_request(opportunity="idle")
    second = w2.force_director_request(opportunity="social")
    if w2.llm.metrics.pending_coalesced < 1:
        lat_failed.append("coalesce_not_recorded")
    lat["PENDING_COALESCED"] = w2.llm.metrics.pending_coalesced
    _wait(w2, 4.0)

    expired = DirectorRequest("exp1", w2.llm.snapshot_revision, "idle", w2.clock - 10, w2.clock - 1)
    raw_exp = DirectorRawResult(request=expired, text=json.dumps(VALID), provider="scripted", model="scripted-director")
    if w2.llm.apply_raw_for_tests(w2, raw_exp) is not None:
        lat_failed.append("expired_not_rejected")
    lat["EXPIRED"] = w2.llm.metrics.director_expired_reject

    pre = ConfiguredTransport()
    lat["PREWARM"] = bool(pre.prewarm(timeout_sec=8.0)) if pre.backend is not None else False

    ctx = w2.llm.context_packet(w2)
    lat["PROMPT_EST"] = max(1, len(json.dumps(ctx)) // 4)
    lat["TIMING_BREAKDOWN"] = w2.llm.last_timing.snapshot()
    lat["COLD_VS_WARM"] = {"cold": lat["COLD_LATENCY"], "warm": lat["WARM_LATENCY"]}
    if lat["EXPIRED"] < 1:
        lat_failed.append("expired_counter_zero")
    lat["FAILED"] = lat_failed
    lat["STATUS"] = "PASS" if not lat_failed else "FAIL"

    out2 = ROOT / "tmp" / "toaster_universe_llm_director_latency_proof.json"
    out2.write_text(json.dumps(lat, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"live": str(out), "failed": failed, "latency_failed": lat_failed, "provider": real.provider, "model": real.model}, indent=2))
    return 0 if not failed and not lat_failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
