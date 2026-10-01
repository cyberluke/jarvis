"""Hierarchical character graph. Safety interrupts at any time."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from .config import WorldConfig
from .entities import Entity
from .events import EventBus
from .physics import (
    confine,
    crowd_push,
    cursor_repulsion,
    damping,
    hop_offset,
    integrate as phys_integrate,
    jump_arc,
    popcorn_ballistic,
    soft_body,
    wander_target,
)
from .safety import SafetyArbiter, SafetyField
from .signals import UserSignals
from . import social
from .state_machine import (
    EVENT_EDGES,
    LIFECYCLE_OVERRIDE,
    OVERRIDE_REASONS,
    RITUAL_STATES,
    SAFETY_OVERRIDE_STATES,
    STATE_INTENT,
    Transition,
    clear_locks,
    derive_entry,
    elapsed,
    layer_for_reason,
    note_edge,
    penalty_for,
    pop_recovery,
    push_recovery,
    timeout_for,
)
from .types import (
    ActivityState,
    BehaviorLayer,
    EntityKind,
    HIDDEN_STATES,
    OverrideReason,
    Rect,
    SAFETY_STATES,
    SlotAnchor,
    ToastState,
    Vec2,
    WorldEvent,
)


CANONICAL_EDGES: dict[ToastState, tuple[ToastState, ...]] = {
    ToastState.SPAWN: (ToastState.IDLE, ToastState.WANDER, ToastState.APPROACH_TOASTER, ToastState.OBSERVE_TOASTER),
    ToastState.IDLE: (
        ToastState.WANDER,
        ToastState.OBSERVE_USER,
        ToastState.OBSERVE_TOASTER,
        ToastState.SLEEP,
        ToastState.SOCIALIZE,
        ToastState.APPROACH_TOASTER,
        ToastState.WORK_QUIET_MODE,
        ToastState.FOLLOW_CURSOR_AT_DISTANCE,
    ),
    ToastState.WANDER: (
        ToastState.IDLE,
        ToastState.APPROACH_TOASTER,
        ToastState.SOCIALIZE,
        ToastState.OBSERVE_TOASTER,
        ToastState.FOLLOW_CURSOR_AT_DISTANCE,
        ToastState.SLEEP,
        ToastState.OBSERVE_USER,
    ),
    ToastState.OBSERVE_USER: (ToastState.IDLE, ToastState.WANDER, ToastState.FOLLOW_CURSOR_AT_DISTANCE, ToastState.HIDE),
    ToastState.OBSERVE_TOASTER: (ToastState.APPROACH_TOASTER, ToastState.IDLE, ToastState.WANDER),
    ToastState.FOLLOW_CURSOR_AT_DISTANCE: (ToastState.WANDER, ToastState.IDLE, ToastState.OBSERVE_USER),
    ToastState.AVOID_CURSOR: (ToastState.PANIC_RUN, ToastState.HIDE, ToastState.WANDER, ToastState.IDLE),
    ToastState.AVOID_READING_ZONE: (ToastState.WORK_QUIET_MODE, ToastState.HIDE, ToastState.WANDER),
    ToastState.MANUAL_DRAG: (ToastState.WANDER, ToastState.IDLE, ToastState.AVOID_CURSOR),
    ToastState.APPROACH_TOASTER: (ToastState.QUEUE_FOR_SLOT, ToastState.OBSERVE_TOASTER, ToastState.WANDER),
    ToastState.QUEUE_FOR_SLOT: (ToastState.JUMP_INTO_SLOT, ToastState.WANDER, ToastState.SOCIALIZE),
    ToastState.JUMP_INTO_SLOT: (ToastState.TOASTING,),
    ToastState.TOASTING: (ToastState.POP_OUT, ToastState.OVERTOASTING),
    ToastState.OVERTOASTING: (ToastState.POP_OUT, ToastState.BURNT),
    ToastState.POP_OUT: (ToastState.COOL_DOWN, ToastState.BURNT, ToastState.WANDER),
    ToastState.COOL_DOWN: (ToastState.WANDER, ToastState.IDLE, ToastState.SOCIALIZE),
    ToastState.PANIC_RUN: (ToastState.HIDE, ToastState.WANDER, ToastState.IDLE),
    ToastState.SLEEP: (ToastState.WAKE, ToastState.IDLE),
    ToastState.WAKE: (ToastState.IDLE, ToastState.WANDER, ToastState.OBSERVE_USER),
    ToastState.SOCIALIZE: (ToastState.DANCE, ToastState.FIGHT_PLAYFULLY, ToastState.IDLE, ToastState.WANDER, ToastState.COPY_DANCE),
    ToastState.DANCE: (ToastState.COPY_DANCE, ToastState.SOCIALIZE, ToastState.IDLE, ToastState.HEART_MODE),
    ToastState.COPY_DANCE: (ToastState.DANCE, ToastState.SOCIALIZE, ToastState.IDLE),
    ToastState.FIGHT_PLAYFULLY: (ToastState.SOCIALIZE, ToastState.WANDER, ToastState.IDLE),
    ToastState.HIDE: (ToastState.PEEK, ToastState.WANDER, ToastState.IDLE),
    ToastState.PEEK: (ToastState.WANDER, ToastState.IDLE, ToastState.HIDE),
    ToastState.MULTIPLY: (ToastState.WANDER, ToastState.IDLE),
    ToastState.CARRY_CRUMB: (ToastState.EAT_CRUMB, ToastState.WANDER),
    ToastState.EAT_CRUMB: (ToastState.IDLE, ToastState.WANDER),
    ToastState.WATCH_VIDEO: (ToastState.POPCORN_AUDIENCE, ToastState.DOOMSCROLL_COMMENTARY, ToastState.IDLE),
    ToastState.POPCORN_AUDIENCE: (ToastState.WATCH_VIDEO, ToastState.IDLE, ToastState.HEART_MODE, ToastState.DOOMSCROLL_COMMENTARY),
    ToastState.WORK_QUIET_MODE: (ToastState.IDLE, ToastState.WANDER, ToastState.HIDE),
    ToastState.TERMINAL_AWE: (ToastState.OBSERVE_USER, ToastState.IDLE, ToastState.WANDER),
    ToastState.DOOMSCROLL_COMMENTARY: (ToastState.WATCH_VIDEO, ToastState.POPCORN_AUDIENCE, ToastState.IDLE),
    ToastState.HEART_MODE: (ToastState.DANCE, ToastState.IDLE, ToastState.SOCIALIZE),
    ToastState.PORTAL_CURIOUS: (ToastState.PORTAL_ENTER, ToastState.WANDER, ToastState.IDLE),
    ToastState.PORTAL_ENTER: (ToastState.INSIDE_APPLIANCE, ToastState.PORTAL_RETURN),
    ToastState.PORTAL_RETURN: (ToastState.WANDER, ToastState.IDLE, ToastState.OBSERVE_TOASTER),
    ToastState.BURNT: (ToastState.RECOVER, ToastState.RITUAL_FUNERAL, ToastState.COOL_DOWN),
    ToastState.RECOVER: (ToastState.WANDER, ToastState.IDLE, ToastState.SOCIALIZE),
    ToastState.RITUAL_FUNERAL: (ToastState.RESPAWN, ToastState.DESPAWN),
    ToastState.RESPAWN: (ToastState.SPAWN, ToastState.WANDER),
    ToastState.DESPAWN: (),
    ToastState.INSIDE_APPLIANCE: (ToastState.PORTAL_RETURN, ToastState.POP_OUT, ToastState.WANDER),
}


@dataclass
class LayerDecision:
    layer: BehaviorLayer
    state: ToastState | None = None
    veto: bool = False
    reason: str = ""
    override: OverrideReason = OverrideReason.NONE


class BehaviorEngine:
    def __init__(self, cfg: WorldConfig, bus: EventBus, safety: SafetyArbiter, rng: random.Random) -> None:
        self.cfg = cfg
        self.bus = bus
        self.safety = safety
        self.rng = rng
        self.pending_spawns: list[tuple[EntityKind, Vec2]] = []
        self.last_reject: Transition | None = None
        self.toaster_pos: Vec2 = Vec2()

    def allowed(self, src: ToastState, dst: ToastState) -> bool:
        if src is dst:
            return True
        return dst in CANONICAL_EDGES.get(src, ())

    def enter(
        self,
        entity: Entity,
        state: ToastState,
        t: float,
        reason: str,
        *,
        trigger: str = "",
        event_id: int = 0,
        force: bool = False,
    ) -> Transition | None:
        if entity.state is state:
            return None
        override = ""
        guard_ok = True
        rejected = ""
        if reason in OVERRIDE_REASONS or force:
            override = reason
        if entity.state is ToastState.BURNT and state in {ToastState.COPY_DANCE, ToastState.DANCE, ToastState.SOCIALIZE} and not force:
            guard_ok = False
            rejected = "invalid_edge"
        elif reason in {"scene", "director", "comedy", "social", "event", "activity"} and state in SAFETY_OVERRIDE_STATES:
            override = "safety"
        elif not self.allowed(entity.state, state):
            if reason in {"scene", "director"} and state not in RITUAL_STATES:
                # Directors may propose off-graph entertainment states, but never ritual/safety locks.
                if state in LIFECYCLE_OVERRIDE or state in {
                    ToastState.WATCH_VIDEO,
                    ToastState.POPCORN_AUDIENCE,
                    ToastState.HEART_MODE,
                    ToastState.SLEEP,
                    ToastState.DANCE,
                    ToastState.COPY_DANCE,
                    ToastState.MULTIPLY,
                    ToastState.PORTAL_CURIOUS,
                    ToastState.PORTAL_ENTER,
                    ToastState.PORTAL_RETURN,
                    ToastState.INSIDE_APPLIANCE,
                    ToastState.TERMINAL_AWE,
                    ToastState.DOOMSCROLL_COMMENTARY,
                    ToastState.RECOVER,
                    ToastState.RITUAL_FUNERAL,
                    ToastState.BURNT,
                }:
                    override = reason
                else:
                    guard_ok = False
                    rejected = "invalid_edge"
            elif reason == "lifecycle" and state in LIFECYCLE_OVERRIDE:
                override = "lifecycle"
            else:
                guard_ok = False
                rejected = "invalid_edge"
        if not guard_ok and not override:
            trans = Transition(
                entity.id,
                entity.state,
                state,
                reason,
                t,
                trigger=trigger,
                event_id=event_id,
                guard_ok=False,
                rejected=rejected,
                layer=layer_for_reason(reason),
            )
            self.last_reject = trans
            return None
        if penalty_for(entity, entity.state, state) > 0.72 and reason in {"graph", "social", "event"} and not override:
            trans = Transition(
                entity.id,
                entity.state,
                state,
                reason,
                t,
                trigger=trigger,
                event_id=event_id,
                guard_ok=False,
                rejected="repetition_penalty",
                layer=layer_for_reason(reason),
            )
            self.last_reject = trans
            return None
        src = entity.state
        push_recovery(entity, src)
        clear_locks(entity, src)
        entity.previous_state = src
        entity.state = state
        entity.state_t = t
        entity.hidden = state in HIDDEN_STATES
        derive_entry(entity, state, self.toaster_pos)
        note_edge(entity, src, state)
        if state is ToastState.JUMP_INTO_SLOT:
            entity.jump_from = entity.pos
            self.bus.emit(WorldEvent.SLOT_OCCUPIED, t, entity_id=entity.id)
        if state is ToastState.POP_OUT:
            entity.burnt = entity.browning > self.cfg.burn_threshold or src is ToastState.OVERTOASTING
            self.bus.emit(WorldEvent.TOAST_POP, t, entity_id=entity.id)
            if entity.burnt:
                self.bus.emit(WorldEvent.TOAST_BURNT, t, entity_id=entity.id)
            self.bus.emit(WorldEvent.SLOT_FREE, t, entity_id=entity.id)
        if state is ToastState.MULTIPLY:
            self.pending_spawns.append(
                (EntityKind.MINI_TOAST, entity.pos + Vec2(self.rng.uniform(-24, 24), 8))
            )
        if state is ToastState.PORTAL_ENTER:
            self.bus.emit(WorldEvent.PORTAL_OPEN, t, entity_id=entity.id, payload={"appliance": entity.appliance or ""})
        if src is ToastState.INSIDE_APPLIANCE and state is ToastState.PORTAL_RETURN:
            self.bus.emit(WorldEvent.PORTAL_CLOSE, t, entity_id=entity.id)
        if state is ToastState.DESPAWN:
            entity.hidden = True
        return Transition(
            entity.id,
            src,
            state,
            reason,
            t,
            trigger=trigger or STATE_INTENT.get(state, ""),
            event_id=event_id,
            guard_ok=True,
            override_reason=override,
            layer=layer_for_reason(reason),
        )

    def restore(self, entity: Entity, state: ToastState, t: float) -> Transition | None:
        return self.enter(entity, state, t, "restore", force=True)

    def propose_from_event(self, entity: Entity, name: WorldEvent) -> ToastState | None:
        dests = EVENT_EDGES.get(name)
        if not dests:
            return None
        for dest in dests:
            if self.allowed(entity.state, dest) or dest in SAFETY_OVERRIDE_STATES:
                return dest
        return None

    def choose(self, entity: Entity, t: float, signals: UserSignals, slots: list[SlotAnchor], friends: list[Entity]) -> ToastState | None:
        dwell = elapsed(entity, t)
        toasting_timeout = timeout_for(ToastState.TOASTING, self.cfg.toasting_sec)
        overtoast_timeout = timeout_for(ToastState.OVERTOASTING, None, self.cfg.overtoast_sec)
        cool_timeout = timeout_for(ToastState.COOL_DOWN, None, None, self.cfg.cool_down_sec)
        if entity.state in SAFETY_STATES:
            # Stay in the safety state while the yield field is still live.
            # Recovery happens only after apply_safety sees must_yield=False.
            if dwell < timeout_for(entity.state):
                return None
            if entity.last_override:
                return None
            recovered = pop_recovery(entity)
            if recovered is not None and recovered not in SAFETY_STATES:
                return recovered
            if entity.state is ToastState.AVOID_READING_ZONE:
                return ToastState.WORK_QUIET_MODE
            if entity.state is ToastState.PANIC_RUN:
                return ToastState.HIDE if entity.trait("shyness") > 0.5 else ToastState.WANDER
            if entity.state is ToastState.HIDE:
                return ToastState.PEEK
            if entity.state is ToastState.WORK_QUIET_MODE:
                return ToastState.IDLE
            return ToastState.WANDER
        if entity.state is ToastState.SPAWN and dwell > timeout_for(ToastState.SPAWN):
            if entity.temperature < 0.28 + 0.25 * entity.trait("temperature_need"):
                return ToastState.APPROACH_TOASTER
            return ToastState.WANDER
        temp_need = entity.trait("temperature_need")
        if entity.temperature < 0.28 + 0.25 * temp_need and entity.state not in {
            ToastState.APPROACH_TOASTER,
            ToastState.QUEUE_FOR_SLOT,
            ToastState.JUMP_INTO_SLOT,
            ToastState.TOASTING,
            ToastState.OVERTOASTING,
            ToastState.PORTAL_CURIOUS,
            ToastState.PORTAL_ENTER,
            ToastState.PORTAL_RETURN,
            ToastState.INSIDE_APPLIANCE,
            *SAFETY_STATES,
        }:
            return ToastState.APPROACH_TOASTER
        if signals.activity is ActivityState.CODING_FLOW and entity.trait("work_respect") > 0.55:
            if entity.state not in {
                ToastState.WORK_QUIET_MODE,
                ToastState.SLEEP,
                ToastState.HIDE,
                *RITUAL_STATES,
                ToastState.APPROACH_TOASTER,
                ToastState.QUEUE_FOR_SLOT,
            }:
                return ToastState.WORK_QUIET_MODE
        if signals.activity is ActivityState.TERMINAL_HEAVY and entity.trait("curiosity") > 0.55:
            return ToastState.TERMINAL_AWE
        if signals.activity is ActivityState.DOOMSCROLLING:
            if entity.trait("novelty_seeking") > 0.55:
                return ToastState.DOOMSCROLL_COMMENTARY
            return ToastState.WATCH_VIDEO if entity.trait("novelty_seeking") > 0.4 else ToastState.POPCORN_AUDIENCE
        if signals.activity is ActivityState.LATE_NIGHT and entity.trait("sleepiness") > 0.35:
            return ToastState.SLEEP
        if entity.state is ToastState.IDLE and dwell > self.cfg.sleep_after_idle_sec:
            return ToastState.SLEEP
        if entity.state in {ToastState.IDLE, ToastState.WANDER}:
            if entity.trait("attachment_to_main_toaster") > 0.84:
                return ToastState.OBSERVE_TOASTER
            if entity.trait("attachment_to_user") > 0.86:
                return ToastState.OBSERVE_USER
            if entity.trait("portal_curiosity") > 0.88:
                return ToastState.PORTAL_CURIOUS
            if entity.trait("chaos_affinity") > 0.9:
                return ToastState.DANCE
            if entity.trait("smugness") > 0.88:
                return ToastState.DOOMSCROLL_COMMENTARY
            if entity.trait("risk_tolerance") > 0.9 and entity.temperature < 0.4:
                return ToastState.APPROACH_TOASTER
        social_state = social.propose(entity, friends, self.cfg.social_radius)
        if social_state is None and entity.state in {ToastState.IDLE, ToastState.WANDER}:
            social_state = social.propose_weighted(entity, friends, self.cfg.social_radius)
        if social_state is not None and entity.state in {ToastState.IDLE, ToastState.WANDER, ToastState.OBSERVE_TOASTER}:
            return social_state
        dancers = [o for o in friends if o.state in {ToastState.DANCE, ToastState.HEART_MODE}]
        if dancers and entity.trait("copycat_drive") > 0.48 and entity.state not in {
            ToastState.DANCE,
            ToastState.COPY_DANCE,
            ToastState.TOASTING,
            ToastState.OVERTOASTING,
            ToastState.JUMP_INTO_SLOT,
            ToastState.QUEUE_FOR_SLOT,
            ToastState.APPROACH_TOASTER,
            *SAFETY_STATES,
            ToastState.PORTAL_CURIOUS,
            ToastState.PORTAL_ENTER,
            ToastState.PORTAL_RETURN,
            ToastState.INSIDE_APPLIANCE,
            ToastState.CARRY_CRUMB,
            ToastState.EAT_CRUMB,
            ToastState.POP_OUT,
            ToastState.BURNT,
            ToastState.RECOVER,
            ToastState.COOL_DOWN,
            ToastState.RITUAL_FUNERAL,
        }:
            entity.partner_id = dancers[0].id
            return ToastState.COPY_DANCE
        crumbs = [o for o in friends if o.kind is EntityKind.CRUMB and not o.hidden]
        if crumbs and entity.trait("food_drive") > 0.62 and entity.state in {ToastState.IDLE, ToastState.WANDER, ToastState.OBSERVE_TOASTER}:
            entity.carrying = crumbs[0].id
            return ToastState.CARRY_CRUMB
        if entity.state is ToastState.SPAWN and dwell > timeout_for(ToastState.SPAWN):
            return ToastState.WANDER
        if entity.state is ToastState.IDLE and dwell > 1.6 + entity.trait("sleepiness"):
            weights = {
                ToastState.OBSERVE_USER: 0.18 + 0.35 * entity.trait("attachment_to_user") + 0.12 * entity.trait("smugness"),
                ToastState.OBSERVE_TOASTER: 0.18 + 0.35 * entity.trait("attachment_to_main_toaster"),
                ToastState.WANDER: 0.22 + 0.2 * entity.trait("boldness"),
                ToastState.PORTAL_CURIOUS: 0.05 + 0.25 * entity.trait("portal_curiosity"),
                ToastState.DANCE: 0.04 + 0.22 * entity.trait("chaos_affinity"),
            }
            return max(weights, key=weights.get)
        if entity.state is ToastState.WANDER and dwell > 4.5:
            weights = {
                ToastState.SOCIALIZE: entity.trait("sociality") * 0.45 if friends else 0.0,
                ToastState.FOLLOW_CURSOR_AT_DISTANCE: 0.18 + 0.2 * entity.trait("attachment_to_user"),
                ToastState.OBSERVE_USER: 0.12 + 0.18 * entity.trait("curiosity"),
                ToastState.OBSERVE_TOASTER: 0.12 + 0.2 * entity.trait("attachment_to_main_toaster"),
                ToastState.IDLE: 0.18 + 0.15 * entity.trait("sleepiness"),
                ToastState.APPROACH_TOASTER: 0.08 + 0.2 * entity.trait("risk_tolerance") if entity.temperature < 0.45 else 0.0,
            }
            return max(weights, key=weights.get)
        if entity.state is ToastState.OBSERVE_USER and dwell > timeout_for(ToastState.OBSERVE_USER):
            return ToastState.FOLLOW_CURSOR_AT_DISTANCE if self.rng.random() < 0.4 else ToastState.IDLE
        if entity.state is ToastState.OBSERVE_TOASTER and dwell > timeout_for(ToastState.OBSERVE_TOASTER):
            return ToastState.APPROACH_TOASTER if entity.temperature < 0.45 else ToastState.WANDER
        if entity.state is ToastState.APPROACH_TOASTER:
            free = next((s for s in slots if s.occupied_by in (None, entity.id)), None)
            if free and (entity.pos - free.pos).length() < self.cfg.toaster_approach_radius:
                return ToastState.QUEUE_FOR_SLOT
        if entity.state is ToastState.QUEUE_FOR_SLOT:
            free = next((s for s in slots if s.occupied_by in (None, entity.id)), None)
            if free and (entity.pos - free.pos).length() < 22:
                return ToastState.JUMP_INTO_SLOT
            if dwell > 6.0:
                return ToastState.WANDER
        if entity.state is ToastState.JUMP_INTO_SLOT and dwell > 0.42:
            return ToastState.TOASTING
        if entity.state is ToastState.TOASTING:
            if entity.browning > max(self.cfg.burn_threshold, entity.trait("burn_tolerance") * 0.85 + 0.35):
                return ToastState.OVERTOASTING
            if dwell > toasting_timeout:
                return ToastState.POP_OUT
        if entity.state is ToastState.OVERTOASTING and dwell > overtoast_timeout:
            return ToastState.POP_OUT
        if entity.state is ToastState.POP_OUT and dwell > 0.55:
            return ToastState.BURNT if entity.burnt else ToastState.COOL_DOWN
        if entity.state is ToastState.COOL_DOWN and dwell > cool_timeout:
            return ToastState.WANDER
        if entity.state is ToastState.BURNT and dwell > 2.4:
            return ToastState.RITUAL_FUNERAL if entity.trait("dramatic_tendency") > 0.55 or entity.browning > 0.8 else ToastState.RECOVER
        if entity.state is ToastState.RECOVER and dwell > 2.8:
            return ToastState.WANDER
        if entity.state is ToastState.RITUAL_FUNERAL and dwell > 3.5:
            return ToastState.RESPAWN
        if entity.state is ToastState.RESPAWN and dwell > 0.4:
            return ToastState.SPAWN
        if entity.state is ToastState.SLEEP and (signals.activity not in {ActivityState.LATE_NIGHT, ActivityState.IDLE} or dwell > 22):
            return ToastState.WAKE
        if entity.state is ToastState.WAKE and dwell > 0.6:
            return ToastState.IDLE
        if entity.state is ToastState.SOCIALIZE and dwell > 3.2:
            if entity.trait("dance_affinity") > 0.6:
                return ToastState.DANCE
            if entity.trait("boldness") > 0.7:
                return ToastState.FIGHT_PLAYFULLY
            return ToastState.WANDER
        if entity.state is ToastState.DANCE and dwell > 4.0:
            return ToastState.IDLE
        if entity.state is ToastState.COPY_DANCE and dwell > 3.2:
            return ToastState.SOCIALIZE
        if entity.state is ToastState.FIGHT_PLAYFULLY and dwell > 2.2:
            return ToastState.SOCIALIZE
        if entity.state is ToastState.PEEK and dwell > 1.2:
            return ToastState.WANDER
        if entity.state is ToastState.WORK_QUIET_MODE and signals.activity not in {
            ActivityState.CODING_FLOW,
            ActivityState.FOCUSED,
            ActivityState.TERMINAL_HEAVY,
            ActivityState.FRANTIC,
        }:
            return ToastState.IDLE
        if entity.state is ToastState.WATCH_VIDEO and dwell > 8.0:
            return ToastState.IDLE
        if entity.state is ToastState.POPCORN_AUDIENCE and dwell > 7.0:
            return ToastState.IDLE
        if entity.state is ToastState.DOOMSCROLL_COMMENTARY and dwell > 6.0:
            return ToastState.WATCH_VIDEO
        if entity.state is ToastState.HEART_MODE and dwell > 5.0:
            return ToastState.IDLE
        if entity.state is ToastState.TERMINAL_AWE and dwell > 5.5:
            return ToastState.WANDER
        if entity.state is ToastState.PORTAL_CURIOUS and dwell > 0.45:
            return ToastState.PORTAL_ENTER
        if entity.state is ToastState.PORTAL_ENTER and dwell > 0.8:
            return ToastState.INSIDE_APPLIANCE
        if entity.state is ToastState.INSIDE_APPLIANCE and dwell > 4.5:
            return ToastState.PORTAL_RETURN
        if entity.state is ToastState.PORTAL_RETURN and dwell > 0.7:
            return ToastState.WANDER
        if entity.state is ToastState.CARRY_CRUMB and dwell > 2.5:
            return ToastState.EAT_CRUMB
        if entity.state is ToastState.EAT_CRUMB and dwell > 1.1:
            return ToastState.IDLE
        if entity.state is ToastState.FOLLOW_CURSOR_AT_DISTANCE and dwell > 3.5:
            return ToastState.WANDER
        if entity.state is ToastState.MULTIPLY and dwell > 0.5:
            return ToastState.WANDER
        return None

    def apply_safety(self, entity: Entity, field: SafetyField, t: float) -> Transition | None:
        visual_only = entity.state in RITUAL_STATES
        if entity.state is ToastState.DESPAWN:
            return None
        if not field.must_yield:
            entity.last_override = ""
            return None
        if visual_only:
            # Ritual lock preserves semantic occupancy; visual/input must still yield.
            entity.safety_yield_visual = True
            entity.last_override = field.reason.value
            return None
        if field.zone and field.zone.value == "PANIC_RADIUS":
            dest = ToastState.PANIC_RUN if entity.trait("shyness") < 0.7 else ToastState.HIDE
            return self.enter(entity, dest, t, "safety", trigger=field.reason.value)
        if field.reason is OverrideReason.READING_ZONE:
            return self.enter(entity, ToastState.AVOID_READING_ZONE, t, "safety", trigger=field.reason.value)
        if field.reason in {OverrideReason.DRAG_ACTIVE, OverrideReason.SELECTION_ACTIVE, OverrideReason.SCROLL_ACTIVE}:
            return self.enter(entity, ToastState.HIDE, t, "safety", trigger=field.reason.value)
        return self.enter(entity, ToastState.AVOID_CURSOR, t, "safety", trigger=field.reason.value)

    def integrate(
        self,
        entity: Entity,
        dt: float,
        t: float,
        signals: UserSignals,
        desktop: Rect,
        slots: list[SlotAnchor],
        others: list[Entity],
        toaster: Vec2,
    ) -> None:
        entity.age += dt
        if entity.kind is EntityKind.BUTTER_BLOB:
            forced = getattr(entity, "force_warp", None)
            target = forced if forced is not None else 0.35 * math.sin(t * 5.2 + entity.seed)
            warp, wvel = soft_body(entity.pose.body_warp, target, getattr(entity, "_warp_vel", 0.0), dt)
            entity._warp_vel = wvel
            entity.pose.body_warp = warp
            entity.pose.rotation = math.sin(t * 3.4 + entity.seed) * 10.0
            entity.pose.translation_x = math.sin(t * 2.6 + entity.seed) * 6.0
            entity.vel = Vec2(entity.vel.x * 0.92, entity.vel.y * 0.88)
        if entity.state not in {ToastState.TOASTING, ToastState.OVERTOASTING, ToastState.JUMP_INTO_SLOT}:
            entity.temperature = max(0.0, entity.temperature - self.cfg.temperature_loss_per_sec * dt * (0.6 + entity.trait("temperature_need")))
        speed = self.cfg.wander_speed * (0.75 + 0.5 * entity.trait("boldness"))
        desired = Vec2()
        if entity.kind is EntityKind.CRUMB:
            if entity.local_intent == "crumb_king":
                entity.hop_phase = (entity.hop_phase + dt * 4.0) % 1.0
            elif entity.state in {ToastState.IDLE, ToastState.SPAWN, ToastState.WANDER}:
                host = next((o for o in others if o.kind in {EntityKind.MINI_TOAST, EntityKind.BURNT_TOAST} and not o.hidden), None)
                if host is not None:
                    desired = (host.pos + Vec2(-10, 8) - entity.pos).normalized() * speed * 0.7
                    entity.hop_phase = (entity.hop_phase + dt * 5.0) % 1.0
        if entity.kind is EntityKind.RICE_SPIRIT:
            entity.hop_phase = (entity.hop_phase + dt * 0.8) % 1.0
            entity.vel = entity.vel * 0.7
            if entity.state in {ToastState.IDLE, ToastState.SPAWN, ToastState.WANDER}:
                desired = Vec2(math.sin(t * 0.7 + entity.seed) * 8.0, -6.0)
        if entity.kind is EntityKind.PORTAL_ECHO:
            entity.pose.glitch = 0.4 + 0.3 * abs(math.sin(t * 6.0 + entity.seed))
            if entity.state in {ToastState.IDLE, ToastState.SPAWN}:
                desired = Vec2(math.sin(t * 1.4) * 12.0, math.cos(t * 0.9) * 8.0)
        if entity.kind is EntityKind.POPCORN_KERNEL:
            entity.pose.hop = abs(math.sin(t * 14.0 + entity.seed)) * 6.0
            entity.pose.rotation = math.sin(t * 18.0 + entity.seed) * 18.0
        if entity.state is ToastState.WANDER and entity.kind in {EntityKind.MINI_TOAST, EntityKind.BURNT_TOAST}:
            if (entity.pos - entity.target).length() < 18 or entity.target.length() < 1:
                patches = [p.rect for p in getattr(self.safety.field, "patches", ())]
                entity.target = wander_target(entity.pos, desktop, self.rng, forbidden=patches)
            desired = (entity.target - entity.pos).normalized() * speed
            entity.hop_phase = (entity.hop_phase + dt * 3.2) % 1.0
        elif entity.state is ToastState.FOLLOW_CURSOR_AT_DISTANCE:
            orbit = signals.cursor + Vec2(0, 120)
            desired = (orbit - entity.pos).normalized() * speed * 0.8
            entity.hop_phase = (entity.hop_phase + dt * 2.6) % 1.0
        elif entity.state is ToastState.OBSERVE_USER:
            desired = ((signals.cursor + Vec2(0, 160)) - entity.pos).normalized() * speed * 0.35
        elif entity.state in {ToastState.APPROACH_TOASTER, ToastState.QUEUE_FOR_SLOT, ToastState.OBSERVE_TOASTER}:
            free = next((s for s in slots if s.occupied_by in (None, entity.id)), None)
            goal = free.pos if free else toaster
            entity.target = goal
            desired = (goal - entity.pos).normalized() * (self.cfg.hop_speed if entity.state is ToastState.APPROACH_TOASTER else speed)
            entity.hop_phase = (entity.hop_phase + dt * 3.8) % 1.0
        elif entity.state is ToastState.JUMP_INTO_SLOT:
            free = next((s for s in slots if s.occupied_by in (None, entity.id)), slots[0] if slots else None)
            if free:
                free.occupied_by = entity.id
                entity.slot_index = free.index
                origin = entity.jump_from or entity.pos
                p = min(1.0, (t - entity.state_t) / 0.42)
                entity.pos = jump_arc(origin, free.pos, p, 46.0)
                entity.vel = Vec2()
                return
        elif entity.state in {ToastState.TOASTING, ToastState.OVERTOASTING}:
            entity.hidden = not getattr(entity, "safety_yield_visual", False)
            entity.temperature = min(1.0, entity.temperature + self.cfg.temperature_gain_in_slot * dt)
            entity.browning = min(
                1.0,
                entity.browning
                + dt * (self.cfg.toasting_browning_rate if entity.state is ToastState.TOASTING else self.cfg.overtoast_browning_rate),
            )
            entity.classify_browning()
            if entity.browning > self.cfg.burn_threshold:
                entity.burnt = True
                entity.learn("burn_tolerance", 0.004)
                entity.learn("dramatic_tendency", 0.003)
            if getattr(entity, "safety_yield_visual", False):
                away = entity.pos - signals.cursor_predicted
                if away.length() < 1:
                    away = Vec2(0, -1)
                entity.safety_offset = away.normalized() * 48.0
                entity.opacity = min(entity.opacity, 0.15)
            return
        elif entity.state is ToastState.POP_OUT:
            entity.hidden = False
            if entity.slot_index is not None and entity.slot_index < len(slots):
                slots[entity.slot_index].occupied_by = None
                entity.slot_index = None
            burst = Vec2(self.rng.uniform(-80, 80), -160)
            entity.vel = burst
            entity.pos = entity.pos + Vec2(0, -hop_offset(min(1.0, (t - entity.state_t) / 0.5), 50))
        elif entity.state is ToastState.CARRY_CRUMB:
            target = next((o for o in others if o.id == entity.carrying), None)
            if target is not None:
                desired = (target.pos - entity.pos).normalized() * speed
                if (target.pos - entity.pos).length() < 18:
                    target.hidden = True
                    self.enter(target, ToastState.DESPAWN, t, "lifecycle")
            entity.hop_phase = (entity.hop_phase + dt * 3.0) % 1.0
        elif entity.state in {ToastState.PANIC_RUN, ToastState.AVOID_CURSOR, ToastState.AVOID_READING_ZONE}:
            away = entity.pos - signals.cursor_predicted
            desired = (away.normalized() if away.length() > 1 else Vec2(0, -1)) * self.cfg.panic_speed
            entity.hop_phase = (entity.hop_phase + dt * 7.0) % 1.0
        elif entity.state is ToastState.SOCIALIZE:
            if friends := [o for o in others if o.id != entity.id and o.kind is EntityKind.MINI_TOAST]:
                pal = min(friends, key=lambda o: (o.pos - entity.pos).length())
                entity.partner_id = pal.id
                mid = Vec2((entity.pos.x + pal.pos.x) * 0.5, (entity.pos.y + pal.pos.y) * 0.5)
                desired = (mid - entity.pos).normalized() * speed * 0.5
        elif entity.state in {ToastState.DANCE, ToastState.COPY_DANCE, ToastState.HEART_MODE}:
            entity.hop_phase = (entity.hop_phase + dt * 5.5) % 1.0
            desired = Vec2(self.rng.uniform(-20, 20), self.rng.uniform(-10, 10))
        elif entity.state is ToastState.SLEEP:
            desired = Vec2()
        elif entity.state is ToastState.HIDE:
            desired = Vec2(0, 40)
        elif entity.state is ToastState.TERMINAL_AWE:
            goal = toaster + Vec2(0, 80)
            desired = (goal - entity.pos).normalized() * speed * 0.4
            entity.local_intent = "watch_terminal"
        elif entity.state is ToastState.WORK_QUIET_MODE:
            edge = Vec2(desktop.right - 40, desktop.y + desktop.h * 0.35)
            desired = (edge - entity.pos).normalized() * speed * 0.45
        elif entity.state is ToastState.INSIDE_APPLIANCE:
            entity.hidden = not getattr(entity, "safety_yield_visual", False)
            if getattr(entity, "safety_yield_visual", False):
                entity.opacity = min(entity.opacity, 0.12)
            if entity.appliance:
                entity.pos = toaster + Vec2(0.0, -8.0)
            return
        elif entity.state is ToastState.DOOMSCROLL_COMMENTARY:
            desired = Vec2()
        else:
            desired = entity.vel * 0.4

        density = 1.0 + max(0, len(others) - 3) * 0.12
        if entity.state not in RITUAL_STATES:
            entity.vel = entity.vel + cursor_repulsion(entity.pos, signals.cursor, self.cfg.cursor_soft_radius, 28.0)
        crowd = crowd_push(
            entity.id,
            entity.pos,
            [(o.id, o.pos, o.radius) for o in others],
            entity.radius,
            rng=self.rng,
            density=density,
            crowd_tolerance=entity.trait("crowd_tolerance"),
        )
        desired = desired + crowd * (70.0 + density * 18.0)
        acc = (desired - entity.vel) * 4.2
        if entity.kind is EntityKind.POPCORN_KERNEL:
            entity.pos, entity.vel = popcorn_ballistic(entity.pos, entity.vel + acc * dt, dt)
        else:
            entity.pos, entity.vel = phys_integrate(entity.pos, entity.vel, acc, dt, max(speed * 2.4, self.cfg.panic_speed))
            entity.vel = damping(entity.vel, coeff=0.92, desired=desired, mix=0.08)
        confined, bounced = confine(entity.pos, entity.radius, desktop, entity.vel)
        entity.pos = confined
        entity.vel = bounced
        entity.classify_browning()
        if entity.state is ToastState.SOCIALIZE:
            entity.learn("sociality", 0.0008 * dt)
        if entity.state is ToastState.WORK_QUIET_MODE:
            entity.learn("work_respect", 0.0006 * dt)
        if entity.state is ToastState.OBSERVE_TOASTER:
            entity.learn("attachment_to_main_toaster", 0.0005 * dt)
        if entity.state is ToastState.OBSERVE_USER:
            entity.learn("attachment_to_user", 0.0005 * dt)
        if entity.state is ToastState.PORTAL_CURIOUS:
            entity.learn("portal_curiosity", 0.0007 * dt)
        if entity.state is ToastState.DANCE:
            entity.learn("chaos_affinity", 0.0004 * dt)
        if entity.vel.x < -8:
            entity.facing = -1.0
        elif entity.vel.x > 8:
            entity.facing = 1.0
        if entity.state not in HIDDEN_STATES:
            entity.hidden = False
            entity.opacity = min(1.0, entity.opacity + dt * 3.2)
        else:
            entity.opacity = max(0.0, entity.opacity - dt * 4.0)
        entity.safety_yield_visual = False
