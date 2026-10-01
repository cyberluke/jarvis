"""Desktop physics. Springs, hops, edges, crowd, never the user's cursor."""

from __future__ import annotations

import math
import random

from .config import WorldConfig
from .types import Rect, Vec2


def spring(current: float, target: float, vel: float, k: float, damp: float, dt: float) -> tuple[float, float]:
    force = (target - current) * k - vel * damp
    vel += force * dt
    current += vel * dt
    return current, vel


def damping(vel: Vec2, coeff: float = 0.92, desired: Vec2 | None = None, mix: float = 0.08) -> Vec2:
    """Named linear damping module used by the character integrator."""
    if desired is None:
        return vel * coeff
    return vel * coeff + desired * mix


def gravity(vel: Vec2, dt: float, g: float = 180.0) -> Vec2:
    """Named gravity module. Particles and popcorn use this."""
    return vel + Vec2(0.0, g * dt)


def clamp_impulse(vel: Vec2, max_speed: float) -> Vec2:
    return vel.clamped(max_speed)


def integrate(pos: Vec2, vel: Vec2, acc: Vec2, dt: float, max_speed: float) -> tuple[Vec2, Vec2]:
    vel = clamp_impulse(vel + acc * dt, max_speed)
    return pos + vel * dt, vel


def hop_offset(phase: float, height: float) -> float:
    p = max(0.0, min(1.0, phase))
    return math.sin(p * math.pi) * height


def jump_arc(origin: Vec2, target: Vec2, t: float, height: float) -> Vec2:
    t = max(0.0, min(1.0, t))
    x = origin.x + (target.x - origin.x) * t
    y = origin.y + (target.y - origin.y) * t - hop_offset(t, height)
    return Vec2(x, y)


def confine(pos: Vec2, radius: float, desktop: Rect, vel: Vec2 | None = None) -> tuple[Vec2, Vec2] | Vec2:
    x = min(max(pos.x, desktop.x + radius), desktop.right - radius)
    y = min(max(pos.y, desktop.y + radius), desktop.bottom - radius - 48.0)
    bounced = vel if vel is not None else Vec2()
    if vel is not None:
        if pos.x <= desktop.x + radius or pos.x >= desktop.right - radius:
            bounced = Vec2(-vel.x * 0.55, vel.y)
        if pos.y <= desktop.y + radius or pos.y >= desktop.bottom - radius - 48.0:
            bounced = Vec2(bounced.x, -vel.y * 0.55)
        return Vec2(x, y), bounced
    return Vec2(x, y)


def wall_bounce(pos: Vec2, vel: Vec2, radius: float, desktop: Rect) -> tuple[Vec2, Vec2]:
    confined, bounced = confine(pos, radius, desktop, vel)
    return confined, bounced


def popcorn_ballistic(pos: Vec2, vel: Vec2, dt: float) -> tuple[Vec2, Vec2]:
    """Distinct popcorn integrator: lighter gravity, more hang time."""
    vel = gravity(vel, dt, g=92.0)
    vel = damping(vel, coeff=1.0 - min(0.18, dt * 0.35))
    return pos + vel * dt, vel


def soft_body(warp: float, target: float, vel: float, dt: float) -> tuple[float, float]:
    return spring(warp, target, vel, k=38.0, damp=7.2, dt=dt)


def butter_control_points(warp: float) -> list[tuple[float, float]]:
    """Deterministic cubic control points for Butter Blob deformation proof."""
    w = 1.0 + 0.22 * warp
    return [(-10.0 * w, 0.0), (0.0, -8.0 / max(0.65, w)), (10.0 * w, 1.0), (0.0, 7.0 / max(0.65, w))]


def crowd_push(
    self_id: str,
    pos: Vec2,
    others: list[tuple[str, Vec2, float]],
    radius: float,
    rng: random.Random | None = None,
    density: float = 1.0,
    crowd_tolerance: float = 0.5,
) -> Vec2:
    push = Vec2()
    dice = rng or random.Random(int(pos.x * 13 + pos.y * 7) & 0xFFFFFFFF)
    pad = 6.0 + max(0.0, density - 1.0) * 10.0 + (1.0 - crowd_tolerance) * 8.0
    for oid, other, orad in others:
        if oid == self_id:
            continue
        delta = pos - other
        dist = delta.length()
        need = radius + orad + pad
        if 0.0 < dist < need:
            push = push + delta.normalized() * ((need - dist) / need)
        elif dist <= 1e-4:
            push = push + Vec2(dice.uniform(-1, 1), dice.uniform(-1, 1))
    return push


def cursor_repulsion(pos: Vec2, cursor: Vec2, radius: float, strength: float = 1.0) -> Vec2:
    delta = pos - cursor
    dist = delta.length()
    if dist < 1e-4:
        return Vec2(0.0, -1.0) * strength
    if dist > radius:
        return Vec2()
    return delta.normalized() * ((radius - dist) / radius) * strength


def wander_target(
    pos: Vec2,
    desktop: Rect,
    rng: random.Random,
    forbidden: list[Rect] | None = None,
) -> Vec2:
    margin = 80.0
    for _ in range(12):
        candidate = Vec2(
            rng.uniform(desktop.x + margin, desktop.right - margin),
            rng.uniform(desktop.y + margin, desktop.bottom - margin - 64.0),
        )
        if not forbidden or not any(rect.contains(candidate) for rect in forbidden):
            return candidate
    return Vec2(
        rng.uniform(desktop.x + margin, desktop.right - margin),
        rng.uniform(desktop.y + margin, desktop.bottom - margin - 64.0),
    )
