"""Social simulation. Nearby toasts pair, copy, scuffle — never above safety."""

from __future__ import annotations

from .entities import Entity
from .types import EntityKind, SAFETY_STATES, ToastState


def nearest(entity: Entity, others: list[Entity]) -> Entity | None:
    pals = [
        other
        for other in others
        if other.id != entity.id
        and other.kind in {EntityKind.MINI_TOAST, EntityKind.BURNT_TOAST}
        and other.state not in SAFETY_STATES
        and not other.hidden
    ]
    if not pals:
        return None
    return min(pals, key=lambda other: (other.pos - entity.pos).length())


def propose(entity: Entity, others: list[Entity], radius: float) -> ToastState | None:
    if entity.state in SAFETY_STATES or entity.state in {
        ToastState.TOASTING,
        ToastState.OVERTOASTING,
        ToastState.JUMP_INTO_SLOT,
        ToastState.INSIDE_APPLIANCE,
        ToastState.PORTAL_ENTER,
        ToastState.PORTAL_CURIOUS,
        ToastState.PORTAL_RETURN,
        ToastState.APPROACH_TOASTER,
        ToastState.QUEUE_FOR_SLOT,
        ToastState.CARRY_CRUMB,
        ToastState.EAT_CRUMB,
        ToastState.POP_OUT,
        ToastState.BURNT,
        ToastState.RECOVER,
        ToastState.COOL_DOWN,
        ToastState.RITUAL_FUNERAL,
        ToastState.DESPAWN,
    }:
        return None
    pal = nearest(entity, others)
    if pal is None:
        return None
    dist = (pal.pos - entity.pos).length()
    if dist > radius * 2.4:
        return None
    entity.partner_id = pal.id
    if pal.state in {ToastState.DANCE, ToastState.HEART_MODE} and entity.trait("copycat_drive") > 0.42:
        return ToastState.COPY_DANCE
    if pal.state is ToastState.SOCIALIZE and entity.trait("sociality") > 0.4:
        return ToastState.SOCIALIZE
    if pal.state is ToastState.FIGHT_PLAYFULLY and entity.trait("boldness") > 0.55:
        return ToastState.FIGHT_PLAYFULLY
    if dist < radius and entity.trait("sociality") > 0.5 and entity.state in {ToastState.IDLE, ToastState.WANDER}:
        return ToastState.SOCIALIZE
    if (
        dist < radius * 1.8
        and pal.state is ToastState.FOLLOW_CURSOR_AT_DISTANCE
        and entity.state in {ToastState.IDLE, ToastState.WANDER}
        and entity.trait("copycat_drive") > 0.55
    ):
        entity.target = pal.pos
        return ToastState.FOLLOW_CURSOR_AT_DISTANCE
    return None


def propose_weighted(entity: Entity, others: list[Entity], radius: float) -> ToastState | None:
    """Trait-weighted social pairing for unused personality axes."""
    pal = nearest(entity, others)
    if pal is None or entity.state in SAFETY_STATES:
        return None
    dist = (pal.pos - entity.pos).length()
    if dist > radius * 2.6:
        return None
    score = (
        entity.trait("sociality") * 0.28
        + entity.trait("copycat_drive") * 0.18
        + entity.trait("smugness") * 0.08
        + entity.trait("attachment_to_user") * 0.08
        + entity.trait("attachment_to_main_toaster") * 0.08
        + entity.trait("chaos_affinity") * 0.1
        + entity.trait("portal_curiosity") * 0.06
        + entity.trait("risk_tolerance") * 0.06
        + (1.0 - entity.trait("crowd_tolerance")) * 0.08
    )
    if score < 0.42:
        return None
    entity.partner_id = pal.id
    if entity.trait("chaos_affinity") > 0.7:
        return ToastState.DANCE
    if entity.trait("risk_tolerance") > 0.72:
        return ToastState.FIGHT_PLAYFULLY
    if entity.trait("smugness") > 0.7:
        return ToastState.OBSERVE_USER
    return ToastState.SOCIALIZE
