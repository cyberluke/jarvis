"""Luxury appliance universe. Distinct forms, interiors, lights, narrative roles."""

from __future__ import annotations

from dataclasses import dataclass, field

from .types import ApplianceKind, Vec2


@dataclass
class Appliance:
    kind: ApplianceKind
    pos: Vec2
    open: bool = False
    interior_scale: float = 1.0
    heat: float = 0.0
    occupied: int = 0
    last_used: float = -1e9
    portal: bool = False
    label: str = ""
    light: str = ""
    role: str = ""
    form: str = ""
    interior: str = ""
    emerge_to: str = ""
    delayed_return: bool = False
    impossible: bool = False

    def snapshot(self) -> dict:
        return {
            "kind": self.kind.value,
            "open": self.open,
            "heat": round(self.heat, 3),
            "occupied": self.occupied,
            "portal": self.portal,
            "label": self.label,
            "light": self.light,
            "role": self.role,
            "form": self.form,
            "interior": self.interior,
            "interior_scale": self.interior_scale,
            "impossible": self.impossible,
        }


# form, interior, signature light, narrative role, interior_scale, portal
APPLIANCE_META = {
        ApplianceKind.MICROWAVE: (
        "Microwave Chamber",
        "cube_window",
        "resonant_cavity",
        "violet_white_glow",
        "popcorn_portal",
        1.8,
        True,
    ),
    ApplianceKind.AIR_FRYER: (
        "Air Fryer",
        "basket_column",
        "vortex_basket",
        "cyan_vortex",
        "crisp_existentialism",
        1.4,
        False,
    ),
    ApplianceKind.RICE_COOKER: (
        "Rice Cooker",
        "domed_pot",
        "steam_well",
        "soft_white_steam",
        "zen_spirit_host",
        1.6,
        False,
    ),
    ApplianceKind.OVEN: (
        "Oven",
        "wide_hearth",
        "cathedral_rack",
        "orange_hearth",
        "slow_ritual_bakery",
        2.2,
        False,
    ),
    ApplianceKind.ESPRESSO: (
        "Espresso Module",
        "portafilter_tower",
        "group_head",
        "warm_crema",
        "office_alignment_shrine",
        1.1,
        False,
    ),
    ApplianceKind.MINI_FRIDGE: (
        "Mini Fridge",
        "short_cabinet",
        "cold_shelf",
        "blue_hum",
        "late_night_larder",
        1.7,
        False,
    ),
    ApplianceKind.SECRET_CHAMBER: (
        "Secret Chamber",
        "lavender_door",
        "impossible_depth",
        "lavender_anomaly",
        "non_euclidean_passage",
        3.4,
        True,
    ),
}


class ApplianceWorld:
    def __init__(self) -> None:
        self.items: list[Appliance] = []
        self.unlocked: set[ApplianceKind] = set()
        self.passages: list[tuple[str, ApplianceKind, ApplianceKind, float]] = []

    def ensure_layout(self, toaster: Vec2) -> None:
        if self.items:
            for item in self.items:
                # Keep relative kitchen, but allow toaster to move.
                pass
            return
        offsets = {
            ApplianceKind.MICROWAVE: Vec2(-90, 40),
            ApplianceKind.AIR_FRYER: Vec2(-40, 70),
            ApplianceKind.RICE_COOKER: Vec2(40, 74),
            ApplianceKind.OVEN: Vec2(96, 36),
            ApplianceKind.ESPRESSO: Vec2(-120, -10),
            ApplianceKind.MINI_FRIDGE: Vec2(130, -8),
            ApplianceKind.SECRET_CHAMBER: Vec2(0, -70),
        }
        for kind, (label, form, interior, light, role, scale, portal) in APPLIANCE_META.items():
            self.items.append(
                Appliance(
                    kind=kind,
                    pos=toaster + offsets[kind],
                    interior_scale=scale,
                    portal=portal,
                    label=label,
                    form=form,
                    interior=interior,
                    light=light,
                    role=role,
                    open=kind in self.unlocked,
                    impossible=False,
                )
            )

    def unlock(self, kind: ApplianceKind) -> Appliance:
        self.unlocked.add(kind)
        for item in self.items:
            if item.kind is kind:
                item.open = True
                return item
        return self.items[0]

    def by_kind(self, kind: ApplianceKind) -> Appliance | None:
        return next((item for item in self.items if item.kind is kind), None)

    def admit(self, kind: ApplianceKind, t: float) -> Appliance | None:
        item = self.by_kind(kind)
        if item is None or not item.open:
            return None
        item.occupied += 1
        item.last_used = t
        item.heat = min(1.0, item.heat + 0.25)
        if item.impossible:
            item.delayed_return = True
        return item

    def release(self, kind: ApplianceKind) -> None:
        item = self.by_kind(kind)
        if item is None:
            return
        item.occupied = max(0, item.occupied - 1)

    def other_exit(self, kind: ApplianceKind) -> Appliance | None:
        opened = [item for item in self.items if item.open and item.kind is not kind]
        if opened:
            return opened[0]
        return self.by_kind(ApplianceKind.SECRET_CHAMBER)

    def open_passage(self, src: ApplianceKind, dst: ApplianceKind, t: float, entity_id: str) -> None:
        self.passages.append((entity_id, src, dst, t))
        origin = self.by_kind(src)
        dest = self.by_kind(dst)
        if origin:
            origin.impossible = True
        if dest:
            dest.open = True

    def tick(self, dt: float) -> None:
        for item in self.items:
            item.heat = max(0.0, item.heat - dt * 0.05)
            if item.occupied > 0:
                item.heat = min(1.0, item.heat + dt * 0.08)

    def interior_snapshot(self, kind: ApplianceKind) -> dict:
        item = self.by_kind(kind)
        if item is None:
            return {}
        return {
            "kind": item.kind.value,
            "open": item.open,
            "occupied": item.occupied,
            "heat": round(item.heat, 3),
            "interior": item.interior,
            "light": item.light,
            "form": item.form,
            "role": item.role,
            "interior_scale": item.interior_scale,
        }
