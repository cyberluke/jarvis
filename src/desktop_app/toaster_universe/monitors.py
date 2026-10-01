"""Generic multi-monitor habitats. Negative coords and mixed DPI are legal."""

from __future__ import annotations

from dataclasses import dataclass, field

from .types import Rect, Vec2


@dataclass
class MonitorHabitat:
    index: int
    name: str
    geometry: Rect
    available: Rect
    dpi: float = 1.0
    primary: bool = False
    density: float = 1.0

    @property
    def playable(self) -> Rect:
        return Rect(
            self.available.x + 16,
            self.available.y + 16,
            max(40.0, self.available.w - 32),
            max(40.0, self.available.h - 64),
        )


def _screen_habitat(index: int, screen, primary) -> MonitorHabitat:
    geo = screen.geometry()
    avail = screen.availableGeometry()
    try:
        dpi = float(screen.devicePixelRatio())
    except Exception:
        dpi = 1.0
    return MonitorHabitat(
        index=index,
        name=screen.name() or f"screen-{index}",
        geometry=Rect(float(geo.x()), float(geo.y()), float(geo.width()), float(geo.height())),
        available=Rect(float(avail.x()), float(avail.y()), float(avail.width()), float(avail.height())),
        dpi=dpi,
        primary=screen is primary,
        density=1.15 if screen is primary else 0.75,
    )


@dataclass
class MonitorTopology:
    habitats: list[MonitorHabitat] = field(default_factory=list)
    virtual: Rect = field(default_factory=lambda: Rect(0, 0, 1920, 1080))

    def refresh_from_qt(self) -> None:
        try:
            from PyQt6.QtWidgets import QApplication
        except Exception:
            return
        app = QApplication.instance()
        if app is None:
            return
        screens = list(app.screens())
        if not screens:
            return
        primary = app.primaryScreen()
        habitats = [_screen_habitat(i, screen, primary) for i, screen in enumerate(screens)]
        self.habitats = habitats
        xs = [h.geometry.x for h in habitats]
        ys = [h.geometry.y for h in habitats]
        rights = [h.geometry.right for h in habitats]
        bottoms = [h.geometry.bottom for h in habitats]
        x = min(xs)
        y = min(ys)
        self.virtual = Rect(x, y, max(rights) - x, max(bottoms) - y)

    def habitat_at(self, pos: Vec2) -> MonitorHabitat | None:
        for habitat in self.habitats:
            if habitat.geometry.contains(pos):
                return habitat
        if not self.habitats:
            return None
        return min(self.habitats, key=lambda h: (h.geometry.center - pos).length())

    def primary(self) -> MonitorHabitat | None:
        for habitat in self.habitats:
            if habitat.primary:
                return habitat
        if self.habitats:
            return self.habitats[0]
        return None

    def snapshot(self) -> dict:
        return {
            "virtual": (self.virtual.x, self.virtual.y, self.virtual.w, self.virtual.h),
            "count": len(self.habitats),
            "habitats": [
                {
                    "index": h.index,
                    "name": h.name,
                    "geo": (h.geometry.x, h.geometry.y, h.geometry.w, h.geometry.h),
                    "available": (h.available.x, h.available.y, h.available.w, h.available.h),
                    "dpi": h.dpi,
                    "primary": h.primary,
                }
                for h in self.habitats
            ],
        }
