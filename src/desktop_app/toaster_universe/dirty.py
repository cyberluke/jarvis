"""Dirty-region aggregation. Full-screen paint is a rare-event privilege."""

from __future__ import annotations

from .config import WorldConfig
from .types import Rect


def merge_rects(rects: list[Rect], gap: float) -> list[Rect]:
    if not rects:
        return []
    boxes = [r.inflated(0) for r in rects if r.w > 0 and r.h > 0]
    changed = True
    while changed and len(boxes) > 1:
        changed = False
        nxt: list[Rect] = []
        used = [False] * len(boxes)
        for i, a in enumerate(boxes):
            if used[i]:
                continue
            acc = a
            for j in range(i + 1, len(boxes)):
                if used[j]:
                    continue
                b = boxes[j]
                if acc.inflated(gap).intersects(b.inflated(gap)):
                    acc = acc.union(b)
                    used[j] = True
                    changed = True
            nxt.append(acc)
        boxes = nxt
    return boxes


class DirtyAccumulator:
    def __init__(self, cfg: WorldConfig) -> None:
        self.cfg = cfg
        self.rects: list[Rect] = []
        self.full = False
        self.last_count = 0
        self.last_pixels = 0.0
        self.last_ratio = 0.0

    def reset(self) -> None:
        self.rects.clear()
        self.full = False

    def add(self, rect: Rect | None, pad: float | None = None) -> None:
        if self.full or rect is None:
            return
        if rect.w <= 0 or rect.h <= 0:
            return
        self.rects.append(rect.inflated(pad if pad is not None else self.cfg.dirty_padding))

    def force_full(self) -> None:
        self.full = True
        self.rects.clear()

    def consume(self, desktop: Rect) -> tuple[bool, list[Rect]]:
        desktop_area = max(1.0, desktop.area())
        if self.full:
            self.last_count = 1
            self.last_pixels = desktop_area
            self.last_ratio = 1.0
            self.reset()
            return True, [desktop]
        merged = merge_rects(self.rects, self.cfg.dirty_merge_gap)
        pixels = sum(r.area() for r in merged)
        ratio = pixels / desktop_area
        density_full = len(merged) > (16 if desktop_area > 2_000_000 else 24)
        if ratio > 0.55 or density_full:
            self.last_count = 1
            self.last_pixels = desktop_area
            self.last_ratio = 1.0
            self.reset()
            return True, [desktop]
        self.last_count = len(merged)
        self.last_pixels = pixels
        self.last_ratio = ratio
        self.reset()
        return False, merged
