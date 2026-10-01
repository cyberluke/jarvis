"""Precise attention geometry. The whole HWND is not a forbidden country."""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import WorldConfig
from .signals import UserSignals
from .types import Rect, Vec2


@dataclass
class AttentionPatch:
    kind: str
    rect: Rect
    strength: float = 1.0


@dataclass
class AttentionField:
    patches: list[AttentionPatch] = field(default_factory=list)
    last_click: Rect | None = None
    caret: Rect | None = None
    selection: Rect | None = None
    focused_control: Rect | None = None
    uia_ok: bool = False

    def snapshot(self) -> dict:
        return {
            "patches": [{"kind": p.kind, "rect": (p.rect.x, p.rect.y, p.rect.w, p.rect.h), "strength": p.strength} for p in self.patches],
            "uia_ok": self.uia_ok,
            "has_caret": self.caret is not None,
            "has_selection": self.selection is not None,
        }


def _corridor(a: Vec2, b: Vec2, half: float) -> Rect:
    x0, x1 = min(a.x, b.x), max(a.x, b.x)
    y0, y1 = min(a.y, b.y), max(a.y, b.y)
    return Rect(x0 - half, y0 - half, max(8.0, x1 - x0) + half * 2, max(8.0, y1 - y0) + half * 2)


def _probe_uia() -> tuple[bool, Rect | None, Rect | None, Rect | None]:
    """Best-effort Windows UI Automation. Failure is normal and silent."""
    try:
        import ctypes
        from ctypes import wintypes

        class GUITHREADINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND),
                ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND),
                ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND),
                ("hwndCaret", wintypes.HWND),
                ("rcCaret", wintypes.RECT),
            ]

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False, None, None, None
        gui = GUITHREADINFO()
        gui.cbSize = ctypes.sizeof(GUITHREADINFO)
        if not user32.GetGUIThreadInfo(0, ctypes.byref(gui)):
            return False, None, None, None
        caret = None
        focused = None
        rc = gui.rcCaret
        if gui.hwndCaret or (rc.right > rc.left and rc.bottom > rc.top):
            caret = Rect(float(rc.left), float(rc.top), float(max(8, rc.right - rc.left)), float(max(12, rc.bottom - rc.top)))
        if gui.hwndFocus:
            fr = wintypes.RECT()
            if user32.GetWindowRect(gui.hwndFocus, ctypes.byref(fr)):
                w = fr.right - fr.left
                h = fr.bottom - fr.top
                if 16 < w < 1600 and 16 < h < 900:
                    focused = Rect(float(fr.left), float(fr.top), float(w), float(h))
        return True, caret, None, focused
    except Exception:
        return False, None, None, None


class AttentionSampler:
    def __init__(self, cfg: WorldConfig) -> None:
        self.cfg = cfg
        self.field = AttentionField()
        self._last_click_t = -1e9
        self._last_click_pos = Vec2()
        self._still_since = -1e9

    def note_click(self, pos: Vec2, t: float) -> None:
        self._last_click_t = t
        self._last_click_pos = pos
        r = self.cfg.click_sanctuary_radius
        self.field.last_click = Rect(pos.x - r, pos.y - r, r * 2, r * 2)

    def sample(self, signals: UserSignals, t: float) -> AttentionField:
        patches: list[AttentionPatch] = []
        pred = signals.cursor_predicted
        cursor = signals.cursor
        patches.append(AttentionPatch("cursor_predict", Rect(pred.x - 36, pred.y - 36, 72, 72), 1.0))
        if signals.dragging:
            patches.append(AttentionPatch("drag_corridor", _corridor(cursor, pred, 48.0), 1.0))
        if signals.selecting:
            patches.append(AttentionPatch("selection_corridor", _corridor(cursor, pred, 36.0), 0.9))
        if signals.scrolling:
            patches.append(AttentionPatch("scroll_corridor", Rect(cursor.x - 90, cursor.y - 160, 180, 320), 1.0))
        if signals.mouse_velocity < 40.0:
            if self._still_since < 0:
                self._still_since = t
            dwell_ms = (t - self._still_since) * 1000.0
            if dwell_ms >= self.cfg.reading_sanctuary_ms:
                r = self.cfg.reading_sanctuary_radius
                patches.append(AttentionPatch("reading_sanctuary", Rect(cursor.x - r, cursor.y - r, r * 2, r * 2), 0.85))
        else:
            self._still_since = -1e9
        if self.field.last_click and t - self._last_click_t < self.cfg.click_sanctuary_ms / 1000.0:
            patches.append(AttentionPatch("recent_click", self.field.last_click, 0.8))
        uia_ok, caret, selection, focused = _probe_uia()
        self.field.uia_ok = uia_ok
        self.field.caret = caret
        self.field.selection = selection
        self.field.focused_control = focused
        if caret:
            patches.append(AttentionPatch("caret", caret.inflated(28), 0.95))
        if selection:
            patches.append(AttentionPatch("selection", selection.inflated(16), 0.9))
        if focused:
            patches.append(AttentionPatch("focused_control", focused.inflated(8), 0.55))
        self.field.patches = patches
        return self.field

    def nearest_push(self, pos: Vec2, radius: float) -> tuple[Vec2, str, float] | None:
        best = None
        best_kind = ""
        best_strength = 0.0
        for patch in self.field.patches:
            if not patch.rect.intersects_circle(pos, radius):
                continue
            away = pos - patch.rect.center
            if away.length() < 1.0:
                away = Vec2(0.0, -1.0)
            strength = patch.strength
            if best is None or strength > best_strength:
                best = away.normalized()
                best_kind = patch.kind
                best_strength = strength
        if best is None:
            return None
        return best, best_kind, best_strength
