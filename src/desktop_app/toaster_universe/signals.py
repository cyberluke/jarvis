"""Rhythm-only user activity. No semantic surveillance."""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from .config import WorldConfig
from .events import EventBus
from .types import ActivityState, Vec2, WorldEvent


def _now() -> float:
    return time.monotonic()


@dataclass
class UserSignals:
    foreground_app: str = ""
    typing_rate: float = 0.0
    typing_burstiness: float = 0.0
    typing_regularity: float = 0.0
    terminal_activity: float = 0.0
    mouse_velocity: float = 0.0
    mouse_acceleration: float = 0.0
    mouse_idle_time: float = 0.0
    scroll_rate: float = 0.0
    scroll_burstiness: float = 0.0
    tab_switch_rate: float = 0.0
    window_switch_rate: float = 0.0
    time_of_day: float = 0.5
    session_length: float = 0.0
    focus_score: float = 0.0
    doomscroll_score: float = 0.0
    attention_fragmentation: float = 0.0
    novelty_seeking_proxy: float = 0.0
    idle_duration: float = 0.0
    active_monitor: int = 0
    cursor: Vec2 = field(default_factory=Vec2)
    cursor_velocity: Vec2 = field(default_factory=Vec2)
    cursor_predicted: Vec2 = field(default_factory=Vec2)
    buttons_down: int = 0
    selecting: bool = False
    dragging: bool = False
    scrolling: bool = False
    activity: ActivityState = ActivityState.IDLE
    work_rect: object | None = None
    recent_click: Vec2 = field(default_factory=Vec2)
    recent_click_t: float = -1e9
    activity_hold_t: float = 0.0
    browning_comedy: float = 0.0
    window_switch_count: int = 0
    recent_scroll_region: Vec2 = field(default_factory=Vec2)
    tab_switch_count: int = 0

    def snapshot(self) -> dict:
        return {
            "foregroundApp": self.foreground_app,
            "typingRate": round(self.typing_rate, 3),
            "typingBurstiness": round(self.typing_burstiness, 3),
            "focusScore": round(self.focus_score, 3),
            "doomscrollScore": round(self.doomscroll_score, 3),
            "activity": self.activity.value,
            "mouseIdleTime": round(self.mouse_idle_time, 3),
            "cursor": (round(self.cursor.x, 1), round(self.cursor.y, 1)),
            "predicted": (round(self.cursor_predicted.x, 1), round(self.cursor_predicted.y, 1)),
            "dragging": self.dragging,
            "selecting": self.selecting,
            "scrolling": self.scrolling,
            "activeMonitor": self.active_monitor,
            "recentClick": (round(self.recent_click.x, 1), round(self.recent_click.y, 1)),
            "privacy": {
                "typedText": False,
                "clipboard": False,
                "documentBody": False,
                "windowTitle": False,
                "processIdentityOnly": True,
            },
        }


class SignalSampler:
    def __init__(self, cfg: WorldConfig, bus: EventBus) -> None:
        self.cfg = cfg
        self.bus = bus
        self.signals = UserSignals()
        self.started = _now()
        self._keys: deque[float] = deque(maxlen=80)
        self._scrolls: deque[float] = deque(maxlen=40)
        self._tabs: deque[float] = deque(maxlen=20)
        self._windows: deque[float] = deque(maxlen=20)
        self._last_cursor = Vec2()
        self._last_vel = Vec2()
        self._last_sample = self.started
        self._last_input = self.started
        self._last_key = 0.0
        self._last_scroll = 0.0
        self._typing = False
        self._scroll_on = False
        self._last_app = ""
        self._last_activity = ActivityState.IDLE
        self._hook: Callable[[UserSignals], None] | None = None
        self.live_os = False
        self.lock_activity: ActivityState | None = None
        self._last_buttons = 0
        self._activity_since = self.started
        try:
            from PyQt6.QtWidgets import QApplication

            self.live_os = QApplication.instance() is not None
        except Exception:
            self.live_os = False

    def bind_probe(self, hook: Callable[[UserSignals], None]) -> None:
        self._hook = hook

    def note_key(self, t: float | None = None) -> None:
        t = t or _now()
        self._keys.append(t)
        self._last_input = t
        self._last_key = t
        if not self._typing:
            self._typing = True
            self.bus.emit(WorldEvent.USER_TYPING_START, t, source="signals")

    def note_scroll(self, t: float | None = None) -> None:
        t = t or _now()
        self._scrolls.append(t)
        self._last_input = t
        self._last_scroll = t
        if not self._scroll_on:
            self._scroll_on = True
            self.bus.emit(WorldEvent.SCROLL_START, t, source="signals")

    def note_tab(self, t: float | None = None) -> None:
        t = t or _now()
        self._tabs.append(t)
        self._last_input = t

    def note_window(self, app: str, t: float | None = None) -> None:
        t = t or _now()
        self._windows.append(t)
        self.signals.foreground_app = app
        if app and app != self._last_app:
            self._last_app = app
            lowered = app.lower()
            if any(token in lowered for token in ("code", "cursor", "devenv", "idea")):
                self.bus.emit(WorldEvent.FOREGROUND_VSCODE, t, source="signals", payload={"app": app})
            elif any(token in lowered for token in ("term", "pwsh", "cmd", "wt", "alacritty")):
                self.bus.emit(WorldEvent.FOREGROUND_TERMINAL, t, source="signals", payload={"app": app})
            elif any(token in lowered for token in ("chrome", "msedge", "firefox", "brave")):
                self.bus.emit(WorldEvent.FOREGROUND_BROWSER, t, source="signals", payload={"app": app})
            self.bus.emit(WorldEvent.WINDOW_FOCUS_CHANGED, t, source="signals", payload={"app": app})

    def teleport_cursor(self, pos: Vec2, buttons: int = 0, t: float | None = None) -> None:
        """Reposition without inventing a warp-speed prediction."""
        t = t or _now()
        self._last_cursor = pos
        self._last_vel = Vec2()
        self._last_sample = t
        self.signals.cursor = pos
        self.signals.cursor_velocity = Vec2()
        self.signals.cursor_predicted = pos
        self.signals.mouse_velocity = 0.0
        self.signals.mouse_acceleration = 0.0
        self.signals.buttons_down = buttons
        self.signals.dragging = False
        self.signals.selecting = False
        self.signals.scrolling = False

    def note_cursor(self, pos: Vec2, buttons: int = 0, t: float | None = None) -> None:
        t = t or _now()
        dt = max(1e-3, t - self._last_sample)
        vel = Vec2((pos.x - self._last_cursor.x) / dt, (pos.y - self._last_cursor.y) / dt)
        acc = Vec2((vel.x - self._last_vel.x) / dt, (vel.y - self._last_vel.y) / dt)
        speed = vel.length()
        if speed > 8.0:
            self._last_input = t
        self.signals.cursor = pos
        self.signals.cursor_velocity = vel
        predict = self.cfg.cursor_predict_ms / 1000.0
        self.signals.cursor_predicted = Vec2(pos.x + vel.x * predict, pos.y + vel.y * predict)
        self.signals.mouse_velocity = speed
        self.signals.mouse_acceleration = acc.length()
        self.signals.buttons_down = buttons
        self.signals.dragging = buttons != 0 and speed > 40.0
        self.signals.selecting = buttons != 0 and not self.signals.dragging
        if buttons and self._last_buttons == 0:
            self.signals.recent_click = pos
            self.signals.recent_click_t = t
        self._last_buttons = buttons
        if self.signals.dragging:
            last = self.bus.last(WorldEvent.MOUSE_DRAG)
            if last is None or t - last.t > 0.12:
                self.bus.emit(WorldEvent.MOUSE_DRAG, t, source="signals")
        elif self.signals.selecting and buttons:
            last = self.bus.last(WorldEvent.MOUSE_SELECTION)
            if last is None or t - last.t > 0.12:
                self.bus.emit(WorldEvent.MOUSE_SELECTION, t, source="signals")
        if speed > 900.0:
            last = self.bus.last(WorldEvent.MOUSE_MOVE_FAST)
            if last is None or t - last.t > 0.12:
                self.bus.emit(WorldEvent.MOUSE_MOVE_FAST, t, source="signals")
        elif 20.0 < speed < 180.0:
            last = self.bus.last(WorldEvent.MOUSE_MOVE_SLOW)
            if last is None or t - last.t > 0.25:
                self.bus.emit(WorldEvent.MOUSE_MOVE_SLOW, t, source="signals")
        self._last_cursor = pos
        self._last_vel = vel
        self._last_sample = t

    def _probe_keys(self, t: float) -> None:
        try:
            import ctypes

            user32 = ctypes.windll.user32
            # Content-free rhythm probe: any printable / control key down.
            for vk in (0x08, 0x09, 0x0D, 0x20, *range(0x30, 0x5B), *range(0xBA, 0xC1)):
                if user32.GetAsyncKeyState(vk) & 0x0001:
                    self.note_key(t)
                    break
            if user32.GetAsyncKeyState(0x22) & 0x0001 or user32.GetAsyncKeyState(0x21) & 0x0001:
                self.note_scroll(t)
            # Wheel virtual keys (VK_WHEEL up/down aliases used by some hosts) + mouse wheel bit.
            if user32.GetAsyncKeyState(0x05) & 0x0001 or user32.GetAsyncKeyState(0x06) & 0x0001:
                self.note_scroll(t)
        except Exception:
            pass

    def poll_os(self) -> None:
        if not self.live_os:
            return
        self._probe_keys(_now())
        try:
            from PyQt6.QtGui import QCursor, QGuiApplication

            pt = QCursor.pos()
            buttons = 0
            try:
                buttons = int(QGuiApplication.mouseButtons().value)
            except Exception:
                buttons = 0
            self.note_cursor(Vec2(float(pt.x()), float(pt.y())), buttons)
        except Exception:
            pass
        try:
            import ctypes

            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            length = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            name = ""
            try:
                import psutil

                name = psutil.Process(pid.value).name()
            except Exception:
                # Process identity only — never keep the window title.
                name = ""
            if name:
                prev = self._last_app
                self.note_window(name)
                if prev and prev != name:
                    self.note_tab()
        except Exception:
            pass
        if self._hook is not None:
            self._hook(self.signals)
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            rect = wintypes.RECT()
            if hwnd and user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                from .types import Rect as WorldRect

                # Never treat the toaster overlay itself as the work sanctuary.
                w = max(0, rect.right - rect.left)
                h = max(0, rect.bottom - rect.top)
                if w > 280 and h > 280:
                    self.signals.work_rect = WorldRect(float(rect.left), float(rect.top), float(w), float(h))
        except Exception:
            pass

    def tick(self, t: float) -> UserSignals:
        if self.live_os:
            self.poll_os()
        window = 2.4
        keys = [x for x in self._keys if t - x <= window]
        scrolls = [x for x in self._scrolls if t - x <= window]
        tabs = [x for x in self._tabs if t - x <= 8.0]
        wins = [x for x in self._windows if t - x <= 8.0]
        self.signals.typing_rate = len(keys) / window
        if len(keys) >= 3:
            gaps = [keys[i] - keys[i - 1] for i in range(1, len(keys))]
            mean = sum(gaps) / len(gaps)
            var = sum((g - mean) ** 2 for g in gaps) / len(gaps)
            self.signals.typing_burstiness = min(1.0, math.sqrt(var) / max(0.04, mean))
            self.signals.typing_regularity = max(0.0, 1.0 - self.signals.typing_burstiness)
        else:
            self.signals.typing_burstiness = 0.0
            self.signals.typing_regularity = 0.0
        if self._typing and (not keys or t - self._last_key > 1.1):
            self._typing = False
            self.bus.emit(WorldEvent.USER_TYPING_STOP, t, source="signals")
        if self._typing and self.signals.typing_rate >= self.cfg.typing_fast_threshold:
            last = self.bus.last(WorldEvent.USER_TYPING_FAST)
            if last is None or t - last.t > 0.4:
                self.bus.emit(WorldEvent.USER_TYPING_FAST, t, source="signals")
        if self._typing and self.signals.typing_burstiness >= 0.55 and self.signals.typing_rate >= self.cfg.typing_burst_threshold * 0.4:
            last = self.bus.last(WorldEvent.USER_TYPING_BURST)
            if last is None or t - last.t > 0.4:
                self.bus.emit(WorldEvent.USER_TYPING_BURST, t, source="signals")
        self.signals.scroll_rate = len(scrolls) / window
        self.signals.scroll_burstiness = min(1.0, len(scrolls) / 10.0)
        self.signals.scrolling = bool(scrolls) and t - self._last_scroll < 0.35
        if self._scroll_on and t - self._last_scroll > 0.45:
            self._scroll_on = False
            self.bus.emit(WorldEvent.SCROLL_STOP, t, source="signals")
        if self.signals.scroll_burstiness > 0.7:
            self.bus.emit(WorldEvent.SCROLL_BURST, t, source="signals")
        self.signals.tab_switch_rate = len(tabs) / 8.0
        self.signals.window_switch_rate = len(wins) / 8.0
        self.signals.tab_switch_count = len(tabs)
        self.signals.window_switch_count = len(wins)
        if scrolls:
            self.signals.recent_scroll_region = self.signals.cursor
        if self.signals.tab_switch_rate >= self.cfg.tab_switch_threshold:
            last = self.bus.last(WorldEvent.TAB_SWITCH_SPIKE)
            if last is None or t - last.t > 1.0:
                self.bus.emit(WorldEvent.TAB_SWITCH_SPIKE, t, source="signals")
        self.signals.mouse_idle_time = t - self._last_input
        self.signals.idle_duration = self.signals.mouse_idle_time
        self.signals.session_length = t - self.started
        local = time.localtime()
        self.signals.time_of_day = (local.tm_hour * 60 + local.tm_min) / 1440.0
        app = self.signals.foreground_app.lower()
        terminalish = any(token in app for token in ("term", "pwsh", "cmd", "wt"))
        codeish = any(token in app for token in ("code", "cursor", "devenv", "idea", "pycharm"))
        browserish = any(token in app for token in ("chrome", "msedge", "firefox", "brave"))
        self.signals.terminal_activity = min(1.0, (self.signals.typing_rate / 8.0) + (0.4 if terminalish else 0.0))
        if self.signals.terminal_activity > 0.72:
            last = self.bus.last(WorldEvent.TERMINAL_ACTIVITY_HIGH)
            if last is None or t - last.t > 2.0:
                self.bus.emit(WorldEvent.TERMINAL_ACTIVITY_HIGH, t, source="signals")
        self.signals.focus_score = 0.0
        if codeish or terminalish:
            self.signals.focus_score += 0.38
        self.signals.focus_score += min(0.4, self.signals.typing_regularity * 0.45)
        self.signals.focus_score += 0.2 if self.signals.typing_rate > 2.5 else 0.0
        self.signals.focus_score -= min(0.45, self.signals.tab_switch_rate)
        self.signals.focus_score = max(0.0, min(1.0, self.signals.focus_score))
        self.signals.doomscroll_score = 0.0
        if browserish:
            self.signals.doomscroll_score += 0.28
        self.signals.doomscroll_score += min(0.5, self.signals.scroll_burstiness)
        self.signals.doomscroll_score += min(0.3, self.signals.tab_switch_rate)
        self.signals.doomscroll_score -= min(0.4, self.signals.typing_rate / 10.0)
        self.signals.doomscroll_score = max(0.0, min(1.0, self.signals.doomscroll_score))
        self.signals.attention_fragmentation = min(1.0, self.signals.tab_switch_rate + self.signals.window_switch_rate)
        self.signals.novelty_seeking_proxy = min(1.0, self.signals.doomscroll_score * 0.6 + self.signals.attention_fragmentation * 0.4)
        late = local.tm_hour >= 23 or local.tm_hour < 5
        if late:
            last = self.bus.last(WorldEvent.LATE_NIGHT)
            if last is None or t - last.t > 30.0:
                self.bus.emit(WorldEvent.LATE_NIGHT, t, source="signals")
        if 8 <= local.tm_hour < 19:
            last = self.bus.last(WorldEvent.WORK_HOURS)
            if last is None or t - last.t > 30.0:
                self.bus.emit(WorldEvent.WORK_HOURS, t, source="signals")
        if self.signals.idle_duration > 90.0:
            last = self.bus.last(WorldEvent.USER_IDLE_LONG)
            if last is None or t - last.t > 20.0:
                self.bus.emit(WorldEvent.USER_IDLE_LONG, t, source="signals")
        if self.signals.mouse_idle_time > 4.0 and self.signals.mouse_velocity < 4.0:
            last = self.bus.last(WorldEvent.MOUSE_IDLE)
            if last is None or t - last.t > 2.0:
                self.bus.emit(WorldEvent.MOUSE_IDLE, t, source="signals")
        activity = ActivityState.IDLE
        if late and self.signals.typing_rate < 1.0:
            activity = ActivityState.LATE_NIGHT
        elif self.signals.doomscroll_score >= self.cfg.doomscroll_threshold:
            activity = ActivityState.DOOMSCROLLING
            last = self.bus.last(WorldEvent.DOOMSCROLL_SCORE_HIGH)
            if last is None or t - last.t > 2.0:
                self.bus.emit(WorldEvent.DOOMSCROLL_SCORE_HIGH, t, source="signals")
        elif self.signals.terminal_activity > 0.7:
            activity = ActivityState.TERMINAL_HEAVY
        elif self.signals.focus_score >= self.cfg.focus_threshold and (codeish or terminalish):
            activity = ActivityState.CODING_FLOW
            last = self.bus.last(WorldEvent.FOCUS_SCORE_HIGH)
            if last is None or t - last.t > 2.0:
                self.bus.emit(WorldEvent.FOCUS_SCORE_HIGH, t, source="signals")
        elif self.signals.typing_burstiness > 0.7 and self.signals.typing_rate > 6.0:
            activity = ActivityState.FRANTIC
        elif browserish:
            activity = ActivityState.BROWSING
        elif self.signals.focus_score > 0.45:
            activity = ActivityState.FOCUSED
        elif self.signals.idle_duration < 8.0:
            activity = ActivityState.FOCUSED if self.signals.typing_rate > 0.4 else ActivityState.IDLE
        if activity is ActivityState.DOOMSCROLLING and self.signals.scroll_burstiness > 0.55:
            last = self.bus.last(WorldEvent.SHORT_VIDEO_LOOP_PATTERN)
            if last is None or t - last.t > 3.0:
                self.bus.emit(WorldEvent.SHORT_VIDEO_LOOP_PATTERN, t, source="signals")
        if self.lock_activity is not None:
            activity = self.lock_activity
            self._activity_since = t
        elif activity is not self._last_activity and (t - self._activity_since) < 0.55:
            activity = self._last_activity
        elif activity is not self._last_activity:
            self._activity_since = t
        self.signals.activity = activity
        self.signals.activity_hold_t = t - self._activity_since
        self._last_activity = activity
        return self.signals
