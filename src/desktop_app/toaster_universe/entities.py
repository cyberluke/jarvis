"""Living desktop creatures. Vector-native, trait-driven, safety-subordinate."""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from .config import WorldConfig
from .types import EntityKind, Pose, Rect, ToastState, Vec2


TRAITS = (
    "temperature_need",
    "curiosity",
    "sociality",
    "boldness",
    "shyness",
    "smugness",
    "sleepiness",
    "dance_affinity",
    "work_respect",
    "chaos_affinity",
    "portal_curiosity",
    "food_drive",
    "copycat_drive",
    "risk_tolerance",
    "burn_tolerance",
    "attachment_to_main_toaster",
    "attachment_to_user",
    "crowd_tolerance",
    "novelty_seeking",
    "dramatic_tendency",
)


@dataclass
class Particle:
    pos: Vec2
    vel: Vec2
    life: float
    kind: str = "crumb"
    hue: float = 0.12


@dataclass
class Entity:
    id: str
    kind: EntityKind
    pos: Vec2
    vel: Vec2 = field(default_factory=Vec2)
    state: ToastState = ToastState.SPAWN
    previous_state: ToastState = ToastState.SPAWN
    state_t: float = 0.0
    age: float = 0.0
    temperature: float = 0.55
    browning: float = 0.18
    burnt: bool = False
    hidden: bool = False
    radius: float = 16.0
    facing: float = 1.0
    hop_phase: float = 0.0
    target: Vec2 = field(default_factory=Vec2)
    slot_index: int | None = None
    carrying: str | None = None
    opacity: float = 0.0
    pose: Pose = field(default_factory=Pose)
    traits: dict[str, float] = field(default_factory=dict)
    last_override: str = ""
    speech: str = ""
    speech_t: float = 0.0
    partner_id: str | None = None
    appliance: str | None = None
    seed: int = 0
    jump_from: Vec2 | None = None
    proposed: ToastState | None = None
    monitor_index: int = 0
    prev_aabb: Rect | None = None
    recovery_stack: list = field(default_factory=list)
    edge_uses: dict = field(default_factory=dict)
    local_intent: str = ""
    safety_offset: Vec2 = field(default_factory=Vec2)
    safety_yield_visual: bool = False
    last_event_id: int = 0
    browning_class: str = "fresh"
    experience: dict = field(default_factory=dict)

    def trait(self, name: str) -> float:
        return self.traits.get(name, 0.5)

    def learn(self, name: str, delta: float, lo: float = 0.05, hi: float = 0.98) -> None:
        cur = self.traits.get(name, 0.5)
        self.traits[name] = max(lo, min(hi, cur + delta))
        self.experience[name] = self.experience.get(name, 0) + 1

    def classify_browning(self) -> str:
        if self.browning >= 0.85 or self.burnt:
            self.browning_class = "burnt"
        elif self.browning >= 0.60:
            self.browning_class = "crispy"
        elif self.browning >= 0.25:
            self.browning_class = "golden"
        else:
            self.browning_class = "fresh"
        return self.browning_class

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "state": self.state.value,
            "pos": (round(self.pos.x, 1), round(self.pos.y, 1)),
            "browning": round(self.browning, 3),
            "temperature": round(self.temperature, 3),
            "burnt": self.burnt,
            "hidden": self.hidden,
            "override": self.last_override,
        }


def roll_traits(rng: random.Random) -> dict[str, float]:
    return {name: rng.uniform(0.18, 0.92) for name in TRAITS}


def spawn_entity(
    kind: EntityKind,
    pos: Vec2,
    rng: random.Random,
    entity_id: str | None = None,
    cfg: WorldConfig | None = None,
) -> Entity:
    seed = rng.randint(1, 10_000_000)
    radius = {
        EntityKind.MINI_TOAST: 16.0,
        EntityKind.BURNT_TOAST: 16.5,
        EntityKind.CRUMB: 4.5,
        EntityKind.BUTTER_BLOB: 11.0,
        EntityKind.POPCORN_KERNEL: 8.5,
        EntityKind.RICE_SPIRIT: 13.0,
        EntityKind.PORTAL_ECHO: 12.0,
        EntityKind.APPLIANCE: 28.0,
    }[kind]
    return Entity(
        id=entity_id or f"{kind.value}-{seed}",
        kind=kind,
        pos=pos,
        target=pos,
        radius=radius,
        traits=roll_traits(rng),
        seed=seed,
        state=ToastState.SPAWN,
        browning=0.72 if kind is EntityKind.BURNT_TOAST else rng.uniform(0.08, 0.28),
        burnt=kind is EntityKind.BURNT_TOAST,
        temperature=0.2 if kind is EntityKind.BURNT_TOAST else 0.55,
    )
