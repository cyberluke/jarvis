"""Canonical main-character form registry.

World appliances remain independent entities. The selected form only
changes the main Toastovač avatar renderer, action capabilities, and
personality tint. Both Settings and the FaceWindow menu call
``set_character_form``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet

from .types import ApplianceKind


@dataclass(frozen=True)
class CharacterFormProfile:
    id: str
    display_name: str
    subtitle: str
    renderer_kind: str
    appliance_kind: ApplianceKind | None
    default_scale: float = 1.0
    animation_profile: str = "classic"
    sound_profile: str = "toaster"
    personality_profile: str = "troublemaker"
    capabilities: FrozenSet[str] = field(default_factory=frozenset)


_CAP_CLASSIC = frozenset(
    {
        "supports_slot_glow",
        "supports_toast_pop",
        "supports_steam",
        "supports_jelly",
        "supports_look",
    }
)
_CAP_RICE = frozenset({"supports_steam", "supports_lid", "supports_look", "supports_jelly"})
_CAP_MICROWAVE = frozenset({"supports_cavity_glow", "supports_door_open", "supports_look"})
_CAP_AIR = frozenset({"supports_fan", "supports_heat", "supports_look"})
_CAP_ESPRESSO = frozenset({"supports_steam", "supports_look"})
_CAP_OVEN = frozenset({"supports_hearth", "supports_look"})
_CAP_FRIDGE = frozenset({"supports_door_open", "supports_look"})


CHARACTER_FORMS: dict[str, CharacterFormProfile] = {
    "classic_toaster": CharacterFormProfile(
        id="classic_toaster",
        display_name="Classic Toaster",
        subtitle="The original troublemaker",
        renderer_kind="classic_toaster",
        appliance_kind=None,
        capabilities=_CAP_CLASSIC,
    ),
    "rice_cooker_zen": CharacterFormProfile(
        id="rice_cooker_zen",
        display_name="Rice Cooker Zen",
        subtitle="Calm heat, enlightened carbs",
        renderer_kind="rice_cooker",
        appliance_kind=ApplianceKind.RICE_COOKER,
        animation_profile="zen",
        sound_profile="rice",
        personality_profile="zen",
        capabilities=_CAP_RICE,
    ),
    "microwave": CharacterFormProfile(
        id="microwave",
        display_name="Microwave",
        subtitle="Violet cavity, questionable decisions",
        renderer_kind="microwave",
        appliance_kind=ApplianceKind.MICROWAVE,
        animation_profile="cavity",
        sound_profile="microwave",
        personality_profile="chaotic",
        capabilities=_CAP_MICROWAVE,
    ),
    "air_fryer": CharacterFormProfile(
        id="air_fryer",
        display_name="Air Fryer",
        subtitle="High velocity crunch technology",
        renderer_kind="air_fryer",
        appliance_kind=ApplianceKind.AIR_FRYER,
        animation_profile="vortex",
        sound_profile="airfryer",
        personality_profile="crisp",
        capabilities=_CAP_AIR,
    ),
    "espresso": CharacterFormProfile(
        id="espresso",
        display_name="Espresso Module",
        subtitle="Office alignment, warm crema",
        renderer_kind="espresso",
        appliance_kind=ApplianceKind.ESPRESSO,
        animation_profile="portafilter",
        sound_profile="espresso",
        personality_profile="corporate",
        capabilities=_CAP_ESPRESSO,
    ),
    "oven": CharacterFormProfile(
        id="oven",
        display_name="Oven",
        subtitle="Slow ritual bakery",
        renderer_kind="oven",
        appliance_kind=ApplianceKind.OVEN,
        animation_profile="hearth",
        sound_profile="oven",
        personality_profile="ritual",
        capabilities=_CAP_OVEN,
    ),
    "mini_fridge": CharacterFormProfile(
        id="mini_fridge",
        display_name="Mini Fridge",
        subtitle="Late-night larder",
        renderer_kind="mini_fridge",
        appliance_kind=ApplianceKind.MINI_FRIDGE,
        animation_profile="cold",
        sound_profile="fridge",
        personality_profile="night",
        capabilities=_CAP_FRIDGE,
    ),
}

DEFAULT_FORM_ID = "classic_toaster"


def list_forms() -> list[CharacterFormProfile]:
    return list(CHARACTER_FORMS.values())


def get_form(form_id: str | None) -> CharacterFormProfile:
    if form_id and form_id in CHARACTER_FORMS:
        return CHARACTER_FORMS[form_id]
    return CHARACTER_FORMS[DEFAULT_FORM_ID]


def is_registered(form_id: str) -> bool:
    return form_id in CHARACTER_FORMS


def form_supports(form_id: str, capability: str) -> bool:
    return capability in get_form(form_id).capabilities
