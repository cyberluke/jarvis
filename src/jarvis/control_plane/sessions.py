"""Browser session registry for the desktop control plane.

Each open paired v271 tab registers itself. Toastovač keeps only routing
metadata (identity, foreground flag, last-seen stamp) and the owning
pairing id; v271 remains authoritative for conversation state.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class BrowserSession:
    tab_session_id: str
    user_id: str
    pairing_id: str
    foreground: bool = False
    workspace_id: Optional[str] = None
    last_seen_at: float = field(default_factory=time.time)


class BrowserSessionRegistry:
    """Thread-safe registry of paired browser tabs with TTL pruning.

    Voice routing priority (spec §7): explicit selected tab (none in
    v1) → foreground paired tab → most recent paired tab. A session is
    ``expired`` when its ``lastSeenAt`` is older than the TTL; expired
    sessions are pruned and never picked as routing targets.
    """

    def __init__(self, session_ttl_sec: float = 120.0) -> None:
        self._ttl = max(0.0, float(session_ttl_sec))
        self._sessions: Dict[str, BrowserSession] = {}
        self._lock = threading.RLock()

    # -- lifecycle ------------------------------------------------------

    def register(
        self,
        tab_session_id: str,
        user_id: str,
        pairing_id: str,
        *,
        foreground: bool = False,
        workspace_id: Optional[str] = None,
    ) -> BrowserSession:
        """Register (or refresh) a tab session under a pairing."""
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            session = BrowserSession(
                tab_session_id=tab_session_id,
                user_id=user_id,
                pairing_id=pairing_id,
                foreground=bool(foreground),
                workspace_id=workspace_id,
                last_seen_at=now,
            )
            self._sessions[tab_session_id] = session
            return session

    def touch(self, tab_session_id: str, pairing_id: str) -> Optional[BrowserSession]:
        """Refresh the last-seen stamp of a registered tab."""
        with self._lock:
            session = self._sessions.get(tab_session_id)
            if session is None or session.pairing_id != pairing_id:
                return None
            session.last_seen_at = time.time()
            return session

    def unregister(self, tab_session_id: str, pairing_id: Optional[str] = None) -> bool:
        """Forget a tab. When ``pairing_id`` is given, only the owning
        pairing may unregister it."""
        with self._lock:
            session = self._sessions.get(tab_session_id)
            if session is None:
                return False
            if pairing_id is not None and session.pairing_id != pairing_id:
                return False
            del self._sessions[tab_session_id]
            return True

    def get(self, tab_session_id: str) -> Optional[BrowserSession]:
        """Return a live (non-expired) session, or ``None``."""
        now = time.time()
        with self._lock:
            session = self._sessions.get(tab_session_id)
            if session is None:
                return None
            if now - session.last_seen_at > self._ttl:
                return None
            return session

    def get_for_pairing(self, tab_session_id: str, pairing_id: str) -> Optional[BrowserSession]:
        """Return a live session owned by ``pairing_id``, or ``None``."""
        session = self.get(tab_session_id)
        if session is None or session.pairing_id != pairing_id:
            return None
        return session

    def exists(self, tab_session_id: str) -> bool:
        """True when a session with this id is present (even if expired)."""
        with self._lock:
            return tab_session_id in self._sessions

    def all(self) -> List[BrowserSession]:
        """Snapshot of live sessions, most recently seen first."""
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            return sorted(
                self._sessions.values(),
                key=lambda s: s.last_seen_at,
                reverse=True,
            )

    # -- routing --------------------------------------------------------

    def pick_target(self) -> Optional[BrowserSession]:
        """Select the routing target per §7 priority.

        Explicit selected tab is a v1 non-goal (no such field exists
        yet); the foreground paired tab wins, then the most recent.
        """
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            foreground = [
                s
                for s in self._sessions.values()
                if s.foreground and now - s.last_seen_at <= self._ttl
            ]
            if foreground:
                return max(foreground, key=lambda s: s.last_seen_at)
            recent = [
                s
                for s in self._sessions.values()
                if now - s.last_seen_at <= self._ttl
            ]
            if not recent:
                return None
            return max(recent, key=lambda s: s.last_seen_at)

    # -- internals ------------------------------------------------------

    def _prune_locked(self, now: float) -> None:
        stale = [
            sid
            for sid, s in self._sessions.items()
            if now - s.last_seen_at > self._ttl
        ]
        for sid in stale:
            del self._sessions[sid]