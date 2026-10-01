"""Scene performance. Setup → beats → payoff → settle. Visual first."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .llm_director import SCENE_META
from .types import SceneId, ToastState, Vec2


class SceneBeat(str, Enum):
    SETUP = "setup"
    BEAT1 = "beat1"
    BEAT2 = "beat2"
    PAYOFF = "payoff"
    SETTLE = "settle"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"


@dataclass
class SceneScript:
    setup: str
    beat1: str
    beat2: str
    payoff: str
    callback: str
    exit: str
    max_duration: float
    visual_only: bool = True


SCRIPTS: dict[SceneId, SceneScript] = {
    SceneId.QUIET_COMPANIONSHIP: SceneScript(
        "toasts occupy the far edge and breathe",
        "one toast sits still and watches the toaster",
        "a second toast mirrors the sit, no hop",
        "both look at the user, then look away together",
        "",
        "return to wander at low opacity",
        10.0,
    ),
    SceneId.TOAST_WATCH_PARTY: SceneScript(
        "toasts form a loose row facing the work rect",
        "row leans in as if a screen exists",
        "one toast stands, others copy the lean",
        "synchronized hop once, then sit",
        "crumb leftover if callback allowed",
        "row dissolves to wander",
        14.0,
    ),
    SceneId.TOAST_WARMUP_RITUAL: SceneScript(
        "cold toast approaches a slot",
        "queue forms with professional spacing",
        "first toast jumps the slot",
        "slot glow, pop, crumbs fall like paperwork",
        "",
        "cool-down walk away",
        16.0,
    ),
    SceneId.CODING_FLOW_AUDIENCE: SceneScript(
        "toasts retreat to a quiet flank",
        "they sit, opacity drops",
        "one toast peeks, then sits again",
        "silent nod toward the toaster",
        "",
        "remain quiet until scene ends",
        12.0,
        True,
    ),
    SceneId.HR_ROAST_SKIT: SceneScript(
        "two toasts face each other like a standup",
        "deadpan stare, no bounce",
        "one toast turns to the toaster as if reporting",
        "caption only if speech is allowed; otherwise a bow",
        "office_flat_white callback puff",
        "both walk off professionally",
        12.0,
        False,
    ),
    SceneId.RICE_ZEN: SceneScript(
        "rice cooker opens, steam particle",
        "rice spirit rises and hovers",
        "nearby toasts sit and stop hopping",
        "spirit bows, steam fades",
        "",
        "spirit lingers then hides",
        14.0,
    ),
    SceneId.LATE_NIGHT_SLEEP: SceneScript(
        "toasts melt into sleep poses",
        "eyes close, hop stops",
        "one toast snores visually (squash pulse)",
        "all flatten, toaster heat dims",
        "",
        "stay asleep until interrupted",
        16.0,
    ),
    SceneId.POPCORN_BRAIN: SceneScript(
        "kernels spawn near the toaster",
        "toasts sit as audience",
        "popcorn pops in a short burst",
        "audience hop on the last pop",
        "heart particle if escalation >= 3",
        "kernels despawn, toasts wander",
        14.0,
    ),
    SceneId.PORTAL_GLITCH: SceneScript(
        "violet ring appears at the slot",
        "one toast inspects it professionally",
        "toast steps half-in, smear stretch",
        "toast vanishes, echo remains",
        "portal close chime only if not focused",
        "echo fades, remaining toasts resume",
        14.0,
    ),
    SceneId.SECRET_CHAMBER: SceneScript(
        "chamber glyph lights under the toaster",
        "toast circles the glyph",
        "glyph widens, interior larger than box",
        "toast peeks in, then backs out as if that was policy",
        "butter blob if callback present",
        "glyph shrinks",
        14.0,
    ),
    SceneId.MICROWAVE_ANOMALY: SceneScript(
        "microwave door ticks open",
        "star spin particle in the cavity",
        "toast salutes the star",
        "door clicks shut, star collapses",
        "microwave ping if not focused",
        "appliance cools",
        12.0,
    ),
    SceneId.KPOP_HEART_MODE: SceneScript(
        "toasts line up",
        "first toast hops on a beat",
        "line copies the hop one by one",
        "heart burst, then immediate professional sit",
        "",
        "line breaks",
        12.0,
    ),
    SceneId.AIRFRYER_VORTEX: SceneScript(
        "air fryer lid lifts",
        "crumbs orbit the basket",
        "one toast leans into the wind, hairless but committed",
        "vortex dumps crumbs, toast steps back unimpressed",
        "",
        "lid closes",
        12.0,
    ),
    SceneId.TOAST_INFESTATION: SceneScript(
        "extra toast multiplies once",
        "crowd packs the toaster flank",
        "wave hop travels through the crowd",
        "crowd freezes as if caught in a meeting",
        "",
        "extras begin despawn",
        16.0,
    ),
    SceneId.BURN_RECOVERY: SceneScript(
        "burnt toast stands still",
        "others form a respectful gap",
        "burnt toast cools, color holds",
        "recover pose: one professional stretch",
        "",
        "return to wander darker",
        12.0,
    ),
    SceneId.FUNERAL_RITUAL: SceneScript(
        "burnt toast centered",
        "others form a line",
        "slow procession around the toaster",
        "line stops, one crumb placed",
        "",
        "procession dissolves",
        16.0,
    ),
    SceneId.SHAREWARE_APOCALYPSE: SceneScript(
        "swarm path seeds",
        "toasts bounce the screen edge",
        "fake hammer squash on one toast",
        "all freeze mid-bounce, then sit as if nothing happened",
        "",
        "chaos mode off",
        16.0,
    ),
    SceneId.KEYBOARD_FEEDING_FRENZY: SceneScript(
        "toasts gather near cursor orbit",
        "one toast hops on cadence",
        "combo pop of a crumb",
        "remaining toasts sit immediately",
        "",
        "scatter",
        8.0,
    ),
    SceneId.RETURN_TO_CALM: SceneScript(
        "all theatrical FX stop",
        "opacity restores to work-quiet",
        "toasts walk to edges",
        "desktop is boring again",
        "",
        "idle wander",
        8.0,
    ),
}


def _level(escalation: float) -> int:
    return max(0, min(5, int(round(escalation * 5))))


@dataclass
class PerformanceState:
    scene: SceneId = SceneId.QUIET_COMPANIONSHIP
    beat: SceneBeat = SceneBeat.SETUP
    started: float = 0.0
    beat_t: float = 0.0
    duration: float = 0.0
    level: int = 1
    payoff_reached: bool = False
    interrupted: bool = False
    completed: bool = False
    beats: list[str] = field(default_factory=list)
    visual: str = ""
    caption: str = ""

    def snapshot(self) -> dict:
        return {
            "scene": self.scene.value,
            "beat": self.beat.value,
            "duration": round(self.duration, 3),
            "level": self.level,
            "payoff": self.payoff_reached,
            "interrupted": self.interrupted,
            "completed": self.completed,
            "beats": list(self.beats),
            "visual": self.visual,
        }


class ScenePerformance:
    def __init__(self) -> None:
        self.state = PerformanceState()
        self.started = 0
        self.completed = 0
        self.interrupted = 0
        self.payoffs = 0
        self.transitions = 0
        self.durations: list[float] = []

    def start(self, scene: SceneId, t: float, escalation: float) -> None:
        if self.state.beat not in {SceneBeat.COMPLETED, SceneBeat.INTERRUPTED} and self.state.started:
            self.interrupted += 1
        script = SCRIPTS[scene]
        self.state = PerformanceState(
            scene=scene,
            beat=SceneBeat.SETUP,
            started=t,
            beat_t=t,
            level=_level(escalation),
            visual=script.setup,
            beats=["setup"],
        )
        self.started += 1
        self.transitions += 1

    def interrupt(self, t: float) -> None:
        if self.state.completed or self.state.interrupted:
            return
        self.state.interrupted = True
        self.state.beat = SceneBeat.INTERRUPTED
        self.state.duration = t - self.state.started
        self.interrupted += 1
        self.durations.append(self.state.duration)
        self.state.visual = "settle cleanly; work wins"

    def tick(self, world, dt: float, t: float) -> PerformanceState:
        st = self.state
        if st.completed or st.interrupted:
            return st
        script = SCRIPTS.get(st.scene)
        if script is None:
            return st
        st.duration = t - st.started
        elapsed = t - st.beat_t
        meta = SCENE_META.get(st.scene, {"setup": 1.5, "delay": 1.0, "payoff": 6.0})
        if st.beat is SceneBeat.SETUP and elapsed >= meta["setup"]:
            self._advance(st, SceneBeat.BEAT1, script.beat1, t)
        elif st.beat is SceneBeat.BEAT1 and elapsed >= max(0.8, meta["delay"] or 1.2):
            self._advance(st, SceneBeat.BEAT2, script.beat2, t)
        elif st.beat is SceneBeat.BEAT2 and elapsed >= 1.2:
            self._advance(st, SceneBeat.PAYOFF, script.payoff, t)
            st.payoff_reached = True
            self.payoffs += 1
        elif st.beat is SceneBeat.PAYOFF and elapsed >= max(1.4, meta["payoff"] * 0.25):
            self._advance(st, SceneBeat.SETTLE, script.exit, t)
        elif st.beat is SceneBeat.SETTLE and elapsed >= 0.8:
            st.completed = True
            st.beat = SceneBeat.COMPLETED
            self.completed += 1
            self.durations.append(st.duration)
        if st.duration >= script.max_duration and not st.completed:
            st.completed = True
            st.beat = SceneBeat.COMPLETED
            if not st.payoff_reached:
                st.payoff_reached = True
                self.payoffs += 1
            self.completed += 1
            self.durations.append(st.duration)
        self._stage(world, st)
        return st

    def _advance(self, st: PerformanceState, beat: SceneBeat, visual: str, t: float) -> None:
        st.beat = beat
        st.beat_t = t
        st.visual = visual
        st.beats.append(beat.value)
        self.transitions += 1

    def _stage(self, world, st: PerformanceState) -> None:
        living = [e for e in world.entities if not e.hidden]
        n = max(1, 1 + st.level)

        def stage_enter(entity, dest: ToastState) -> None:
            world.behavior.enter(entity, dest, world.clock, "scene")

        if st.scene is SceneId.QUIET_COMPANIONSHIP:
            for entity in living[:n]:
                entity.opacity = 0.55 if st.level <= 1 else 0.8
                if st.beat in {SceneBeat.BEAT1, SceneBeat.BEAT2, SceneBeat.PAYOFF}:
                    stage_enter(entity, ToastState.OBSERVE_TOASTER)
        elif st.scene is SceneId.TOAST_WATCH_PARTY:
            for i, entity in enumerate(living[: min(6, 2 + st.level)]):
                entity.target = world.toaster + Vec2(-70 + i * 22, 50)
                if st.beat is SceneBeat.PAYOFF:
                    stage_enter(entity, ToastState.DANCE)
                else:
                    stage_enter(entity, ToastState.WATCH_VIDEO)
        elif st.scene is SceneId.TOAST_WARMUP_RITUAL and st.beat in {SceneBeat.SETUP, SceneBeat.BEAT1}:
            for entity in living[:2]:
                if entity.state in {ToastState.IDLE, ToastState.WANDER}:
                    stage_enter(entity, ToastState.APPROACH_TOASTER)
        elif st.scene is SceneId.CODING_FLOW_AUDIENCE:
            for entity in living[:n]:
                if entity.state in {ToastState.IDLE, ToastState.WANDER, ToastState.OBSERVE_TOASTER, ToastState.SOCIALIZE}:
                    entity.opacity = 0.28 if st.level <= 1 else 0.4
                    stage_enter(entity, ToastState.WORK_QUIET_MODE)
        elif st.scene is SceneId.HR_ROAST_SKIT:
            for entity in living[:2]:
                stage_enter(entity, ToastState.SOCIALIZE if st.beat is not SceneBeat.PAYOFF else ToastState.OBSERVE_TOASTER)
            intent = world.narrative.active.intent if world.narrative.active else None
            if st.beat is SceneBeat.PAYOFF and st.level >= 2 and not world.caption and intent is not None and not intent.visual_only:
                world.caption = "Commit prošel. Culture fit stále čeká na stakeholder approval."
                world.caption_until = world.clock + 3.2
        elif st.scene is SceneId.KEYBOARD_FEEDING_FRENZY:
            for entity in living[: min(4, 1 + st.level)]:
                if st.beat in {SceneBeat.SETUP, SceneBeat.BEAT1}:
                    stage_enter(entity, ToastState.FOLLOW_CURSOR_AT_DISTANCE)
                elif st.beat is SceneBeat.BEAT2:
                    stage_enter(entity, ToastState.POP_OUT)
                else:
                    stage_enter(entity, ToastState.IDLE)
        elif st.scene is SceneId.RICE_ZEN:
            for entity in living[:n]:
                stage_enter(entity, ToastState.IDLE)
                entity.vel = Vec2()
        elif st.scene is SceneId.LATE_NIGHT_SLEEP:
            for entity in living:
                stage_enter(entity, ToastState.SLEEP)
        elif st.scene is SceneId.POPCORN_BRAIN:
            for entity in living[: min(6, 2 + st.level)]:
                stage_enter(entity, ToastState.POPCORN_AUDIENCE)
            if st.beat is SceneBeat.BEAT2:
                world._burst(world.toaster + Vec2(20, 30), "crumb", 3 + st.level)
        elif st.scene is SceneId.PORTAL_GLITCH and living:
            if st.beat in {SceneBeat.BEAT2, SceneBeat.PAYOFF}:
                stage_enter(living[0], ToastState.PORTAL_CURIOUS)
        elif st.scene is SceneId.SECRET_CHAMBER and living:
            if st.beat is SceneBeat.BEAT1 and living[0].state not in {
                ToastState.PORTAL_CURIOUS,
                ToastState.PORTAL_ENTER,
                ToastState.INSIDE_APPLIANCE,
                ToastState.PORTAL_RETURN,
            }:
                stage_enter(living[0], ToastState.OBSERVE_TOASTER)
        elif st.scene is SceneId.MICROWAVE_ANOMALY and st.beat is SceneBeat.BEAT2:
            world._burst(world.toaster + Vec2(-90, 40), "crumb", 2)
        elif st.scene is SceneId.KPOP_HEART_MODE:
            for entity in living[: min(5, 1 + st.level)]:
                stage_enter(entity, ToastState.HEART_MODE if st.beat is not SceneBeat.SETTLE else ToastState.IDLE)
            if st.beat is SceneBeat.PAYOFF:
                world._burst(world.toaster + Vec2(0, -20), "heart", 2 + st.level)
        elif st.scene is SceneId.AIRFRYER_VORTEX and st.beat in {SceneBeat.BEAT1, SceneBeat.BEAT2}:
            world._burst(world.toaster + Vec2(-40, 70), "crumb", 2 + st.level)
        elif st.scene is SceneId.TOAST_INFESTATION and st.beat is SceneBeat.BEAT2:
            for entity in living[:4]:
                stage_enter(entity, ToastState.DANCE)
        elif st.scene is SceneId.BURN_RECOVERY:
            for entity in living:
                if entity.burnt and st.beat is SceneBeat.PAYOFF:
                    stage_enter(entity, ToastState.RECOVER)
        elif st.scene is SceneId.FUNERAL_RITUAL:
            for entity in living:
                if entity.burnt:
                    stage_enter(entity, ToastState.RITUAL_FUNERAL)
                elif st.beat in {SceneBeat.BEAT1, SceneBeat.BEAT2, SceneBeat.PAYOFF}:
                    stage_enter(entity, ToastState.OBSERVE_TOASTER)
        elif st.scene is SceneId.SHAREWARE_APOCALYPSE and st.beat is SceneBeat.PAYOFF:
            for entity in living[:1]:
                entity.pose.squash_y = 0.45
                entity.pose.squash_x = 1.4
        elif st.scene is SceneId.RETURN_TO_CALM:
            for entity in living:
                if entity.state not in {ToastState.AVOID_CURSOR, ToastState.PANIC_RUN, ToastState.HIDE}:
                    entity.opacity = min(entity.opacity, 0.5)

    def snapshot(self) -> dict:
        durs = self.durations or [self.state.duration]
        return {
            "current": self.state.snapshot(),
            "started": self.started,
            "completed": self.completed,
            "interrupted": self.interrupted,
            "payoffs": self.payoffs,
            "transitions": self.transitions,
            "avg_duration": round(sum(durs) / len(durs), 3),
            "max_duration": round(max(durs), 3),
        }
