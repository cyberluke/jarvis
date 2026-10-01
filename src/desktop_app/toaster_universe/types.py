"""Canonical enums and value objects for the toaster desktop world."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class BehaviorLayer(str, Enum):
    L0_USER_INTENT = "L0_USER_INTENT"
    L1_ATTENTION_AVOIDANCE = "L1_ATTENTION_AVOIDANCE"
    L2_ACTIVITY_CONTEXT = "L2_ACTIVITY_CONTEXT"
    L3_WORLD_PHYSICS = "L3_WORLD_PHYSICS"
    L4_CHARACTER_GRAPH = "L4_CHARACTER_GRAPH"
    L5_SOCIAL_SIMULATION = "L5_SOCIAL_SIMULATION"
    L6_COMEDY_DIRECTOR = "L6_COMEDY_DIRECTOR"
    L7_NARRATIVE_DIRECTOR = "L7_NARRATIVE_DIRECTOR"
    L8_LLM_DIRECTOR = "L8_LLM_DIRECTOR"


LAYER_PRIORITY = (
    BehaviorLayer.L0_USER_INTENT,
    BehaviorLayer.L1_ATTENTION_AVOIDANCE,
    BehaviorLayer.L2_ACTIVITY_CONTEXT,
    BehaviorLayer.L3_WORLD_PHYSICS,
    BehaviorLayer.L4_CHARACTER_GRAPH,
    BehaviorLayer.L5_SOCIAL_SIMULATION,
    BehaviorLayer.L6_COMEDY_DIRECTOR,
    BehaviorLayer.L7_NARRATIVE_DIRECTOR,
    BehaviorLayer.L8_LLM_DIRECTOR,
)


class EntityKind(str, Enum):
    MINI_TOAST = "mini_toast"
    BURNT_TOAST = "burnt_toast"
    CRUMB = "crumb"
    BUTTER_BLOB = "butter_blob"
    POPCORN_KERNEL = "popcorn_kernel"
    RICE_SPIRIT = "rice_spirit"
    PORTAL_ECHO = "portal_echo"
    APPLIANCE = "appliance"


class ToastState(str, Enum):
    SPAWN = "SPAWN"
    IDLE = "IDLE"
    WANDER = "WANDER"
    OBSERVE_USER = "OBSERVE_USER"
    OBSERVE_TOASTER = "OBSERVE_TOASTER"
    FOLLOW_CURSOR_AT_DISTANCE = "FOLLOW_CURSOR_AT_DISTANCE"
    AVOID_CURSOR = "AVOID_CURSOR"
    AVOID_READING_ZONE = "AVOID_READING_ZONE"
    APPROACH_TOASTER = "APPROACH_TOASTER"
    QUEUE_FOR_SLOT = "QUEUE_FOR_SLOT"
    JUMP_INTO_SLOT = "JUMP_INTO_SLOT"
    TOASTING = "TOASTING"
    OVERTOASTING = "OVERTOASTING"
    POP_OUT = "POP_OUT"
    COOL_DOWN = "COOL_DOWN"
    PANIC_RUN = "PANIC_RUN"
    SLEEP = "SLEEP"
    WAKE = "WAKE"
    SOCIALIZE = "SOCIALIZE"
    DANCE = "DANCE"
    COPY_DANCE = "COPY_DANCE"
    FIGHT_PLAYFULLY = "FIGHT_PLAYFULLY"
    HIDE = "HIDE"
    PEEK = "PEEK"
    MULTIPLY = "MULTIPLY"
    CARRY_CRUMB = "CARRY_CRUMB"
    EAT_CRUMB = "EAT_CRUMB"
    WATCH_VIDEO = "WATCH_VIDEO"
    POPCORN_AUDIENCE = "POPCORN_AUDIENCE"
    WORK_QUIET_MODE = "WORK_QUIET_MODE"
    TERMINAL_AWE = "TERMINAL_AWE"
    DOOMSCROLL_COMMENTARY = "DOOMSCROLL_COMMENTARY"
    HEART_MODE = "HEART_MODE"
    PORTAL_CURIOUS = "PORTAL_CURIOUS"
    PORTAL_ENTER = "PORTAL_ENTER"
    PORTAL_RETURN = "PORTAL_RETURN"
    BURNT = "BURNT"
    RECOVER = "RECOVER"
    RITUAL_FUNERAL = "RITUAL_FUNERAL"
    RESPAWN = "RESPAWN"
    DESPAWN = "DESPAWN"
    INSIDE_APPLIANCE = "INSIDE_APPLIANCE"
    MANUAL_DRAG = "MANUAL_DRAG"


HIDDEN_STATES = frozenset(
    {
        ToastState.TOASTING,
        ToastState.OVERTOASTING,
        ToastState.INSIDE_APPLIANCE,
        ToastState.PORTAL_ENTER,
        ToastState.DESPAWN,
    }
)

SAFETY_STATES = frozenset(
    {
        ToastState.AVOID_CURSOR,
        ToastState.AVOID_READING_ZONE,
        ToastState.PANIC_RUN,
        ToastState.HIDE,
        ToastState.WORK_QUIET_MODE,
    }
)


class WorldEvent(str, Enum):
    USER_TYPING_START = "USER_TYPING_START"
    USER_TYPING_BURST = "USER_TYPING_BURST"
    USER_TYPING_FAST = "USER_TYPING_FAST"
    USER_TYPING_STOP = "USER_TYPING_STOP"
    MOUSE_MOVE_SLOW = "MOUSE_MOVE_SLOW"
    MOUSE_MOVE_FAST = "MOUSE_MOVE_FAST"
    MOUSE_APPROACH_ENTITY = "MOUSE_APPROACH_ENTITY"
    MOUSE_IDLE = "MOUSE_IDLE"
    MOUSE_DRAG = "MOUSE_DRAG"
    MOUSE_SELECTION = "MOUSE_SELECTION"
    SCROLL_START = "SCROLL_START"
    SCROLL_BURST = "SCROLL_BURST"
    SCROLL_STOP = "SCROLL_STOP"
    WINDOW_FOCUS_CHANGED = "WINDOW_FOCUS_CHANGED"
    FOREGROUND_VSCODE = "FOREGROUND_VSCODE"
    FOREGROUND_TERMINAL = "FOREGROUND_TERMINAL"
    FOREGROUND_BROWSER = "FOREGROUND_BROWSER"
    TAB_SWITCH_SPIKE = "TAB_SWITCH_SPIKE"
    TERMINAL_ACTIVITY_HIGH = "TERMINAL_ACTIVITY_HIGH"
    USER_IDLE_LONG = "USER_IDLE_LONG"
    LATE_NIGHT = "LATE_NIGHT"
    WORK_HOURS = "WORK_HOURS"
    SHORT_VIDEO_LOOP_PATTERN = "SHORT_VIDEO_LOOP_PATTERN"
    DOOMSCROLL_SCORE_HIGH = "DOOMSCROLL_SCORE_HIGH"
    FOCUS_SCORE_HIGH = "FOCUS_SCORE_HIGH"
    TOASTER_CLICK = "TOASTER_CLICK"
    TOASTER_HOVER = "TOASTER_HOVER"
    TOAST_CLICK = "TOAST_CLICK"
    TOAST_HOVER = "TOAST_HOVER"
    TOAST_POP = "TOAST_POP"
    TOAST_BURNT = "TOAST_BURNT"
    SLOT_FREE = "SLOT_FREE"
    SLOT_OCCUPIED = "SLOT_OCCUPIED"
    POPULATION_LOW = "POPULATION_LOW"
    POPULATION_HIGH = "POPULATION_HIGH"
    POPULATION_INFESTATION = "POPULATION_INFESTATION"
    RARE_EVENT_TICK = "RARE_EVENT_TICK"
    PORTAL_OPEN = "PORTAL_OPEN"
    PORTAL_CLOSE = "PORTAL_CLOSE"
    WEATHER_GLOOMY_SIGNAL = "WEATHER_GLOOMY_SIGNAL"
    NIGHT_THEME = "NIGHT_THEME"
    SYSTEM_WAKE = "SYSTEM_WAKE"
    SYSTEM_SLEEP = "SYSTEM_SLEEP"
    VOICE_SPEAK_START = "VOICE_SPEAK_START"
    VOICE_SPEAK_END = "VOICE_SPEAK_END"
    AGENT_REPLY_START = "AGENT_REPLY_START"
    AGENT_REPLY_END = "AGENT_REPLY_END"
    KEYBOARD_SLICE = "KEYBOARD_SLICE"
    SAFETY_YIELD = "SAFETY_YIELD"
    ANIM_START = "ANIM_START"
    ANIM_END = "ANIM_END"
    ACTION_START = "ACTION_START"
    ACTION_END = "ACTION_END"
    POPULATION_CHANGE = "POPULATION_CHANGE"


class ActivityState(str, Enum):
    IDLE = "IDLE"
    FOCUSED = "FOCUSED"
    CODING_FLOW = "CODING_FLOW"
    FRANTIC = "FRANTIC"
    BROWSING = "BROWSING"
    DOOMSCROLLING = "DOOMSCROLLING"
    TERMINAL_HEAVY = "TERMINAL_HEAVY"
    LATE_NIGHT = "LATE_NIGHT"


class SceneId(str, Enum):
    QUIET_COMPANIONSHIP = "quiet_companionship"
    TOAST_WARMUP_RITUAL = "toast_warmup_ritual"
    BURN_RECOVERY = "burn_recovery"
    TOAST_WATCH_PARTY = "toast_watch_party"
    CODING_FLOW_AUDIENCE = "coding_flow_audience"
    HR_ROAST_SKIT = "hr_roast_skit"
    TOAST_INFESTATION = "toast_infestation"
    KEYBOARD_FEEDING_FRENZY = "keyboard_feeding_frenzy"
    POPCORN_BRAIN = "popcorn_brain"
    KPOP_HEART_MODE = "kpop_heart_mode"
    LATE_NIGHT_SLEEP = "late_night_sleep"
    FUNERAL_RITUAL = "funeral_ritual"
    PORTAL_GLITCH = "portal_glitch"
    SECRET_CHAMBER = "secret_chamber"
    RICE_ZEN = "rice_zen"
    AIRFRYER_VORTEX = "airfryer_vortex"
    MICROWAVE_ANOMALY = "microwave_anomaly"
    SHAREWARE_APOCALYPSE = "shareware_apocalypse"
    RETURN_TO_CALM = "return_to_calm"


class ComedyStyle(str, Enum):
    TECHNICAL_PRAGUE_DEADPAN = "TECHNICAL_PRAGUE_DEADPAN"
    CUTE_KAWAII = "CUTE_KAWAII"
    DARK_TOAST = "DARK_TOAST"
    ABSURD_PROFESSIONAL = "ABSURD_PROFESSIONAL"
    CORPORATE_ROAST = "CORPORATE_ROAST"
    EXISTENTIAL_CRUMB = "EXISTENTIAL_CRUMB"
    ANIME_GAG = "ANIME_GAG"
    QUIET_VISUAL_ONLY = "QUIET_VISUAL_ONLY"
    SHAREWARE_CHAOS = "SHAREWARE_CHAOS"
    ZEN_IRONY = "ZEN_IRONY"
    META_AI = "META_AI"
    OFFICE_SATIRE = "OFFICE_SATIRE"


class RareEventId(str, Enum):
    THE_OTHER_MONITOR_RETURN = "THE_OTHER_MONITOR_RETURN"
    THE_BLACK_TOAST_FUNERAL = "THE_BLACK_TOAST_FUNERAL"
    THE_ONE_TOAST_CHOIR = "THE_ONE_TOAST_CHOIR"
    MIDNIGHT_PORTAL = "MIDNIGHT_PORTAL"
    MICROWAVE_STAR = "MICROWAVE_STAR"
    RICE_SPIRIT_VISIT = "RICE_SPIRIT_VISIT"
    AIRFRYER_TORNADO = "AIRFRYER_TORNADO"
    POPCORN_STORM = "POPCORN_STORM"
    SILENT_HEART = "SILENT_HEART"
    TOAST_CLONE_GLITCH = "TOAST_CLONE_GLITCH"
    SLOT_WITH_NO_BOTTOM = "SLOT_WITH_NO_BOTTOM"
    WINDOW_EDGE_CAMP = "WINDOW_EDGE_CAMP"
    TASKBAR_PARADE = "TASKBAR_PARADE"
    FOURTH_TERMINAL_ORACLE = "FOURTH_TERMINAL_ORACLE"
    HQ_VAE_CEREMONY = "HQ_VAE_CEREMONY"
    FLAT_WHITE_ALIGNMENT = "FLAT_WHITE_ALIGNMENT"
    SHAREWARE_INVASION = "SHAREWARE_INVASION"
    CRUMB_KING = "CRUMB_KING"
    BUTTER_ECLIPSE = "BUTTER_ECLIPSE"


class ApplianceKind(str, Enum):
    MICROWAVE = "microwave"
    AIR_FRYER = "air_fryer"
    RICE_COOKER = "rice_cooker"
    OVEN = "oven"
    ESPRESSO = "espresso"
    MINI_FRIDGE = "mini_fridge"
    SECRET_CHAMBER = "secret_chamber"


class PopulationMode(str, Enum):
    EMPTY = "EMPTY"
    INTIMATE = "INTIMATE"
    ALIVE = "ALIVE"
    INFESTATION = "INFESTATION"
    SWARM_EVENT = "SWARM_EVENT"


class ClipId(str, Enum):
    HOVER_SCALE = "hover_scale"
    BOUNCE = "bounce"
    SPIN = "spin"
    WIGGLE = "wiggle"
    SQUASH = "squash"
    POP = "pop"
    CURSOR_EYE_FOLLOW = "cursor_eye_follow"
    FACE_TURN = "face_turn"
    BLINK = "blink"
    DOUBLE_BLINK = "double_blink"
    IDLE_BREATHE = "idle_breathe"
    JELLY_BODY = "jelly_body"
    IMPACT_SHOCKWAVE = "impact_shockwave"
    TOAST_CRUMBS = "toast_crumbs"
    HEAT_SHIMMER = "heat_shimmer"
    ELECTRIC_ANGER = "electric_anger"
    GHOST_TRAIL = "ghost_trail"
    VELOCITY_SQUASH_STRETCH = "velocity_squash_stretch"
    SLOT_JUMP = "slot_jump"
    SLOT_SINK = "slot_sink"
    TOAST_BROWN = "toast_brown"
    TOAST_SMOKE = "toast_smoke"
    TOAST_POPOUT = "toast_popout"
    PANIC_RUN = "panic_run"
    SLEEP_MELT = "sleep_melt"
    WAKE_SNAP = "wake_snap"
    DANCE_COMMITMENT = "dance_commitment"
    DEADPAN_STARE = "deadpan_stare"
    HEART_BURST = "heart_burst"
    POPCORN_BURST = "popcorn_burst"
    PORTAL_WARP = "portal_warp"
    TELEPORT_GLITCH = "teleport_glitch"
    FUNERAL_PROCESSION = "funeral_procession"
    CROWD_WAVE = "crowd_wave"
    QUEUE_SHUFFLE = "queue_shuffle"
    STRETCH = "stretch"


class ToasterAction(str, Enum):
    LOOK_AT_CURSOR = "LOOK_AT_CURSOR"
    LOOK_AT_PET = "LOOK_AT_PET"
    BLINK = "BLINK"
    DOUBLE_BLINK = "DOUBLE_BLINK"
    BREATHE = "BREATHE"
    SMILE = "SMILE"
    SMIRK = "SMIRK"
    FROWN = "FROWN"
    EYE_ROLL = "EYE_ROLL"
    SQUASH = "SQUASH"
    STRETCH = "STRETCH"
    WIGGLE = "WIGGLE"
    SPIN = "SPIN"
    BOUNCE = "BOUNCE"
    POP = "POP"
    JELLY_WARP = "JELLY_WARP"
    HEAT_SHIMMER = "HEAT_SHIMMER"
    SLOT_GLOW = "SLOT_GLOW"
    TOAST_EJECT = "TOAST_EJECT"
    TOAST_CATCH = "TOAST_CATCH"
    SHOCKWAVE = "SHOCKWAVE"
    CRUMB_BURST = "CRUMB_BURST"
    GHOST_TRAIL = "GHOST_TRAIL"
    ELECTRIC_ARC = "ELECTRIC_ARC"
    STEAM_PUFF = "STEAM_PUFF"
    SLEEP_DIM = "SLEEP_DIM"
    WAKE_FLASH = "WAKE_FLASH"
    PORTAL_GLITCH = "PORTAL_GLITCH"
    DEADPAN_STARE = "DEADPAN_STARE"


class ChaosMode(str, Enum):
    NONE = "NONE"
    TOAST_SWARM = "TOAST_SWARM"
    EDGE_BOUNCE = "EDGE_BOUNCE"
    PARADE = "PARADE"
    SCREEN_SAVER_HORDE = "SCREEN_SAVER_HORDE"
    HAMMER_FAKEOUT = "HAMMER_FAKEOUT"
    DOOM_MONSTER_HOMAGE = "DOOM_MONSTER_HOMAGE"
    MULTI_MONITOR_MIGRATION = "MULTI_MONITOR_MIGRATION"
    CRUMB_STORM = "CRUMB_STORM"


class KeyboardMode(str, Enum):
    NONE = "NONE"
    FRUIT_NINJA_FEED = "FRUIT_NINJA_FEED"
    GUITAR_HERO_RHYTHM = "GUITAR_HERO_RHYTHM"
    COMBO_CLEANUP = "COMBO_CLEANUP"
    TERMINAL_FEEDING_FRENZY = "TERMINAL_FEEDING_FRENZY"
    QUIET_TAP = "QUIET_TAP"
    BURST_YEET = "BURST_YEET"


class SafetyZone(str, Enum):
    PANIC_RADIUS = "PANIC_RADIUS"
    STRONG_REPULSION = "STRONG_REPULSION"
    SOFT_REPULSION = "SOFT_REPULSION"
    READING_SANCTUARY = "READING_SANCTUARY"
    CLICK_SANCTUARY = "CLICK_SANCTUARY"
    DRAG_CORRIDOR = "DRAG_CORRIDOR"
    SCROLL_CORRIDOR = "SCROLL_CORRIDOR"
    FOREGROUND_WORK_RECT = "FOREGROUND_WORK_RECT"
    TASKBAR_SAFE_EDGE = "TASKBAR_SAFE_EDGE"
    SECONDARY_MONITOR_PLAY_ZONE = "SECONDARY_MONITOR_PLAY_ZONE"


class OverrideReason(str, Enum):
    NONE = "NONE"
    PRECISION_POINTER = "PRECISION_POINTER"
    DRAG_ACTIVE = "DRAG_ACTIVE"
    SELECTION_ACTIVE = "SELECTION_ACTIVE"
    SCROLL_ACTIVE = "SCROLL_ACTIVE"
    CURSOR_INTERSECT = "CURSOR_INTERSECT"
    CURSOR_PREDICTED = "CURSOR_PREDICTED"
    READING_ZONE = "READING_ZONE"
    CLICK_SANCTUARY = "CLICK_SANCTUARY"
    CARET_ZONE = "CARET_ZONE"
    SELECTION_ZONE = "SELECTION_ZONE"
    FOCUSED_CONTROL = "FOCUSED_CONTROL"
    WORK_RECT = "WORK_RECT"
    FOCUS_YIELD = "FOCUS_YIELD"
    NO_SAFE_TARGET = "NO_SAFE_TARGET"


@dataclass(frozen=True)
class Vec2:
    x: float = 0.0
    y: float = 0.0

    def __add__(self, other: "Vec2") -> "Vec2":
        return Vec2(self.x + other.x, self.y + other.y)

    def __sub__(self, other: "Vec2") -> "Vec2":
        return Vec2(self.x - other.x, self.y - other.y)

    def __mul__(self, scalar: float) -> "Vec2":
        return Vec2(self.x * scalar, self.y * scalar)

    __rmul__ = __mul__

    def length(self) -> float:
        return (self.x * self.x + self.y * self.y) ** 0.5

    def normalized(self) -> "Vec2":
        length = self.length()
        if length <= 1e-6:
            return Vec2()
        return Vec2(self.x / length, self.y / length)

    def clamped(self, max_length: float) -> "Vec2":
        length = self.length()
        if length <= max_length or length <= 1e-6:
            return self
        scale = max_length / length
        return Vec2(self.x * scale, self.y * scale)

    def dot(self, other: "Vec2") -> float:
        return self.x * other.x + self.y * other.y


@dataclass
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h

    @property
    def center(self) -> Vec2:
        return Vec2(self.x + self.w * 0.5, self.y + self.h * 0.5)

    def inflated(self, pad: float) -> "Rect":
        return Rect(self.x - pad, self.y - pad, self.w + pad * 2, self.h + pad * 2)

    def contains(self, point: Vec2) -> bool:
        return self.x <= point.x <= self.right and self.y <= point.y <= self.bottom

    def intersects_circle(self, center: Vec2, radius: float) -> bool:
        cx = min(max(center.x, self.x), self.right)
        cy = min(max(center.y, self.y), self.bottom)
        dx = center.x - cx
        dy = center.y - cy
        return dx * dx + dy * dy <= radius * radius

    def intersects(self, other: "Rect") -> bool:
        return self.x < other.right and self.right > other.x and self.y < other.bottom and self.bottom > other.y

    def union(self, other: "Rect") -> "Rect":
        x = min(self.x, other.x)
        y = min(self.y, other.y)
        return Rect(x, y, max(self.right, other.right) - x, max(self.bottom, other.bottom) - y)

    def area(self) -> float:
        return max(0.0, self.w) * max(0.0, self.h)

    def to_ints(self) -> tuple[int, int, int, int]:
        x = int(self.x)
        y = int(self.y)
        return x, y, max(1, int(self.right) - x), max(1, int(self.bottom) - y)


@dataclass
class SlotAnchor:
    index: int
    pos: Vec2
    occupied_by: Optional[str] = None


@dataclass
class BusEvent:
    name: WorldEvent
    t: float
    source: str = "world"
    entity_id: Optional[str] = None
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: int = 0


@dataclass
class SceneIntent:
    scene: SceneId
    tone: ComedyStyle = ComedyStyle.TECHNICAL_PRAGUE_DEADPAN
    participants: int = 1
    escalation: float = 0.2
    line_intent: str = ""
    duration: float = 18.0
    novelty_budget: float = 0.4
    allowed_entities: tuple[str, ...] = ()
    forbidden_zones: tuple[str, ...] = ()
    speech_budget: int = 1
    visual_only: bool = True
    callback_key: str = ""
    rarity_class: str = "common"
    source: str = "heuristic"
    content_key: str = ""


@dataclass
class Pose:
    squash_x: float = 1.0
    squash_y: float = 1.0
    rotation: float = 0.0
    hop: float = 0.0
    blink: float = 0.0
    opacity: float = 1.0
    glow: float = 0.0
    look_x: float = 0.0
    look_y: float = 0.0
    smear: float = 0.0
    translation_x: float = 0.0
    translation_y: float = 0.0
    body_warp: float = 0.0
    eye_squint: float = 0.0
    mouth_open: float = 0.0
    brow_raise: float = 0.0
    viseme: str = "rest"
    face_yaw: float = 0.0
    trail: float = 0.0
    shockwave: float = 0.0
    smoke: float = 0.0
    arc: float = 0.0
    glitch: float = 0.0
    stare: float = 0.0
    sink: float = 0.0
    scale: float = 1.0

    def overlay(self, other: "Pose") -> "Pose":
        return Pose(
            squash_x=self.squash_x * other.squash_x,
            squash_y=self.squash_y * other.squash_y,
            rotation=self.rotation + other.rotation,
            hop=self.hop + other.hop,
            blink=max(self.blink, other.blink),
            opacity=min(self.opacity, other.opacity),
            glow=max(self.glow, other.glow),
            look_x=max(-1.0, min(1.0, self.look_x + other.look_x)),
            look_y=max(-1.0, min(1.0, self.look_y + other.look_y)),
            smear=max(self.smear, other.smear),
            translation_x=self.translation_x + other.translation_x,
            translation_y=self.translation_y + other.translation_y,
            body_warp=self.body_warp + other.body_warp,
            eye_squint=max(self.eye_squint, other.eye_squint),
            mouth_open=max(self.mouth_open, other.mouth_open),
            brow_raise=max(self.brow_raise, other.brow_raise),
            viseme=other.viseme if other.viseme and other.viseme != "rest" else self.viseme,
            face_yaw=self.face_yaw + other.face_yaw,
            trail=max(self.trail, other.trail),
            shockwave=max(self.shockwave, other.shockwave),
            smoke=max(self.smoke, other.smoke),
            arc=max(self.arc, other.arc),
            glitch=max(self.glitch, other.glitch),
            stare=max(self.stare, other.stare),
            sink=max(self.sink, other.sink),
            scale=self.scale * other.scale,
        )
