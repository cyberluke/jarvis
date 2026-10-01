"""LLM is a director, never a physics engine.

Outbound SceneIntent only. World tick never waits on I/O.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from .catalog import CATALOG, CATEGORIES, LORE_KEYS
from .config import WorldConfig
from .session import ARC_PREF, RARE_SCENES, SessionPacing
from .signals import UserSignals
from .types import (
    ActivityState,
    ApplianceKind,
    ComedyStyle,
    EntityKind,
    SceneId,
    SceneIntent,
    SafetyZone,
)


ALLOWED_SCENES = {item.value for item in SceneId}
ALLOWED_TONES = {item.value.lower(): item for item in ComedyStyle}
TONE_ALIASES = {
    "deadpan_cute": ComedyStyle.CUTE_KAWAII,
    "cute": ComedyStyle.CUTE_KAWAII,
    "technical_prague_deadpan": ComedyStyle.TECHNICAL_PRAGUE_DEADPAN,
    "quiet": ComedyStyle.QUIET_VISUAL_ONLY,
    "chaos": ComedyStyle.SHAREWARE_CHAOS,
    "office": ComedyStyle.OFFICE_SATIRE,
    "meta": ComedyStyle.META_AI,
}
ALLOWED_SPECIES = {item.value for item in EntityKind}
ALLOWED_APPLIANCES = {item.value for item in ApplianceKind}
ALLOWED_ZONES = {item.value.lower() for item in SafetyZone}
ALLOWED_CALLBACKS = {e.key for e in CATALOG} | set(LORE_KEYS)
ALLOWED_CATEGORIES = set(CATEGORIES) | {"personal_lore"}

PRIVACY_FORBIDDEN = frozenset(
    {
        "typed",
        "text",
        "clipboard",
        "terminal",
        "editor",
        "browser",
        "filename",
        "document",
        "key",
        "char",
        "vk",
        "path",
        "url",
        "title",
    }
)


def _log(message: str) -> None:
    """Director engagement trace. Always lands in the rolling runtime log;
    console (stderr) is on by default — via voice_debug when that is enabled,
    otherwise via the director flag (JARVIS_DIRECTOR_DEBUG, default 1).
    Set JARVIS_DIRECTOR_DEBUG=0 to silence the flag path only."""
    mirrored = False
    try:
        from jarvis.debug import _is_debug_enabled, debug_log

        debug_log(message, "director")
        # voice_debug already mirrors the category to stderr; avoid double lines.
        mirrored = _is_debug_enabled()
    except Exception:
        pass
    if not mirrored and os.environ.get("JARVIS_DIRECTOR_DEBUG", "1") != "0":
        try:
            print(f"[director] {message}", file=sys.stderr)
        except Exception:
            pass

AMBIENT_SCENES = frozenset(
    {
        SceneId.QUIET_COMPANIONSHIP,
        SceneId.TOAST_WATCH_PARTY,
        SceneId.CODING_FLOW_AUDIENCE,
        SceneId.HR_ROAST_SKIT,
        SceneId.LATE_NIGHT_SLEEP,
        SceneId.RICE_ZEN,
        SceneId.BURN_RECOVERY,
        SceneId.POPCORN_BRAIN,
    }
)
LONG_FORM_SCENES = frozenset(
    {
        SceneId.PORTAL_GLITCH,
        SceneId.SECRET_CHAMBER,
        SceneId.SHAREWARE_APOCALYPSE,
        SceneId.FUNERAL_RITUAL,
        SceneId.MICROWAVE_ANOMALY,
        SceneId.AIRFRYER_VORTEX,
        SceneId.TOAST_INFESTATION,
        SceneId.TOAST_WARMUP_RITUAL,
    }
)
IMMEDIATE_SCENES = frozenset({SceneId.RETURN_TO_CALM, SceneId.KEYBOARD_FEEDING_FRENZY})

# Contextual eligible sets — actual catalog IDs only.
ELIGIBLE = {
    "idle": (
        SceneId.QUIET_COMPANIONSHIP,
        SceneId.TOAST_WATCH_PARTY,
        SceneId.TOAST_WARMUP_RITUAL,
        SceneId.PORTAL_GLITCH,
        SceneId.SECRET_CHAMBER,
        SceneId.HR_ROAST_SKIT,
        SceneId.KEYBOARD_FEEDING_FRENZY,
        SceneId.RETURN_TO_CALM,
    ),
    "coding_flow": (
        SceneId.QUIET_COMPANIONSHIP,
        SceneId.CODING_FLOW_AUDIENCE,
        SceneId.HR_ROAST_SKIT,
        SceneId.TOAST_WARMUP_RITUAL,
    ),
    "terminal_heavy": (
        SceneId.CODING_FLOW_AUDIENCE,
        SceneId.HR_ROAST_SKIT,
        SceneId.TOAST_WARMUP_RITUAL,
        SceneId.MICROWAVE_ANOMALY,
    ),
    "focused": (
        SceneId.QUIET_COMPANIONSHIP,
        SceneId.CODING_FLOW_AUDIENCE,
        SceneId.TOAST_WARMUP_RITUAL,
        SceneId.KEYBOARD_FEEDING_FRENZY,
    ),
    "browsing": (
        SceneId.TOAST_WATCH_PARTY,
        SceneId.POPCORN_BRAIN,
        SceneId.HR_ROAST_SKIT,
        SceneId.QUIET_COMPANIONSHIP,
    ),
    "doomscrolling": (
        SceneId.TOAST_WATCH_PARTY,
        SceneId.POPCORN_BRAIN,
        SceneId.KPOP_HEART_MODE,
        SceneId.HR_ROAST_SKIT,
    ),
    "late_night": (
        SceneId.RICE_ZEN,
        SceneId.LATE_NIGHT_SLEEP,
        SceneId.PORTAL_GLITCH,
        SceneId.QUIET_COMPANIONSHIP,
        SceneId.SECRET_CHAMBER,
    ),
    "frantic": (SceneId.RETURN_TO_CALM, SceneId.QUIET_COMPANIONSHIP, SceneId.KEYBOARD_FEEDING_FRENZY),
    "social": (
        SceneId.TOAST_WATCH_PARTY,
        SceneId.TOAST_INFESTATION,
        SceneId.KPOP_HEART_MODE,
        SceneId.HR_ROAST_SKIT,
        SceneId.POPCORN_BRAIN,
    ),
    "appliance": (
        SceneId.TOAST_WARMUP_RITUAL,
        SceneId.MICROWAVE_ANOMALY,
        SceneId.RICE_ZEN,
        SceneId.AIRFRYER_VORTEX,
        SceneId.SECRET_CHAMBER,
    ),
    "rare": (
        SceneId.PORTAL_GLITCH,
        SceneId.SHAREWARE_APOCALYPSE,
        SceneId.FUNERAL_RITUAL,
        SceneId.SECRET_CHAMBER,
        SceneId.AIRFRYER_VORTEX,
    ),
    "burn": (SceneId.BURN_RECOVERY, SceneId.FUNERAL_RITUAL, SceneId.TOAST_WARMUP_RITUAL),
}

SCENE_META = {
    SceneId.QUIET_COMPANIONSHIP: {"cd": 18.0, "max30": 6, "esc_max": 1, "rarity": "common", "setup": 2.0, "delay": 0.0, "payoff": 8.0, "cb": False},
    SceneId.TOAST_WARMUP_RITUAL: {"cd": 24.0, "max30": 4, "esc_max": 2, "rarity": "common", "setup": 1.5, "delay": 1.0, "payoff": 6.0, "cb": False},
    SceneId.BURN_RECOVERY: {"cd": 40.0, "max30": 2, "esc_max": 2, "rarity": "uncommon", "setup": 2.0, "delay": 1.5, "payoff": 8.0, "cb": True},
    SceneId.TOAST_WATCH_PARTY: {"cd": 22.0, "max30": 4, "esc_max": 3, "rarity": "common", "setup": 2.0, "delay": 2.0, "payoff": 10.0, "cb": True},
    SceneId.CODING_FLOW_AUDIENCE: {"cd": 20.0, "max30": 5, "esc_max": 1, "rarity": "common", "setup": 1.0, "delay": 0.0, "payoff": 12.0, "cb": False},
    SceneId.HR_ROAST_SKIT: {"cd": 36.0, "max30": 3, "esc_max": 2, "rarity": "uncommon", "setup": 2.0, "delay": 1.0, "payoff": 8.0, "cb": True},
    SceneId.TOAST_INFESTATION: {"cd": 80.0, "max30": 1, "esc_max": 4, "rarity": "rare", "setup": 3.0, "delay": 2.0, "payoff": 12.0, "cb": True},
    SceneId.KEYBOARD_FEEDING_FRENZY: {"cd": 50.0, "max30": 1, "esc_max": 3, "rarity": "uncommon", "setup": 0.5, "delay": 0.0, "payoff": 5.0, "cb": False},
    SceneId.POPCORN_BRAIN: {"cd": 28.0, "max30": 3, "esc_max": 3, "rarity": "uncommon", "setup": 2.0, "delay": 2.5, "payoff": 10.0, "cb": True},
    SceneId.KPOP_HEART_MODE: {"cd": 40.0, "max30": 2, "esc_max": 3, "rarity": "uncommon", "setup": 1.0, "delay": 1.0, "payoff": 7.0, "cb": True},
    SceneId.LATE_NIGHT_SLEEP: {"cd": 30.0, "max30": 4, "esc_max": 1, "rarity": "common", "setup": 3.0, "delay": 0.0, "payoff": 14.0, "cb": False},
    SceneId.FUNERAL_RITUAL: {"cd": 180.0, "max30": 1, "esc_max": 4, "rarity": "very_rare", "setup": 2.0, "delay": 1.5, "payoff": 8.0, "cb": True},
    SceneId.PORTAL_GLITCH: {"cd": 90.0, "max30": 1, "esc_max": 4, "rarity": "rare", "setup": 3.0, "delay": 2.0, "payoff": 10.0, "cb": True},
    SceneId.SECRET_CHAMBER: {"cd": 70.0, "max30": 2, "esc_max": 3, "rarity": "rare", "setup": 3.0, "delay": 2.0, "payoff": 10.0, "cb": True},
    SceneId.RICE_ZEN: {"cd": 28.0, "max30": 4, "esc_max": 1, "rarity": "common", "setup": 3.0, "delay": 1.0, "payoff": 12.0, "cb": False},
    SceneId.AIRFRYER_VORTEX: {"cd": 80.0, "max30": 1, "esc_max": 4, "rarity": "rare", "setup": 2.0, "delay": 2.0, "payoff": 8.0, "cb": True},
    SceneId.MICROWAVE_ANOMALY: {"cd": 70.0, "max30": 2, "esc_max": 3, "rarity": "rare", "setup": 2.0, "delay": 1.5, "payoff": 8.0, "cb": True},
    SceneId.SHAREWARE_APOCALYPSE: {"cd": 240.0, "max30": 1, "esc_max": 5, "rarity": "very_rare", "setup": 2.0, "delay": 3.0, "payoff": 16.0, "cb": True},
    SceneId.RETURN_TO_CALM: {"cd": 12.0, "max30": 4, "esc_max": 0, "rarity": "common", "setup": 0.5, "delay": 0.0, "payoff": 6.0, "cb": False},
}

ESC_CAP = {
    "idle": 3,
    "coding_flow": 1,
    "terminal_heavy": 2,
    "focused": 1,
    "browsing": 3,
    "doomscrolling": 3,
    "late_night": 2,
    "frantic": 0,
    "social": 3,
    "appliance": 3,
    "rare": 5,
    "burn": 3,
}


class DirectorState(str, Enum):
    IDLE = "IDLE"
    REQUESTED = "REQUESTED"
    IN_FLIGHT = "IN_FLIGHT"
    RESULT_READY = "RESULT_READY"
    VALIDATED = "VALIDATED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    STALE = "STALE"
    FAILED = "FAILED"


class SceneTiming(str, Enum):
    IMMEDIATE = "IMMEDIATE"
    SHORT_WINDOW = "SHORT_WINDOW"
    AMBIENT = "AMBIENT"
    LONG_FORM = "LONG_FORM"


def scene_timing(scene: SceneId) -> SceneTiming:
    if scene in IMMEDIATE_SCENES:
        return SceneTiming.IMMEDIATE
    if scene in LONG_FORM_SCENES:
        return SceneTiming.LONG_FORM
    if scene in AMBIENT_SCENES:
        return SceneTiming.AMBIENT
    return SceneTiming.SHORT_WINDOW


@dataclass
class DirectorDecision:
    intent: SceneIntent | None
    rejected: str = ""


@dataclass
class DirectorRequest:
    request_id: str
    snapshot_revision: int
    opportunity_id: str
    requested_at: float
    expires_at: float
    force: bool = False
    context: dict[str, Any] = field(default_factory=dict)
    prompt_tokens_est: int = 0


@dataclass
class DirectorTiming:
    queue_ms: float = 0.0
    connect_ms: float = 0.0
    ttft_ms: float = 0.0
    generation_ms: float = 0.0
    parse_validate_ms: float = 0.0
    arbitration_ms: float = 0.0
    total_ms: float = 0.0

    def snapshot(self) -> dict[str, float]:
        return {
            "queue_ms": round(self.queue_ms, 2),
            "connect_ms": round(self.connect_ms, 2),
            "ttft_ms": round(self.ttft_ms, 2),
            "generation_ms": round(self.generation_ms, 2),
            "parse_validate_ms": round(self.parse_validate_ms, 2),
            "arbitration_ms": round(self.arbitration_ms, 2),
            "total_ms": round(self.total_ms, 2),
        }


@dataclass
class DirectorRawResult:
    request: DirectorRequest
    text: str = ""
    error: str = ""
    provider: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    timing: DirectorTiming = field(default_factory=DirectorTiming)


@dataclass
class DirectorMetrics:
    director_requests: int = 0
    director_success: int = 0
    director_timeout: int = 0
    director_network_error: int = 0
    director_schema_reject: int = 0
    director_unknown_scene_reject: int = 0
    director_stale_reject: int = 0
    director_expired_reject: int = 0
    director_safety_reject: int = 0
    director_cooldown_skip: int = 0
    director_focus_skip: int = 0
    pending_coalesced: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    selected_scene: str = ""
    rejected_scene: str = ""
    provider: str = ""
    model: str = ""
    last_error: str = ""
    last_state: str = DirectorState.IDLE.value
    last_request_id: str = ""
    cold_latency_ms: float = 0.0
    warm_latency_ms: float = 0.0
    prewarm_ok: bool = False

    def snapshot(self) -> dict[str, Any]:
        return {
            "director_requests": self.director_requests,
            "director_success": self.director_success,
            "director_timeout": self.director_timeout,
            "director_network_error": self.director_network_error,
            "director_schema_reject": self.director_schema_reject,
            "director_unknown_scene_reject": self.director_unknown_scene_reject,
            "director_stale_reject": self.director_stale_reject,
            "director_expired_reject": self.director_expired_reject,
            "director_safety_reject": self.director_safety_reject,
            "director_cooldown_skip": self.director_cooldown_skip,
            "director_focus_skip": self.director_focus_skip,
            "pending_coalesced": self.pending_coalesced,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "latency_ms": round(self.latency_ms, 2),
            "selected_scene": self.selected_scene,
            "rejected_scene": self.rejected_scene,
            "provider": self.provider,
            "model": self.model,
            "last_error": self.last_error,
            "last_state": self.last_state,
            "last_request_id": self.last_request_id,
            "cold_latency_ms": round(self.cold_latency_ms, 2),
            "warm_latency_ms": round(self.warm_latency_ms, 2),
            "prewarm_ok": self.prewarm_ok,
        }


SYSTEM_PROMPT = (
    "Return one SceneIntent JSON object only. No prose. No chain-of-thought. "
    "Keys: scene,tone,participants,escalation,duration,lineIntent,callbackKey,"
    "visualOnly,speechBudget,rarityClass. "
    "Choose scene ONLY from eligible[]. Prefer a valid scene not in recent[]. "
    "Respect cooldown/penalty. Keep escalation <= escMax. "
    "Work/focus: visualOnly=true, speechBudget=0, escalation 0-1. "
    "Absurd scenes are performed seriously. No self-aware jokes. "
    "Never emit coordinates, velocities, physics, safety, or spawn counts. "
    "If uncertain, pick the lowest-impact eligible scene."
)


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _time_bucket(tod: float) -> str:
    hour = int(tod * 24)
    if hour >= 23 or hour < 5:
        return "late"
    if 8 <= hour < 19:
        return "work"
    return "evening"


class ConfiguredTransport:
    """Exact configured Jarvis provider/model. No alternate backend."""

    def __init__(self) -> None:
        self.provider = ""
        self.model = ""
        self.backend = None
        self.last_error = ""
        try:
            from jarvis.config import load_settings
            from jarvis.llm import get_llm_backend

            settings = load_settings()
            self.provider = str(getattr(settings, "llm_provider", "") or "")
            self.model = str(getattr(settings, "llm_chat_model", "") or getattr(settings, "ollama_chat_model", "") or "")
            self.backend = get_llm_backend(settings)
        except Exception as exc:
            self.last_error = type(exc).__name__

    def complete(self, system: str, user: str, timeout_sec: float) -> DirectorRawResult:
        timing = DirectorTiming()
        t0 = time.perf_counter()
        if self.backend is None:
            timing.total_ms = (time.perf_counter() - t0) * 1000
            return DirectorRawResult(request=DirectorRequest("", 0, "", 0, 0), error="provider_unavailable", provider=self.provider, model=self.model, timing=timing)
        try:
            from jarvis.llm import extract_text_from_response

            connect_t = time.perf_counter()
            extra = {"temperature": 0.15, "max_tokens": 160}
            resp = self.backend.chat(
                self.model,
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                timeout_sec=timeout_sec,
                extra_options=extra,
                thinking=False,
            )
            timing.connect_ms = (time.perf_counter() - connect_t) * 1000
            timing.generation_ms = timing.connect_ms
            timing.ttft_ms = timing.connect_ms
            timing.total_ms = (time.perf_counter() - t0) * 1000
            if resp is None:
                return DirectorRawResult(
                    request=DirectorRequest("", 0, "", 0, 0),
                    error="timeout",
                    provider=self.provider,
                    model=self.model,
                    timing=timing,
                )
            text = extract_text_from_response(resp) or ""
            usage = resp.get("usage") if isinstance(resp, dict) else {}
            if not isinstance(usage, dict):
                usage = {}
            return DirectorRawResult(
                request=DirectorRequest("", 0, "", 0, 0),
                text=text,
                provider=self.provider,
                model=self.model,
                prompt_tokens=int(usage.get("prompt_tokens") or _estimate_tokens(system + user)),
                completion_tokens=int(usage.get("completion_tokens") or _estimate_tokens(text)),
                timing=timing,
            )
        except Exception as exc:
            name = type(exc).__name__.lower()
            kind = "timeout" if "timeout" in name else "network"
            timing.total_ms = (time.perf_counter() - t0) * 1000
            return DirectorRawResult(
                request=DirectorRequest("", 0, "", 0, 0),
                error=kind,
                provider=self.provider,
                model=self.model,
                timing=timing,
            )

    def prewarm(self, timeout_sec: float = 20.0) -> bool:
        if self.backend is None or not self.model:
            return False
        try:
            return bool(self.backend.warm_up(self.model, timeout_sec=timeout_sec))
        except Exception:
            return False


def _extract_json_object(text: str, last: bool = False) -> str:
    """Return a balanced `{...}` object in `text`, or "" if none.

    Same brace/string/escape walk as the voice intent judge
    (``jarvis.listening.intent_judge._extract_json_object``): handles
    markdown code fences and braces inside string values, and returns the
    first balanced object — or the last when ``last=True`` (reasoning
    models echo the system prompt's JSON example before the real answer).
    Unbalanced objects are skipped so a truncated draft cannot hide a
    later complete answer.
    """
    candidates: list[str] = []
    search_from = 0
    while True:
        start = text.find("{", search_from)
        if start == -1:
            break
        depth = 0
        in_string = False
        escape = False
        end = -1
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end == -1:
            # Unbalanced from this `{` — skip it and keep scanning for a
            # later complete object.
            search_from = start + 1
            continue
        candidates.append(text[start:end])
        search_from = end
    if not candidates:
        return ""
    return candidates[-1] if last else candidates[0]


def _repair_truncated_json(text: str) -> str:
    """Return the last object in ``text`` with its open braces/quotes closed.

    A ``max_tokens`` cap can cut the answer off mid-object, which leaves no
    balanced object for :func:`_extract_json_object` and the whole decision
    would be lost even though the decisive fields are already on the wire.
    Closes one ``"`` when the cut lands inside a string and appends one
    ``}`` per still-open brace. Values are the model's own; only structural
    characters are added. "" when there is no unclosed ``{`` at all.
    Mirrors ``jarvis.listening.intent_judge._repair_truncated_json``.
    """
    # Start of the last *unclosed* top-level object: earlier complete objects
    # (system-prompt examples echoed) are skipped, and a nested inner brace
    # never becomes the start.
    start = -1
    depth = 0
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth <= 0:
                depth = 0
                start = -1
    if start == -1:
        return ""
    fragment = text[start:]
    depth = 0
    in_string = False
    escape = False
    for ch in fragment:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
    if depth <= 0 and not in_string:
        return fragment
    return fragment + ('"' if in_string else "") + ("}" * max(depth, 0))


def _unique_prefix(value: str, allowed: set[str], min_len: int = 4) -> str | None:
    """The single allowed value that ``value`` starts, or ``None``.

    A ``max_tokens`` cap can cut a value mid-word; after the structural
    repair has closed the string, a unique prefix of an allowed value IS
    that value. Ambiguous prefixes (several allowed values share it) and
    very short fragments return ``None`` so the strict schema still
    governs everything else.
    """
    value = value.strip().lower()
    if not value or len(value) < min_len:
        return None
    matches = [a for a in allowed if a.startswith(value)]
    return matches[0] if len(matches) == 1 else None


def _loads_director_json(raw: str) -> tuple[dict | None, str]:
    """Parse one SceneIntent JSON object out of a raw model answer.

    Tries, in order: the raw text as JSON, the **last** balanced object
    (a model that echoes the context packet or an example before the real
    answer puts the decision at the end), the first balanced object, then
    a truncated-object repair. Returns ``(data, "")`` on success and
    ``(None, "invalid_json")`` when nothing parses. The old strict
    ``json.loads`` rejected every fence-wrapped, prose-wrapped, or
    token-capped answer as invalid_json and the whole social beat was lost.
    """
    for candidate in (
        raw.strip(),
        _extract_json_object(raw, last=True),
        _extract_json_object(raw),
        _repair_truncated_json(raw),
    ):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except Exception:
            continue
        if isinstance(data, dict):
            return data, ""
    return None, "invalid_json"


class LlmDirector:
    def __init__(self, cfg: WorldConfig, transport: Any | None = None) -> None:
        self.cfg = cfg
        self.transport = transport
        self.state = DirectorState.IDLE
        self.last_call = -1e9
        self.last_by_scene: dict[str, float] = {}
        self.call_times: list[float] = []
        self.snapshot_revision = 1
        self._last_activity: ActivityState | None = None
        self.metrics = DirectorMetrics()
        self.accepted_scenes: list[str] = []
        self.rejected: list[tuple[str, str]] = []
        self.recent_tones: list[str] = []
        self.recent_callbacks: list[str] = []
        self.recent_participants: list[int] = []
        self.penalties_applied: int = 0
        self.session = SessionPacing()
        self.last_context: dict[str, Any] = {}
        self.last_timing = DirectorTiming()
        self.in_flight: DirectorRequest | None = None
        self.pending: DirectorRequest | None = None
        self._future: Future | None = None
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="toaster-director")
        self._prewarm_started = False
        self._lock = threading.Lock()

    def _ensure_transport(self) -> Any:
        if self.transport is None:
            self.transport = ConfiguredTransport()
        return self.transport

    def parse(self, raw: str | dict) -> DirectorDecision:
        if isinstance(raw, dict):
            data = raw
        else:
            data, _reason = _loads_director_json(raw)
        if data is None:
            return DirectorDecision(None, "invalid_json")
        if not isinstance(data, dict):
            return DirectorDecision(None, "not_object")
        extra = set(data) - {
            "scene",
            "tone",
            "participants",
            "escalation",
            "duration",
            "lineIntent",
            "line_intent",
            "noveltyBudget",
            "novelty_budget",
            "allowedEntities",
            "allowed_entities",
            "forbiddenZones",
            "forbidden_zones",
            "speechBudget",
            "speech_budget",
            "visualOnly",
            "visual_only",
            "callbackKey",
            "callback_key",
            "rarityClass",
            "rarity_class",
            "contentKey",
            "content_key",
        }
        if extra:
            return DirectorDecision(None, "unknown_field")
        scene_raw = str(data.get("scene", "")).strip().lower()
        if scene_raw not in ALLOWED_SCENES:
            # Token-capped answer: the structural repair closed the string
            # but the scene value may still be cut mid-word.
            scene_raw = _unique_prefix(scene_raw, ALLOWED_SCENES) or scene_raw
            if scene_raw not in ALLOWED_SCENES:
                return DirectorDecision(None, "unknown_scene")
        tone_raw = str(data.get("tone", "")).strip().lower()
        if not tone_raw:
            return DirectorDecision(None, "unknown_tone")
        if tone_raw in TONE_ALIASES:
            tone = TONE_ALIASES[tone_raw]
        elif tone_raw in ALLOWED_TONES:
            tone = ALLOWED_TONES[tone_raw]
        else:
            prefix_tone = _unique_prefix(
                tone_raw, set(ALLOWED_TONES) | set(TONE_ALIASES)
            )
            if prefix_tone is None:
                return DirectorDecision(None, "unknown_tone")
            tone = ALLOWED_TONES.get(prefix_tone) or TONE_ALIASES[prefix_tone]
        try:
            participants = int(data.get("participants", 1))
            escalation = float(data.get("escalation", 0.2))
            duration = float(data.get("duration", 16.0))
        except (TypeError, ValueError):
            return DirectorDecision(None, "bad_numeric")
        if participants < 0:
            return DirectorDecision(None, "bad_numeric")
        if not 0.0 <= escalation <= 1.0:
            return DirectorDecision(None, "bad_numeric")
        if duration < 4.0 or duration > self.cfg.max_scene_duration_sec:
            return DirectorDecision(None, "bad_numeric")
        entities = tuple(str(x) for x in data.get("allowedEntities", data.get("allowed_entities", ())) or ())
        if any(item not in ALLOWED_SPECIES for item in entities):
            return DirectorDecision(None, "unknown_entity")
        zones = tuple(str(x) for x in data.get("forbiddenZones", data.get("forbidden_zones", ())) or ())
        if any(item.lower() not in ALLOWED_ZONES for item in zones):
            return DirectorDecision(None, "unknown_zone")
        callback = str(data.get("callbackKey", data.get("callback_key", "")) or "")[:80]
        if callback and callback not in ALLOWED_CALLBACKS:
            return DirectorDecision(None, "unknown_callback")
        content = str(data.get("contentKey", data.get("content_key", "")) or "")
        if content and content not in ALLOWED_CATEGORIES and content not in ALLOWED_CALLBACKS:
            return DirectorDecision(None, "unknown_category")
        rarity_raw = str(data.get("rarityClass", data.get("rarity_class", "common"))).strip().lower() or "common"
        if rarity_raw not in {"common", "uncommon", "rare", "very_rare", "legendary"}:
            return DirectorDecision(None, "unknown_rarity")
        visual_raw = data.get("visualOnly", data.get("visual_only", True))
        if not isinstance(visual_raw, bool):
            return DirectorDecision(None, "bad_visual_only")
        try:
            novelty_budget = float(data.get("noveltyBudget", data.get("novelty_budget", 0.4)))
            speech_budget = int(data.get("speechBudget", data.get("speech_budget", 1)))
        except (TypeError, ValueError):
            return DirectorDecision(None, "bad_numeric")
        if not 0.0 <= novelty_budget <= 1.0:
            return DirectorDecision(None, "bad_numeric")
        if speech_budget < 0 or speech_budget > 3:
            return DirectorDecision(None, "bad_numeric")
        intent = SceneIntent(
            scene=SceneId(scene_raw),
            tone=tone,
            participants=participants,
            escalation=escalation,
            line_intent=str(data.get("lineIntent") or data.get("line_intent") or "")[:160],
            duration=duration,
            novelty_budget=novelty_budget,
            allowed_entities=entities,
            forbidden_zones=zones,
            speech_budget=speech_budget,
            visual_only=visual_raw,
            callback_key=callback,
            rarity_class=rarity_raw,
            source="llm",
            content_key=content,
        )
        return DirectorDecision(intent)

    def observe(self, signals: UserSignals) -> None:
        if self._last_activity is not None and signals.activity is not self._last_activity:
            self.snapshot_revision += 1
        if signals.dragging or signals.selecting or signals.scrolling:
            self.snapshot_revision += 1
        self._last_activity = signals.activity

    def activity_label(self, world) -> str:
        signals = world.signals.signals
        return {
            ActivityState.DOOMSCROLLING: "doomscrolling",
            ActivityState.LATE_NIGHT: "late_night",
            ActivityState.TERMINAL_HEAVY: "terminal_heavy",
            ActivityState.CODING_FLOW: "coding_flow",
            ActivityState.FRANTIC: "frantic",
            ActivityState.FOCUSED: "focused",
            ActivityState.BROWSING: "browsing",
            ActivityState.IDLE: "idle",
        }.get(signals.activity, "idle")

    def context_label(self, world) -> str:
        if any(getattr(e, "burnt", False) for e in getattr(world, "entities", ())):
            return "burn"
        return self.activity_label(world)

    def eligible_scenes(self, world) -> list[SceneId]:
        label = self.context_label(world)
        seen: list[SceneId] = []
        for key in (label,):
            for scene in ELIGIBLE.get(key, ()):
                if scene not in seen:
                    seen.append(scene)
        opp = self.opportunity(world)
        if opp in ELIGIBLE and opp not in {label, "idle"}:
            for scene in ELIGIBLE[opp]:
                if scene not in seen:
                    seen.append(scene)
        if world.metrics.population < 2:
            seen = [
                scene
                for scene in seen
                if scene
                not in {SceneId.TOAST_INFESTATION, SceneId.TOAST_WATCH_PARTY, SceneId.POPCORN_BRAIN, SceneId.KPOP_HEART_MODE}
            ]
        if not seen:
            seen = [SceneId.QUIET_COMPANIONSHIP, SceneId.TOAST_WARMUP_RITUAL]
        now = getattr(world, "clock", 0.0)
        novelty = getattr(getattr(world, "rare", None), "novelty", 1.0)
        filtered: list[SceneId] = []
        soft: list[tuple[float, SceneId]] = []
        for scene in seen:
            if scene in RARE_SCENES and not self.session.rare_ok(scene, now, novelty):
                continue
            fat = self.session.fatigue(scene, now)
            soft.append((fat, scene))
        soft.sort(key=lambda item: item[0])
        for fat, scene in soft:
            if fat >= 1.35 and len(filtered) >= 2:
                self.session.mem.fatigue_suppressions += 1
                continue
            filtered.append(scene)
        preferred = ARC_PREF.get(self.session.mem.arc, set())
        if preferred:
            head = [s for s in filtered if s in preferred]
            tail = [s for s in filtered if s not in preferred]
            filtered = head + tail
        return filtered or seen[:2]

    def scene_memory(self, now: float) -> dict[str, Any]:
        last30 = self.accepted_scenes[-30:]
        counts: dict[str, int] = {}
        for sid in last30:
            counts[sid] = counts.get(sid, 0) + 1
        ages: dict[str, float] = {}
        for sid, when in self.last_by_scene.items():
            ages[sid] = round(max(0.0, now - when), 1)
        penalties = []
        for scene in SceneId:
            meta = SCENE_META.get(scene, {"cd": 20.0, "max30": 3})
            count = counts.get(scene.value, 0)
            age = ages.get(scene.value, 1e9)
            pen = 0.0
            if age < meta["cd"]:
                pen += 1.0 - (age / meta["cd"])
            if count >= meta["max30"]:
                pen += 1.0
            elif count:
                pen += 0.12 * count
            if pen > 0:
                penalties.append({"id": scene.value, "p": round(pen, 2), "n": count, "age": age if age < 1e8 else None})
        return {
            "acc": self.accepted_scenes[-8:],
            "n30": counts,
            "pen": penalties,
            "ton": self.recent_tones[-6:],
            "cb": self.recent_callbacks[-6:],
            "part": self.recent_participants[-6:],
        }

    def context_packet(self, world) -> dict[str, Any]:
        signals: UserSignals = world.signals.signals
        label = self.context_label(world)
        eligible = self.eligible_scenes(world)
        esc_max = ESC_CAP.get(label, 2)
        if signals.focus_score >= self.cfg.focus_threshold or signals.activity in {ActivityState.CODING_FLOW, ActivityState.FOCUSED, ActivityState.FRANTIC}:
            esc_max = min(esc_max, 1)
        mem = self.scene_memory(world.clock)
        timing = {
            scene.value: {
                "s": SCENE_META[scene]["setup"],
                "d": SCENE_META[scene]["delay"],
                "p": SCENE_META[scene]["payoff"],
                "cb": SCENE_META[scene]["cb"],
                "cd": SCENE_META[scene]["cd"],
            }
            for scene in eligible
            if scene in SCENE_META
        }
        packet = {
            "ctx": label,
            "opp": self.opportunity(world) or "none",
            "act": signals.activity.value,
            "foc": round(signals.focus_score, 3),
            "pop": int(world.metrics.population),
            "scn": world.narrative.current().value,
            "nov": round(world.rare.novelty, 3),
            "escMax": esc_max,
            "timing": timing,
            "saf": {
                "drag": bool(signals.dragging),
                "sel": bool(signals.selecting),
                "scroll": bool(signals.scrolling),
                "focus": signals.focus_score >= self.cfg.focus_threshold,
            },
            "tod": _time_bucket(signals.time_of_day),
            "eligible": [s.value for s in eligible],
            "ton": sorted(item.value for item in ComedyStyle),
            "cb": self.session.mature_callbacks(),
            "mem": mem,
            "sess": self.session.mem.compact(),
            "silenceOk": True,
        }
        self.last_context = packet
        return packet

    def privacy_ok(self, packet: dict[str, Any] | None = None) -> bool:
        blob = json.dumps(packet or self.last_context).lower()
        return not any(token in blob for token in ("clipboard", "filename", "document contents", "typed text"))

    def _suppressed(self, world, *, force: bool) -> str:
        signals = world.signals.signals
        if signals.dragging or signals.selecting or signals.scrolling:
            return "safety"
        if signals.activity in {ActivityState.CODING_FLOW, ActivityState.FRANTIC, ActivityState.FOCUSED, ActivityState.TERMINAL_HEAVY} and signals.focus_score > 0.72:
            if not force:
                return "focus"
        if signals.typing_burstiness > 0.7 and signals.typing_rate >= self.cfg.typing_burst_threshold * 0.4:
            return "safety"
        if not force:
            if world.clock - self.last_call < getattr(self.cfg, "llm_director_cooldown_sec", 45.0):
                return "cooldown"
            hour = [x for x in self.call_times if world.clock - x <= 3600]
            if len(hour) >= int(getattr(self.cfg, "llm_director_max_calls_per_hour", 24)):
                return "cooldown"
        return ""

    def _rare_scene_active(self, world) -> bool:
        narr = getattr(world, "narrative", None)
        if narr is None or narr.active is None:
            return False
        return getattr(narr.active.intent, "source", "") == "rare"

    def opportunity(self, world) -> str:
        signals = world.signals.signals
        if self._last_activity is not None and signals.activity is not self._last_activity:
            return "activity_transition"
        if signals.idle_duration >= 8.0 and signals.activity is ActivityState.IDLE:
            return "idle"
        if world.metrics.population >= 2:
            return "social"
        if any(item.open for item in world.appliances.items):
            return "appliance"
        if world.rare.novelty > 0.7:
            return "rare"
        if self.recent_callbacks:
            return "callback"
        return ""

    def maybe_request_payload(self, t: float, signals: UserSignals, novelty: float) -> dict | None:
        if not self.cfg.llm_director_enabled:
            return None
        if t - self.last_call < getattr(self.cfg, "llm_director_cooldown_sec", 45.0):
            return None
        if novelty < 0.35:
            return None
        return {"activity": signals.activity.value, "focus": round(signals.focus_score, 3)}

    def _ttl(self, opportunity: str) -> float:
        if opportunity in {"rare", "appliance"}:
            return float(getattr(self.cfg, "llm_director_ttl_long_sec", 90.0))
        return float(getattr(self.cfg, "llm_director_ttl_ambient_sec", 45.0))

    def request(self, world, *, force: bool = False, opportunity: str = "") -> DirectorRequest | None:
        if not self.cfg.llm_director_enabled and not force:
            return None
        reason = self._suppressed(world, force=force)
        if reason == "focus":
            self.metrics.director_focus_skip += 1
            self.state = DirectorState.REJECTED
            self.metrics.last_state = self.state.value
            return None
        if reason == "safety":
            self.metrics.director_safety_reject += 1
            self.state = DirectorState.REJECTED
            self.metrics.last_state = self.state.value
            return None
        if reason == "cooldown":
            self.metrics.director_cooldown_skip += 1
            return None
        if self._rare_scene_active(world):
            # A rare event is performing; the LLM must not override it.
            self.metrics.director_focus_skip += 1
            return None
        signals = world.signals.signals
        burnt = any(getattr(e, "burnt", False) for e in getattr(world, "entities", ()))
        self.session.update_arc(world.clock, signals.activity, getattr(world.rare, "novelty", 1.0), burnt)
        self.session.note_attention(signals.activity, signals.focus_score)
        eligible = self.eligible_scenes(world)
        fat = min((self.session.fatigue(s, world.clock) for s in eligible), default=0.0) if eligible else 1.0
        if self.session.want_silence(world.clock, signals.activity, signals.focus_score, getattr(world.rare, "novelty", 1.0), fat):
            self.session.note_silence(world.clock)
            return None
        opportunity = opportunity or self.opportunity(world) or "manual"
        packet = self.context_packet(world)
        user = json.dumps(packet, separators=(",", ":"), ensure_ascii=True)
        req = DirectorRequest(
            request_id=uuid.uuid4().hex[:12],
            snapshot_revision=self.snapshot_revision,
            opportunity_id=opportunity,
            requested_at=world.clock,
            expires_at=world.clock + self._ttl(opportunity),
            force=force,
            context=packet,
            prompt_tokens_est=_estimate_tokens(SYSTEM_PROMPT + user),
        )
        if self.in_flight is not None:
            self.pending = req
            self.metrics.pending_coalesced += 1
            return req
        self._launch(world, req, user)
        return req

    def _launch(self, world, req: DirectorRequest, user: str) -> None:
        self.in_flight = req
        self.pending = None
        self.state = DirectorState.REQUESTED
        self.metrics.director_requests += 1
        self.metrics.last_request_id = req.request_id
        self.last_call = world.clock
        self.call_times.append(world.clock)
        transport = self._ensure_transport()
        self.metrics.provider = getattr(transport, "provider", "")
        self.metrics.model = getattr(transport, "model", "")
        _log(f"engage request={req.request_id} opp={req.opportunity_id} {self.metrics.provider}/{self.metrics.model}")
        timeout = max(0.2, float(self.cfg.llm_director_timeout_ms) / 1000.0)
        queued = time.perf_counter()

        def work() -> DirectorRawResult:
            self.state = DirectorState.IN_FLIGHT
            raw = transport.complete(SYSTEM_PROMPT, user, timeout)
            raw.request = req
            raw.timing.queue_ms = (time.perf_counter() - queued) * 1000.0
            if not raw.provider:
                raw.provider = self.metrics.provider
            if not raw.model:
                raw.model = self.metrics.model
            return raw

        self.state = DirectorState.IN_FLIGHT
        self.metrics.last_state = self.state.value
        self._future = self._pool.submit(work)

    def poll(self, world) -> SceneIntent | None:
        self.observe(world.signals.signals)
        self._maybe_prewarm()
        if self._future is None:
            return None
        if not self._future.done():
            return None
        try:
            raw: DirectorRawResult = self._future.result()
        except Exception:
            raw = DirectorRawResult(request=self.in_flight or DirectorRequest("", 0, "", 0, 0), error="network")
        self._future = None
        inflight = self.in_flight
        self.in_flight = None
        self.state = DirectorState.RESULT_READY
        intent = self._arbitrate(world, inflight, raw)
        if self.pending is not None and self.cfg.llm_director_enabled:
            nxt = self.pending
            self.pending = None
            if world.clock <= nxt.expires_at and not self._suppressed(world, force=nxt.force):
                user = json.dumps(self.context_packet(world), separators=(",", ":"), ensure_ascii=True)
                nxt.snapshot_revision = self.snapshot_revision
                self._launch(world, nxt, user)
        return intent

    def _arbitrate(self, world, req: DirectorRequest | None, raw: DirectorRawResult) -> SceneIntent | None:
        t0 = time.perf_counter()
        self.last_timing = raw.timing
        self.metrics.latency_ms = raw.timing.total_ms
        self.metrics.provider = raw.provider or self.metrics.provider
        self.metrics.model = raw.model or self.metrics.model
        if self.metrics.cold_latency_ms <= 0 and raw.timing.total_ms:
            self.metrics.cold_latency_ms = raw.timing.total_ms
        elif raw.timing.total_ms:
            self.metrics.warm_latency_ms = raw.timing.total_ms
        if raw.error == "timeout":
            self.metrics.director_timeout += 1
            self.metrics.last_error = "timeout"
            self.state = DirectorState.FAILED
            self.metrics.last_state = self.state.value
            _log(f"fail timeout {raw.provider}/{raw.model}")
            return None
        if raw.error:
            self.metrics.director_network_error += 1
            self.metrics.last_error = raw.error
            self.state = DirectorState.FAILED
            self.metrics.last_state = self.state.value
            _log(f"fail {raw.error} {raw.provider}/{raw.model}")
            return None
        parse_t = time.perf_counter()
        decision = self.parse(raw.text)
        raw.timing.parse_validate_ms = (time.perf_counter() - parse_t) * 1000
        if decision.intent is None:
            if decision.rejected == "unknown_scene":
                self.metrics.director_unknown_scene_reject += 1
            else:
                self.metrics.director_schema_reject += 1
            self.rejected.append((decision.rejected, ""))
            self.metrics.rejected_scene = decision.rejected
            self.state = DirectorState.REJECTED
            self.metrics.last_state = self.state.value
            _log(f"reject {decision.rejected}")
            return None
        self.state = DirectorState.VALIDATED
        now = world.clock
        if req is not None and req.snapshot_revision != self.snapshot_revision:
            self.metrics.director_stale_reject += 1
            self.rejected.append(("stale", decision.intent.scene.value))
            self.state = DirectorState.STALE
            self.metrics.last_state = self.state.value
            _log(f"reject stale scene={decision.intent.scene.value}")
            return None
        if req is not None and now > req.expires_at:
            self.metrics.director_expired_reject += 1
            self.rejected.append(("expired", decision.intent.scene.value))
            self.state = DirectorState.REJECTED
            self.metrics.last_state = self.state.value
            _log(f"reject expired scene={decision.intent.scene.value}")
            return None
        if self._rare_scene_active(world):
            self.metrics.director_safety_reject += 1
            self.rejected.append(("rare_active", decision.intent.scene.value))
            self.state = DirectorState.REJECTED
            self.metrics.last_state = self.state.value
            _log(f"reject rare_active scene={decision.intent.scene.value}")
            return None
        if not self.admissible(world, decision.intent):
            self.metrics.director_safety_reject += 1
            self.rejected.append(("safety", decision.intent.scene.value))
            self.metrics.rejected_scene = decision.intent.scene.value
            self.state = DirectorState.REJECTED
            self.metrics.last_state = self.state.value
            _log(f"reject safety scene={decision.intent.scene.value}")
            return None
        if scene_timing(decision.intent.scene) is SceneTiming.IMMEDIATE and decision.intent.scene is not SceneId.RETURN_TO_CALM:
            if world.signals.signals.activity is not ActivityState.FRANTIC:
                self.metrics.director_schema_reject += 1
                self.rejected.append(("immediate", decision.intent.scene.value))
                self.state = DirectorState.REJECTED
                self.metrics.last_state = self.state.value
                return None
        raw.timing.arbitration_ms = (time.perf_counter() - t0) * 1000
        self.metrics.prompt_tokens += raw.prompt_tokens or (req.prompt_tokens_est if req else 0)
        self.metrics.completion_tokens += raw.completion_tokens
        _log(
            f"accept scene={decision.intent.scene.value} tone={decision.intent.tone.value} "
            f"participants={decision.intent.participants} escalation={decision.intent.escalation:.2f} "
            f"visual_only={decision.intent.visual_only} callback={decision.intent.callback_key or '-'} "
            f"line='{(decision.intent.line_intent or '')[:48]}'"
        )
        applied = world.apply_director_intent(decision.intent)
        # Record scene usage only after the intent was actually applied: the
        # cooldown/max30 checks inside apply_director_intent's admissible()
        # re-run must not see the scene as just-used when it is the scene being
        # applied right now.
        self.last_by_scene[decision.intent.scene.value] = now
        if not applied:
            self.metrics.director_safety_reject += 1
            self.rejected.append(("apply_rejected", decision.intent.scene.value))
            self.metrics.rejected_scene = decision.intent.scene.value
            self.state = DirectorState.REJECTED
            self.metrics.last_state = self.state.value
            _log(f"reject apply scene={decision.intent.scene.value}")
            return None
        self.metrics.director_success += 1
        self.metrics.selected_scene = decision.intent.scene.value
        self.accepted_scenes.append(decision.intent.scene.value)
        self.recent_tones.append(decision.intent.tone.value)
        self.recent_participants.append(decision.intent.participants)
        if decision.intent.callback_key:
            self.recent_callbacks.append(decision.intent.callback_key)
        self.state = DirectorState.ACCEPTED
        self.metrics.last_state = self.state.value
        return decision.intent

    def admissible(self, world, intent: SceneIntent) -> bool:
        if self._suppressed(world, force=True):
            return False
        cap = max(1, self.cfg.max_mini_toasts_event)
        if intent.participants > cap:
            intent.participants = cap
        if intent.callback_key and not self.session.callback_ok(intent.callback_key, world.clock):
            return False
        eligible = self.eligible_scenes(world)
        if intent.scene not in eligible:
            return False
        if intent.novelty_budget < getattr(getattr(world, "rare", None), "novelty", 1.0) * 0.12 and intent.rarity_class in {"rare", "very_rare", "legendary"}:
            return False
        label = self.context_label(world)
        esc_max = ESC_CAP.get(label, 2)
        meta = SCENE_META.get(intent.scene, {"esc_max": 2, "cd": 20.0, "max30": 3})
        if intent.escalation > max(esc_max, meta["esc_max"]) / 5.0 + 0.05 and intent.escalation > 0.85:
            intent.escalation = min(intent.escalation, esc_max / 5.0)
        last30 = self.accepted_scenes[-30:]
        if last30.count(intent.scene.value) >= meta["max30"]:
            self.penalties_applied += 1
            return False
        age = world.clock - self.last_by_scene.get(intent.scene.value, -1e9)
        if age < meta["cd"] * 0.35 and intent.scene not in {SceneId.QUIET_COMPANIONSHIP, SceneId.CODING_FLOW_AUDIENCE}:
            self.penalties_applied += 1
            return False
        if intent.escalation >= 0.8 and (world.rare.novelty < 0.45 or world.signals.signals.focus_score >= 0.45):
            intent.escalation = min(intent.escalation, 0.35)
        return True

    def apply_raw_for_tests(self, world, raw: DirectorRawResult) -> SceneIntent | None:
        """Deliver a completed result on the world thread. Used by proofs."""
        return self._arbitrate(world, raw.request, raw)

    def tick(self, world) -> SceneIntent | None:
        accepted = self.poll(world)
        if accepted is not None:
            return accepted
        if not self.cfg.llm_director_enabled:
            return None
        if self.in_flight is not None:
            return None
        if self.opportunity(world):
            req = self.request(world)
            world.pending_director_payload = getattr(req, "payload", None) or {
                "opportunity": True,
                "enabled": True,
            }
        return None

    def _maybe_prewarm(self) -> None:
        if self._prewarm_started or not getattr(self.cfg, "llm_director_prewarm", True):
            return
        if not self.cfg.llm_director_enabled:
            return
        self._prewarm_started = True
        transport = self._ensure_transport()

        def _warm() -> None:
            ok = bool(getattr(transport, "prewarm", lambda: False)())
            self.metrics.prewarm_ok = ok

        self._pool.submit(_warm)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False)
