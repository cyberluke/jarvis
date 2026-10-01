"""Event bus for the desktop control plane.

Delivers outbound protocol events (dispatch, and later resource /
subscription events) to the browser tabs that are listening on
``GET /desktop/v1/events``. Each ``tabSessionId`` keeps a bounded ring
buffer so a reconnecting stream can replay events it missed (``since``
cursor), plus live subscriber queues.

Thread-safe: publishers (voice routing, HTTP handlers) and SSE streams
run on different threads.
"""

from __future__ import annotations

import queue
import threading
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

EventItem = Tuple[int, str, Dict[str, Any]]


class EventBus:
    """Per-session ring + live queues with replay support."""

    def __init__(self, max_per_session: int = 200) -> None:
        self._max = max(1, int(max_per_session))
        self._rings: Dict[str, Deque[EventItem]] = {}
        self._seq: Dict[str, int] = {}
        self._streams: Dict[str, Set["queue.Queue[EventItem]"]] = {}
        self._lock = threading.Lock()

    def publish(
        self,
        session_id: Optional[str],
        event_type: str,
        payload: Dict[str, Any],
    ) -> int:
        """Append one event to a session's ring (or broadcast to all live
        streams when ``session_id`` is ``None``). Returns its sequence
        number."""
        with self._lock:
            if session_id is None:
                seq = -1
                streams = self._all_streams_locked()
            else:
                seq = self._seq.get(session_id, 0) + 1
                self._seq[session_id] = seq
                ring = self._rings.setdefault(session_id, deque(maxlen=self._max))
                ring.append((seq, event_type, payload))
                streams = list(self._streams.get(session_id, ()))
        for q in streams:
            try:
                q.put_nowait((seq, event_type, payload))
            except queue.Full:
                pass
        return seq

    def subscribe(self, session_id: str, since: int = 0) -> "queue.Queue[EventItem]":
        """Open a live stream for a session, seeded with backlog events
        whose sequence is greater than ``since`` (in order)."""
        q: "queue.Queue[EventItem]" = queue.Queue()
        with self._lock:
            backlog = [
                item for item in self._rings.get(session_id, ()) if item[0] > since
            ]
            self._streams.setdefault(session_id, set()).add(q)
        for item in backlog:
            q.put_nowait(item)
        return q

    def unsubscribe(self, session_id: str, q: "queue.Queue") -> None:
        with self._lock:
            streams = self._streams.get(session_id)
            if streams is not None:
                streams.discard(q)
                if not streams:
                    self._streams.pop(session_id, None)

    def backlog(
        self, session_id: str, since: int = 0
    ) -> List[EventItem]:
        """Snapshot of a session's ring after ``since`` (tests/diag)."""
        with self._lock:
            return [i for i in self._rings.get(session_id, ()) if i[0] > since]

    def sequence(self, session_id: str) -> int:
        with self._lock:
            return self._seq.get(session_id, 0)

    def _all_streams_locked(self) -> List["queue.Queue[EventItem]"]:
        out: List["queue.Queue[EventItem]"] = []
        for streams in self._streams.values():
            out.extend(streams)
        return out