"""Composable clip/action pose runtime. QPainter transforms, not sprite sheets."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from .entities import Entity
from .signals import UserSignals
from .types import ClipId, EntityKind, Pose, ToastState, Vec2, WorldEvent

if TYPE_CHECKING:
    from .events import EventBus


class InterruptPolicy(str, Enum):
    BLEND = "blend"
    REPLACE = "replace"
    HOLD = "hold"
    SAFETY_ABORT = "safety_abort"


@dataclass
class ClipInstance:
    clip: ClipId
    start: float
    duration: float
    phase: float = 0.0
    interrupt: InterruptPolicy = InterruptPolicy.BLEND
    entity_id: str | None = None
    started: bool = False
    ended: bool = False
    reduced: bool = False
    seed: float = 0.0

    def progress(self, t: float) -> float:
        if self.duration <= 1e-6:
            return 1.0
        return max(0.0, min(1.0, (t - self.start) / self.duration))

    def live(self, t: float) -> bool:
        return not self.ended and t >= self.start and self.progress(t) < 1.0


def ease_out(p: float) -> float:
    return 1.0 - (1.0 - p) ** 2


def ease_in_out(p: float) -> float:
    return 0.5 - 0.5 * math.cos(p * math.pi)


def spring_step(current: float, target: float, vel: float, k: float, damp: float, dt: float) -> tuple[float, float]:
    force = (target - current) * k - vel * damp
    vel += force * dt
    current += vel * dt
    return current, vel


def _noise(t: float, seed: float) -> float:
    return math.sin(t * 7.13 + seed * 2.17) * 0.55 + math.sin(t * 3.07 + seed) * 0.45


CLIP_DEFAULTS: dict[ClipId, tuple[float, InterruptPolicy]] = {
    ClipId.HOVER_SCALE: (0.0, InterruptPolicy.HOLD),
    ClipId.BOUNCE: (0.32, InterruptPolicy.REPLACE),
    ClipId.SPIN: (0.36, InterruptPolicy.REPLACE),
    ClipId.WIGGLE: (0.34, InterruptPolicy.REPLACE),
    ClipId.SQUASH: (0.30, InterruptPolicy.REPLACE),
    ClipId.STRETCH: (0.30, InterruptPolicy.REPLACE),
    ClipId.POP: (0.28, InterruptPolicy.REPLACE),
    ClipId.CURSOR_EYE_FOLLOW: (0.0, InterruptPolicy.HOLD),
    ClipId.FACE_TURN: (0.45, InterruptPolicy.BLEND),
    ClipId.BLINK: (0.24, InterruptPolicy.BLEND),
    ClipId.DOUBLE_BLINK: (0.42, InterruptPolicy.REPLACE),
    ClipId.IDLE_BREATHE: (0.0, InterruptPolicy.HOLD),
    ClipId.JELLY_BODY: (0.55, InterruptPolicy.BLEND),
    ClipId.IMPACT_SHOCKWAVE: (0.48, InterruptPolicy.REPLACE),
    ClipId.TOAST_CRUMBS: (0.70, InterruptPolicy.BLEND),
    ClipId.HEAT_SHIMMER: (0.0, InterruptPolicy.HOLD),
    ClipId.ELECTRIC_ANGER: (0.55, InterruptPolicy.REPLACE),
    ClipId.GHOST_TRAIL: (0.50, InterruptPolicy.BLEND),
    ClipId.VELOCITY_SQUASH_STRETCH: (0.0, InterruptPolicy.HOLD),
    ClipId.SLOT_JUMP: (0.42, InterruptPolicy.HOLD),
    ClipId.SLOT_SINK: (0.28, InterruptPolicy.HOLD),
    ClipId.TOAST_BROWN: (0.0, InterruptPolicy.HOLD),
    ClipId.TOAST_SMOKE: (1.10, InterruptPolicy.BLEND),
    ClipId.TOAST_POPOUT: (0.55, InterruptPolicy.REPLACE),
    ClipId.PANIC_RUN: (0.0, InterruptPolicy.HOLD),
    ClipId.SLEEP_MELT: (0.0, InterruptPolicy.HOLD),
    ClipId.WAKE_SNAP: (0.40, InterruptPolicy.REPLACE),
    ClipId.DANCE_COMMITMENT: (0.0, InterruptPolicy.HOLD),
    ClipId.DEADPAN_STARE: (1.80, InterruptPolicy.HOLD),
    ClipId.HEART_BURST: (0.70, InterruptPolicy.BLEND),
    ClipId.POPCORN_BURST: (0.80, InterruptPolicy.BLEND),
    ClipId.PORTAL_WARP: (0.90, InterruptPolicy.REPLACE),
    ClipId.TELEPORT_GLITCH: (0.36, InterruptPolicy.REPLACE),
    ClipId.FUNERAL_PROCESSION: (0.0, InterruptPolicy.HOLD),
    ClipId.CROWD_WAVE: (1.20, InterruptPolicy.BLEND),
    ClipId.QUEUE_SHUFFLE: (0.80, InterruptPolicy.BLEND),
}


def pose_for_clip(clip: ClipId, p: float, t: float, seed: float = 0.0, reduced: bool = False) -> Pose:
    pose = Pose()
    if reduced:
        p = min(1.0, p * 1.4)
        amp = 0.35
    else:
        amp = 1.0
    e = ease_out(p)
    wave = math.sin(p * math.pi)
    if clip is ClipId.HOVER_SCALE:
        pose.scale = 1.07
    elif clip is ClipId.BOUNCE:
        pose.hop = wave * 16.0 * amp
    elif clip is ClipId.SPIN:
        pose.rotation = e * 360.0 * amp
    elif clip is ClipId.WIGGLE:
        pose.rotation = math.sin(e * math.pi * 4) * 11.0 * (1.0 - e) * amp
    elif clip is ClipId.SQUASH:
        pose.squash_y = 1.0 - wave * 0.18 * amp
        pose.squash_x = 1.0 + wave * 0.18 * amp
    elif clip is ClipId.STRETCH:
        pose.squash_y = 1.0 + wave * 0.22 * amp
        pose.squash_x = 1.0 - wave * 0.14 * amp
    elif clip is ClipId.POP:
        pose.scale = 1.0 + wave * 0.22 * amp
        pose.squash_x = pose.squash_y = pose.scale
    elif clip is ClipId.FACE_TURN:
        pose.face_yaw = math.sin(ease_in_out(p) * math.pi) * 0.85 * amp
        pose.look_x = pose.face_yaw
    elif clip is ClipId.BLINK:
        pose.blink = wave
    elif clip is ClipId.DOUBLE_BLINK:
        first = math.sin(min(1.0, p / 0.42) * math.pi) if p < 0.42 else 0.0
        second = math.sin(min(1.0, max(0.0, p - 0.48) / 0.42) * math.pi) if p >= 0.48 else 0.0
        pose.blink = max(first, second)
    elif clip is ClipId.IDLE_BREATHE:
        pose.scale = 1.0 + 0.012 * math.sin(t * 1.6) * amp
        pose.body_warp = 0.04 * math.sin(t * 1.6)
    elif clip is ClipId.JELLY_BODY:
        pose.body_warp = math.sin(t * 14.0 + seed) * 0.14 * (1.0 - e) * amp
        pose.squash_x = 1.0 + pose.body_warp
        pose.squash_y = 1.0 - pose.body_warp
    elif clip is ClipId.IMPACT_SHOCKWAVE:
        pose.shockwave = wave * amp
        pose.squash_y = 1.0 - wave * 0.08
    elif clip is ClipId.TOAST_CRUMBS:
        pose.glow = 0.12 * (1.0 - p)
        pose.shockwave = 0.18 * (1.0 - p)
        pose.translation_x = math.sin(p * 18.0 + seed) * 3.0 * (1.0 - p)
    elif clip is ClipId.HEAT_SHIMMER:
        pose.translation_y = math.sin(t * 11.0 + seed) * 1.6 * amp
        pose.glow = 0.22
    elif clip is ClipId.ELECTRIC_ANGER:
        pose.arc = (0.35 + 0.65 * abs(_noise(t * 2.2, seed))) * (1.0 - p * 0.4) * amp
        pose.glow = pose.arc
    elif clip is ClipId.GHOST_TRAIL:
        pose.trail = (1.0 - p) * amp
        pose.smear = 0.28 * (1.0 - p)
        pose.opacity = 0.85
    elif clip is ClipId.VELOCITY_SQUASH_STRETCH:
        pose.smear = 0.2
    elif clip is ClipId.SLOT_JUMP:
        pose.squash_y = 1.18
        pose.squash_x = 0.82
        pose.rotation = 10.0 * amp
        pose.hop = wave * 22.0
    elif clip is ClipId.SLOT_SINK:
        pose.sink = e
        pose.squash_x = 1.0 - e * 0.35
        pose.squash_y = 1.0 - e * 0.55
        pose.opacity = 1.0 - e * 0.85
        pose.hop = -e * 10.0
    elif clip is ClipId.TOAST_BROWN:
        pose.glow = 0.18
    elif clip is ClipId.TOAST_SMOKE:
        pose.smoke = (1.0 - p) * amp
    elif clip is ClipId.TOAST_POPOUT:
        pose.hop = wave * 28.0 * amp
        pose.squash_y = 1.0 + 0.22 * wave
    elif clip is ClipId.PANIC_RUN:
        pose.hop = abs(math.sin(t * 18.0)) * 9.0 * amp
        pose.smear = 0.32
        pose.trail = 0.35
    elif clip is ClipId.SLEEP_MELT:
        pose.squash_y = 0.78
        pose.squash_x = 1.18
        pose.hop = -3.0
        pose.blink = 1.0
    elif clip is ClipId.WAKE_SNAP:
        pose.squash_y = 1.16 - e * 0.16
        pose.squash_x = 0.88 + e * 0.12
        pose.glow = (1.0 - e) * 0.4
    elif clip is ClipId.DANCE_COMMITMENT:
        pose.hop = math.sin(t * 10.0) * 7.0 * amp
        pose.rotation = math.sin(t * 8.0) * 14.0 * amp
        pose.squash_x = 1.0 + 0.1 * math.sin(t * 12.0)
        pose.squash_y = 1.0 - 0.1 * math.sin(t * 12.0)
        pose.glow = 0.45
    elif clip is ClipId.DEADPAN_STARE:
        pose.stare = 1.0
        pose.blink = 0.0
        pose.eye_squint = 0.15
        pose.rotation = 0.0
        pose.hop = 0.0
    elif clip is ClipId.HEART_BURST:
        pose.glow = wave * 0.55
        pose.hop = wave * 6.0
    elif clip is ClipId.POPCORN_BURST:
        pose.hop = wave * 5.0
    elif clip is ClipId.PORTAL_WARP:
        pose.glitch = wave * amp
        pose.rotation = math.sin(p * math.pi * 6) * 8.0
        pose.scale = 1.0 + math.sin(p * math.pi * 5) * 0.12
    elif clip is ClipId.TELEPORT_GLITCH:
        pose.glitch = 1.0 - e
        pose.translation_x = math.sin(p * 40.0 + seed) * 7.0 * (1.0 - e) * amp
        pose.opacity = 0.35 + 0.65 * e
    elif clip is ClipId.FUNERAL_PROCESSION:
        pose.rotation = math.sin(t * 2.0) * 4.0
        pose.opacity = 0.7
    elif clip is ClipId.CROWD_WAVE:
        pose.hop = math.sin(p * math.pi) * 10.0 * amp
    elif clip is ClipId.QUEUE_SHUFFLE:
        pose.translation_x = math.sin(p * math.pi * 2) * 6.0 * amp
    elif clip is ClipId.CURSOR_EYE_FOLLOW:
        pose.look_x = 0.85 * amp
        pose.look_y = 0.35 * amp
        pose.face_yaw = 0.55 * amp
    return pose


STATE_CLIPS: dict[ToastState, tuple[ClipId, ...]] = {
    ToastState.WANDER: (ClipId.VELOCITY_SQUASH_STRETCH, ClipId.IDLE_BREATHE),
    ToastState.FOLLOW_CURSOR_AT_DISTANCE: (ClipId.CURSOR_EYE_FOLLOW, ClipId.VELOCITY_SQUASH_STRETCH),
    ToastState.OBSERVE_USER: (ClipId.CURSOR_EYE_FOLLOW, ClipId.FACE_TURN, ClipId.IDLE_BREATHE),
    ToastState.OBSERVE_TOASTER: (ClipId.FACE_TURN, ClipId.IDLE_BREATHE),
    ToastState.JUMP_INTO_SLOT: (ClipId.SLOT_JUMP,),
    ToastState.TOASTING: (ClipId.SLOT_SINK, ClipId.TOAST_BROWN, ClipId.HEAT_SHIMMER),
    ToastState.OVERTOASTING: (ClipId.SLOT_SINK, ClipId.TOAST_SMOKE, ClipId.TOAST_BROWN),
    ToastState.POP_OUT: (ClipId.TOAST_POPOUT, ClipId.TOAST_CRUMBS, ClipId.IMPACT_SHOCKWAVE),
    ToastState.PANIC_RUN: (ClipId.PANIC_RUN, ClipId.GHOST_TRAIL),
    ToastState.AVOID_CURSOR: (ClipId.GHOST_TRAIL,),
    ToastState.SLEEP: (ClipId.SLEEP_MELT,),
    ToastState.WAKE: (ClipId.WAKE_SNAP,),
    ToastState.DANCE: (ClipId.DANCE_COMMITMENT,),
    ToastState.COPY_DANCE: (ClipId.DANCE_COMMITMENT, ClipId.CROWD_WAVE),
    ToastState.HEART_MODE: (ClipId.HEART_BURST, ClipId.DANCE_COMMITMENT),
    ToastState.BURNT: (ClipId.TOAST_SMOKE, ClipId.ELECTRIC_ANGER),
    ToastState.RITUAL_FUNERAL: (ClipId.FUNERAL_PROCESSION,),
    ToastState.PORTAL_ENTER: (ClipId.PORTAL_WARP, ClipId.TELEPORT_GLITCH),
    ToastState.PORTAL_RETURN: (ClipId.TELEPORT_GLITCH, ClipId.PORTAL_WARP),
    ToastState.PORTAL_CURIOUS: (ClipId.PORTAL_WARP,),
    ToastState.POPCORN_AUDIENCE: (ClipId.POPCORN_BURST,),
    ToastState.DOOMSCROLL_COMMENTARY: (ClipId.DEADPAN_STARE,),
    ToastState.QUEUE_FOR_SLOT: (ClipId.QUEUE_SHUFFLE,),
    ToastState.WORK_QUIET_MODE: (ClipId.IDLE_BREATHE,),
    ToastState.HIDE: (ClipId.SQUASH,),
}


class AnimationDirector:
    def __init__(self, bus: "EventBus | None" = None, reduced_motion: bool = False) -> None:
        self.bus = bus
        self.reduced_motion = reduced_motion
        self.active: list[ClipInstance] = []

    def play(
        self,
        clip: ClipId,
        t: float,
        *,
        duration: float | None = None,
        entity_id: str | None = None,
        interrupt: InterruptPolicy | None = None,
        seed: float = 0.0,
    ) -> ClipInstance:
        default_dur, default_int = CLIP_DEFAULTS[clip]
        inst = ClipInstance(
            clip=clip,
            start=t,
            duration=duration if duration is not None else default_dur,
            interrupt=interrupt or default_int,
            entity_id=entity_id,
            reduced=self.reduced_motion,
            seed=seed,
        )
        if inst.interrupt is InterruptPolicy.REPLACE:
            self.active = [c for c in self.active if c.entity_id != entity_id or c.clip is not clip]
        self.active.append(inst)
        return inst

    def cancel_entity(self, entity_id: str, t: float) -> None:
        for inst in self.active:
            if inst.entity_id == entity_id and not inst.ended:
                inst.ended = True
                self._emit_end(inst, t)

    def tick(self, t: float) -> list[ClipInstance]:
        live: list[ClipInstance] = []
        for inst in self.active:
            if inst.ended:
                continue
            if t < inst.start:
                live.append(inst)
                continue
            if not inst.started:
                inst.started = True
                self._emit_start(inst, t)
            inst.phase = inst.progress(t)
            if inst.duration > 0 and inst.phase >= 1.0:
                inst.ended = True
                self._emit_end(inst, t)
                continue
            live.append(inst)
        self.active = live
        return live

    def compose(self, t: float, entity_id: str | None = None) -> Pose:
        pose = Pose()
        for inst in self.active:
            if entity_id is not None and inst.entity_id not in {None, entity_id}:
                continue
            if inst.duration <= 0:
                p = 0.5
            else:
                p = inst.progress(t)
            pose = pose.overlay(pose_for_clip(inst.clip, p, t, inst.seed, inst.reduced or self.reduced_motion))
        return pose

    def _emit_start(self, inst: ClipInstance, t: float) -> None:
        if self.bus is None:
            return
        self.bus.emit(
            WorldEvent.ANIM_START,
            t,
            source="animation",
            entity_id=inst.entity_id,
            payload={"clip": inst.clip.value, "duration": inst.duration},
        )

    def _emit_end(self, inst: ClipInstance, t: float) -> None:
        if self.bus is None:
            return
        self.bus.emit(
            WorldEvent.ANIM_END,
            t,
            source="animation",
            entity_id=inst.entity_id,
            payload={"clip": inst.clip.value},
        )


def compose_pose(entity: Entity, t: float, signals: UserSignals, director: AnimationDirector | None = None) -> Pose:
    pose = Pose(opacity=entity.opacity)
    if entity.kind is EntityKind.BUTTER_BLOB:
        pose.body_warp = entity.pose.body_warp
        pose.rotation = entity.pose.rotation
        pose.translation_x = entity.pose.translation_x
    phase = entity.hop_phase
    speed = entity.vel.length()
    reduced = bool(director and director.reduced_motion)
    amp = 0.35 if reduced else 1.0
    if entity.state in {ToastState.WANDER, ToastState.APPROACH_TOASTER, ToastState.FOLLOW_CURSOR_AT_DISTANCE, ToastState.PANIC_RUN}:
        pose.hop = math.sin(phase * math.pi) * (5.0 if entity.state is not ToastState.PANIC_RUN else 9.0) * amp
        pose.squash_y = 1.0 - 0.12 * math.sin(phase * math.pi) * amp
        pose.squash_x = 1.0 + 0.12 * math.sin(phase * math.pi) * amp
        pose.smear = min(0.35, speed / 400.0)
        if entity.state is ToastState.PANIC_RUN:
            pose.trail = 0.35
    elif entity.state in {ToastState.DANCE, ToastState.COPY_DANCE, ToastState.HEART_MODE}:
        wave_off = 0.0
        if entity.state is ToastState.COPY_DANCE:
            wave_off = (entity.seed % 7) * 0.18
        pose.hop = math.sin(t * 10.0 + wave_off) * 7.0 * amp
        pose.rotation = math.sin(t * 8.0 + wave_off) * 14.0 * amp
        pose.squash_x = 1.0 + 0.1 * math.sin(t * 12.0)
        pose.squash_y = 1.0 - 0.1 * math.sin(t * 12.0)
        pose.glow = 0.45
    elif entity.state is ToastState.SLEEP:
        pose.squash_y = 0.78
        pose.squash_x = 1.18
        pose.hop = -3.0
        pose.blink = 1.0
    elif entity.state is ToastState.WAKE:
        pose.squash_y = 1.16
        pose.squash_x = 0.88
    elif entity.state is ToastState.POP_OUT:
        p = min(1.0, (t - entity.state_t) / 0.5)
        pose.hop = math.sin(p * math.pi) * 28.0 * amp
        pose.squash_y = 1.0 + 0.22 * math.sin(p * math.pi)
        pose.shockwave = math.sin(p * math.pi) * amp
    elif entity.state is ToastState.JUMP_INTO_SLOT:
        pose.squash_y = 1.18
        pose.squash_x = 0.82
        pose.rotation = entity.facing * 12.0
        pose.hop = math.sin(min(1.0, (t - entity.state_t) / 0.42) * math.pi) * 18.0
    elif entity.state is ToastState.TOASTING:
        sink = min(1.0, (t - entity.state_t) / 0.28)
        pose.sink = sink
        pose.squash_x = 1.0 - sink * 0.28
        pose.squash_y = 1.0 - sink * 0.45
        pose.opacity = min(pose.opacity, 1.0 - sink * 0.7)
    elif entity.state is ToastState.OVERTOASTING:
        pose.sink = 1.0
        pose.smoke = 0.7
        pose.glow = 0.35
        pose.opacity = min(pose.opacity, 0.2)
    elif entity.state is ToastState.HIDE:
        pose.squash_y = 0.7
        pose.opacity = min(pose.opacity, 0.4)
    elif entity.state is ToastState.WORK_QUIET_MODE:
        pose.opacity = min(pose.opacity, 0.32)
        pose.blink = 0.2
    elif entity.state is ToastState.BURNT:
        pose.squash_x = 1.08
        pose.glow = 0.2
        pose.smoke = 0.55
        pose.arc = 0.25 + 0.15 * abs(math.sin(t * 9.0 + entity.seed))
    elif entity.state is ToastState.RITUAL_FUNERAL:
        pose.rotation = math.sin(t * 2.0) * 4.0
        pose.opacity = 0.7
    elif entity.state is ToastState.DOOMSCROLL_COMMENTARY:
        pose.stare = 1.0
        pose.blink = 0.0
        pose.eye_squint = 0.2
    elif entity.state in {ToastState.PORTAL_ENTER, ToastState.PORTAL_RETURN}:
        p = min(1.0, (t - entity.state_t) / 0.7)
        pose.glitch = 1.0 - p
        pose.translation_x = math.sin(t * 40.0 + entity.seed) * 6.0 * (1.0 - p)
        pose.opacity = min(pose.opacity, 0.4 + 0.6 * p)
    elif entity.state is ToastState.QUEUE_FOR_SLOT:
        pose.translation_x = math.sin(t * 3.0 + entity.seed) * 3.0
    if entity.kind is not EntityKind.BUTTER_BLOB:
        intensity = 0.55 + 0.7 * entity.trait("dramatic_tendency")
        pose.hop *= intensity
        pose.rotation *= 0.65 + 0.7 * entity.trait("chaos_affinity")
        pose.body_warp *= 0.5 + entity.trait("novelty_seeking")
    if entity.state is ToastState.OBSERVE_USER:
        pose.eye_squint = max(pose.eye_squint, 0.35)
        pose.mouth_open = max(pose.mouth_open, 0.12)
        pose.brow_raise = max(pose.brow_raise, 0.28)
        pose.viseme = "smirk"
    elif entity.state is ToastState.TERMINAL_AWE:
        pose.mouth_open = max(pose.mouth_open, 0.45)
        pose.eye_squint = max(pose.eye_squint, 0.2)
        pose.brow_raise = max(pose.brow_raise, 0.55)
        pose.viseme = "oh"
    elif entity.state is ToastState.DOOMSCROLL_COMMENTARY:
        pose.eye_squint = max(pose.eye_squint, 0.55)
        pose.brow_raise = max(pose.brow_raise, 0.18)
        pose.viseme = "line"
    elif entity.state in {ToastState.DANCE, ToastState.HEART_MODE}:
        pose.viseme = "smile"
        pose.brow_raise = max(pose.brow_raise, 0.22)
    elif entity.state is ToastState.SLEEP:
        pose.viseme = "rest"
        pose.brow_raise = 0.0
    if entity.browning >= 0.60:
        pose.smoke = max(pose.smoke, min(1.0, (entity.browning - 0.55) * 1.6))
        pose.body_warp += (entity.browning - 0.55) * 0.22
    if entity.state not in {ToastState.SLEEP, ToastState.HIDE} and pose.stare < 0.5:
        look = signals.cursor - entity.pos
        pose.look_x = max(-1.0, min(1.0, look.x / 180.0))
        pose.look_y = max(-1.0, min(1.0, look.y / 180.0))
        pose.face_yaw = max(-1.0, min(1.0, look.x / 240.0))
        blink = 0.5 + 0.5 * math.sin(t * 0.7 + entity.seed)
        pose.blink = 1.0 if blink > 0.97 else 0.0
    pose.glow = max(pose.glow, entity.temperature * 0.35)
    if director is not None:
        pose = pose.overlay(director.compose(t, entity.id))
        if getattr(entity, "safety_offset", None) is not None:
            pose.translation_x += entity.safety_offset.x
            pose.translation_y += entity.safety_offset.y
    entity.pose = pose
    return pose


def browning_color(amount: float, burnt: bool) -> tuple[int, int, int]:
    amount = max(0.0, min(1.0, amount))
    pale = (232, 185, 107)
    brown = (122, 72, 32)
    black = (32, 22, 16)
    if burnt or amount > 0.92:
        src, dst, u = brown, black, min(1.0, (amount - 0.75) / 0.25)
    else:
        src, dst, u = pale, brown, amount
    return (
        int(src[0] + (dst[0] - src[0]) * u),
        int(src[1] + (dst[1] - src[1]) * u),
        int(src[2] + (dst[2] - src[2]) * u),
    )
