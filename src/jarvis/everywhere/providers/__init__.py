"""Selection provider hierarchy (deterministic, fail-closed).

One provider per ``source_kind``. The chosen provider never silently swaps:
``select_provider`` maps the source kind, and each provider revalidates its
own identity fields at apply time. See ``everywhere.spec.md`` §3.
"""

from __future__ import annotations

from typing import Optional, Tuple

from . import ocr, terminal, uia, vscode
from ..protocol import FAILURE_CODES  # noqa: F401 (re-export)

_PROVIDER_BY_KIND = {
    "vscode-editor": vscode,
    "vscode-terminal": terminal,
    "powershell": terminal,
    "windows-terminal": terminal,
    "uia-text": uia,
    "ocr-region": ocr,
}


def select_provider(source_kind: str):
    """The one provider for ``source_kind``; None means fail closed."""
    return _PROVIDER_BY_KIND.get(source_kind)


def revalidate(snapshot_dict: dict, current: dict) -> Tuple[bool, str]:
    """Revalidate a target at apply time. ``(ok, failure_code)``.

    ``snapshot_dict`` is the frozen capture; ``current`` is the live state
    the host reports. An unknown provider or a missing provider module fails
    closed with ``PROVIDER_UNAVAILABLE``.
    """
    source_kind = str((snapshot_dict or {}).get("source_kind") or "")
    provider = select_provider(source_kind)
    if provider is None:
        return False, "PROVIDER_UNAVAILABLE"
    return provider.revalidate(snapshot_dict, current)


__all__ = [
    "FAILURE_CODES",
    "ocr",
    "revalidate",
    "select_provider",
    "terminal",
    "uia",
    "vscode",
]
