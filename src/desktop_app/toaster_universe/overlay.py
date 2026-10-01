"""Virtual-desktop click-through habitat. Dirty-region paints, not 4K every frame."""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QRect, Qt, QTimer
from PyQt6.QtGui import QPainter, QRegion
from PyQt6.QtWidgets import QApplication, QWidget

from .renderer import paint_world
from .types import Rect, Vec2
from .world import ToasterWorld


class WorldOverlay(QWidget):
    def __init__(self, world: ToasterWorld, parent=None):
        super().__init__(parent)
        self.world = world
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        interval = max(16, int(1000 / max(12.0, world.cfg.animation_fps_target)))
        self._timer.start(interval)
        self.last_dirty_full = True
        self.last_dirty_count = 0
        self.last_dirty_ratio = 1.0
        self._cover_desktop()
        self._apply_native_click_through()

    def _cover_desktop(self) -> None:
        self.world.topology.refresh_from_qt()
        virtual = self.world.topology.virtual
        if self.world.topology.habitats:
            self.setGeometry(int(virtual.x), int(virtual.y), int(virtual.w), int(virtual.h))
            self.world.set_desktop(virtual)
            return
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.geometry()
        self.setGeometry(geo)
        self.world.set_desktop(Rect(float(geo.x()), float(geo.y()), float(geo.width()), float(geo.height())))

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._apply_native_click_through()

    def _apply_native_click_through(self) -> None:
        try:
            import ctypes
            from ctypes import wintypes

            hwnd = int(self.winId())
            gwl_exstyle = -20
            ws_ex_layered = 0x00080000
            ws_ex_transparent = 0x00000020
            ws_ex_noactivate = 0x08000000
            user32 = ctypes.windll.user32
            get = user32.GetWindowLongW
            set_ = user32.SetWindowLongW
            style = get(wintypes.HWND(hwnd), gwl_exstyle)
            set_(wintypes.HWND(hwnd), gwl_exstyle, style | ws_ex_layered | ws_ex_transparent | ws_ex_noactivate)
        except Exception:
            pass

    def apply_dirty(self) -> None:
        full, rects = self.world.consume_dirty()
        self.last_dirty_full = full
        self.last_dirty_count = len(rects)
        self.last_dirty_ratio = self.world.metrics.dirty_ratio
        origin = Vec2(float(self.x()), float(self.y()))
        if full:
            self.update()
            return
        region = QRegion()
        for rect in rects:
            x, y, w, h = rect.to_ints()
            region = region.united(QRect(int(x - origin.x), int(y - origin.y), w, h))
        if region.isEmpty():
            return
        self.update(region)

    def _tick(self) -> None:
        if not self.world.cfg.enabled:
            return
        if not self.world.anchors_ready:
            return
        self.world.tick()
        self.apply_dirty()

    def paintEvent(self, event) -> None:  # noqa: N802
        import time as _time

        t0 = _time.perf_counter()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setClipRegion(event.region())
        origin = Vec2(float(self.x()), float(self.y()))
        paint_world(painter, self.world, origin)
        painter.end()
        paint_ms = (_time.perf_counter() - t0) * 1000.0
        self.world.metrics.paint_ms = paint_ms
        self.world.metrics.record("paint_time", t=self.world.clock, ms=round(paint_ms, 3))


_overlay: Optional[WorldOverlay] = None


def attach_overlay(world: ToasterWorld) -> WorldOverlay:
    global _overlay
    if _overlay is None:
        _overlay = WorldOverlay(world)
    _overlay.show()
    return _overlay
