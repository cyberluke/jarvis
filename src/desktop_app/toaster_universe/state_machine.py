"""Canonical state-machine contract. Raw entity.state writes are not legal."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .types import BehaviorLayer, HIDDEN_STATES, OverrideReason, SAFETY_STATES, ToastState, Vec2, WorldEvent


RITUAL_STATES = frozenset(
    {
        ToastState.TOASTING,
        ToastState.OVERTOASTING,
        ToastState.JUMP_INTO_SLOT,
        ToastState.INSIDE_APPLIANCE,
        ToastState.PORTAL_ENTER,
    }
)

SAFETY_OVERRIDE_STATES = frozenset(
    {
        ToastState.AVOID_CURSOR,
        ToastState.AVOID_READING_ZONE,
        ToastState.PANIC_RUN,
        ToastState.HIDE,
        ToastState.WORK_QUIET_MODE,
        ToastState.DESPAWN,
    }
)

LIFECYCLE_OVERRIDE = frozenset(
    {
        ToastState.DESPAWN,
        ToastState.RESPAWN,
        ToastState.SPAWN,
        ToastState.BURNT,
        ToastState.RECOVER,
        ToastState.RITUAL_FUNERAL,
        ToastState.POP_OUT,
        ToastState.MULTIPLY,
    }
)

OVERRIDE_REASONS = frozenset({"safety", "lifecycle", "bootstrap", "restore"})

STATE_TIMEOUTS: dict[ToastState, float] = {
    ToastState.SPAWN: 0.45,
    ToastState.IDLE: 1.6,
    ToastState.WANDER: 4.5,
    ToastState.OBSERVE_USER: 3.2,
    ToastState.OBSERVE_TOASTER: 3.4,
    ToastState.FOLLOW_CURSOR_AT_DISTANCE: 3.5,
    ToastState.AVOID_CURSOR: 0.85,
    ToastState.AVOID_READING_ZONE: 0.85,
    ToastState.APPROACH_TOASTER: 8.0,
    ToastState.QUEUE_FOR_SLOT: 6.0,
    ToastState.JUMP_INTO_SLOT: 0.42,
    ToastState.TOASTING: 3.4,
    ToastState.OVERTOASTING: 2.2,
    ToastState.POP_OUT: 0.55,
    ToastState.COOL_DOWN: 2.6,
    ToastState.PANIC_RUN: 1.1,
    ToastState.SLEEP: 22.0,
    ToastState.WAKE: 0.6,
    ToastState.SOCIALIZE: 3.2,
    ToastState.DANCE: 4.0,
    ToastState.COPY_DANCE: 3.2,
    ToastState.FIGHT_PLAYFULLY: 2.2,
    ToastState.HIDE: 2.0,
    ToastState.PEEK: 1.2,
    ToastState.MULTIPLY: 0.5,
    ToastState.CARRY_CRUMB: 2.5,
    ToastState.EAT_CRUMB: 1.1,
    ToastState.WATCH_VIDEO: 8.0,
    ToastState.POPCORN_AUDIENCE: 7.0,
    ToastState.WORK_QUIET_MODE: 12.0,
    ToastState.TERMINAL_AWE: 5.5,
    ToastState.DOOMSCROLL_COMMENTARY: 6.0,
    ToastState.HEART_MODE: 5.0,
    ToastState.PORTAL_CURIOUS: 1.4,
    ToastState.PORTAL_ENTER: 0.8,
    ToastState.PORTAL_RETURN: 0.7,
    ToastState.BURNT: 2.4,
    ToastState.RECOVER: 2.8,
    ToastState.RITUAL_FUNERAL: 3.5,
    ToastState.RESPAWN: 0.4,
    ToastState.DESPAWN: 0.0,
    ToastState.INSIDE_APPLIANCE: 4.5,
}

STATE_INTENT: dict[ToastState, str] = {
    ToastState.SPAWN: "appear",
    ToastState.IDLE: "rest",
    ToastState.WANDER: "roam",
    ToastState.OBSERVE_USER: "watch_user",
    ToastState.OBSERVE_TOASTER: "watch_host",
    ToastState.FOLLOW_CURSOR_AT_DISTANCE: "orbit_cursor",
    ToastState.AVOID_CURSOR: "yield_cursor",
    ToastState.AVOID_READING_ZONE: "yield_reading",
    ToastState.APPROACH_TOASTER: "seek_slot",
    ToastState.QUEUE_FOR_SLOT: "wait_slot",
    ToastState.JUMP_INTO_SLOT: "enter_slot",
    ToastState.TOASTING: "toast",
    ToastState.OVERTOASTING: "overtoast",
    ToastState.POP_OUT: "eject",
    ToastState.COOL_DOWN: "cool",
    ToastState.PANIC_RUN: "flee",
    ToastState.SLEEP: "sleep",
    ToastState.WAKE: "wake",
    ToastState.SOCIALIZE: "pair",
    ToastState.DANCE: "dance",
    ToastState.COPY_DANCE: "mirror",
    ToastState.FIGHT_PLAYFULLY: "scuffle",
    ToastState.HIDE: "hide",
    ToastState.PEEK: "peek",
    ToastState.MULTIPLY: "split",
    ToastState.CARRY_CRUMB: "carry",
    ToastState.EAT_CRUMB: "eat",
    ToastState.WATCH_VIDEO: "watch",
    ToastState.POPCORN_AUDIENCE: "audience",
    ToastState.WORK_QUIET_MODE: "work_quiet",
    ToastState.TERMINAL_AWE: "awe",
    ToastState.DOOMSCROLL_COMMENTARY: "comment",
    ToastState.HEART_MODE: "hearts",
    ToastState.PORTAL_CURIOUS: "portal_look",
    ToastState.PORTAL_ENTER: "portal_in",
    ToastState.PORTAL_RETURN: "portal_out",
    ToastState.BURNT: "burnt",
    ToastState.RECOVER: "recover",
    ToastState.RITUAL_FUNERAL: "funeral",
    ToastState.RESPAWN: "respawn",
    ToastState.DESPAWN: "leave",
    ToastState.INSIDE_APPLIANCE: "inside",
}

EVENT_EDGES: dict[WorldEvent, tuple[ToastState, ...]] = {
    WorldEvent.USER_TYPING_START: (ToastState.OBSERVE_USER, ToastState.WORK_QUIET_MODE),
    WorldEvent.USER_TYPING_FAST: (ToastState.WORK_QUIET_MODE, ToastState.TERMINAL_AWE),
    WorldEvent.USER_TYPING_BURST: (ToastState.TERMINAL_AWE,),
    WorldEvent.USER_TYPING_STOP: (ToastState.IDLE, ToastState.WANDER),
    WorldEvent.MOUSE_MOVE_SLOW: (ToastState.FOLLOW_CURSOR_AT_DISTANCE, ToastState.OBSERVE_USER),
    WorldEvent.MOUSE_MOVE_FAST: (ToastState.AVOID_CURSOR, ToastState.PANIC_RUN),
    WorldEvent.MOUSE_APPROACH_ENTITY: (ToastState.AVOID_CURSOR, ToastState.PANIC_RUN),
    WorldEvent.MOUSE_IDLE: (ToastState.OBSERVE_USER, ToastState.IDLE),
    WorldEvent.MOUSE_DRAG: (ToastState.HIDE,),
    WorldEvent.MOUSE_SELECTION: (ToastState.HIDE,),
    WorldEvent.SCROLL_START: (ToastState.HIDE,),
    WorldEvent.SCROLL_BURST: (ToastState.HIDE,),
    WorldEvent.SCROLL_STOP: (ToastState.WANDER, ToastState.IDLE),
    WorldEvent.FOREGROUND_VSCODE: (ToastState.WORK_QUIET_MODE,),
    WorldEvent.FOREGROUND_TERMINAL: (ToastState.TERMINAL_AWE,),
    WorldEvent.FOREGROUND_BROWSER: (ToastState.WATCH_VIDEO, ToastState.OBSERVE_USER),
    WorldEvent.TAB_SWITCH_SPIKE: (ToastState.WATCH_VIDEO,),
    WorldEvent.TERMINAL_ACTIVITY_HIGH: (ToastState.TERMINAL_AWE,),
    WorldEvent.USER_IDLE_LONG: (ToastState.SLEEP, ToastState.IDLE),
    WorldEvent.LATE_NIGHT: (ToastState.SLEEP,),
    WorldEvent.WORK_HOURS: (ToastState.WORK_QUIET_MODE, ToastState.IDLE),
    WorldEvent.SHORT_VIDEO_LOOP_PATTERN: (ToastState.WATCH_VIDEO, ToastState.POPCORN_AUDIENCE),
    WorldEvent.DOOMSCROLL_SCORE_HIGH: (ToastState.DOOMSCROLL_COMMENTARY, ToastState.WATCH_VIDEO),
    WorldEvent.FOCUS_SCORE_HIGH: (ToastState.WORK_QUIET_MODE,),
    WorldEvent.TOASTER_CLICK: (ToastState.OBSERVE_TOASTER,),
    WorldEvent.TOASTER_HOVER: (ToastState.OBSERVE_TOASTER, ToastState.APPROACH_TOASTER),
    WorldEvent.SLOT_FREE: (ToastState.APPROACH_TOASTER, ToastState.QUEUE_FOR_SLOT),
    WorldEvent.SLOT_OCCUPIED: (ToastState.QUEUE_FOR_SLOT, ToastState.WANDER),
    WorldEvent.TOAST_POP: (ToastState.COOL_DOWN, ToastState.OBSERVE_TOASTER),
    WorldEvent.TOAST_BURNT: (ToastState.BURNT, ToastState.RITUAL_FUNERAL),
    WorldEvent.POPULATION_LOW: (ToastState.SOCIALIZE, ToastState.IDLE),
    WorldEvent.POPULATION_HIGH: (ToastState.WANDER, ToastState.SOCIALIZE),
    WorldEvent.POPULATION_INFESTATION: (ToastState.MULTIPLY, ToastState.DANCE),
    WorldEvent.PORTAL_OPEN: (ToastState.PORTAL_CURIOUS,),
    WorldEvent.PORTAL_CLOSE: (ToastState.WANDER,),
    WorldEvent.NIGHT_THEME: (ToastState.SLEEP,),
    WorldEvent.SYSTEM_WAKE: (ToastState.WAKE, ToastState.SPAWN),
    WorldEvent.SYSTEM_SLEEP: (ToastState.SLEEP,),
    WorldEvent.VOICE_SPEAK_START: (ToastState.OBSERVE_TOASTER,),
    WorldEvent.VOICE_SPEAK_END: (ToastState.IDLE,),
    WorldEvent.AGENT_REPLY_START: (ToastState.TERMINAL_AWE, ToastState.OBSERVE_USER),
    WorldEvent.AGENT_REPLY_END: (ToastState.IDLE,),
    WorldEvent.KEYBOARD_SLICE: (ToastState.POP_OUT, ToastState.DANCE),
    WorldEvent.SAFETY_YIELD: (ToastState.AVOID_CURSOR, ToastState.HIDE, ToastState.PANIC_RUN),
}


@dataclass
class Transition:
    entity_id: str
    src: ToastState
    dst: ToastState
    reason: str
    t: float
    trigger: str = ""
    event_id: int = 0
    guard_ok: bool = True
    override_reason: str = ""
    layer: str = BehaviorLayer.L4_CHARACTER_GRAPH.value
    rejected: str = ""

    def as_payload(self) -> dict[str, Any]:
        return {
            "entity": self.entity_id,
            "src": self.src.value,
            "dst": self.dst.value,
            "reason": self.reason,
            "trigger": self.trigger,
            "t": round(self.t, 4),
            "event_id": self.event_id,
            "guard_ok": self.guard_ok,
            "override_reason": self.override_reason,
            "layer": self.layer,
            "rejected": self.rejected,
        }


def timeout_for(state: ToastState, cfg_toasting: float | None = None, cfg_overtoast: float | None = None, cfg_cool: float | None = None) -> float:
    if state is ToastState.TOASTING and cfg_toasting is not None:
        return cfg_toasting
    if state is ToastState.OVERTOASTING and cfg_overtoast is not None:
        return cfg_overtoast
    if state is ToastState.COOL_DOWN and cfg_cool is not None:
        return cfg_cool
    return STATE_TIMEOUTS.get(state, 4.0)


def elapsed(entity, t: float) -> float:
    return max(0.0, t - getattr(entity, "state_t", t))


def timed_out(entity, t: float, extra: float = 0.0) -> bool:
    limit = timeout_for(entity.state) + extra
    return elapsed(entity, t) >= limit


def edge_key(src: ToastState, dst: ToastState) -> str:
    return f"{src.value}->{dst.value}"


def penalty_for(entity, src: ToastState, dst: ToastState) -> float:
    uses = getattr(entity, "edge_uses", None) or {}
    return min(0.85, float(uses.get(edge_key(src, dst), 0)) * 0.12)


def note_edge(entity, src: ToastState, dst: ToastState) -> None:
    if not hasattr(entity, "edge_uses") or entity.edge_uses is None:
        entity.edge_uses = {}
    key = edge_key(src, dst)
    entity.edge_uses[key] = int(entity.edge_uses.get(key, 0)) + 1


def push_recovery(entity, state: ToastState) -> None:
    if state in SAFETY_STATES or state in RITUAL_STATES or state is ToastState.DESPAWN:
        return
    stack = getattr(entity, "recovery_stack", None)
    if stack is None:
        entity.recovery_stack = []
        stack = entity.recovery_stack
    if not stack or stack[-1] is not state:
        stack.append(state)
        if len(stack) > 6:
            del stack[0]


def pop_recovery(entity) -> ToastState | None:
    stack = getattr(entity, "recovery_stack", None)
    if not stack:
        return None
    return stack.pop()


def clear_locks(entity, leaving: ToastState) -> None:
    if leaving is ToastState.CARRY_CRUMB and entity.state is not ToastState.EAT_CRUMB:
        entity.carrying = None
    if leaving in {ToastState.SOCIALIZE, ToastState.DANCE, ToastState.COPY_DANCE, ToastState.FIGHT_PLAYFULLY, ToastState.HEART_MODE}:
        if entity.state not in {ToastState.COPY_DANCE, ToastState.DANCE, ToastState.SOCIALIZE, ToastState.HEART_MODE}:
            entity.partner_id = None
    if leaving is ToastState.JUMP_INTO_SLOT and entity.state not in {ToastState.TOASTING, ToastState.OVERTOASTING}:
        entity.jump_from = None
        entity.slot_index = None
    if leaving is ToastState.POP_OUT:
        entity.jump_from = None
    if leaving in {ToastState.PORTAL_RETURN, ToastState.WANDER, ToastState.IDLE} and entity.state not in {
        ToastState.PORTAL_CURIOUS,
        ToastState.PORTAL_ENTER,
        ToastState.INSIDE_APPLIANCE,
    }:
        if leaving is ToastState.PORTAL_RETURN:
            entity.appliance = None
    entity.local_intent = STATE_INTENT.get(entity.state, "")


def derive_entry(entity, state: ToastState, toaster: Vec2 | None = None) -> None:
    entity.local_intent = STATE_INTENT.get(state, "")
    host = toaster if toaster is not None else getattr(entity, "target", entity.pos)
    if state is ToastState.SPAWN:
        entity.opacity = 0.2
        entity.target = entity.pos
    elif state is ToastState.WANDER:
        entity.target = entity.pos
    elif state is ToastState.OBSERVE_USER:
        entity.vel = entity.vel * 0.4
    elif state is ToastState.FOLLOW_CURSOR_AT_DISTANCE:
        entity.local_intent = "orbit_cursor"
    elif state in {ToastState.APPROACH_TOASTER, ToastState.OBSERVE_TOASTER, ToastState.QUEUE_FOR_SLOT}:
        entity.target = host
    elif state is ToastState.IDLE:
        entity.target = entity.pos
        entity.vel = Vec2()
        entity.local_intent = "rest"
    elif state is ToastState.TERMINAL_AWE:
        entity.local_intent = "watch_terminal"
        entity.target = host
    elif state is ToastState.SLEEP:
        entity.vel = Vec2()
        entity.opacity = min(entity.opacity, 0.55)
    elif state is ToastState.WAKE:
        entity.opacity = 1.0
    elif state is ToastState.HIDE:
        entity.opacity = min(entity.opacity, 0.45)
    elif state is ToastState.PEEK:
        entity.opacity = min(1.0, max(entity.opacity, 0.55))
    elif state is ToastState.WORK_QUIET_MODE:
        entity.opacity = min(entity.opacity, 0.35)
    elif state is ToastState.JUMP_INTO_SLOT:
        entity.jump_from = entity.pos
    elif state is ToastState.AVOID_CURSOR:
        entity.local_intent = "yield_cursor"
    elif state is ToastState.AVOID_READING_ZONE:
        entity.local_intent = "yield_reading"
    elif state is ToastState.PANIC_RUN:
        entity.local_intent = "flee"
    elif state is ToastState.SOCIALIZE:
        entity.local_intent = "pair"
    elif state is ToastState.DANCE:
        entity.local_intent = "dance"
    elif state is ToastState.COPY_DANCE:
        entity.local_intent = "mirror"
    elif state is ToastState.FIGHT_PLAYFULLY:
        entity.local_intent = "scuffle"
    elif state is ToastState.MULTIPLY:
        entity.local_intent = "split"
    elif state is ToastState.CARRY_CRUMB:
        entity.local_intent = "carry"
    elif state is ToastState.EAT_CRUMB:
        entity.local_intent = "eat"
    elif state is ToastState.WATCH_VIDEO:
        entity.local_intent = "watch"
        entity.vel = entity.vel * 0.3
    elif state is ToastState.POPCORN_AUDIENCE:
        entity.local_intent = "audience"
    elif state is ToastState.DOOMSCROLL_COMMENTARY:
        entity.local_intent = "comment"
        entity.vel = Vec2()
    elif state is ToastState.HEART_MODE:
        entity.local_intent = "hearts"
    elif state is ToastState.PORTAL_CURIOUS:
        entity.local_intent = "portal_look"
    elif state is ToastState.PORTAL_ENTER:
        entity.local_intent = "portal_in"
    elif state is ToastState.PORTAL_RETURN:
        entity.local_intent = "portal_out"
        entity.hidden = False
        entity.opacity = 1.0
    elif state is ToastState.INSIDE_APPLIANCE:
        entity.local_intent = "inside"
    elif state is ToastState.BURNT:
        entity.local_intent = "burnt"
        entity.burnt = True
    elif state is ToastState.RECOVER:
        entity.local_intent = "recover"
        entity.burnt = False
        entity.browning = min(entity.browning, 0.4)
    elif state is ToastState.RITUAL_FUNERAL:
        entity.local_intent = "funeral"
    elif state is ToastState.RESPAWN:
        entity.local_intent = "respawn"
        entity.hidden = False
        entity.opacity = 0.4
        entity.burnt = False
    elif state is ToastState.DESPAWN:
        entity.local_intent = "leave"
        entity.hidden = True
    elif state is ToastState.COOL_DOWN:
        entity.local_intent = "cool"
        entity.hidden = False
    elif state is ToastState.POP_OUT:
        entity.local_intent = "eject"
        entity.hidden = False
        entity.opacity = 1.0
    elif state is ToastState.TOASTING:
        entity.local_intent = "toast"
    elif state is ToastState.OVERTOASTING:
        entity.local_intent = "overtoast"
    entity.hidden = state in HIDDEN_STATES


def layer_for_reason(reason: str) -> str:
    return {
        "safety": BehaviorLayer.L1_ATTENTION_AVOIDANCE.value,
        "intent": BehaviorLayer.L0_USER_INTENT.value,
        "activity": BehaviorLayer.L2_ACTIVITY_CONTEXT.value,
        "physics": BehaviorLayer.L3_WORLD_PHYSICS.value,
        "graph": BehaviorLayer.L4_CHARACTER_GRAPH.value,
        "social": BehaviorLayer.L5_SOCIAL_SIMULATION.value,
        "comedy": BehaviorLayer.L6_COMEDY_DIRECTOR.value,
        "scene": BehaviorLayer.L7_NARRATIVE_DIRECTOR.value,
        "director": BehaviorLayer.L8_LLM_DIRECTOR.value,
        "lifecycle": BehaviorLayer.L4_CHARACTER_GRAPH.value,
        "bootstrap": BehaviorLayer.L4_CHARACTER_GRAPH.value,
        "restore": BehaviorLayer.L4_CHARACTER_GRAPH.value,
        "event": BehaviorLayer.L4_CHARACTER_GRAPH.value,
    }.get(reason, BehaviorLayer.L4_CHARACTER_GRAPH.value)
