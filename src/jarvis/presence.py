from dataclasses import dataclass, field
from enum import Enum
import time
import threading
from typing import Dict, Any, Optional
from .debug import debug_log

class PresenceMode(Enum):
    """High-level interaction modes for Toustovač."""
    PASSIVE = "passive"            # Background presence, no active engagement
    ADDRESSED = "addressed"        # User just spoke the wake word or addressed the assistant
    CONVERSATION = "conversation"  # Active multi-turn dialogue
    COMPANION = "companion"        # Proactive/reactive engagement, "living" with the user
    QUIET_COMPANY = "quiet_company" # Presence acknowledged, but staying out of the way
    FOCUS = "focus"                # User is busy, assistant should minimize interruptions
    QUIET = "quiet"                # Explicit "do not disturb" or silent mode


def _fold(text: str) -> str:
    import unicodedata
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", str(text)).casefold()
        if not unicodedata.combining(ch)
    ).strip()


# Deterministic normalized aliases (doc §5). Longest-prefix match wins.
# QUIET_COMPANY first: its "delej mi spolecnost..." is a longer prefix of
# the COMPANION alias and must win on overlapping phrases.
_MODE_TRIGGERS = {
    PresenceMode.COMPANION: (
        "pojd pokecat", "pojd si povilat", "delej mi spolecnost",
        "jsem sam", "nudim se", "talk to me for a while",
        "keep me company", "i'm bored", "im bored", "let's talk", "lets talk",
    ),
    PresenceMode.QUIET_COMPANY: (
        "delej mi spolecnost, ja budu pracovat",
        "delej mi spolecnost, ja budu pracovat.",
        "bud tu se mnou, ale moc nemluv",
        "jen tu se mnou bud", "keep me company while i work",
        "stay with me but keep it quiet", "hang around while i code",
    ),
    PresenceMode.FOCUS: (
        "pojdme pracovat", "ted makame", "soustred se se mnou",
        "focus mode", "let's work", "lets work", "help me focus",
    ),
    PresenceMode.QUIET: (
        "ticho", "bud ticho", "prestan mluvit", "quiet", "stop talking",
        "be quiet for a while",
    ),
}

_EXIT_ALIASES = (
    "zpatky do normalu", "muze zase normalne", "to staci",
    "uz nemusis delat spolecnost", "normal mode", "back to normal",
    "that's enough for now", "thats enough for now",
)


def _sorted_alias_pairs():
    """All (alias, mode) pairs sorted longest-first, so the more specific
    QUIET_COMPANY phrase beats its COMPANION prefix on overlap."""
    pairs = []
    for mode, aliases in _MODE_TRIGGERS.items():
        for alias in aliases:
            pairs.append((alias, mode))
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


_ALIAS_PAIRS = _sorted_alias_pairs()


def match_mode_command(text: str):
    """Deterministic presence mode for an utterance, or None (§5)."""
    folded = _fold(text or "")
    if not folded:
        return None
    for alias, mode in _ALIAS_PAIRS:
        if folded == alias or folded.startswith(alias):
            return mode
    for alias in _EXIT_ALIASES:
        if folded == alias or folded.startswith(alias):
            return PresenceMode.PASSIVE
    return None


_MODE_LABELS: Dict[PresenceMode, str] = {
    PresenceMode.PASSIVE: "Passive",
    PresenceMode.ADDRESSED: "Addressed",
    PresenceMode.CONVERSATION: "Conversation",
    PresenceMode.COMPANION: "Companion",
    PresenceMode.QUIET_COMPANY: "Quiet company",
    PresenceMode.FOCUS: "Focus",
    PresenceMode.QUIET: "Quiet",
}


@dataclass(frozen=True)
class PresenceState:
    """Immutable snapshot of the current presence state."""
    mode: PresenceMode
    last_transition_time: float
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        """Human-readable label for UI indicators (doc §6.1)."""
        return _MODE_LABELS.get(self.mode, self.mode.value)

class PresenceCoordinator:
    """
    Orchestration layer for managing Toustovač's interaction mode.
    This is a deterministic state machine that reacts to system events.
    """

    def __init__(self, default_mode: Optional[str] = None):
        boot = PresenceMode.PASSIVE
        if default_mode:
            try:
                boot = PresenceMode(str(default_mode).strip().lower())
            except ValueError:
                pass  # unknown string → keep safe PASSIVE boot
        self._current_state = PresenceState(
            mode=boot,
            last_transition_time=time.time()
        )
        self._lock = threading.Lock()
        self._subscribers: list[callable] = []

    def get_state(self) -> PresenceState:
        """Return the current presence state."""
        with self._lock:
            return self._current_state

    def transition_to(self, new_mode: PresenceMode, metadata: Optional[Dict[str, Any]] = None) -> bool:
        """
        Attempt to transition to a new mode. 
        Returns True if the transition was successful and meaningful.
        """
        with self._lock:
            if self._current_state.mode == new_mode:
                return False

            old_mode = self._current_state.mode
            new_state = PresenceState(
                mode=new_mode,
                last_transition_time=time.time(),
                metadata=metadata or {}
            )
            self._current_state = new_state
            
            debug_log(f"Presence transition: {old_mode.value} -> {new_mode.value}", "presence")
            
            # Notify subscribers
            for callback in self._subscribers:
                try:
                    callback(new_state)
                except Exception as e:
                    debug_log(f"Error in presence subscriber: {e}", "presence")
            
            return True

    def subscribe(self, callback: callable) -> None:
        """Register a callback for presence changes."""
        with self._lock:
            self._subscribers.append(callback)

    # Deterministic API methods for common triggers
    def on_wake_word_detected(self) -> None:
        """Triggered when the wake word is recognized."""
        self.transition_to(PresenceMode.ADDRESSED)

    def on_conversation_started(self) -> None:
        """Triggered when a multi-turn dialogue begins."""
        self.transition_to(PresenceMode.CONVERSATION)

    def on_conversation_ended(self) -> None:
        """Triggered when a dialogue concludes."""
        self.transition_to(PresenceMode.QUIET_COMPANY)

    def on_user_focus_detected(self) -> None:
        """Triggered when user enters a focused work state."""
        self.transition_to(PresenceMode.FOCUS)

    def on_user_idle_detected(self) -> None:
        """Triggered when user has been idle for a while."""
        self.transition_to(PresenceMode.PASSIVE)

    def on_explicit_quiet_requested(self) -> None:
        """Triggered by user command to enter quiet mode."""
        self.transition_to(PresenceMode.QUIET)

    # ── Deterministic capability queries (doc §4) ──────────────────────
    def requires_wake_word(self) -> bool:
        """PASSIVE/ADDRESSED need the wake word; active sessions do not."""
        return self.get_state().mode in (PresenceMode.PASSIVE, PresenceMode.QUIET)

    def allows_proactive(self, event_type: str) -> bool:
        """Mode-aware proactive gate (doc §25). Critical events pass everywhere."""
        mode = self.get_state().mode
        if mode is PresenceMode.QUIET:
            return event_type in _CRITICAL_EVENTS
        if mode is PresenceMode.FOCUS:
            return event_type in _CRITICAL_EVENTS | {"build.success", "build.failed",
                                                     "terminal.command_failed"}
        if mode is PresenceMode.QUIET_COMPANY:
            return event_type in _CRITICAL_EVENTS | {
                "build.success", "build.failed", "download.completed",
                "long_task.completed", "user.returned",
            }
        if mode is PresenceMode.COMPANION:
            return True
        return True  # PASSIVE/ADDRESSED/CONVERSATION: existing policy

    def allows_auto_followup(self) -> bool:
        mode = self.get_state().mode
        return mode in (PresenceMode.CONVERSATION, PresenceMode.COMPANION)

    def allows_social_reengagement(self) -> bool:
        """One-time re-engagement after a pause — COMPANION only."""
        return self.get_state().mode is PresenceMode.COMPANION

    def snapshot(self) -> Dict[str, Any]:
        state = self.get_state()
        return {
            "mode": state.mode.value,
            "last_transition_time": state.last_transition_time,
            "metadata": dict(state.metadata),
        }

    def runtime_block(self) -> str:
        """Compact [Presence] block for the reply system prompt (§17–19, §40)."""
        mode = self.get_state().mode
        lines = [f"[Presence] mode={mode.value}"]
        if mode is PresenceMode.COMPANION:
            lines.append(
                "Interaction mode: companion conversation. Maintain a natural "
                "two-way conversation; help carry it instead of waiting for a "
                "perfectly formed request. Do not ask a question every turn; "
                "alternate observations, reactions, questions, related ideas. "
                "Silence is allowed. Do not manufacture memories."
            )
        elif mode is PresenceMode.QUIET_COMPANY:
            lines.append(
                "Interaction mode: quiet company. The user is working and wants "
                "your presence without chatter. Do not initiate ordinary "
                "conversation to fill silence; respond naturally when "
                "addressed; short remark only on genuinely meaningful events."
            )
        elif mode is PresenceMode.FOCUS:
            lines.append(
                "Interaction mode: focus. Prioritize the active task, desktop "
                "context, diagnostics and requested actions; minimize social "
                "chatter and proactive humor."
            )
        elif mode is PresenceMode.QUIET:
            lines.append(
                "Interaction mode: quiet. Only directly addressed requests "
                "are answered; critical notifications may still fire."
            )
        return "\n".join(lines)


_CRITICAL_EVENTS = frozenset({
    "app.error", "network.disconnected", "battery.low",
    "build.failed", "system.temperature_high",
})
