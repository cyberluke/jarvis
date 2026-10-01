"""World event bus. Handlers propose; safety still owns the last word."""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Callable, Deque, Iterable

from .types import BusEvent, WorldEvent


Handler = Callable[[BusEvent], None]

# Per-type debounce floors (seconds). Duplicate identity is still event_id.
DEBOUNCE_SEC: dict[WorldEvent, float] = {
    WorldEvent.MOUSE_MOVE_SLOW: 0.25,
    WorldEvent.MOUSE_MOVE_FAST: 0.12,
    WorldEvent.MOUSE_DRAG: 0.12,
    WorldEvent.MOUSE_SELECTION: 0.12,
    WorldEvent.MOUSE_IDLE: 2.0,
    WorldEvent.USER_TYPING_FAST: 0.4,
    WorldEvent.USER_TYPING_BURST: 0.4,
    WorldEvent.SCROLL_BURST: 0.4,
    WorldEvent.TAB_SWITCH_SPIKE: 1.0,
    WorldEvent.TERMINAL_ACTIVITY_HIGH: 2.0,
    WorldEvent.LATE_NIGHT: 30.0,
    WorldEvent.WORK_HOURS: 30.0,
    WorldEvent.USER_IDLE_LONG: 20.0,
    WorldEvent.DOOMSCROLL_SCORE_HIGH: 2.0,
    WorldEvent.FOCUS_SCORE_HIGH: 2.0,
    WorldEvent.SHORT_VIDEO_LOOP_PATTERN: 3.0,
    WorldEvent.POPULATION_LOW: 4.0,
    WorldEvent.POPULATION_HIGH: 4.0,
    WorldEvent.POPULATION_INFESTATION: 4.0,
    WorldEvent.POPULATION_CHANGE: 0.5,
    WorldEvent.WEATHER_GLOOMY_SIGNAL: 20.0,
    WorldEvent.NIGHT_THEME: 20.0,
}

STALE_SEC = 8.0


class EventBus:
    def __init__(self, history: int = 256, world_id: str = "world-0", session_id: str = "session-0") -> None:
        self._handlers: dict[WorldEvent, list[Handler]] = defaultdict(list)
        self._any: list[Handler] = []
        self._history: Deque[BusEvent] = deque(maxlen=history)
        self._seq = 0
        self._seen: Deque[int] = deque(maxlen=512)
        self.world_id = world_id
        self.session_id = session_id
        self.dropped_stale = 0
        self.dropped_debounce = 0

    def on(self, name: WorldEvent, handler: Handler) -> None:
        self._handlers[name].append(handler)

    def on_any(self, handler: Handler) -> None:
        self._any.append(handler)

    def emit(
        self,
        name: WorldEvent,
        t: float,
        *,
        source: str = "world",
        entity_id: str | None = None,
        payload: dict | None = None,
        now: float | None = None,
    ) -> BusEvent | None:
        clock = now if now is not None else t
        if clock - t > STALE_SEC:
            self.dropped_stale += 1
            return None
        floor = DEBOUNCE_SEC.get(name)
        if floor is not None:
            last = self.last(name)
            if last is not None and t - last.t < floor:
                self.dropped_debounce += 1
                return last
        self._seq += 1
        body = dict(payload or {})
        body.setdefault("world_id", self.world_id)
        body.setdefault("session_id", self.session_id)
        event = BusEvent(
            name=name,
            t=t,
            source=source,
            entity_id=entity_id,
            payload=body,
            event_id=self._seq,
        )
        if event.event_id in self._seen:
            return event
        self._seen.append(event.event_id)
        self._history.append(event)
        for handler in tuple(self._handlers.get(name, ())):
            handler(event)
        for handler in tuple(self._any):
            handler(event)
        return event

    def already_seen(self, event_id: int) -> bool:
        return event_id in self._seen

    def recent(self, name: WorldEvent | None = None, limit: int = 32) -> list[BusEvent]:
        items: Iterable[BusEvent] = reversed(self._history)
        out: list[BusEvent] = []
        for event in items:
            if name is None or event.name is name:
                out.append(event)
            if len(out) >= limit:
                break
        return out

    def last(self, name: WorldEvent) -> BusEvent | None:
        for event in reversed(self._history):
            if event.name is name:
                return event
        return None
