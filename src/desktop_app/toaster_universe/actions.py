"""First-class Main Toaster action engine. Composes onto the existing QPainter face."""

from __future__ import annotations

from dataclasses import dataclass, field

from .animation import AnimationDirector, CLIP_DEFAULTS, InterruptPolicy
from .events import EventBus
from .types import ClipId, Pose, ToasterAction, Vec2, WorldEvent


ACTION_CLIPS: dict[ToasterAction, tuple[ClipId, ...]] = {
    ToasterAction.LOOK_AT_CURSOR: (ClipId.CURSOR_EYE_FOLLOW,),
    ToasterAction.LOOK_AT_PET: (ClipId.FACE_TURN, ClipId.CURSOR_EYE_FOLLOW),
    ToasterAction.BLINK: (ClipId.BLINK,),
    ToasterAction.DOUBLE_BLINK: (ClipId.DOUBLE_BLINK,),
    ToasterAction.BREATHE: (ClipId.IDLE_BREATHE,),
    ToasterAction.SMILE: (ClipId.IDLE_BREATHE,),
    ToasterAction.SMIRK: (ClipId.WIGGLE,),
    ToasterAction.FROWN: (ClipId.SQUASH,),
    ToasterAction.EYE_ROLL: (ClipId.FACE_TURN,),
    ToasterAction.SQUASH: (ClipId.SQUASH,),
    ToasterAction.STRETCH: (ClipId.STRETCH,),
    ToasterAction.WIGGLE: (ClipId.WIGGLE,),
    ToasterAction.SPIN: (ClipId.SPIN,),
    ToasterAction.BOUNCE: (ClipId.BOUNCE,),
    ToasterAction.POP: (ClipId.POP,),
    ToasterAction.JELLY_WARP: (ClipId.JELLY_BODY,),
    ToasterAction.HEAT_SHIMMER: (ClipId.HEAT_SHIMMER,),
    ToasterAction.SLOT_GLOW: (ClipId.TOAST_BROWN, ClipId.HEAT_SHIMMER),
    ToasterAction.TOAST_EJECT: (ClipId.TOAST_POPOUT, ClipId.TOAST_CRUMBS),
    ToasterAction.TOAST_CATCH: (ClipId.SQUASH, ClipId.JELLY_BODY),
    ToasterAction.SHOCKWAVE: (ClipId.IMPACT_SHOCKWAVE,),
    ToasterAction.CRUMB_BURST: (ClipId.TOAST_CRUMBS,),
    ToasterAction.GHOST_TRAIL: (ClipId.GHOST_TRAIL,),
    ToasterAction.ELECTRIC_ARC: (ClipId.ELECTRIC_ANGER,),
    ToasterAction.STEAM_PUFF: (ClipId.TOAST_SMOKE,),
    ToasterAction.SLEEP_DIM: (ClipId.SLEEP_MELT,),
    ToasterAction.WAKE_FLASH: (ClipId.WAKE_SNAP,),
    ToasterAction.PORTAL_GLITCH: (ClipId.TELEPORT_GLITCH, ClipId.PORTAL_WARP),
    ToasterAction.DEADPAN_STARE: (ClipId.DEADPAN_STARE,),
}

MOUTH_SHAPES = {
    ToasterAction.SMILE: 0.85,
    ToasterAction.SMIRK: 0.55,
    ToasterAction.FROWN: -0.75,
    ToasterAction.DEADPAN_STARE: 0.05,
}


@dataclass
class ActionInstance:
    action: ToasterAction
    start: float
    duration: float
    clips: tuple[ClipId, ...]
    look_target: Vec2 | None = None
    mouth: float = 0.0
    ended: bool = False


@dataclass
class MainToasterSoul:
    look: Vec2 = field(default_factory=Vec2)
    jelly: float = 0.0
    crumbs: float = 0.0
    shimmer: float = 0.0
    blink: float = 0.0
    heat: float = 0.35
    line: str = ""
    line_until: float = 0.0
    pose: Pose = field(default_factory=Pose)
    mouth: float = 0.0
    slot_glow: float = 0.0
    steam: float = 0.0
    shockwave: float = 0.0
    trail: float = 0.0
    arc: float = 0.0
    glitch: float = 0.0
    stare: float = 0.0
    look_mode: str = "cursor"
    pet_target: Vec2 | None = None
    last_action: str = ""
    last_action_t: float = -1e9
    voice_ready_t: float = -1e9


class ActionEngine:
    def __init__(self, bus: EventBus, animation: AnimationDirector) -> None:
        self.bus = bus
        self.animation = animation
        self.active: list[ActionInstance] = []

    def play(self, action: ToasterAction, t: float, *, look_target: Vec2 | None = None, duration: float | None = None) -> ActionInstance:
        clips = ACTION_CLIPS[action]
        if duration is None:
            duration = max((CLIP_DEFAULTS[c][0] for c in clips), default=0.4)
            if duration <= 0:
                duration = 0.8 if action in {ToasterAction.BREATHE, ToasterAction.HEAT_SHIMMER, ToasterAction.LOOK_AT_CURSOR, ToasterAction.LOOK_AT_PET} else 0.4
        inst = ActionInstance(
            action=action,
            start=t,
            duration=duration,
            clips=clips,
            look_target=look_target,
            mouth=MOUTH_SHAPES.get(action, 0.0),
        )
        for clip in clips:
            self.animation.play(clip, t, duration=duration if CLIP_DEFAULTS[clip][0] > 0 else duration, entity_id="main_toaster", seed=hash(action.value) % 97)
        self.active.append(inst)
        self.bus.emit(
            WorldEvent.ACTION_START,
            t,
            source="main_toaster",
            entity_id="main_toaster",
            payload={"action": action.value},
        )
        return inst

    def tick(self, soul: MainToasterSoul, t: float) -> Pose:
        self.animation.tick(t)
        pose = self.animation.compose(t, "main_toaster")
        live: list[ActionInstance] = []
        mouth = 0.0
        for inst in self.active:
            p = 0.0 if inst.duration <= 0 else max(0.0, min(1.0, (t - inst.start) / inst.duration))
            if inst.look_target is not None:
                soul.pet_target = inst.look_target
                soul.look_mode = "pet" if inst.action is ToasterAction.LOOK_AT_PET else soul.look_mode
            if inst.action is ToasterAction.LOOK_AT_CURSOR:
                soul.look_mode = "cursor"
            if inst.action is ToasterAction.EYE_ROLL:
                pose.look_x = math_sin_roll(p)
                pose.look_y = -0.7 + p * 1.2
            if inst.action is ToasterAction.SLOT_GLOW:
                soul.slot_glow = max(soul.slot_glow, 1.0 - p)
            if inst.action is ToasterAction.STEAM_PUFF:
                soul.steam = max(soul.steam, 1.0 - p)
            if inst.action is ToasterAction.CRUMB_BURST:
                soul.crumbs = max(soul.crumbs, 1.0 - p)
            if inst.action is ToasterAction.JELLY_WARP:
                soul.jelly = max(soul.jelly, 1.0 - p)
            if inst.action is ToasterAction.TOAST_EJECT:
                soul.crumbs = max(soul.crumbs, 0.8)
                soul.slot_glow = max(soul.slot_glow, 0.6 * (1.0 - p))
            if inst.mouth:
                mouth = inst.mouth
            if p >= 1.0 and CLIP_DEFAULTS.get(inst.clips[0], (0.4, InterruptPolicy.BLEND))[0] > 0:
                if not inst.ended:
                    inst.ended = True
                    soul.last_action = inst.action.value
                    soul.last_action_t = t
                    self.bus.emit(
                        WorldEvent.ACTION_END,
                        t,
                        source="main_toaster",
                        entity_id="main_toaster",
                        payload={"action": inst.action.value},
                    )
                continue
            live.append(inst)
        self.active = live
        soul.mouth = mouth
        soul.pose = pose
        soul.shockwave = pose.shockwave
        soul.trail = pose.trail
        soul.arc = pose.arc
        soul.glitch = pose.glitch
        soul.stare = pose.stare
        soul.blink = max(soul.blink, pose.blink)
        return pose


def math_sin_roll(p: float) -> float:
    import math

    return math.sin(p * math.pi * 2.0) * 0.9
