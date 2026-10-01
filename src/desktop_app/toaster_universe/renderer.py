"""QPainter vector renderer for Mini Toasts, crumbs, appliances, and FX."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from .animation import browning_color
from .types import ApplianceKind, EntityKind, ToastState, Vec2

if TYPE_CHECKING:
    from .world import ToasterWorld


def _q():
    from PyQt6.QtCore import QPointF, QRectF, Qt
    from PyQt6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen, QRadialGradient

    return QPointF, QRectF, Qt, QBrush, QColor, QPainter, QPainterPath, QPen, QRadialGradient


SHADOW_POLICY = {
    "kind": "contact_ellipse",
    "color": (15, 17, 23, 90),
    "offset_y": 16.0,
    "rx": 10.0,
    "ry": 3.4,
}

STROKE_POLICY = {
    "min_px": 1.15,
    "toast_crust": 1.6,
    "toast_inner": 1.0,
    "toast_leg": 2.0,
    "toast_mouth": 1.3,
    "appliance": 1.4,
}

SCALE_HIERARCHY = {
    "main_toaster": 1.0,
    "appliance": 0.42,
    "mini_toast": 0.28,
    "guest": 0.18,
    "particle": 0.08,
}


def painter_dpi(painter) -> float:
    try:
        device = painter.device()
        dpr = float(device.devicePixelRatioF()) if hasattr(device, "devicePixelRatioF") else float(device.devicePixelRatio())
        return max(1.0, dpr)
    except Exception:
        return 1.0


def stroke_px(logical: float, dpi: float) -> float:
    return max(STROKE_POLICY["min_px"], logical * max(1.0, dpi * 0.55))


def _draw_contact_shadow(painter, x: float, y: float, scale: float, opacity: float, QPointF, Qt, QBrush, QColor) -> None:
    painter.save()
    painter.setOpacity(max(0.08, min(0.35, 0.28 * opacity)))
    painter.setPen(Qt.PenStyle.NoPen)
    a, b, c, d = SHADOW_POLICY["color"]
    painter.setBrush(QBrush(QColor(a, b, c, d)))
    painter.drawEllipse(
        QPointF(x, y + SHADOW_POLICY["offset_y"] * scale),
        SHADOW_POLICY["rx"] * scale,
        SHADOW_POLICY["ry"] * scale,
    )
    painter.restore()


def _draw_main_form(painter, world, origin, form_id, QPointF, QRectF, Qt, QBrush, QColor, QPen, dpi) -> None:
    """Paint the selected main-character form at the toaster anchor."""
    if form_id in {None, "", "classic_toaster"}:
        return
    from .character_forms import get_form

    profile = get_form(form_id)
    kind = profile.appliance_kind
    if kind is None:
        return
    item = world.appliances.by_kind(kind)
    if item is None:
        return
    saved = item.pos
    item.pos = world.toaster
    item.open = True
    _draw_appliance(painter, item, origin, QPointF, QRectF, Qt, QBrush, QColor, QPen, dpi)
    item.pos = saved


def paint_world(painter, world: "ToasterWorld", origin: Vec2) -> None:
    QPointF, QRectF, Qt, QBrush, QColor, QPainter, QPainterPath, QPen, QRadialGradient = _q()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    dpi = painter_dpi(painter)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    night = bool(getattr(world, "_night_emitted", False) or getattr(world, "night_mode", False))
    world.night_mode = night
    if night:
        painter.save()
        painter.setOpacity(0.38)
        device = painter.device()
        rw = int(device.width()) if device is not None else 4096
        rh = int(device.height()) if device is not None else 4096
        painter.fillRect(0, 0, rw, rh, QColor(8, 16, 48, 180))
        painter.restore()
    form_id = getattr(world, "character_form", "classic_toaster")
    if world.cfg.appliances_enabled:
        for appliance in world.appliances.items:
            if appliance.kind is ApplianceKind.SECRET_CHAMBER and not appliance.open:
                continue
            _draw_appliance(painter, appliance, origin, QPointF, QRectF, Qt, QBrush, QColor, QPen, dpi)
    _draw_main_form(painter, world, origin, form_id, QPointF, QRectF, Qt, QBrush, QColor, QPen, dpi)
    for particle in world.particles:
        _draw_particle(painter, particle, origin, QPointF, Qt, QBrush, QColor)
    for entity in world.entities:
        if entity.hidden or entity.opacity <= 0.02:
            continue
        local = Vec2(entity.pos.x - origin.x, entity.pos.y - origin.y)
        entity._night_material = night
        if entity.kind is EntityKind.CRUMB:
            _draw_crumb(painter, entity, local, QPointF, Qt, QBrush, QColor)
        elif entity.kind is EntityKind.POPCORN_KERNEL:
            _draw_popcorn(painter, entity, local, QPointF, Qt, QBrush, QColor)
        elif entity.kind is EntityKind.RICE_SPIRIT:
            _draw_rice(painter, entity, local, QPointF, QRectF, Qt, QBrush, QColor, QPen)
        elif entity.kind is EntityKind.BUTTER_BLOB:
            _draw_butter(painter, entity, local, QPointF, QRectF, Qt, QBrush, QColor, QPen)
        elif entity.kind is EntityKind.PORTAL_ECHO:
            _draw_portal_echo(painter, entity, local, QPointF, Qt, QBrush, QColor, QPen)
        else:
            _draw_toast(painter, entity, local, QPointF, QRectF, Qt, QBrush, QColor, QPainterPath, QPen, dpi)
    _draw_scene_fx(painter, world, origin, QPointF, QRectF, Qt, QBrush, QColor, QPen)
    chairs = getattr(world.doom, "chairs", ())
    for chair in chairs:
        painter.save()
        painter.translate(chair.x - origin.x, chair.y - origin.y)
        painter.setPen(QPen(QColor("#78716c"), 1.2))
        painter.setBrush(QBrush(QColor("#a8a29e")))
        painter.drawRoundedRect(QRectF(-6, -4, 12, 7), 2, 2)
        painter.drawLine(QPointF(-5, 3), QPointF(-5, 8))
        painter.drawLine(QPointF(5, 3), QPointF(5, 8))
        painter.restore()
    if world.caption:
        painter.setPen(QPen(QColor("#fbbf24")))
        painter.drawText(12, 22, world.caption[:96])


def _draw_toast(painter, entity, local, QPointF, QRectF, Qt, QBrush, QColor, QPainterPath, QPen, dpi: float = 1.0) -> None:
    pose = entity.pose
    painter.save()
    if pose.trail > 0.04:
        painter.save()
        painter.setOpacity(min(0.28, pose.trail))
        painter.translate(local.x - entity.vel.x * 0.04, local.y - pose.hop - entity.vel.y * 0.04)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(232, 185, 107, 90)))
        painter.drawRoundedRect(QRectF(-10, -12, 20, 24), 5, 5)
        painter.restore()
    if pose.glitch > 0.04:
        painter.save()
        painter.setOpacity(min(0.35, pose.glitch))
        painter.translate(local.x + 4, local.y - pose.hop)
        painter.setPen(QPen(QColor(167, 139, 250, 160), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(QRectF(-11, -13, 22, 26), 5, 5)
        painter.restore()
    _draw_contact_shadow(
        painter,
        local.x + pose.translation_x,
        local.y,
        max(0.6, pose.scale),
        pose.opacity,
        QPointF,
        Qt,
        QBrush,
        QColor,
    )
    painter.translate(local.x + pose.translation_x, local.y - pose.hop + pose.translation_y)
    painter.rotate(pose.rotation + pose.face_yaw * 12.0)
    sink = max(0.0, min(1.0, pose.sink))
    painter.scale(pose.squash_x * entity.facing * pose.scale * (1.0 - sink * 0.2), pose.squash_y * pose.scale * (1.0 - sink * 0.45))
    painter.setOpacity(max(0.05, min(1.0, pose.opacity)))
    w, h = 22.0, 26.0
    if pose.smear > 0.08:
        painter.setOpacity(max(0.05, min(1.0, pose.opacity)) * 0.55)
        painter.translate(-pose.smear * 8.0, 0)
    rgb = browning_color(entity.browning, entity.burnt)
    night = bool(getattr(entity, "_night_material", False))
    if night:
        rgb = (max(12, int(rgb[0] * 0.28 + 10)), max(16, int(rgb[1] * 0.26 + 22)), max(64, int(rgb[2] * 0.48 + 78)))
    body = QColor(*rgb)
    crust = QColor(max(0, rgb[0] - 40), max(0, rgb[1] - 40), max(0, rgb[2] - 30))
    path = QPainterPath()
    path.addRoundedRect(QRectF(-w / 2, -h / 2, w, h), 6, 6)
    painter.setPen(QPen(crust, stroke_px(STROKE_POLICY["toast_crust"], dpi)))
    from PyQt6.QtGui import QLinearGradient

    grad = QLinearGradient(-w / 2, -h / 2, w / 2, h / 2)
    highlight = QColor(min(255, rgb[0] + 28), min(255, rgb[1] + 22), min(255, rgb[2] + 12))
    grad.setColorAt(0.0, highlight)
    grad.setColorAt(1.0, body)
    painter.setBrush(QBrush(grad))
    painter.drawPath(path)
    painter.setPen(QPen(QColor(rgb[0] - 20, rgb[1] - 20, rgb[2] - 10), stroke_px(STROKE_POLICY["toast_inner"], dpi)))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(QRectF(-w / 2 + 3, -h / 2 + 3, w - 6, h - 6), 4, 4)
    # legs
    swing = math.sin(entity.hop_phase * math.pi) * 5.0
    painter.setPen(QPen(crust, stroke_px(STROKE_POLICY["toast_leg"], dpi)))
    painter.drawLine(QPointF(-5, h / 2 - 1), QPointF(-6 + swing, h / 2 + 7))
    painter.drawLine(QPointF(5, h / 2 - 1), QPointF(6 - swing, h / 2 + 7))
    # face
    eye_y = -2 + pose.look_y * 2.0
    stare_lock = 1.0 if pose.stare > 0.5 else 0.0
    micro = 0.35 * pose.eye_squint + 0.25 * pose.mouth_open
    er = 2.1 * (1.0 - 0.85 * (0.0 if stare_lock else pose.blink)) * (1.0 - 0.25 * pose.eye_squint) * (1.0 - 0.12 * micro)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor("#3a2412")))
    if er > 0.35:
        yaw = pose.face_yaw * 3.2
        painter.drawEllipse(QPointF(-5 + pose.look_x * 2.2 + yaw, eye_y), er, max(0.7, er))
        painter.drawEllipse(QPointF(5 + pose.look_x * 2.2 + yaw, eye_y), er, max(0.7, er))
    if entity.state is ToastState.SLEEP:
        painter.setPen(QPen(QColor("#3a2412"), 1.4))
        painter.drawLine(QPointF(-7, -1), QPointF(-3, -1))
        painter.drawLine(QPointF(3, -1), QPointF(7, -1))
    if pose.brow_raise > 0.15:
        painter.setPen(QPen(QColor("#3a2412"), stroke_px(1.05, dpi)))
        lift = 3.2 + pose.brow_raise * 3.4
        painter.drawLine(QPointF(-7, eye_y - lift), QPointF(-3, eye_y - lift - 0.8))
        painter.drawLine(QPointF(3, eye_y - lift - 0.8), QPointF(7, eye_y - lift))
    mouth = QPainterPath()
    mouth.moveTo(-4, 6)
    viseme = pose.viseme or "rest"
    if entity.burnt:
        mouth.cubicTo(-1, 3, 1, 3, 4, 6)
    elif viseme == "oh" or pose.mouth_open > 0.4:
        mouth.cubicTo(-1, 11, 1, 11, 4, 6)
    elif viseme == "smile" or entity.state in {ToastState.DANCE, ToastState.HEART_MODE}:
        mouth.cubicTo(-1, 10, 1, 10, 4, 6)
    elif viseme == "smirk":
        mouth.cubicTo(0, 7, 2, 9, 5, 5)
    elif viseme == "line" or pose.eye_squint > 0.4:
        mouth.cubicTo(-1, 7, 1, 7, 4, 5)
    else:
        mouth.cubicTo(-1, 8, 1, 8, 4, 6)
    painter.setPen(QPen(QColor("#3a2412"), stroke_px(STROKE_POLICY["toast_mouth"], dpi)))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(mouth)
    if pose.glow > 0.05:
        painter.setOpacity(min(0.35, pose.glow))
        painter.setBrush(QBrush(QColor(251, 191, 36, 90)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPointF(0, 0), 16, 18)
    if pose.arc > 0.05:
        painter.setOpacity(min(0.7, pose.arc))
        painter.setPen(QPen(QColor(125, 211, 252, 200), 1.2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawLine(QPointF(-10, -8), QPointF(-2, 4))
        painter.drawLine(QPointF(10, -6), QPointF(3, 5))
        painter.drawLine(QPointF(0, -12), QPointF(2, 2))
    if pose.smoke > 0.05:
        painter.setOpacity(min(0.45, pose.smoke))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(148, 163, 184, 120)))
        painter.drawEllipse(QPointF(-3, -16), 5, 4)
        painter.drawEllipse(QPointF(4, -20), 4, 3)
    if pose.shockwave > 0.04:
        painter.setOpacity(min(0.4, pose.shockwave))
        painter.setPen(QPen(QColor(251, 191, 36, 160), 1.4))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        r = 10 + pose.shockwave * 18
        painter.drawEllipse(QPointF(0, 0), r, r * 0.72)
    painter.restore()


def _draw_crumb(painter, entity, local, QPointF, Qt, QBrush, QColor) -> None:
    painter.save()
    painter.translate(local.x, local.y)
    painter.setOpacity(entity.opacity)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor(196, 140, 64)))
    painter.drawEllipse(QPointF(0, 0), 3.2, 2.4)
    if getattr(entity, "local_intent", "") == "crumb_king":
        painter.setBrush(QBrush(QColor(251, 191, 36)))
        painter.drawEllipse(QPointF(0, -4), 2.4, 1.4)
    painter.restore()


def _draw_popcorn(painter, entity, local, QPointF, Qt, QBrush, QColor) -> None:
    painter.save()
    hop = getattr(entity.pose, "hop", 0.0)
    painter.translate(local.x, local.y - hop)
    painter.rotate(getattr(entity.pose, "rotation", 0.0))
    painter.setOpacity(entity.opacity)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor(255, 244, 214)))
    painter.drawEllipse(QPointF(0, 0), 6, 5)
    painter.setBrush(QBrush(QColor(255, 228, 170)))
    painter.drawEllipse(QPointF(-3, -2), 3.2, 2.8)
    painter.restore()


def _draw_rice(painter, entity, local, QPointF, QRectF, Qt, QBrush, QColor, QPen) -> None:
    painter.save()
    hover = 3.0 + 2.0 * math.sin(entity.age * 1.6)
    painter.translate(local.x, local.y - hover)
    painter.setOpacity(min(0.75, entity.opacity))
    painter.setPen(QPen(QColor(230, 230, 235, 160), 1))
    painter.setBrush(QBrush(QColor(250, 250, 252, 90)))
    painter.drawEllipse(QPointF(0, 0), 10, 14)
    painter.restore()


def _draw_butter(painter, entity, local, QPointF, QRectF, Qt, QBrush, QColor, QPen) -> None:
    painter.save()
    _draw_contact_shadow(painter, local.x, local.y, 0.9, entity.opacity, QPointF, Qt, QBrush, QColor)
    warp = 1.0 + 0.22 * entity.pose.body_warp + 0.12 * math.sin(entity.age * 6.0 + entity.seed)
    painter.translate(local.x + entity.pose.translation_x, local.y + entity.pose.translation_y)
    painter.scale(1.18 * max(0.7, warp), 0.78 / max(0.65, warp))
    painter.rotate(entity.pose.rotation * 0.35)
    painter.setOpacity(entity.opacity)
    painter.setPen(QPen(QColor(214, 176, 60), 1))
    painter.setBrush(QBrush(QColor(255, 221, 90)))
    from PyQt6.QtGui import QPainterPath as _Path

    blob = _Path()
    blob.moveTo(-10, 0)
    blob.cubicTo(-11, -8, -4, -9, 0, -8)
    blob.cubicTo(6, -9, 12, -5, 10, 1)
    blob.cubicTo(9, 7, 3, 8, 0, 7)
    blob.cubicTo(-6, 8, -11, 5, -10, 0)
    painter.drawPath(blob)
    painter.setBrush(QBrush(QColor(255, 244, 170, 140)))
    painter.drawEllipse(QPointF(-2, -1), 4, 3)
    painter.restore()


def _draw_particle(painter, particle, origin, QPointF, Qt, QBrush, QColor) -> None:
    painter.save()
    painter.translate(particle.pos.x - origin.x, particle.pos.y - origin.y)
    painter.setOpacity(max(0.0, min(1.0, particle.life)))
    painter.setPen(Qt.PenStyle.NoPen)
    if particle.kind == "heart":
        painter.setBrush(QBrush(QColor(244, 63, 94)))
        painter.drawEllipse(QPointF(-2, 0), 3, 3)
        painter.drawEllipse(QPointF(2, 0), 3, 3)
    else:
        painter.setBrush(QBrush(QColor(196, 140, 64)))
        painter.drawEllipse(QPointF(0, 0), 2.2, 1.6)
    painter.restore()


def _draw_portal_echo(painter, entity, local, QPointF, Qt, QBrush, QColor, QPen) -> None:
    painter.save()
    glitch = 2.0 * math.sin(entity.age * 9.0 + entity.seed)
    painter.translate(local.x + glitch, local.y)
    painter.setOpacity(min(0.55, entity.opacity))
    painter.setPen(QPen(QColor(167, 139, 250), 1.4))
    painter.setBrush(QBrush(QColor(91, 33, 182, 80)))
    painter.drawEllipse(QPointF(0, 0), 9, 12)
    painter.restore()


def _draw_scene_fx(painter, world, origin, QPointF, QRectF, Qt, QBrush, QColor, QPen) -> None:
    perf = getattr(world, "performance", None)
    if perf is None:
        return
    st = perf.state
    if not st.started or st.beat.value in {"completed", "interrupted"}:
        return
    toaster = Vec2(world.toaster.x - origin.x, world.toaster.y - origin.y)
    level = st.level
    painter.save()
    if st.scene.value == "portal_glitch":
        r = 10 + 6 * level + (8 if st.beat.value in {"beat2", "payoff"} else 0)
        painter.setPen(QPen(QColor(167, 139, 250, 180), 2))
        painter.setBrush(QBrush(QColor(91, 33, 182, 40 + 12 * level)))
        painter.drawEllipse(QPointF(toaster.x, toaster.y - 36), r, r * 1.15)
    elif st.scene.value == "secret_chamber":
        painter.setPen(QPen(QColor(124, 58, 237, 160), 1.5))
        painter.setBrush(QBrush(QColor(124, 58, 237, 30 + 10 * level)))
        painter.drawRoundedRect(QRectF(toaster.x - 18 - level, toaster.y - 24, 36 + 2 * level, 20), 4, 4)
    elif st.scene.value == "microwave_anomaly":
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(253, 224, 71, 50 + 15 * level)))
        painter.drawEllipse(QPointF(toaster.x - 90, toaster.y + 40), 8 + level, 8 + level)
    elif st.scene.value == "rice_zen":
        painter.setPen(QPen(QColor(226, 232, 240, 120), 1))
        painter.setBrush(QBrush(QColor(248, 250, 252, 40)))
        painter.drawEllipse(QPointF(toaster.x + 40, toaster.y + 50), 14, 10)
    elif st.scene.value == "airfryer_vortex":
        painter.setPen(QPen(QColor(120, 113, 108, 140), 1.2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QPointF(toaster.x - 40, toaster.y + 70), 12 + level * 3, 8 + level)
    elif st.scene.value == "kpop_heart_mode" and st.beat.value == "payoff":
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(244, 63, 94, 90)))
        painter.drawEllipse(QPointF(toaster.x, toaster.y - 50), 10 + level, 8 + level)
    elif st.scene.value == "shareware_apocalypse":
        painter.setOpacity(0.12 + 0.04 * level)
        painter.setPen(QPen(QColor(251, 191, 36), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(QRectF(8, 8, 120 + 20 * level, 40))
    elif st.scene.value == "funeral_ritual":
        painter.setPen(QPen(QColor(68, 64, 60, 160), 1.4))
        painter.drawLine(QPointF(toaster.x - 30, toaster.y + 20), QPointF(toaster.x + 30, toaster.y + 20))
    painter.restore()


def _draw_appliance(painter, appliance, origin, QPointF, QRectF, Qt, QBrush, QColor, QPen, dpi: float = 1.0) -> None:
    p = Vec2(appliance.pos.x - origin.x, appliance.pos.y - origin.y)
    if abs(p.x) > 420 or abs(p.y) > 420:
        return
    painter.save()
    painter.translate(p.x, p.y)
    painter.setOpacity(0.72 if appliance.open else 0.22)
    kind = appliance.kind
    if kind is ApplianceKind.MICROWAVE:
        painter.setPen(QPen(QColor("#64748b"), stroke_px(STROKE_POLICY["appliance"], dpi)))
        painter.setBrush(QBrush(QColor("#94a3b8")))
        painter.drawRoundedRect(QRectF(-18, -14, 36, 26), 3, 3)
        painter.setBrush(QBrush(QColor("#0f172a")))
        painter.drawRoundedRect(QRectF(-12, -8, 18, 14), 2, 2)
        # Resonant cavity + violet-white signature (audit 20 R09/R10).
        cavity = QColor(226, 232, 240, 70 + int(appliance.heat * 90))
        painter.setBrush(QBrush(cavity))
        painter.drawEllipse(QPointF(-3, -1), 6 + appliance.heat * 2, 5)
        painter.setPen(QPen(QColor(196, 181, 253, 160 + int(appliance.heat * 80)), 1.2))
        painter.setBrush(QBrush(QColor(237, 233, 254, 90 + int(appliance.heat * 80))))
        painter.drawEllipse(QPointF(-3, -1), 3.5, 3.5)
        if appliance.occupied:
            painter.setPen(QPen(QColor(167, 139, 250, 200), 1.0))
            painter.drawArc(QRectF(-10, -6, 14, 10), 0, 180 * 16)
    elif kind is ApplianceKind.AIR_FRYER:
        painter.setPen(QPen(QColor("#44403c"), 1.4))
        painter.setBrush(QBrush(QColor("#78716c")))
        painter.drawRoundedRect(QRectF(-12, -18, 24, 32), 8, 8)
        painter.setPen(QPen(QColor("#f59e0b"), 1.1))
        painter.setBrush(QBrush(QColor(251, 191, 36, 80 + int(appliance.heat * 110))))
        painter.drawEllipse(QPointF(0, 4), 7 + appliance.heat * 4, 5)
        painter.setPen(QPen(QColor("#22d3ee"), 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QPointF(0, 4), 9 + appliance.heat * 3, 6)
    elif kind is ApplianceKind.RICE_COOKER:
        painter.setPen(QPen(QColor("#cbd5e1"), 1.3))
        painter.setBrush(QBrush(QColor("#f8fafc")))
        painter.drawEllipse(QPointF(0, 4), 16, 12)
        painter.drawEllipse(QPointF(0, -6), 10, 8)
        painter.setBrush(QBrush(QColor(248, 250, 252, 80 + int(appliance.heat * 80))))
        painter.drawEllipse(QPointF(-3, -16), 5, 4)
        painter.drawEllipse(QPointF(4, -20), 4, 3)
    elif kind is ApplianceKind.OVEN:
        painter.setPen(QPen(QColor("#44403c"), 1.6))
        painter.setBrush(QBrush(QColor("#57534e")))
        painter.drawRoundedRect(QRectF(-22, -16, 44, 34), 3, 3)
        painter.setBrush(QBrush(QColor(234, 88, 12, 90 + int(appliance.heat * 150))))
        painter.drawRoundedRect(QRectF(-16, -10, 32, 22), 2, 2)
        painter.setBrush(QBrush(QColor(127, 29, 29, 80)))
        painter.drawRoundedRect(QRectF(-14, -8, 28, 18), 2, 2)
        painter.setPen(QPen(QColor("#a8a29e"), 1))
        painter.drawLine(QPointF(-16, 0), QPointF(16, 0))
    elif kind is ApplianceKind.ESPRESSO:
        painter.setPen(QPen(QColor("#292524"), 1.4))
        painter.setBrush(QBrush(QColor("#1c1917")))
        painter.drawRoundedRect(QRectF(-10, -16, 20, 28), 4, 4)
        painter.setBrush(QBrush(QColor("#78716c")))
        painter.drawEllipse(QPointF(0, 12), 8, 4)
        painter.setBrush(QBrush(QColor(251, 191, 36, 110 + int(appliance.heat * 120))))
        painter.drawEllipse(QPointF(0, 2), 5, 3.4)
        painter.setBrush(QBrush(QColor(253, 224, 71, 90)))
        painter.drawEllipse(QPointF(0, 8), 3, 2)
    elif kind is ApplianceKind.MINI_FRIDGE:
        painter.setPen(QPen(QColor("#94a3b8"), 1.4))
        painter.setBrush(QBrush(QColor("#cbd5e1")))
        painter.drawRoundedRect(QRectF(-14, -20, 28, 38), 4, 4)
        painter.setPen(QPen(QColor("#64748b"), 1.2))
        painter.drawLine(QPointF(-14, 0), QPointF(14, 0))
        painter.setBrush(QBrush(QColor(56, 189, 248, 90 + int(appliance.heat * 50))))
        painter.drawRoundedRect(QRectF(-10, -16, 20, 12), 2, 2)
        painter.drawEllipse(QPointF(8, -12), 2.4, 2.4)
    else:
        scale = max(1.0, appliance.interior_scale)
        painter.setPen(QPen(QColor("#7c3aed"), stroke_px(1.6, dpi)))
        painter.setBrush(QBrush(QColor(124, 58, 237, 70 + int(20 * scale))))
        painter.drawRoundedRect(QRectF(-10 * scale * 0.45, -12 * scale * 0.35, 20 * scale * 0.45, 22 * scale * 0.35), 6, 6)
        # Impossible-depth motif: nested vanishing ellipses.
        painter.setBrush(QBrush(QColor(250, 250, 255, 70)))
        painter.drawEllipse(QPointF(0, -2), 6 * scale * 0.28, 8 * scale * 0.28)
        painter.setBrush(QBrush(QColor(91, 33, 182, 90)))
        painter.drawEllipse(QPointF(0, 1), 3.2 * scale * 0.4, 8 * scale * 0.55)
        painter.setBrush(QBrush(QColor(167, 139, 250, 110)))
        painter.drawEllipse(QPointF(0, 0), 5 * scale * 0.35, 7 * scale * 0.35)
    if appliance.portal and kind is not ApplianceKind.SECRET_CHAMBER:
        painter.setBrush(QBrush(QColor(124, 58, 237, 90)))
        painter.drawEllipse(QPointF(0, 0), 7, 9)
    if appliance.occupied:
        painter.setPen(QPen(QColor("#fbbf24"), 1.1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QPointF(0, -18), 3, 3)
    painter.restore()
