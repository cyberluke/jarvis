"""Action table: id -> (result_mode, prompt source, model profile default).

The broker dispatches on these ids; the native toolbar renders them in the
same order. Terminal-surface extras are only offered when the snapshot's
``source_kind`` is a terminal surface, and their insertion still goes through
the existing terminal composer (``sendText(command, false)`` semantics).
"""

from __future__ import annotations

from typing import Dict, Optional

#: action id -> result mode (§Actions table).
ACTION_RESULT_MODES: Dict[str, str] = {
    "rewrite": "replace",
    "proofread": "structured",
    "alternatives": "alternatives",
    "explain": "panel",
    "translate": "panel",
    "prompt": "per-prompt",
    "fix_command": "panel",
    "explain_command": "panel",
    "safer_variant": "panel",
    "docker_help": "panel",
    "devops_help": "panel",
}

#: Default direct-action hotkeys (all configurable in ``everywhere_hotkeys``).
DEFAULT_HOTKEYS: Dict[str, str] = {
    "toolbar": "ctrl+shift+space",
    "rewrite": "ctrl+shift+c",
    "proofread": "ctrl+shift+h",
    "alternatives": "ctrl+shift+l",
    "explain": "ctrl+shift+x",
    "translate": "ctrl+shift+t",
    "prompt": "ctrl+shift+j",
    "ocr": "alt+drag",
    "help": "ctrl+/",
    "cancel": "esc",
}

TERMINAL_SOURCE_KINDS = ("vscode-terminal", "powershell", "windows-terminal")


def actions_for_source(source_kind: str) -> tuple:
    """Deterministic action list for one source kind (§Provider hierarchy)."""
    base = ("rewrite", "proofread", "alternatives", "explain", "translate",
            "prompt")
    if source_kind in TERMINAL_SOURCE_KINDS:
        return base + ("fix_command", "explain_command", "safer_variant",
                       "docker_help", "devops_help")
    if source_kind == "ocr-region":
        # Pixels cannot be written back into the source: Copy replaces Insert.
        return ("proofread", "alternatives", "explain", "translate", "prompt")
    return base


def is_insertable(source_kind: str, replace_capability: str) -> bool:
    """True when an explicit Insert may modify the origin target."""
    if source_kind == "ocr-region":
        return False
    return replace_capability != "COPY_ONLY"


def resolve_hotkeys(cfg) -> Dict[str, str]:
    """Config-overridable hotkey table (explicit strings, no magic)."""
    table = dict(DEFAULT_HOTKEYS)
    override = getattr(cfg, "everywhere_hotkeys", None)
    if isinstance(override, dict):
        for key, value in override.items():
            if str(key) in table and isinstance(value, str) and value.strip():
                table[str(key)] = value.strip().lower()
    return table


def alternatives_count(cfg) -> int:
    """Clamped alternatives count for the current request (§Alternatives)."""
    minimum = _as_int(getattr(cfg, "everywhere_alternatives_min", 3), 3)
    maximum = _as_int(getattr(cfg, "everywhere_alternatives_max", 15), 15)
    default = _as_int(getattr(cfg, "everywhere_alternatives_count", 3), 3)
    minimum = max(1, min(minimum, 15))
    maximum = max(minimum, min(maximum, 15))
    return max(minimum, min(maximum, default))


def _as_int(value: object, fallback: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
