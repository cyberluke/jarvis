"""Voice routing for the desktop control plane.

``VoiceRouteResolver`` decides, deterministically and without an LLM
round trip, whether a final ASR utterance is addressed to a v271 agent
(and which app owns it), and ``ContinuityCache`` keeps the thread
metadata v271 reported back (appId / agentId / lastChatId) so voice
follow-ups reuse the same thread.

Spec §15: explicit app name → foreground Agentic App → last active
Agentic App → generic v271 target.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .apps import AppRegistry
from .sessions import BrowserSession, BrowserSessionRegistry

#: Continuity marker for the generic (non-app) v271 target.
V271_MARKER = "v271"

#: Routing verbs broaden the gate for the generic v271 target. App
#: aliases route on their own; the verb list only matters when the user
#: explicitly says "v271" or for diagnostics.
DEFAULT_ROUTING_VERBS = (
    "zeptej se",
    "poptej se",
    "zeptej",
    "otázka",
    "dej do",
    "pošli do",
    "pošli",
    "přepni",
    "přepni na",
    "ask",
    "send to",
    "route",
    "switch to",
    "query",
)


@dataclass(frozen=True)
class VoiceRoute:
    """Outcome of the resolver for one utterance."""

    routed: bool = False
    app_id: Optional[str] = None
    app_display_name: Optional[str] = None
    generic_v271: bool = False
    reason: str = "not-routed"


@dataclass
class ContinuityEntry:
    app_id: Optional[str]  # app id or V271_MARKER
    agent_id: Optional[str]
    last_chat_id: Optional[str]
    timestamp: float = field(default_factory=time.time)


class ContinuityCache:
    """Thread metadata per routed target, TTL-bounded.

    Only routing metadata is cached: appId, agentId, lastChatId. v271
    remains authoritative for the conversation itself.
    """

    def __init__(self, ttl_sec: float = 600.0) -> None:
        self._ttl = max(0.0, float(ttl_sec))
        self._entries: Dict[str, ContinuityEntry] = {}
        self._lock = threading.Lock()

    def update(
        self,
        app_id: Optional[str],
        agent_id: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> None:
        key = app_id or V271_MARKER
        with self._lock:
            existing = self._entries.get(key)
            self._entries[key] = ContinuityEntry(
                app_id=key,
                agent_id=agent_id if agent_id is not None else (existing.agent_id if existing else None),
                last_chat_id=chat_id if chat_id is not None else (existing.last_chat_id if existing else None),
                timestamp=time.time(),
            )

    def touch(self, app_id: Optional[str]) -> None:
        key = app_id or V271_MARKER
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                entry.timestamp = time.time()

    def get(self, app_id: Optional[str]) -> Optional[ContinuityEntry]:
        key = app_id or V271_MARKER
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if time.time() - entry.timestamp > self._ttl:
                self._entries.pop(key, None)
                return None
            return entry

    def latest(self) -> Optional[ContinuityEntry]:
        """Most recent live entry (for the last-active-app cascade)."""
        now = time.time()
        with self._lock:
            live = [
                e
                for e in self._entries.values()
                if now - e.timestamp <= self._ttl
            ]
            if not live:
                return None
            return max(live, key=lambda e: e.timestamp)


class VoiceRouteResolver:
    """Deterministic routing of a final ASR utterance to a v271 target.

    Resolution order:
      1. explicit app alias in the text → that app
      2. ``v271`` mentioned → generic v271 target
      3. continuity follow-up (last routed target younger than TTL)
      4. otherwise → not routed (local reply engine handles it)

    The browser session is then selected by
    ``BrowserSessionRegistry.pick_target`` (§7 priority); a routed
    utterance with no session yields ``V271_BROWSER_UNAVAILABLE``.
    """

    def __init__(
        self,
        apps: AppRegistry,
        sessions: BrowserSessionRegistry,
        continuity: ContinuityCache,
        *,
        enabled: bool = True,
    ) -> None:
        self._apps = apps
        self._sessions = sessions
        self._continuity = continuity
        self._enabled = enabled
        self._v271_re = re.compile(r"v\s*271", re.IGNORECASE)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def resolve(self, text: str) -> VoiceRoute:
        """Resolve one utterance; never raises."""
        if not self._enabled or not text or not text.strip():
            return VoiceRoute()
        norm = _normalize(text)
        if not norm:
            return VoiceRoute()

        app = self._apps.by_alias(norm)
        if app is not None:
            return VoiceRoute(
                routed=True,
                app_id=app.app_id,
                app_display_name=app.display_name,
                reason=f"explicit-app:{app.app_id}",
            )

        if self._v271_re.search(norm):
            return VoiceRoute(
                routed=True,
                generic_v271=True,
                reason="explicit-v271",
            )

        latest = self._continuity.latest()
        if latest is not None:
            return VoiceRoute(
                routed=True,
                app_id=None if latest.app_id == V271_MARKER else latest.app_id,
                generic_v271=latest.app_id == V271_MARKER,
                reason=f"continuity:{latest.app_id}",
            )

        return VoiceRoute()

    def pick_session(self) -> Optional[BrowserSession]:
        """Select the target tab per §7 priority (or ``None``)."""
        return self._sessions.pick_target()


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())