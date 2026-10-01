"""Attention safety. The simulation yields to human intent. Always."""

from __future__ import annotations

from dataclasses import dataclass, field

from .attention import AttentionField, AttentionSampler
from .config import WorldConfig
from .monitors import MonitorHabitat
from .signals import UserSignals
from .types import OverrideReason, Rect, SafetyZone, Vec2


_PATCH_REASON = {
    "cursor_predict": (OverrideReason.CURSOR_PREDICTED, SafetyZone.PANIC_RADIUS),
    "drag_corridor": (OverrideReason.DRAG_ACTIVE, SafetyZone.DRAG_CORRIDOR),
    "selection_corridor": (OverrideReason.SELECTION_ACTIVE, SafetyZone.CLICK_SANCTUARY),
    "scroll_corridor": (OverrideReason.SCROLL_ACTIVE, SafetyZone.SCROLL_CORRIDOR),
    "reading_sanctuary": (OverrideReason.READING_ZONE, SafetyZone.READING_SANCTUARY),
    "recent_click": (OverrideReason.CLICK_SANCTUARY, SafetyZone.CLICK_SANCTUARY),
    "caret": (OverrideReason.CARET_ZONE, SafetyZone.READING_SANCTUARY),
    "selection": (OverrideReason.SELECTION_ZONE, SafetyZone.CLICK_SANCTUARY),
    "focused_control": (OverrideReason.FOCUSED_CONTROL, SafetyZone.READING_SANCTUARY),
}


@dataclass
class SafetyField:
    reason: OverrideReason = OverrideReason.NONE
    zone: SafetyZone | None = None
    predicted: Vec2 = field(default_factory=Vec2)
    work_rect: Rect | None = None
    force_click_through: bool = True
    must_yield: bool = False
    retreat: Vec2 = field(default_factory=Vec2)
    opacity_scale: float = 1.0
    speed_scale: float = 1.0


class SafetyArbiter:
    def __init__(self, cfg: WorldConfig) -> None:
        self.cfg = cfg
        self.work_rect: Rect | None = None
        self.taskbar = 48.0
        self.attention = AttentionSampler(cfg)
        self.field: AttentionField = self.attention.field

    def set_work_rect(self, rect: Rect | None) -> None:
        # Kept for diagnostics. Whole-HWND exile is no longer a yield reason.
        self.work_rect = rect

    def refresh(self, signals: UserSignals, t: float) -> AttentionField:
        self.field = self.attention.sample(signals, t)
        return self.field

    def evaluate(self, pos: Vec2, radius: float, signals: UserSignals, desktop: Rect) -> SafetyField:
        field = SafetyField(predicted=signals.cursor_predicted)
        field.work_rect = self.work_rect
        field.force_click_through = self.cfg.click_through_pets
        cursor = signals.cursor
        predicted = signals.cursor_predicted
        dist = (pos - cursor).length()
        pred_dist = (pos - predicted).length()

        if signals.dragging:
            field.must_yield = True
            field.reason = OverrideReason.DRAG_ACTIVE
            field.zone = SafetyZone.DRAG_CORRIDOR
        elif self.field.last_click is not None and self.field.last_click.contains(pos):
            field.must_yield = True
            field.reason = OverrideReason.CLICK_SANCTUARY
            field.zone = SafetyZone.CLICK_SANCTUARY
        elif signals.selecting and not signals.scrolling:
            field.must_yield = True
            field.reason = OverrideReason.SELECTION_ACTIVE
            field.zone = SafetyZone.CLICK_SANCTUARY
        elif signals.scrolling:
            field.must_yield = True
            field.reason = OverrideReason.SCROLL_ACTIVE
            field.zone = SafetyZone.SCROLL_CORRIDOR
        elif pred_dist < self.cfg.cursor_panic_radius or dist < self.cfg.cursor_panic_radius:
            field.must_yield = True
            field.reason = OverrideReason.CURSOR_PREDICTED if pred_dist <= dist else OverrideReason.CURSOR_INTERSECT
            field.zone = SafetyZone.PANIC_RADIUS
        elif getattr(signals, "mouse_velocity", 99) < 12.0 and signals.focus_score >= 0.72 and dist < self.cfg.cursor_strong_radius * 1.4:
            field.must_yield = True
            field.reason = OverrideReason.PRECISION_POINTER
            field.zone = SafetyZone.STRONG_REPULSION
        elif dist < self.cfg.cursor_strong_radius:
            field.must_yield = True
            field.reason = OverrideReason.CURSOR_INTERSECT
            field.zone = SafetyZone.STRONG_REPULSION
        else:
            hit = self.attention.nearest_push(pos, radius)
            if hit is not None:
                retreat, kind, strength = hit
                reason, zone = _PATCH_REASON.get(kind, (OverrideReason.FOCUS_YIELD, SafetyZone.READING_SANCTUARY))
                field.retreat = retreat
                field.reason = reason
                field.zone = zone
                field.must_yield = strength >= 0.8 or kind in {
                    "caret",
                    "drag_corridor",
                    "scroll_corridor",
                    "recent_click",
                    "reading_sanctuary",
                }
                if not field.must_yield:
                    field.opacity_scale = 0.72
                    field.speed_scale = 1.1
            elif dist < self.cfg.cursor_soft_radius:
                field.reason = OverrideReason.CURSOR_INTERSECT
                field.zone = SafetyZone.SOFT_REPULSION
                field.opacity_scale = 0.72

        if field.must_yield or field.zone is SafetyZone.SOFT_REPULSION:
            if field.retreat.length() < 1e-4:
                away = pos - predicted
                if away.length() < 1.0:
                    away = pos - cursor
                if away.length() < 1.0:
                    away = Vec2(0.0, -1.0)
                field.retreat = away.normalized()
            if field.zone is SafetyZone.PANIC_RADIUS:
                field.speed_scale = 2.4
                field.opacity_scale = 0.35
            elif field.zone is SafetyZone.STRONG_REPULSION:
                field.speed_scale = 1.8
                field.opacity_scale = 0.55
            elif field.reason in {OverrideReason.READING_ZONE, OverrideReason.CARET_ZONE, OverrideReason.FOCUSED_CONTROL}:
                field.speed_scale = 1.3
                field.opacity_scale = self.cfg.work_quiet_opacity
            elif field.reason is OverrideReason.SCROLL_ACTIVE:
                field.speed_scale = 1.6
                field.opacity_scale = 0.4
            else:
                field.speed_scale = max(field.speed_scale, 1.15)
        if (
            self.work_rect is not None
            and signals.activity.value in {"CODING_FLOW", "FOCUSED", "FRANTIC", "TERMINAL_HEAVY"}
            and self.work_rect.intersects_circle(pos, radius)
        ):
            field.must_yield = True
            field.reason = OverrideReason.WORK_RECT
            field.zone = SafetyZone.FOREGROUND_WORK_RECT
            away = pos - self.work_rect.center
            if away.length() < 1.0:
                away = Vec2(1.0, 0.0)
            field.retreat = away.normalized()
        if pos.y > desktop.bottom - self.taskbar - radius:
            field.must_yield = True
            field.retreat = Vec2(field.retreat.x, -abs(field.retreat.y) - 0.4).normalized()
            if field.reason is OverrideReason.NONE:
                field.reason = OverrideReason.WORK_RECT
                field.zone = SafetyZone.TASKBAR_SAFE_EDGE
        return field

    def habitat_nudge(self, pos: Vec2, habitat: MonitorHabitat | None) -> Vec2:
        if habitat is None:
            return Vec2()
        play = habitat.playable
        nudge = Vec2()
        if pos.x < play.x:
            nudge = Vec2(1.0, nudge.y)
        elif pos.x > play.right:
            nudge = Vec2(-1.0, nudge.y)
        if pos.y < play.y:
            nudge = Vec2(nudge.x, 1.0)
        elif pos.y > play.bottom:
            nudge = Vec2(nudge.x, -1.0)
        return nudge

    def low_attention_target(
        self,
        pos: Vec2,
        desktop: Rect,
        signals: UserSignals,
        habitat: MonitorHabitat | None = None,
    ) -> tuple[Vec2 | None, OverrideReason]:
        """Pick a navigation target outside attention sanctuaries."""
        candidates: list[Vec2] = [
            Vec2(desktop.x + 48, desktop.y + desktop.h * 0.35),
            Vec2(desktop.right - 48, desktop.y + desktop.h * 0.35),
            Vec2(desktop.x + desktop.w * 0.5, desktop.y + 56),
            Vec2(desktop.x + desktop.w * 0.2, desktop.bottom - self.taskbar - 72),
            Vec2(desktop.right - 80, desktop.bottom - self.taskbar - 72),
        ]
        if habitat is not None:
            play = habitat.playable
            candidates.append(Vec2(play.x + 36, play.y + play.h * 0.4))
            if not habitat.primary:
                candidates.insert(0, play.center)
        forbidden = [p.rect for p in self.field.patches]
        if self.work_rect is not None:
            forbidden.append(self.work_rect)
        cursor = Rect(signals.cursor.x - 48, signals.cursor.y - 48, 96, 96)
        forbidden.append(cursor)
        best = None
        best_score = -1e9
        for cand in candidates:
            if any(rect.contains(cand) for rect in forbidden):
                continue
            score = (cand - pos).length()
            if score > best_score:
                best = cand
                best_score = score
        if best is None:
            return None, OverrideReason.NO_SAFE_TARGET
        return best, OverrideReason.NONE

    def steer(self, pos: Vec2, desired: Vec2, field: SafetyField, speed: float) -> Vec2:
        if field.must_yield:
            if field.retreat.length() < 1e-4:
                return Vec2()
            return field.retreat * (speed * field.speed_scale)
        if field.zone is SafetyZone.SOFT_REPULSION:
            blended = desired + field.retreat * (speed * 0.65)
            return blended.clamped(speed * 1.15)
        return desired.clamped(speed)
