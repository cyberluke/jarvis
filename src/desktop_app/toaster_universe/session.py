"""Long-session retention. Fatigue, arcs, rarity, silence. No new scene IDs."""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
from enum import Enum

from .catalog import LORE_KEYS
from .types import ActivityState, SceneId, SceneIntent


RARE_SCENES = frozenset(
    {
        SceneId.FUNERAL_RITUAL,
        SceneId.SHAREWARE_APOCALYPSE,
        SceneId.PORTAL_GLITCH,
        SceneId.SECRET_CHAMBER,
    }
)
THEATRICAL_SCENES = frozenset(
    {
        SceneId.SHAREWARE_APOCALYPSE,
        SceneId.TOAST_INFESTATION,
        SceneId.FUNERAL_RITUAL,
        SceneId.AIRFRYER_VORTEX,
        SceneId.PORTAL_GLITCH,
        SceneId.KPOP_HEART_MODE,
    }
)
CALLBACK_SETUP = {
    "THE_HONZA": SceneId.HR_ROAST_SKIT,
    "THE_BRNO_TESTER": SceneId.CODING_FLOW_AUDIENCE,
    "THE_TOOLLESS_AI_EXPERT": SceneId.TOAST_WARMUP_RITUAL,
    "THE_ARCHITECT_OF_NOTHING": SceneId.SECRET_CHAMBER,
    "THE_FLAT_WHITE_HR": SceneId.HR_ROAST_SKIT,
    "THE_ALIGNMENT_MANAGER": SceneId.CODING_FLOW_AUDIENCE,
}
RARE_MIN_SEC = {
    SceneId.FUNERAL_RITUAL: 3 * 3600,
    SceneId.SHAREWARE_APOCALYPSE: 4 * 3600,
    SceneId.PORTAL_GLITCH: 45 * 60,
    SceneId.SECRET_CHAMBER: 35 * 60,
}
RARE_MIN_SCENES = {
    SceneId.FUNERAL_RITUAL: 24,
    SceneId.SHAREWARE_APOCALYPSE: 28,
    SceneId.PORTAL_GLITCH: 10,
    SceneId.SECRET_CHAMBER: 8,
}
RARE_NOVELTY_COST = {
    SceneId.FUNERAL_RITUAL: 0.55,
    SceneId.SHAREWARE_APOCALYPSE: 0.7,
    SceneId.PORTAL_GLITCH: 0.4,
    SceneId.SECRET_CHAMBER: 0.35,
}


class SessionArc(str, Enum):
    CALM = "calm"
    ALIVE = "alive"
    PLAYFUL = "playful"
    THEATRICAL = "theatrical"
    RECOVERY = "recovery"


ARC_PREF = {
    SessionArc.CALM: {
        SceneId.QUIET_COMPANIONSHIP,
        SceneId.CODING_FLOW_AUDIENCE,
        SceneId.RICE_ZEN,
        SceneId.LATE_NIGHT_SLEEP,
        SceneId.RETURN_TO_CALM,
    },
    SessionArc.ALIVE: {
        SceneId.TOAST_WARMUP_RITUAL,
        SceneId.TOAST_WATCH_PARTY,
        SceneId.HR_ROAST_SKIT,
        SceneId.MICROWAVE_ANOMALY,
        SceneId.QUIET_COMPANIONSHIP,
    },
    SessionArc.PLAYFUL: {
        SceneId.POPCORN_BRAIN,
        SceneId.KPOP_HEART_MODE,
        SceneId.TOAST_WATCH_PARTY,
        SceneId.TOAST_INFESTATION,
        SceneId.HR_ROAST_SKIT,
    },
    SessionArc.THEATRICAL: {
        SceneId.SHAREWARE_APOCALYPSE,
        SceneId.PORTAL_GLITCH,
        SceneId.AIRFRYER_VORTEX,
        SceneId.FUNERAL_RITUAL,
        SceneId.SECRET_CHAMBER,
    },
    SessionArc.RECOVERY: {
        SceneId.BURN_RECOVERY,
        SceneId.RETURN_TO_CALM,
        SceneId.QUIET_COMPANIONSHIP,
        SceneId.RICE_ZEN,
    },
}


@dataclass
class SessionMemory:
    scenes: deque[str] = field(default_factory=lambda: deque(maxlen=32))
    tones: deque[str] = field(default_factory=lambda: deque(maxlen=16))
    callbacks: deque[str] = field(default_factory=lambda: deque(maxlen=16))
    participants: deque[int] = field(default_factory=lambda: deque(maxlen=16))
    payoffs: deque[str] = field(default_factory=lambda: deque(maxlen=16))
    counts: Counter[str] = field(default_factory=Counter)
    last_used: dict[str, float] = field(default_factory=dict)
    interrupts: Counter[str] = field(default_factory=Counter)
    attention: deque[str] = field(default_factory=lambda: deque(maxlen=16))
    setup_seen: set[str] = field(default_factory=set)
    callback_count: Counter[str] = field(default_factory=Counter)
    last_callback: dict[str, float] = field(default_factory=dict)
    last_theatrical: float = -1e9
    last_rare: dict[str, float] = field(default_factory=dict)
    last_rare_index: dict[str, int] = field(default_factory=dict)
    started: int = 0
    silence: int = 0
    fatigue_suppressions: int = 0
    novelty_suppressions: int = 0
    focus_suppressions: int = 0
    interruptions: int = 0
    theatrical_gaps: list[float] = field(default_factory=list)
    rare_gaps: list[float] = field(default_factory=list)
    arc: SessionArc = SessionArc.CALM
    last_silence: float = -1e9

    def compact(self) -> dict:
        return {
            "acc": list(self.scenes)[-8:],
            "ton": list(self.tones)[-6:],
            "cb": list(self.callbacks)[-6:],
            "part": list(self.participants)[-6:],
            "pay": list(self.payoffs)[-6:],
            "arc": self.arc.value,
            "n": dict(self.counts),
        }


class SessionPacing:
    def __init__(self) -> None:
        self.mem = SessionMemory()

    def note_attention(self, activity: ActivityState, focus: float) -> None:
        label = "focus" if focus >= 0.55 or activity in {ActivityState.CODING_FLOW, ActivityState.FOCUSED, ActivityState.FRANTIC} else activity.value.lower()
        self.mem.attention.append(label)

    def note_interrupt(self, scene: SceneId) -> None:
        self.mem.interrupts[scene.value] += 1
        self.mem.interruptions += 1

    def note_setup(self, scene: SceneId) -> None:
        for key, needed in CALLBACK_SETUP.items():
            if scene is needed:
                self.mem.setup_seen.add(key)

    def note_accept(self, intent: SceneIntent, t: float, payoff: str = "") -> None:
        sid = intent.scene.value
        self.mem.scenes.append(sid)
        self.mem.tones.append(intent.tone.value)
        self.mem.participants.append(intent.participants)
        self.mem.payoffs.append(payoff or sid)
        self.mem.counts[sid] += 1
        self.mem.last_used[sid] = t
        self.mem.started += 1
        self.note_setup(intent.scene)
        if intent.callback_key:
            self.mem.callbacks.append(intent.callback_key)
            self.mem.callback_count[intent.callback_key] += 1
            self.mem.last_callback[intent.callback_key] = t
        if intent.scene in THEATRICAL_SCENES:
            if self.mem.last_theatrical > -1e8:
                self.mem.theatrical_gaps.append(t - self.mem.last_theatrical)
            self.mem.last_theatrical = t
        if intent.scene in RARE_SCENES:
            prev = self.mem.last_rare.get(sid, -1e9)
            if prev > -1e8:
                self.mem.rare_gaps.append(t - prev)
            self.mem.last_rare[sid] = t
            self.mem.last_rare_index[sid] = self.mem.started

    def note_silence(self, t: float) -> None:
        self.mem.silence += 1
        self.mem.last_silence = t

    def update_arc(self, t: float, activity: ActivityState, novelty: float, burnt: bool) -> SessionArc:
        recent = list(self.mem.scenes)[-8:]
        theatrical_recent = sum(1 for s in recent if s in {x.value for x in THEATRICAL_SCENES})
        if burnt:
            arc = SessionArc.RECOVERY
        elif activity in {ActivityState.CODING_FLOW, ActivityState.FOCUSED, ActivityState.FRANTIC}:
            arc = SessionArc.CALM
        elif activity is ActivityState.LATE_NIGHT:
            arc = SessionArc.CALM if novelty < 0.7 else SessionArc.ALIVE
        elif theatrical_recent >= 2 or t - self.mem.last_theatrical < 180:
            arc = SessionArc.RECOVERY
        elif activity is ActivityState.DOOMSCROLLING:
            arc = SessionArc.PLAYFUL
        elif novelty > 0.78 and activity is ActivityState.IDLE and t - self.mem.last_theatrical > 900:
            arc = SessionArc.THEATRICAL
        elif activity in {ActivityState.BROWSING, ActivityState.IDLE} and novelty > 0.4:
            arc = SessionArc.ALIVE if len(recent) % 5 < 3 else SessionArc.PLAYFUL
        else:
            arc = SessionArc.ALIVE
        # Prevent theatrical clustering and calm lock-in.
        if arc is SessionArc.THEATRICAL and t - self.mem.last_theatrical < 600:
            arc = SessionArc.ALIVE
        if arc is SessionArc.CALM and activity is ActivityState.IDLE and novelty > 0.55 and recent.count(SceneId.QUIET_COMPANIONSHIP.value) >= 4:
            arc = SessionArc.ALIVE
        self.mem.arc = arc
        return arc

    def fatigue(self, scene: SceneId, t: float, tone: str = "", participants: int | None = None) -> float:
        sid = scene.value
        recent = list(self.mem.scenes)
        rec16 = recent[-16:]
        freq = rec16.count(sid)
        age = t - self.mem.last_used.get(sid, -1e9)
        score = 0.0
        score += min(1.4, freq * 0.28)
        if age < 90:
            score += 0.7 * (1.0 - age / 90.0)
        elif age < 240:
            score += 0.25
        if tone and list(self.mem.tones)[-6:].count(tone) >= 4:
            score += 0.2
        if participants is not None and list(self.mem.participants)[-6:].count(participants) >= 5:
            score += 0.15
        if list(self.mem.payoffs)[-8:].count(sid) >= 3:
            score += 0.2
        score += min(0.4, self.mem.interrupts[sid] * 0.12)
        if scene in RARE_SCENES:
            score += 0.15
        return score

    def rare_ok(self, scene: SceneId, t: float, novelty: float) -> bool:
        if scene not in RARE_SCENES:
            return True
        min_t = RARE_MIN_SEC[scene]
        last_t = self.mem.last_rare.get(scene.value, -1e9)
        if t - last_t < min_t:
            return False
        last_i = self.mem.last_rare_index.get(scene.value, -10_000)
        if self.mem.started - last_i < RARE_MIN_SCENES[scene]:
            return False
        if novelty < RARE_NOVELTY_COST[scene] + 0.15:
            self.mem.novelty_suppressions += 1
            return False
        return True

    def callback_ok(self, key: str, t: float) -> bool:
        if not key:
            return True
        if key not in LORE_KEYS:
            return False
        if key not in self.mem.setup_seen:
            return False
        if self.mem.callback_count[key] >= 2 and t - self.mem.last_callback.get(key, -1e9) < 2400:
            return False
        if t - self.mem.last_callback.get(key, -1e9) < 600:
            return False
        # Harassment guard: never fire the same private alias more than twice per session.
        if self.mem.callback_count[key] >= 2:
            return False
        return True

    def want_silence(
        self,
        t: float,
        activity: ActivityState,
        focus: float,
        novelty: float,
        eligible_fatigue: float,
    ) -> bool:
        focus_heavy = focus >= 0.55 or activity in {ActivityState.CODING_FLOW, ActivityState.FOCUSED, ActivityState.FRANTIC, ActivityState.TERMINAL_HEAVY}
        if focus_heavy and focus > 0.72:
            self.mem.focus_suppressions += 1
            return True
        if activity is ActivityState.FRANTIC:
            return True
        if novelty < 0.28 and activity in {ActivityState.IDLE, ActivityState.LATE_NIGHT}:
            return True
        if eligible_fatigue >= 0.95:
            self.mem.fatigue_suppressions += 1
            return True
        if self.mem.interruptions >= 4 and self.mem.started and self.mem.interruptions / max(1, self.mem.started) > 0.35:
            return True
        recent_same = False
        acc = list(self.mem.scenes)
        if len(acc) >= 3 and acc[-1] == acc[-2] == acc[-3]:
            recent_same = True
        if recent_same:
            return True
        if activity is ActivityState.LATE_NIGHT and novelty < 0.55:
            return True
        if t - self.mem.last_silence < 25 and not focus_heavy:
            return False
        # Natural breathing room after a scene.
        last = max(self.mem.last_used.values(), default=-1e9)
        if 0 < t - last < 18:
            return True
        return False

    def mature_callbacks(self) -> list[str]:
        return [k for k in LORE_KEYS if k in self.mem.setup_seen]

    def snapshot(self, hours: float = 0.0) -> dict:
        mem = self.mem
        scenes = list(mem.scenes)
        tones = list(mem.tones)
        parts = list(mem.participants)
        pays = list(mem.payoffs)
        scene_n = Counter(scenes)
        tone_n = Counter(tones)
        total = max(1, mem.started)
        consec = 0
        for i in range(1, len(scenes)):
            if scenes[i] == scenes[i - 1]:
                consec += 1
        pay_rep = 0
        for i in range(1, len(pays)):
            if pays[i] == pays[i - 1]:
                pay_rep += 1
        part_rep = 0
        for i in range(1, len(parts)):
            if parts[i] == parts[i - 1]:
                part_rep += 1
        top_scene = scene_n.most_common(1)[0][1] / total if scene_n else 0.0
        top_tone = tone_n.most_common(1)[0][1] / max(1, len(tones)) if tones else 0.0
        return {
            "SIMULATED_HOURS": hours,
            "TOTAL_SCENES": mem.started,
            "SILENCE_DECISIONS": mem.silence,
            "SCENES_PER_HOUR": round(mem.started / hours, 3) if hours else mem.started,
            "SCENE_DISTRIBUTION": dict(scene_n),
            "TONE_DISTRIBUTION": dict(tone_n),
            "TOP_SCENE_SHARE": round(top_scene, 4),
            "TOP_TONE_SHARE": round(top_tone, 4),
            "RARE_SCENE_COUNTS": {k: mem.counts[k] for k in ("funeral_ritual", "shareware_apocalypse", "portal_glitch", "secret_chamber") if mem.counts[k]},
            "THEATRICAL_INTERVAL_AVG": round(sum(mem.theatrical_gaps) / len(mem.theatrical_gaps), 2) if mem.theatrical_gaps else None,
            "RARE_INTERVAL_AVG": round(sum(mem.rare_gaps) / len(mem.rare_gaps), 2) if mem.rare_gaps else None,
            "CONSECUTIVE_REPEAT_RATE": round(consec / max(1, len(scenes) - 1), 4) if len(scenes) > 1 else 0.0,
            "PAYOFF_REPEAT_RATE": round(pay_rep / max(1, len(pays) - 1), 4) if len(pays) > 1 else 0.0,
            "PARTICIPANT_REPEAT_RATE": round(part_rep / max(1, len(parts) - 1), 4) if len(parts) > 1 else 0.0,
            "CALLBACK_REUSE": dict(mem.callback_count),
            "FATIGUE_SUPPRESSIONS": mem.fatigue_suppressions,
            "NOVELTY_SUPPRESSIONS": mem.novelty_suppressions,
            "FOCUS_SUPPRESSIONS": mem.focus_suppressions,
            "INTERRUPTION_RATE": round(mem.interruptions / total, 4),
            "ARC": mem.arc.value,
        }
