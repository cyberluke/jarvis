"""Runtime diagnostics. Replay-friendly, no secret content."""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any


NAMED_KINDS = (
    "scene_started",
    "scene_finished",
    "entity_spawned",
    "entity_despawned",
    "transition",
    "safety_override",
    "attention_retreat",
    "llm_director_call",
    "llm_director_reject",
    "comedy_gag_used",
    "gag_repetition_penalty",
    "rare_event",
    "population_change",
    "frame_time",
    "paint_time",
    "physics_time",
)


@dataclass
class WorldMetrics:
    ticks: int = 0
    frame_ms: float = 0.0
    paint_ms: float = 0.0
    physics_ms: float = 0.0
    entities: int = 0
    population: int = 0
    scene: str = ""
    activity: str = ""
    last_override: str = ""
    last_transition: str = ""
    last_rare_event: str = ""
    llm_calls: int = 0
    llm_rejects: int = 0
    safety_overrides: int = 0
    attention_retreats: int = 0
    novelty: float = 1.0
    dirty_count: int = 0
    dirty_pixels: float = 0.0
    dirty_ratio: float = 0.0
    dirty_full: bool = False
    world_id: str = "world-0"
    session_id: str = "session-0"
    last_spike_scene: str = ""
    last_spike_entities: int = 0
    counters: Counter[str] = field(default_factory=Counter)
    ring: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=400))

    def record(self, kind: str, t: float | None = None, **payload: Any) -> None:
        self.counters[kind] += 1
        row = {
            "kind": kind,
            "t": None if t is None else round(t, 4),
            "world_id": self.world_id,
            "session_id": self.session_id,
            **payload,
        }
        # Never persist spoken gag text as a high-cardinality label.
        row.pop("text", None)
        row.pop("line", None)
        row.pop("caption", None)
        self.ring.append(row)
        if kind == "transition":
            self.last_transition = f"{payload.get('entity')}:{payload.get('src')}->{payload.get('dst')}"
        elif kind == "safety_override":
            self.safety_overrides += 1
            self.last_override = str(payload.get("reason", ""))
        elif kind == "attention_retreat":
            self.attention_retreats += 1
            if not self.last_override:
                self.last_override = str(payload.get("reason", ""))
        elif kind == "rare_event":
            self.last_rare_event = str(payload.get("event", ""))
        elif kind == "llm_director_call":
            self.llm_calls += 1
        elif kind == "llm_director_reject":
            self.llm_rejects += 1
        elif kind == "scene_finished":
            pass
        elif kind == "frame_time":
            self.frame_ms = float(payload.get("ms", self.frame_ms))
        elif kind == "paint_time":
            self.paint_ms = float(payload.get("ms", self.paint_ms))
        elif kind == "physics_time":
            self.physics_ms = float(payload.get("ms", self.physics_ms))

    def note_spike(self, frame_ms: float, scene: str, entities: int) -> None:
        if frame_ms >= 12.0:
            self.last_spike_scene = scene
            self.last_spike_entities = entities
            self.record("perf_spike", ms=round(frame_ms, 3), scene=scene, entities=entities)

    def snapshot(self) -> dict[str, Any]:
        return {
            "ticks": self.ticks,
            "frame_ms": round(self.frame_ms, 3),
            "paint_ms": round(self.paint_ms, 3),
            "physics_ms": round(self.physics_ms, 3),
            "entities": self.entities,
            "population": self.population,
            "scene": self.scene,
            "activity": self.activity,
            "last_override": self.last_override,
            "last_transition": self.last_transition,
            "last_rare_event": self.last_rare_event,
            "llm_calls": self.llm_calls,
            "llm_rejects": self.llm_rejects,
            "safety_overrides": self.safety_overrides,
            "attention_retreats": self.attention_retreats,
            "novelty": round(self.novelty, 3),
            "dirty_count": self.dirty_count,
            "dirty_pixels": int(self.dirty_pixels),
            "dirty_ratio": round(self.dirty_ratio, 4),
            "dirty_full": self.dirty_full,
            "world_id": self.world_id,
            "session_id": self.session_id,
            "last_spike_scene": self.last_spike_scene,
            "last_spike_entities": self.last_spike_entities,
            "counters": dict(self.counters),
            "director": {
                "llm_calls": self.llm_calls,
                "llm_rejects": self.llm_rejects,
            },
        }
