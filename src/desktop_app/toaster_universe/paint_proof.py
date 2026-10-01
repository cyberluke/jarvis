"""Offscreen QPainter proof: named clip/action channels actually paint pixels."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class PaintDelta:
    name: str
    changed_pixels: int
    before_nonzero: int
    after_nonzero: int
    channels: dict[str, float]


def _qimage(w: int = 320, h: int = 240):
    from PyQt6.QtGui import QImage

    return QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)


def _paint_world(world, w: int = 320, h: int = 240):
    from PyQt6.QtGui import QPainter, QColor
    from PyQt6.QtCore import Qt

    from .renderer import paint_world
    from .types import Vec2

    image = _qimage(w, h)
    image.fill(QColor(0, 0, 0, 0))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.fillRect(0, 0, w, h, QColor(0, 0, 0, 0))
    origin = Vec2(world.toaster.x - w / 2, world.toaster.y - h / 2)
    paint_world(painter, world, origin)
    painter.end()
    return image


def _nonzero(image) -> int:
    bits = image.constBits()
    try:
        buf = bits.asarray(image.sizeInBytes())
    except Exception:
        return image.width() * image.height() if not image.isNull() else 0
    return sum(1 for i in range(0, len(buf), 4) if buf[i + 3] > 8)


def _changed(a, b) -> int:
    ba = a.constBits().asarray(a.sizeInBytes())
    bb = b.constBits().asarray(b.sizeInBytes())
    n = 0
    for i in range(0, min(len(ba), len(bb)), 4):
        if abs(int(ba[i]) - int(bb[i])) + abs(int(ba[i + 1]) - int(bb[i + 1])) + abs(int(ba[i + 2]) - int(bb[i + 2])) > 18:
            n += 1
    return n


def prove_world_paint(world, name: str, mutate) -> dict[str, Any]:
    before = _paint_world(world)
    mutate(world)
    after = _paint_world(world)
    entity = world.entities[0] if world.entities else None
    channels = {}
    if entity is not None:
        pose = entity.pose
        channels = {
            "blink": pose.blink,
            "shockwave": pose.shockwave,
            "trail": pose.trail,
            "arc": pose.arc,
            "smoke": pose.smoke,
            "sink": pose.sink,
            "stare": pose.stare,
            "glitch": pose.glitch,
            "hop": pose.hop,
            "face_yaw": pose.face_yaw,
            "body_warp": pose.body_warp,
        }
    return {
        "name": name,
        "changed_pixels": _changed(before, after),
        "before_nonzero": _nonzero(before),
        "after_nonzero": _nonzero(after),
        "channels": channels,
        "night_mode": bool(getattr(world, "night_mode", False)),
    }
