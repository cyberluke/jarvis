"""Immutable SelectionSnapshot: the one canonical unit of context.

Never pass naked strings between providers and actions. Every interaction
begins by freezing a snapshot; a changed selection produces another snapshot
with another revision, never a mutation of the old one.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from .protocol import SOURCE_KINDS, REPLACE_CAPABILITIES


def text_hash(text: str) -> str:
    """Lowercase hex SHA-256 of the UTF-8 text (logs carry this, not text)."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SelectionSnapshot:
    """One frozen observation of a selection plus its provenance."""

    snapshot_id: str
    revision: int
    captured_at: float
    source_kind: str
    provider_id: str
    text: str
    text_sha: str
    hwnd: Optional[int] = None
    process_id: Optional[int] = None
    process_name: str = ""
    window_title: str = ""
    editable: bool = False
    replace_capability: str = "COPY_ONLY"
    selection_bounds: Tuple[Tuple[float, float, float, float], ...] = ()
    cursor_position: Optional[int] = None
    semantic_context: Dict[str, Any] = field(default_factory=dict)
    provider_token: Dict[str, Any] = field(default_factory=dict)

    @property
    def text_length(self) -> int:
        return len(self.text)

    def to_dict(self) -> Dict[str, Any]:
        """Wire form (what the native host sends/receives)."""
        return {
            "snapshot_id": self.snapshot_id,
            "revision": self.revision,
            "captured_at": self.captured_at,
            "source_kind": self.source_kind,
            "provider_id": self.provider_id,
            "text": self.text,
            "text_hash": self.text_sha,
            "hwnd": self.hwnd,
            "process_id": self.process_id,
            "process_name": self.process_name,
            "window_title": self.window_title,
            "editable": self.editable,
            "replace_capability": self.replace_capability,
            "selection_bounds": [list(b) for b in self.selection_bounds],
            "cursor_position": self.cursor_position,
            "semantic_context": self.semantic_context,
            "provider_token": self.provider_token,
        }

    @classmethod
    def from_dict(cls, obj: Dict[str, Any]) -> "SelectionSnapshot":
        """Parse the wire form. Raises ValueError on an invalid shape so the
        broker can fail closed with an explicit reason."""
        if not isinstance(obj, dict):
            raise ValueError("snapshot must be an object")
        source_kind = str(obj.get("source_kind") or "")
        if source_kind not in SOURCE_KINDS:
            raise ValueError(f"unknown source_kind: {source_kind!r}")
        cap = str(obj.get("replace_capability") or "COPY_ONLY")
        if cap not in REPLACE_CAPABILITIES:
            raise ValueError(f"unknown replace_capability: {cap!r}")
        bounds_raw = obj.get("selection_bounds") or ()
        bounds: list = []
        if isinstance(bounds_raw, (list, tuple)):
            for b in bounds_raw:
                if isinstance(b, (list, tuple)) and len(b) == 4:
                    try:
                        bounds.append((float(b[0]), float(b[1]),
                                       float(b[2]), float(b[3])))
                    except (TypeError, ValueError):
                        continue
        try:
            revision = int(obj.get("revision") or 0)
        except (TypeError, ValueError):
            revision = 0
        text = str(obj.get("text") or "")
        sha = str(obj.get("text_hash") or "") or text_hash(text)
        sem = obj.get("semantic_context")
        tok = obj.get("provider_token")
        return cls(
            snapshot_id=str(obj.get("snapshot_id") or ""),
            revision=revision,
            captured_at=float(obj.get("captured_at") or time.time()),
            source_kind=source_kind,
            provider_id=str(obj.get("provider_id") or source_kind),
            text=text,
            text_sha=sha,
            hwnd=_as_int(obj.get("hwnd")),
            process_id=_as_int(obj.get("process_id")),
            process_name=str(obj.get("process_name") or ""),
            window_title=str(obj.get("window_title") or ""),
            editable=bool(obj.get("editable", False)),
            replace_capability=cap,
            selection_bounds=tuple(bounds),
            cursor_position=_as_int(obj.get("cursor_position")),
            semantic_context=sem if isinstance(sem, dict) else {},
            provider_token=tok if isinstance(tok, dict) else {},
        )


def _as_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def make_snapshot(obj: Dict[str, Any]) -> SelectionSnapshot:
    """Freeze a wire dict into a snapshot, recomputing the hash when absent."""
    snap = SelectionSnapshot.from_dict(obj)
    if not snap.text_sha:
        object.__setattr__  # noqa: B018 (documented frozen dataclass)
    return snap
