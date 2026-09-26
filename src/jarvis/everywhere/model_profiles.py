"""Action-oriented model profiles mapped onto the existing two-tier router.

No new model client lives here. Each Everywhere action names a profile; the
profile resolves through ``jarvis.llm.resolve_model`` (Tier.FAST / Tier.CHAT)
against the active ``Settings``. Latency-sensitive actions (rewrite,
proofread) ride the FAST tier by default; reasoning-heavy ones (explain,
translate, custom prompts) ride the CHAT tier. The mapping itself is config
(``everywhere_profiles``), so no magic values hide in this file.
"""

from __future__ import annotations

from typing import Dict

from ..llm import Tier, resolve_model
from .protocol import ACTION_IDS

#: Profile names the config may assign per action.
PROFILE_NAMES = (
    "fast-edit",        # small warm model: rewrite / proofread / alternatives
    "structured-edit",
    "creative-edit",
    "reasoning",        # capable model: explain / translate
    "translation",
    "user-selected",    # saved-prompt library entry carries its own model
)

#: Built-in defaults, overridable via the ``everywhere_profiles`` mapping.
DEFAULT_PROFILES: Dict[str, str] = {
    "rewrite": "fast-edit",
    "proofread": "structured-edit",
    "alternatives": "creative-edit",
    "explain": "reasoning",
    # Translate defaults to the FAST tier: a direct translation is a
    # latency-sensitive action (the user will not wait minutes for one
    # sentence). The reasoning model ("translation" profile) is available for
    # high-quality work via ``everywhere_profiles`` override.
    "translate": "fast-edit",
    "prompt": "user-selected",
    "fix_command": "fast-edit",
    "explain_command": "reasoning",
    "safer_variant": "fast-edit",
    "docker_help": "fast-edit",
    "devops_help": "fast-edit",
}

#: Which tier each profile name rides on the existing router.
_PROFILE_TIER: Dict[str, Tier] = {
    "fast-edit": Tier.FAST,
    "structured-edit": Tier.FAST,
    "creative-edit": Tier.CHAT,
    "reasoning": Tier.CHAT,
    "translation": Tier.CHAT,
    "user-selected": Tier.CHAT,
}


def resolve_profile_name(cfg, action: str) -> str:
    """Config-driven profile name for ``action`` (falls back to defaults)."""
    table: Dict[str, str] = dict(DEFAULT_PROFILES)
    override = getattr(cfg, "everywhere_profiles", None)
    if isinstance(override, dict):
        for key, value in override.items():
            if str(key) in table and isinstance(value, str) and value.strip():
                table[str(key)] = value.strip()
    if action not in ACTION_IDS:
        return "reasoning"
    return table.get(action, "reasoning")


def resolve_model_for_action(cfg, action: str) -> str:
    """Model name for ``action`` via the canonical two-tier resolution."""
    profile = resolve_profile_name(cfg, action)
    tier = _PROFILE_TIER.get(profile, Tier.CHAT)
    return resolve_model(cfg, tier)
