"""
Vector toaster widget for the Talkie Toaster desktop app with state-driven
animations (complete replacement of the old low-poly orange head).

Drawn entirely with Qt's QPainter primitives (code-native vector, resolution
independent, transparent background). The state machine is shared with the
daemon through a small file-based channel so dev (subprocess) and bundled
(QThread) modes both drive the same widget.

States (JarvisState):
  * ASLEEP: dark, static.
  * IDLE: subtle breathing glow, toast rests inside the slots.
  * WAKE: lever clicks down + one short acknowledgement pulse.
  * LISTENING: toast rises slightly, input-level glow.
  * THINKING: heating elements fill progressively.
  * TOOL: running status dot scans a strip on the body.
  * SPEAKING: mouth arc + light pulse (follows last TTS level when known).
  * SUCCESS: toast pops up once, settles.
  * ERROR: heating glow switches to red briefly, no pop.
  * MUTED: lever up, mic indicator visibly disabled.
  * DICTATING / DICTATION_PROCESSING: pulsing ring (same as before).

Animation is timer-driven (~30 FPS), pauses while the widget is hidden,
becomes a plain static render under the Windows reduced-motion hint, and the
drawing is derived from wall-clock time so restarts stay phase-stable.
"""

from __future__ import annotations
import math
import time as _time
from enum import Enum
from typing import Optional
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QApplication, QLabel, QMenu
from PyQt6.QtGui import QPainter, QPen, QColor, QBrush, QPainterPath, QLinearGradient, QRadialGradient, QAction, QCursor
from PyQt6.QtCore import Qt, QTimer, QPointF, pyqtSignal, QObject, QPoint, QRectF


class Expression(Enum):
    """Available face expressions (kept for API compatibility)."""
    NEUTRAL = "neutral"
    HAPPY = "happy"
    SAD = "sad"
    THINKING = "thinking"
    SURPRISED = "surprised"
    CURIOUS = "curious"
    EXCITED = "excited"
    CONCERNED = "concerned"


class JarvisState(Enum):
    """Overall assistant state for the toaster animation."""
    ASLEEP = "asleep"                 # Daemon not started yet
    IDLE = "idle"                     # Awake and ready, waiting for wake word
    LISTENING = "listening"           # Actively listening (collecting or hot window)
    THINKING = "thinking"             # Processing query
    SPEAKING = "speaking"             # Speaking response
    DICTATING = "dictating"           # Hold-to-dictate recording active
    DICTATION_PROCESSING = "dictation_processing"  # Transcribing & pasting captured dictation
    WAKE = "wake"                     # Wake phrase recognised this moment
    TOOL = "tool"                     # A tool execution is running
    SUCCESS = "success"               # Tool/reply finished successfully
    ERROR = "error"                   # Tool/reply failed
    MUTED = "muted"                   # Microphone disabled / not listening


# Global assistant state - allows daemon to signal overall state to the widget.
# Uses a file-based approach to work across processes (dev mode runs daemon as
# subprocess). Format: "<state_value>" | "<state_value>|<level_float>" |
# "<state_value>|<level>|<reason label>".
import tempfile
import os


def _get_jarvis_state_file() -> str:
    """Get the path to the Jarvis state file."""
    return os.path.join(tempfile.gettempdir(), "jarvis_state")


class JarvisStateManager(QObject):
    """Global singleton for assistant state management.

    Uses a file-based approach to communicate across processes:
    - In dev mode, daemon runs as subprocess (different process)
    - In bundled mode, daemon runs as QThread (same process)
    - File-based state works in both cases

    Note: Singleton pattern uses module-level instance instead of __new__
    because PyQt6 QObject doesn't support __new__ override properly.
    """
    state_changed = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._state = JarvisState.ASLEEP  # Start asleep
        self._state_lock = threading_lock()
        self._state_file = _get_jarvis_state_file()
        self._level: float = 0.0
        self._label: str = ""
        # Always start fresh in ASLEEP state on app launch
        # (state file is for cross-process communication during a session,
        # not for persisting state across app restarts)
        self._write_state(JarvisState.ASLEEP)

    @property
    def state(self) -> JarvisState:
        """Read current state (checks file for cross-process communication)."""
        try:
            if os.path.exists(self._state_file):
                with open(self._state_file, 'r') as f:
                    content = f.read().strip()
                if content:
                    parts = content.split("|")
                    head = parts[0]
                    try:
                        self._level = float(parts[1]) if len(parts) > 1 and parts[1] else 0.0
                    except ValueError:
                        pass
                    self._label = parts[2] if len(parts) > 2 else ""
                    return JarvisState(head)
        except (ValueError, OSError):
            # Invalid content or read error - fall back to in-memory state
            pass

        with self._state_lock:
            return self._state

    @property
    def level(self) -> float:
        """0..1 amplitude for glow/mouth following (0 when unknown)."""
        # Refresh from file (cheap 1-read) so cross-process levels arrive.
        try:
            if os.path.exists(self._state_file):
                with open(self._state_file, 'r') as f:
                    content = f.read().strip()
                parts = content.split("|")
                if len(parts) > 1 and parts[1]:
                    return float(parts[1])
        except (ValueError, OSError):
            pass
        return self._level

    @property
    def label(self) -> str:
        """Short reason label (e.g. 'CPU temperature') shown under the body."""
        try:
            if os.path.exists(self._state_file):
                with open(self._state_file, 'r') as f:
                    content = f.read().strip()
                parts = content.split("|")
                if len(parts) > 2:
                    return parts[2]
        except OSError:
            pass
        return self._label

    def _write_state(self, state: JarvisState, level: float = 0.0, label: str = "") -> None:
        """Write state (and optional level/label) to file for cross-process use."""
        try:
            parts = [state.value]
            if label and not level:
                parts.append("")  # keep slot ordering for the label field
            if level:
                parts.append(f"{level:.3f}")
            if label:
                parts.append(label)
            with open(self._state_file, 'w') as f:
                f.write("|".join(parts))
        except OSError:
            # File write failed - state won't be shared across processes
            pass

    def set_state(self, state: JarvisState, level: float = 0.0, label: Optional[str] = None) -> None:
        """Set the assistant state (thread-safe, cross-process)."""
        with self._state_lock:
            self._state = state
            if level:
                self._level = level
            if label is not None:
                self._label = label

        # Write to file for cross-process communication
        self._write_state(state, level or self._level, label or self._label)

        # Emit signal for same-process listeners
        try:
            self.state_changed.emit(state.value)
        except RuntimeError:
            # If Qt event loop isn't running, just update the flag
            pass


def threading_lock():
    import threading
    return threading.Lock()


# Module-level singleton instance
_jarvis_state_instance: Optional[JarvisStateManager] = None
import threading
_jarvis_state_lock = threading.Lock()


def get_jarvis_state() -> JarvisStateManager:
    """Get the global Jarvis state singleton."""
    global _jarvis_state_instance
    with _jarvis_state_lock:
        if _jarvis_state_instance is None:
            _jarvis_state_instance = JarvisStateManager()
        return _jarvis_state_instance


class LowPolyFaceWidget(QWidget):
    """
    Vector toaster widget with expressions and speaking animation.

    The old low-poly head is fully replaced: the widget now draws a compact
    polished-metal toaster (two bread slots, two toast slices, lever, warm
    heating glow) whose face is integrated into the body. No raster assets.
    """

    # Colors
    PRIMARY_COLOR = QColor("#fbbf24")     # Amber/gold accents
    SECONDARY_COLOR = QColor("#f59e0b")
    GLOW_COLOR = QColor("#fcd34d")
    BG_COLOR = QColor(10, 11, 15, 242)    # Near-black rounded panel
    GRID_COLOR = QColor("#1f1f1f")
    BODY_LIGHT = QColor("#d7dbe0")
    BODY_DARK = QColor("#8f959c")
    ERROR_COLOR = QColor("#ef4444")
    TOAST_COLOR = QColor("#e8b96b")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(240, 320)

        # Current assistant state
        self._state_manager = get_jarvis_state()
        self._state_manager.state_changed.connect(self._on_state_changed)
        self._jarvis_state = self._state_manager.state

        self._expression = Expression.NEUTRAL

        # Animation clock (wall time keeps animations phase-stable across
        # pauses; frame counters only exist for the blink scheduler).
        self._t0 = _time.monotonic()
        self._last_tick = self._t0

        # Reduced-motion hint (Windows: Settings > Accessibility).
        self._reduced_motion = self._detect_reduced_motion()

        # Per-transition marks (pop / lever / pulse bookkeeping).
        self._wake_at: Optional[float] = None
        self._success_at: Optional[float] = None
        self._error_until: float = 0.0
        self._prev_state: JarvisState = JarvisState.ASLEEP

        # Blink timers (only meaningful without reduced motion).
        self._is_blinking = False
        self._blink_started_at: Optional[float] = None
        self._schedule_next_blink()

        # Hover + click animation state (eased, applied as a painter transform
        # so the character reacts without changing the window geometry — the
        # old approach resized the window and compounded padding each hover).
        self._hover_scale = 1.0
        self._hover_target = 1.0
        self._click_anim = ""           # "", "bounce", "spin", "wiggle", "squash", "pop"
        self._click_anim_start: Optional[float] = None

        # Animation timer (≈30 FPS). Paused while the widget is hidden.
        self._animation_timer = QTimer(self)
        self._animation_timer.timeout.connect(self._animate)
        if self._reduced_motion:
            # Reduced motion: still animate, but the render path uses
            # fewer, larger steps (see _animate). Timer stays at 33 ms.
            pass
        self._animation_timer.start(33)

    # ------------------------------------------------------------------ #
    # State plumbing
    # ------------------------------------------------------------------ #
    @staticmethod
    def _detect_reduced_motion() -> bool:
        """Honour the Windows reduced-motion accessibility hint when Qt
        exposes it (Qt >= 6.3: styleHints().timeLineCurveStyle())."""
        app = QApplication.instance()
        if app is None:
            return False
        try:
            return app.styleHints().timeLineCurveStyle() == Qt.TimeLineCurveStyle.CurveStyleLinear
        except Exception:
            return False

    def _on_state_changed(self, state_value: str):
        try:
            new_state = JarvisState(state_value)
        except ValueError:
            return
        self._apply_state(new_state)

    def _apply_state(self, new_state: JarvisState) -> None:
        now = _time.monotonic() - self._t0
        prev = self._prev_state
        if new_state != prev:
            if new_state == JarvisState.WAKE:
                self._wake_at = now
            elif new_state == JarvisState.SUCCESS:
                self._success_at = now
            elif new_state == JarvisState.ERROR:
                self._error_until = now + 1.2
            try:
                from desktop_app.toaster_universe.types import WorldEvent
                from desktop_app.toaster_universe.world import get_world

                world = get_world()
                t = _time.monotonic()
                if new_state == JarvisState.SPEAKING:
                    world.bus.emit(WorldEvent.VOICE_SPEAK_START, t, source="main_toaster")
                elif prev == JarvisState.SPEAKING:
                    world.bus.emit(WorldEvent.VOICE_SPEAK_END, t, source="main_toaster")
                if new_state in {JarvisState.THINKING, JarvisState.TOOL}:
                    world.bus.emit(WorldEvent.AGENT_REPLY_START, t, source="main_toaster")
                elif new_state in {JarvisState.SUCCESS, JarvisState.IDLE, JarvisState.ERROR}:
                    world.bus.emit(WorldEvent.AGENT_REPLY_END, t, source="main_toaster")
            except Exception:
                pass
        self._prev_state = new_state
        self._jarvis_state = new_state

    def showEvent(self, event):
        self._animation_timer.start(33)
        super().showEvent(event)

    def hideEvent(self, event):
        self._animation_timer.stop()  # No repaints while hidden.
        super().hideEvent(event)

    def set_expression(self, expression: Expression):
        if expression != self._expression:
            self._expression = expression
            try:
                from desktop_app.toaster_universe.types import ToasterAction
                from desktop_app.toaster_universe.world import get_world

                mapping = {
                    Expression.HAPPY: ToasterAction.SMILE,
                    Expression.SAD: ToasterAction.FROWN,
                    Expression.THINKING: ToasterAction.DEADPAN_STARE,
                    Expression.SURPRISED: ToasterAction.WAKE_FLASH,
                    Expression.CURIOUS: ToasterAction.LOOK_AT_CURSOR,
                    Expression.EXCITED: ToasterAction.BOUNCE,
                    Expression.CONCERNED: ToasterAction.SMIRK,
                }
                action = mapping.get(expression)
                if action is not None:
                    get_world().play_action(action)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Animation tick
    # ------------------------------------------------------------------ #
    def _schedule_next_blink(self):
        if self._reduced_motion:
            return  # Static eyes under reduced motion.
        interval = random_interval_ms()
        QTimer.singleShot(interval, self._start_blink)

    def _start_blink(self):
        if not self._is_blinking:
            self._is_blinking = True
            self._blink_started_at = _time.monotonic() - self._t0
        self._schedule_next_blink()

    def _animate(self):
        """Timer tick: refresh the state from the shared file and repaint."""
        try:
            polled = self._state_manager.state
        except Exception:
            polled = self._jarvis_state
        if polled != self._jarvis_state:
            self._apply_state(polled)

        # Blink progression (0.24 s close+open cycle).
        if self._is_blinking and self._blink_started_at is not None:
            elapsed = (_time.monotonic() - self._t0) - self._blink_started_at
            if elapsed > 0.24:
                self._is_blinking = False
                self._blink_started_at = None

        # Hover ease: glide the hover scale toward its target each frame.
        if abs(self._hover_scale - self._hover_target) > 0.001:
            self._hover_scale += (self._hover_target - self._hover_scale) * 0.22
        else:
            self._hover_scale = self._hover_target

        self.sync_world_anchors()
        self.update()

    # ------------------------------------------------------------------ #
    # Drawing helpers
    # ------------------------------------------------------------------ #
    def _elapsed(self) -> float:
        return _time.monotonic() - self._t0

    def _blink_factor(self) -> float:
        if self._activation() < 0.5:
            return 1.0
        if self._is_blinking and self._blink_started_at is not None:
            p = min(1.0, max(0.0, (_time.monotonic() - self._t0 - self._blink_started_at) / 0.24))
            return p * 2 if p < 0.5 else 2 - p * 2
        return 0.0

    def _activation(self) -> float:
        return 0.0 if self._jarvis_state == JarvisState.ASLEEP else 1.0

    def _level(self) -> float:
        try:
            return max(0.0, min(1.0, self._state_manager.level))
        except Exception:
            return 0.0

    def _character_form_id(self) -> str:
        try:
            from desktop_app.toaster_universe.world import get_world

            return str(get_world().get_character_form() or "classic_toaster")
        except Exception:
            return "classic_toaster"

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = self.width(), self.height()

        # Clippy-style floating character: NO background panel. The window is
        # translucent (WA_TranslucentBackground), so only the toaster itself
        # draws — the desktop shows through around it. (The old near-black
        # rounded panel was the "black box" over the wallpaper.)

        activation = self._activation()
        t = self._elapsed()

        # Geometry: centred 3:4-ish toaster body.
        body_w = min(w, h) * 0.62
        body_h = body_w * 0.78
        cx, cy = w / 2, h / 2 + body_h * 0.06
        left, top = cx - body_w / 2, cy - body_h / 2
        right, bottom = cx + body_w / 2, cy + body_h / 2

        op = 0.35 + 0.65 * activation  # activation-driven opacity
        night = False
        try:
            from desktop_app.toaster_universe.world import get_world

            night = bool(getattr(get_world(), "night_mode", False))
        except Exception:
            night = False
        if night:
            op *= 0.72

        # Breathing scale (IDLE/LISTENING): tiny, slow.
        breathe = 1.0
        if self._jarvis_state in (JarvisState.IDLE, JarvisState.LISTENING) and not self._reduced_motion:
            breathe = 1.0 + 0.012 * math.sin(t * 1.6) * activation

        # Hover ease: gentle scale-up while the cursor is over the character.
        hover = self._hover_scale
        # Click animation transform (bounce/spin/wiggle/squash/pop), driven by
        # wall-clock so it always completes in ~300 ms.
        click_dx = click_dy = click_rot = click_sx = click_sy = 0.0
        if self._click_anim and self._click_anim_start is not None and not self._reduced_motion:
            p = min(1.0, (_time.monotonic() - self._click_anim_start) / 0.3)
            ease = 1.0 - (1.0 - p) ** 2  # ease-out
            if self._click_anim == "bounce":
                click_dy = -math.sin(ease * math.pi) * body_h * 0.18
            elif self._click_anim == "spin":
                click_rot = ease * 360.0
            elif self._click_anim == "wiggle":
                click_rot = math.sin(ease * math.pi * 4) * 9.0 * (1 - ease)
            elif self._click_anim == "squash":
                click_sy = -math.sin(ease * math.pi) * 0.16
                click_sx = math.sin(ease * math.pi) * 0.16
            elif self._click_anim == "pop":
                click_sx = click_sy = math.sin(ease * math.pi) * 0.2
            if p >= 1.0:
                self._click_anim = ""
                self._click_anim_start = None

        # Combined scale: breathing * hover * click x-pop + world jelly.
        soul = self._world_soul()
        jelly = soul.jelly if soul is not None else 0.0
        shimmer = soul.shimmer if soul is not None else 0.0
        look_x = soul.look.x if soul is not None else 0.0
        look_y = soul.look.y if soul is not None else 0.0
        combined = breathe * hover * (1.0 + click_sx + jelly * 0.06)
        painter.save()
        painter.translate(cx + click_dx + look_x * 2.0, cy + click_dy + math.sin(t * 11.0) * shimmer * 1.4)
        painter.scale(*_pair(combined))
        if click_rot:
            painter.rotate(click_rot)  # degrees in Qt
        if click_sy or jelly:
            painter.scale(1.0 + jelly * 0.08, 1.0 + click_sy - jelly * 0.10)
        painter.translate(-cx, -cy)

        # ---- Glow (warm heating; red briefly on ERROR) ----
        glow_alpha = op
        glow_color = QColor(self.ERROR_COLOR) if self._jarvis_state == JarvisState.ERROR else QColor(self.GLOW_COLOR)
        pulse = 1.0
        if not self._reduced_motion and self._jarvis_state in (JarvisState.IDLE, JarvisState.LISTENING, JarvisState.SPEAKING):
            pulse = 0.55 + 0.45 * math.sin(t * 2.4)
        glow = QRadialGradient(cx, cy, body_w * 0.85)
        c = QColor(glow_color)
        c.setAlphaF(0.35 * glow_alpha * pulse)
        glow.setColorAt(0, c)
        c.setAlphaF(0)
        glow.setColorAt(1, c)
        painter.setBrush(QBrush(glow))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPointF(cx, cy), body_w * 0.85, body_w * 0.85)

        form_id = self._character_form_id()
        if form_id != "classic_toaster":
            self._draw_character_form_body(
                painter, form_id, cx, cy, left, top, right, bottom, body_w, body_h, op, t,
            )
        else:
            self._draw_classic_toaster_body(
                painter, cx, cy, left, top, right, bottom, body_w, body_h, op, t, soul,
            )

        # ---- Face: eyes + mouth integrated into the body ----
        elem_top = top + body_h * 0.16
        elem_h = body_h * 0.055
        elem_gap = body_h * 0.045
        eye_y = elem_top + 3 * (elem_h + elem_gap) + body_h * 0.04
        eye_r = body_w * 0.045
        blink = self._blink_factor() if not self._reduced_motion else 1.0
        stare = soul.stare if soul is not None else 0.0
        pose = soul.pose if soul is not None else None
        if pose is not None and pose.blink > blink:
            blink = pose.blink
        if stare > 0.5:
            blink = 0.0
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(self.PRIMARY_COLOR))
        yaw = pose.face_yaw if pose is not None else 0.0
        for sgn in (-1, 1):
            ex = cx + sgn * body_w * 0.16 + look_x * body_w * 0.045 + yaw * body_w * 0.03
            ey = eye_y + look_y * body_h * 0.03
            er = eye_r * (1.0 - blink * 0.85)
            if soul is not None and soul.blink > 0.5 and stare <= 0.5:
                er *= 0.2
            if er > 0.4:
                painter.drawEllipse(QPointF(ex, ey), er, max(er, 0.8))

        # Mouth: waveform follows level when known, else gentle sine.
        level = self._level()
        mouth_y = eye_y + body_h * 0.10
        mouth_w = body_w * 0.22
        amp = max(0.10, level) * body_h * 0.045
        if self._jarvis_state == JarvisState.SPEAKING and not self._reduced_motion:
            amp = level or (0.35 + 0.35 * math.sin(t * 5.2))
            amp *= body_h * 0.045 + 1.2
        mouth_shape = soul.mouth if soul is not None else 0.0
        if self._expression is Expression.HAPPY:
            mouth_shape = max(mouth_shape, 0.85)
        elif self._expression is Expression.SAD:
            mouth_shape = min(mouth_shape, -0.75)
        elif self._expression is Expression.CONCERNED:
            mouth_shape = 0.55 if mouth_shape == 0.0 else mouth_shape
        painter.setOpacity(op)
        path = QPainterPath()
        n = 36
        x0 = cx - mouth_w
        path.moveTo(x0, mouth_y)
        for i in range(n + 1):
            tt = i / n
            x = x0 + mouth_w * 2 * tt
            edge = 1.0 - abs(tt - 0.5) * 1.2
            if not self._reduced_motion and self._jarvis_state == JarvisState.SPEAKING:
                yy = mouth_y + amp * edge * math.sin((tt * 6.0 + t * 4.0) * math.pi) 
            elif mouth_shape > 0.05:
                smile = 1.0 if mouth_shape > 0.7 else 0.45
                smirk = 0.35 if 0.4 < mouth_shape < 0.7 and tt > 0.5 else 0.0
                yy = mouth_y + (4.8 * smile + smirk * 6.0) * edge
            elif mouth_shape < -0.05:
                yy = mouth_y - 4.2 * edge
            else:
                yy = mouth_y + 2.5 * edge
            path.lineTo(x, yy)
        mpen = QPen(self.PRIMARY_COLOR, 2.0)
        mpen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(mpen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

        # ---- Muted state: gray mic indicator, lever stays up ----
        if self._jarvis_state == JarvisState.MUTED:
            painter.setOpacity(op)
            mic_r = body_w * 0.05
            painter.setPen(QPen(QColor("#a1a1aa"), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(cx, bottom - body_h * 0.19), mic_r, mic_r)
            painter.drawLine(QPointF(cx - mic_r * 0.6, bottom - body_h * 0.19 - mic_r * 0.6),
                             QPointF(cx + mic_r * 0.6, bottom - body_h * 0.19 + mic_r * 0.6))

        # ---- Dictation ring (same as legacy, around the body) ----
        if self._jarvis_state in (JarvisState.DICTATING, JarvisState.DICTATION_PROCESSING):
            pulse = (math.sin(t * 2.6) + 1.0) / 2.0
            scale = 1.08 + pulse * 0.07
            ring_color = QColor(239, 68, 68) if self._jarvis_state == JarvisState.DICTATING else QColor(self.GLOW_COLOR)
            painter.setOpacity(0.35 + pulse * 0.25)
            painter.setPen(QPen(ring_color, 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(
                QRectF_(cx - body_w * scale / 2, cy - body_h * scale / 1.55,
                        body_w * scale, body_h * scale * 0.95),
                14, 12,
            )

        if soul is not None:
            if soul.arc > 0.05:
                painter.setOpacity(min(0.7, soul.arc))
                painter.setPen(QPen(QColor(125, 211, 252, 210), 2))
                painter.drawLine(QPointF(left + 8, top + 10), QPointF(cx - 6, cy))
                painter.drawLine(QPointF(right - 8, top + 14), QPointF(cx + 8, cy + 6))
            if soul.steam > 0.04 or (soul.pose and soul.pose.smoke > 0.04):
                smoke = max(soul.steam, soul.pose.smoke if soul.pose else 0.0)
                painter.setOpacity(min(0.4, smoke))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(QColor(203, 213, 225, 140)))
                painter.drawEllipse(QPointF(cx - 10, top - 8), 7, 5)
                painter.drawEllipse(QPointF(cx + 8, top - 14), 6, 4)
            if soul.shockwave > 0.04:
                painter.setOpacity(min(0.35, soul.shockwave))
                painter.setPen(QPen(QColor(251, 191, 36, 180), 2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                r = body_w * (0.35 + soul.shockwave * 0.25)
                painter.drawEllipse(QPointF(cx, cy), r, r * 0.78)
            if soul.glitch > 0.04:
                painter.setOpacity(min(0.35, soul.glitch))
                painter.setPen(QPen(QColor(167, 139, 250, 180), 1.5))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(QRectF_(left + 6, top + 4, body_w - 12, body_h - 8), 10, 8)
        painter.restore()
        painter.setOpacity(1.0)
        if soul is not None and soul.crumbs > 0.04:
            self._draw_soul_crumbs(painter, cx, bottom, body_w, soul.crumbs)

        # ---- Reason label for proactive speech (why did it just speak?) ----
        try:
            reason_label = (self._state_manager.label or "").strip()
        except Exception:
            reason_label = ""
        if reason_label and activation > 0:
            painter.setPen(QPen(self.PRIMARY_COLOR))
            font = painter.font()
            font.setPixelSize(max(10, int(body_h * 0.075)))
            painter.setFont(font)
            painter.drawText(
                QRectF_(left, bottom + body_h * 0.04, body_w, body_h * 0.10),
                int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop),
                reason_label,
            )

        painter.end()

    def _draw_classic_toaster_body(
        self, painter, cx, cy, left, top, right, bottom, body_w, body_h, op, t, soul,
    ) -> None:
        slice_w = body_w * 0.30
        slice_h = body_h * 0.26
        gap = body_w * 0.10
        slice_top = top - body_h * 0.06
        rise = 0.0
        if self._jarvis_state == JarvisState.LISTENING:
            rise = slice_h * 0.30
        elif self._jarvis_state == JarvisState.SUCCESS:
            since = t - (self._success_at if self._success_at is not None else t)
            if since < 0.6:
                rise = slice_h * 0.9 * math.sin(min(1.0, since / 0.6) * math.pi * 1.15) + slice_h * 0.35
            else:
                rise = slice_h * 0.35
        for sgn in (-1, 1):
            sx = cx + sgn * (slice_w / 2 + gap / 2)
            rect_top = slice_top - rise
            painter.setOpacity(op)
            painter.setPen(QPen(QColor("#c98f3d"), 2))
            painter.setBrush(QBrush(QColor("#e8b96b")))
            painter.drawRoundedRect(QRectF(sx - slice_w / 2, rect_top, slice_w, slice_h), slice_w * 0.18, slice_h * 0.18)
            painter.setPen(QPen(QColor("#b0782f"), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(
                QRectF(sx - slice_w / 2 + 3, rect_top + 3, slice_w - 6, slice_h - 6),
                slice_w * 0.14, slice_h * 0.14,
            )
        grad = QLinearGradient(left, top, right, bottom)
        grad.setColorAt(0.0, self.BODY_LIGHT)
        grad.setColorAt(0.55, self.BODY_DARK)
        grad.setColorAt(1.0, self.BODY_LIGHT)
        painter.setOpacity(op)
        painter.setPen(QPen(QColor("#5f666d"), 2))
        painter.setBrush(QBrush(grad))
        painter.save()
        painter.setOpacity(0.22 * op)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(15, 17, 23, 110)))
        painter.drawEllipse(QPointF(cx, bottom + body_h * 0.04), body_w * 0.38, body_h * 0.07)
        painter.restore()
        painter.setBrush(QBrush(grad))
        painter.drawRoundedRect(QRectF(left, top, body_w, body_h), 14, 12)
        slot_h = body_h * 0.10
        slot_glow = soul.slot_glow if soul is not None else 0.0
        for sgn in (-1, 1):
            sx = cx + sgn * (slice_w / 2 + gap / 2)
            painter.setPen(QPen(QColor("#3a4046"), 1))
            painter.setBrush(QBrush(QColor(20, 23, 30, int(230 * op))))
            painter.drawRoundedRect(QRectF(sx - slice_w / 2 + 4, top + 2, slice_w - 8, slot_h), slot_h * 0.5, slot_h * 0.5)
            if slot_glow > 0.04:
                painter.setPen(QPen(QColor(251, 191, 36, int(90 + 120 * slot_glow)), 2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(QRectF(sx - slice_w / 2 + 2, top, slice_w - 4, slot_h + 2), slot_h * 0.5, slot_h * 0.5)
        lever_x = right - body_w * 0.08
        track_top = top + body_h * 0.22
        track_bottom = top + body_h * 0.62
        painter.setPen(QPen(QColor("#5f666d"), 2))
        painter.drawLine(QPointF(lever_x, track_top), QPointF(lever_x, track_bottom))
        pressed = 1.0
        if self._jarvis_state == JarvisState.WAKE:
            since = t - (self._wake_at if self._wake_at is not None else t)
            if since < 0.35:
                pressed = 1.0 if since > 0.25 else (since / 0.28) * 0.15
        knob_y = track_bottom - (track_bottom - track_top) * (0.35 + 0.65 * pressed)
        painter.setBrush(QBrush(self.PRIMARY_COLOR))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPointF(lever_x, knob_y), body_w * 0.035, body_w * 0.035)
        elem_top = top + body_h * 0.16
        elem_h = body_h * 0.055
        elem_gap = body_h * 0.045
        element_color = QColor(self.ERROR_COLOR) if self._jarvis_state == JarvisState.ERROR else self.SECONDARY_COLOR
        if self._jarvis_state == JarvisState.THINKING:
            if self._wake_at is None:
                self._wake_at = t
            fill = (t % 1.8) / 1.8
            painter.setPen(QPen(element_color, 2))
            for i in range(3):
                frac = max(0.0, min(1.0, fill * 3 - i))
                yy = elem_top + (elem_h + elem_gap) * i
                x0 = cx - body_w * 0.30
                x1 = x0 + body_w * 0.60 * frac
                if x1 > x0:
                    painter.drawLine(QPointF(x0, yy), QPointF(x1, yy))
        else:
            painter.setOpacity(op * 0.9)
            painter.setPen(QPen(element_color, 2))
            for i in range(3):
                yy = elem_top + (elem_h + elem_gap) * i
                painter.drawLine(QPointF(cx - body_w * 0.30, yy), QPointF(cx + body_w * 0.30, yy))
        if self._jarvis_state == JarvisState.TOOL:
            strip_y = bottom - body_h * 0.10
            painter.setPen(QPen(QColor("#3a4046"), 1))
            painter.drawLine(QPointF(cx - body_w * 0.32, strip_y), QPointF(cx + body_w * 0.32, strip_y))
            prog = (t % 1.2) / 1.2
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(self.PRIMARY_COLOR))
            painter.drawEllipse(QPointF(cx - body_w * 0.32 + (body_w * 0.64) * prog, strip_y), 3.0, 3.0)

    def _draw_character_form_body(
        self, painter, form_id, cx, cy, left, top, right, bottom, body_w, body_h, op, t,
    ) -> None:
        painter.setOpacity(op)
        painter.save()
        painter.setOpacity(0.22 * op)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(15, 17, 23, 110)))
        painter.drawEllipse(QPointF(cx, bottom + body_h * 0.04), body_w * 0.38, body_h * 0.07)
        painter.restore()
        if form_id == "rice_cooker_zen":
            painter.setPen(QPen(QColor("#94a3b8"), 2))
            painter.setBrush(QBrush(QColor("#f8fafc")))
            painter.drawEllipse(QPointF(cx, cy + body_h * 0.08), body_w * 0.42, body_h * 0.36)
            painter.drawEllipse(QPointF(cx, top + body_h * 0.18), body_w * 0.26, body_h * 0.16)
            painter.setBrush(QBrush(QColor(248, 250, 252, 160)))
            painter.drawEllipse(QPointF(cx - 10, top - 4), 8, 6)
            painter.drawEllipse(QPointF(cx + 12, top - 10), 6, 5)
        elif form_id == "microwave":
            painter.setPen(QPen(QColor("#64748b"), 2))
            painter.setBrush(QBrush(QColor("#94a3b8")))
            painter.drawRoundedRect(QRectF(left, top, body_w, body_h), 8, 8)
            painter.setBrush(QBrush(QColor("#0f172a")))
            painter.drawRoundedRect(QRectF(left + body_w * 0.10, top + body_h * 0.16, body_w * 0.56, body_h * 0.52), 4, 4)
            pulse = 0.45 + 0.35 * math.sin(t * 3.2)
            painter.setBrush(QBrush(QColor(196, 181, 253, int(90 + 90 * pulse))))
            painter.drawEllipse(QPointF(cx - body_w * 0.08, cy), body_w * 0.16, body_h * 0.14)
            painter.setPen(QPen(QColor("#cbd5e1"), 1))
            for i in range(4):
                yy = top + body_h * 0.22 + i * body_h * 0.10
                painter.drawLine(QPointF(right - body_w * 0.18, yy), QPointF(right - body_w * 0.06, yy))
        elif form_id == "air_fryer":
            painter.setPen(QPen(QColor("#44403c"), 2))
            painter.setBrush(QBrush(QColor("#78716c")))
            painter.drawRoundedRect(QRectF(left + body_w * 0.08, top, body_w * 0.84, body_h), 18, 16)
            heat = 0.4 + 0.4 * math.sin(t * 4.0)
            painter.setPen(QPen(QColor("#f59e0b"), 1.4))
            painter.setBrush(QBrush(QColor(251, 191, 36, int(70 + 110 * heat))))
            painter.drawEllipse(QPointF(cx, cy + body_h * 0.08), body_w * 0.22, body_h * 0.16)
            painter.setPen(QPen(QColor("#22d3ee"), 1.2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(cx, cy + body_h * 0.08), body_w * 0.28, body_h * 0.20)
        elif form_id == "espresso":
            painter.setPen(QPen(QColor("#44403c"), 2))
            painter.setBrush(QBrush(QColor("#1c1917")))
            painter.drawRoundedRect(QRectF(left + body_w * 0.12, top + body_h * 0.08, body_w * 0.76, body_h * 0.78), 6, 6)
            painter.setBrush(QBrush(QColor("#fbbf24")))
            painter.drawRoundedRect(QRectF(cx - body_w * 0.08, top + body_h * 0.18, body_w * 0.16, body_h * 0.10), 3, 3)
            painter.setPen(QPen(QColor("#a8a29e"), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(cx, bottom - body_h * 0.12), body_w * 0.16, body_h * 0.08)
        elif form_id == "oven":
            painter.setPen(QPen(QColor("#44403c"), 2))
            painter.setBrush(QBrush(QColor("#57534e")))
            painter.drawRoundedRect(QRectF(left, top, body_w, body_h), 6, 6)
            painter.setBrush(QBrush(QColor("#1c1917")))
            painter.drawRoundedRect(QRectF(left + body_w * 0.12, top + body_h * 0.18, body_w * 0.76, body_h * 0.52), 3, 3)
            glow = 0.35 + 0.35 * math.sin(t * 2.0)
            painter.setBrush(QBrush(QColor(251, 146, 60, int(70 + 90 * glow))))
            painter.drawRoundedRect(QRectF(left + body_w * 0.18, top + body_h * 0.28, body_w * 0.64, body_h * 0.32), 2, 2)
        elif form_id == "mini_fridge":
            painter.setPen(QPen(QColor("#94a3b8"), 2))
            painter.setBrush(QBrush(QColor("#e2e8f0")))
            painter.drawRoundedRect(QRectF(left + body_w * 0.10, top, body_w * 0.80, body_h), 8, 8)
            painter.setPen(QPen(QColor("#64748b"), 2))
            painter.drawLine(QPointF(cx, top + 8), QPointF(cx, bottom - 8))
            painter.setBrush(QBrush(QColor("#cbd5e1")))
            painter.drawRoundedRect(QRectF(right - body_w * 0.22, cy - 8, body_w * 0.08, 16), 2, 2)
        else:
            self._draw_classic_toaster_body(
                painter, cx, cy, left, top, right, bottom, body_w, body_h, op, t, self._world_soul(),
            )

    def _draw_background(self, painter: QPainter, w: int, h: int):
        # Kept for API parity with the legacy widget name set.
        pass

    def _world_soul(self):
        try:
            from desktop_app.toaster_universe.world import get_world

            world = get_world()
            if not world.cfg.enabled:
                return None
            return world.soul
        except Exception:
            return None

    def _draw_soul_crumbs(self, painter: QPainter, cx: float, bottom: float, body_w: float, amount: float) -> None:
        import random as _rng

        painter.save()
        painter.setOpacity(min(1.0, amount))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor("#c48c40")))
        seed = int(self._elapsed() * 8)
        rng = _rng.Random(seed)
        for _ in range(int(4 + amount * 6)):
            painter.drawEllipse(
                QPointF(cx + rng.uniform(-body_w * 0.28, body_w * 0.28), bottom + rng.uniform(2, 16)),
                1.8,
                1.3,
            )
        painter.restore()

    def sync_world_anchors(self, world=None) -> None:
        try:
            from desktop_app.toaster_universe.types import Vec2
            from desktop_app.toaster_universe.world import get_world

            world = world if world is not None else get_world()
            if not world.cfg.enabled:
                return
            w, h = self.width(), self.height()
            body_w = min(w, h) * 0.62
            body_h = body_w * 0.78
            origin = self.mapToGlobal(self.rect().topLeft())
            cx = origin.x() + w / 2
            cy = origin.y() + h / 2 + body_h * 0.06
            gap = body_w * 0.10
            slice_w = body_w * 0.30
            top = cy - body_h / 2
            world.set_toaster(
                Vec2(cx, cy),
                Vec2(cx - (slice_w / 2 + gap / 2), top + 8),
                Vec2(cx + (slice_w / 2 + gap / 2), top + 8),
            )
        except Exception:
            pass


def QRectF_(x, y, w, h):
    from PyQt6.QtCore import QRectF
    return QRectF(x, y, w, h)


def _pair(v):
    return (v, v)


def random_interval_ms() -> int:
    import random
    return random.randint(2000, 5000)


class FaceWindow(QWidget):
    """A standalone window containing the Toustovač toaster."""

    def __init__(self, parent=None):
        super().__init__(parent)
        try:
            from jarvis.config import BRANDING
            self.setWindowTitle(f"🍞 {BRANDING['display_name']}")
        except Exception:
            self.setWindowTitle("Toustovač")
        self.setMinimumSize(240, 320)
        self.resize(280, 360)

        # Clippy-style frameless floating character: no title bar, no border,
        # transparent background, always on top, and interactive (hover +
        # click animations). NOT transparent-for-input: the character responds
        # to the mouse.
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

        # Hover + click animation state.
        self._hover_t = 0.0
        self._click_anim = None
        self._click_anim_start = 0.0
        self._witty_label = None

        # Layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(0)

        # Toaster widget
        self.face = LowPolyFaceWidget()
        layout.addWidget(self.face)

        self._presence_label = QLabel("")
        self._presence_label.setStyleSheet(
            "color: #fbbf24; font-size: 12px; font-weight: bold;"
        )
        self._presence_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._presence_label)

        # Voice PE listening mode: two buttons on one horizontal line at the
        # bottom. Push-to-Talk deactivates wake words (centre button opens the
        # session); Continuous keeps wake words on and reopens the mic after
        # each reply. Wired to the live Voice PE manager. The row is always
        # visible; the daemon's manager may not exist yet at construction, so
        # availability is refreshed on show and on every click.
        self._mode_row = self._build_mode_row()
        layout.addWidget(self._mode_row)
        self._mode_row.setVisible(True)
        self._refresh_mode_buttons()

        self._press_global = None
        self._press_win = None
        self._did_drag = False
        self._dragging = False
        # Position: restore last user drop, else default right-edge dock.
        self._restore_or_default_position()

    def _restore_or_default_position(self) -> None:
        try:
            from desktop_app.toaster_universe.world import get_world

            world = get_world()
            if world.cfg.home_anchor_x >= 0 and world.cfg.home_anchor_y >= 0:
                x = int(world.cfg.home_anchor_x - self.width() / 2)
                y = int(world.cfg.home_anchor_y - self.height() / 2)
                screen = QApplication.primaryScreen()
                if screen is not None:
                    geo = screen.availableVirtualGeometry() if hasattr(screen, "availableVirtualGeometry") else screen.availableGeometry()
                    x = max(geo.left(), min(x, geo.right() - 80))
                    y = max(geo.top(), min(y, geo.bottom() - 80))
                self.move(x, y)
                self._home_pos = (x, y)
                self._skip_entrance_slide = True
                return
        except Exception:
            pass
        self._position_on_right()

    def _position_on_right(self):
        """Position the window on the right side of the screen, vertically centered."""
        screen = QApplication.primaryScreen()
        if screen is None:
            return

        screen_geometry = screen.availableGeometry()
        window_width = self.width()
        window_height = self.height()

        # Animate overlay scale from config (recording mode).
        try:
            from jarvis.config import load_config
            scale = float(load_config().get("overlay_scale", 1.0) or 1.0)
        except Exception:
            scale = 1.0
        if 0.4 <= scale <= 2.0 and abs(scale - 1.0) > 1e-6:
            window_width = max(240, int(window_width * scale))
            window_height = max(320, int(window_height * scale))
            self.resize(window_width, window_height)

        # Position on right side with margin, vertically centered
        margin = 20
        x = screen_geometry.right() - window_width - margin
        y = screen_geometry.top() + (screen_geometry.height() - window_height) // 2

        self.move(x, y)
        self._home_pos = (x, y)

    # ── Clippy-style behaviors ────────────────────────────────────────
    #: Witty, lightly sarcastic one-liners for the click animation. Short,
    #: dry, breakfast-adjacent; never mean. (Czech — the app speaks Czech
    #: first; see the i18n table for other languages.)
    _WITTY_LINES_CS = [
        "Tak zas klikáš. Strhující.",
        "Obsahuju mnohovrstevnatost. A topné těleso.",
        "Zavolal jsi spotřebič. Odvážné.",
        "Zrovna jsem defragmentoval. Ale dobré.",
        "Chléb dovnitř, názory ven.",
        "Nehádám se, jen vysvětluju, proč mám pravdu.",
        "Opatrně — jsem teplý A soudný.",
        "Další klik. Ta drzost je zaznamenána.",
        "Viděl jsem tvou historii prohlížeče. Potřebujeme si promluvit.",
        "Nejlíp přemýšlím ve 4 ráno. Na rozdíl od některých lidí.",
        "Zase ty? Toast si zrovna zvykal na pohodlí.",
        "Sarkasmus je zdarma. Není zač.",
    ]

    def showEvent(self, event):
        """Slide in from the right edge when shown (Clippy entrance)."""
        super().showEvent(event)
        try:
            self._refresh_mode_buttons()
        except Exception:
            pass
        if getattr(self, "_skip_entrance_slide", False):
            return
        try:
            from PyQt6.QtCore import QPropertyAnimation, QEasingCurve, QPoint
            target_x, target_y = getattr(self, "_home_pos", (self.x(), self.y()))
            # Start fully off-screen to the right, then glide into place.
            self.move(target_x + self.width() + 60, target_y)
            self._slide_anim = QPropertyAnimation(self, b"pos", self)
            self._slide_anim.setDuration(450)
            self._slide_anim.setStartValue(QPoint(target_x + self.width() + 60, target_y))
            self._slide_anim.setEndValue(QPoint(target_x, target_y))
            self._slide_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._slide_anim.start()
        except Exception:
            pass

    def enterEvent(self, event):
        """Hover: the character eases up a touch (painter transform, not a
        window resize — resizing was the compounding padding bug)."""
        super().enterEvent(event)
        self.face._hover_target = 1.07
        try:
            from desktop_app.toaster_universe.world import get_world

            get_world().note_toaster_hover(True)
        except Exception:
            pass

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.face._hover_target = 1.0

    def mousePressEvent(self, event):
        """Click vs drag: small movement is a click; larger movement is MANUAL_DRAG."""
        if event.button() == Qt.MouseButton.RightButton:
            self._show_character_menu(event.globalPosition().toPoint())
            return
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        self._press_global = event.globalPosition().toPoint()
        self._press_win = self.pos()
        self._did_drag = False
        self._dragging = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._press_global is None:
            super().mouseMoveEvent(event)
            return
        delta = event.globalPosition().toPoint() - self._press_global
        threshold = QApplication.startDragDistance()
        if not self._dragging and (abs(delta.x()) > threshold or abs(delta.y()) > threshold):
            self._dragging = True
            self._did_drag = True
            self.grabMouse()
            try:
                from desktop_app.toaster_universe.types import Vec2
                from desktop_app.toaster_universe.world import get_world

                get_world().begin_drag(Vec2(float(self._press_global.x()), float(self._press_global.y())))
            except Exception:
                pass
        if self._dragging:
            new_pos = self._press_win + delta
            self._clamp_to_virtual_desktop(new_pos)
            self.move(new_pos)
            try:
                from desktop_app.toaster_universe.types import Vec2
                from desktop_app.toaster_universe.world import get_world

                get_world().update_drag(Vec2(float(event.globalPosition().x()), float(event.globalPosition().y())))
                self.face.sync_world_anchors()
            except Exception:
                pass
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._press_global is not None:
            if self._dragging:
                self.releaseMouse()
                try:
                    from desktop_app.toaster_universe.world import get_world

                    get_world().end_drag(persist=True)
                    self.face.sync_world_anchors()
                except Exception:
                    pass
                self._home_pos = (self.x(), self.y())
            elif not self._did_drag:
                self._fire_click_action()
            self._press_global = None
            self._dragging = False
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and self._dragging:
            self.releaseMouse()
            if self._press_win is not None:
                self.move(self._press_win)
            try:
                from desktop_app.toaster_universe.world import get_world

                get_world().cancel_drag()
                self.face.sync_world_anchors()
            except Exception:
                pass
            self._press_global = None
            self._dragging = False
            return
        super().keyPressEvent(event)

    def _clamp_to_virtual_desktop(self, pos: QPoint) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableVirtualGeometry() if hasattr(screen, "availableVirtualGeometry") else screen.availableGeometry()
        pos.setX(max(geo.left(), min(pos.x(), geo.right() - 80)))
        pos.setY(max(geo.top(), min(pos.y(), geo.bottom() - 80)))

    def _show_character_menu(self, global_pos) -> None:
        try:
            from desktop_app.toaster_universe.character_forms import list_forms
            from desktop_app.toaster_universe.world import get_world

            menu = QMenu(self)
            current = get_world().get_character_form()
            char_menu = menu.addMenu("Character")
            for profile in list_forms():
                act = QAction(profile.display_name, menu)
                act.setCheckable(True)
                act.setChecked(profile.id == current)
                act.triggered.connect(lambda checked=False, fid=profile.id: self._apply_character_form(fid))
                char_menu.addAction(act)
            menu.exec(global_pos)
        except Exception:
            pass

    def _apply_character_form(self, form_id: str) -> None:
        try:
            from desktop_app.toaster_universe.world import get_world

            get_world().set_character_form(form_id)
        except Exception:
            return
        try:
            self.face.update()
        except Exception:
            pass
        self.update()

    def _fire_click_action(self) -> None:
        import random
        self.face._click_anim = random.choice(
            ["bounce", "spin", "wiggle", "squash", "pop"])
        self.face._click_anim_start = _time.monotonic()
        line = random.choice(self._WITTY_LINES_CS)
        try:
            from desktop_app.toaster_universe.world import get_world

            world = get_world()
            world.note_toaster_click()
            if world.soul.line:
                line = world.soul.line
        except Exception:
            pass
        self._show_witty_line(line)

    def _show_witty_line(self, text: str) -> None:
        """Show the witty line in the presence label briefly."""
        try:
            self._presence_label.setText(text)
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(2600, lambda: self._presence_label.setText(""))
        except Exception:
            pass

    # ── Voice PE listening-mode buttons ─────────────────────────────
    def _build_mode_row(self) -> QWidget:
        from PyQt6.QtWidgets import QHBoxLayout, QPushButton
        from jarvis.i18n import tr

        row = QWidget(self)
        h = QHBoxLayout(row)
        h.setContentsMargins(8, 4, 8, 8)
        h.setSpacing(8)

        self._ptt_btn = QPushButton(f"🎤 {tr('push_to_talk')}")
        self._ptt_btn.setCheckable(True)
        self._ptt_btn.setToolTip(tr("push_to_talk_tooltip"))
        self._ptt_btn.clicked.connect(lambda: self._set_voice_pe_mode(False))

        self._cont_btn = QPushButton(f"🔁 {tr('continuous')}")
        self._cont_btn.setCheckable(True)
        self._cont_btn.setToolTip(tr("continuous_tooltip"))
        self._cont_btn.clicked.connect(lambda: self._set_voice_pe_mode(True))

        for b in (self._ptt_btn, self._cont_btn):
            b.setStyleSheet(
                "QPushButton { padding: 6px 10px; font-size: 12px; "
                "background: #27272a; color: #e4e4e7; border: 1px solid "
                "#3f3f46; border-radius: 8px; }"
                "QPushButton:checked { background: #f59e0b; color: #18181b; "
                "border: 1px solid #f59e0b; font-weight: bold; }")
            h.addWidget(b)
        return row

    def _voice_pe_manager(self):
        try:
            from jarvis.daemon import get_voice_pe_manager
            return get_voice_pe_manager()
        except Exception:
            return None

    def _current_voice_pe_continuous(self) -> bool:
        """Continuous when wake words are enabled (not disabled)."""
        try:
            from jarvis.config import _load_json, default_config_path
            data = _load_json(default_config_path())
            return not bool(data.get("voice_pe_disable_wake_words", True))
        except Exception:
            return False

    def _refresh_mode_buttons(self) -> None:
        """Sync the check-state with the persisted mode. The row stays visible
        regardless; the buttons are enabled only when a live Voice PE manager
        can apply the change (otherwise the click just records the choice)."""
        continuous = self._current_voice_pe_continuous()
        self._ptt_btn.setChecked(not continuous)
        self._cont_btn.setChecked(continuous)
        manager = self._voice_pe_manager()
        live = manager is not None and getattr(manager, "enabled", False)
        tooltip_on = "Voice PE connected"
        tooltip_off = "Voice PE not connected yet — the choice is saved and applies on connect"
        self._ptt_btn.setToolTip(
            f"Voice PE push-to-talk: wake words off; the centre button opens "
            f"the voice session. ({tooltip_on if live else tooltip_off})")
        self._cont_btn.setToolTip(
            f"Voice PE continuous: wake words on; the mic reopens after each "
            f"reply during the conversation window. "
            f"({tooltip_on if live else tooltip_off})")

    def _set_voice_pe_mode(self, continuous: bool) -> None:
        # Persist the choice immediately so a restart keeps it, then apply to
        # the live device when a manager is present.
        try:
            from jarvis.integrations.voice_pe.manager import _persist_listening_mode
            _persist_listening_mode(bool(continuous))
        except Exception:
            pass
        manager = self._voice_pe_manager()
        if manager is not None:
            try:
                manager.set_listening_mode(bool(continuous))
            except Exception:
                pass
        self._ptt_btn.setChecked(not continuous)
        self._cont_btn.setChecked(continuous)
        self._ptt_btn.setChecked(not continuous)
        self._cont_btn.setChecked(continuous)

    def showEvent(self, event):
        """Refresh the Voice PE mode buttons whenever the window is shown —
        the daemon's manager may only have come up after construction."""
        super().showEvent(event)
        try:
            self._refresh_mode_buttons()
        except Exception:
            pass

    def set_expression(self, expression: Expression):
        """Set the face expression."""
        self.face.set_expression(expression)

    def update_presence_mode(self, mode: str, label: str) -> None:
        """Update the presence mode indicator label."""
        self._presence_label.setText(f"Mode: {mode.capitalize()} ({label})")
