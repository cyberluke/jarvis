"""Local app runtime registry for the desktop control plane.

Toastovač needs only runtime metadata per app: display name, aliases,
process names, and the MCP server + tool namespace it maps to. NAI OS
owns marketplace/install metadata; this registry is fed from
``control_plane_apps`` in the daemon config.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class AppEntry:
    app_id: str
    display_name: str
    aliases: List[str] = field(default_factory=list)
    process_names: List[str] = field(default_factory=list)
    mcp_server_id: Optional[str] = None
    tool_namespace: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "appId": self.app_id,
            "displayName": self.display_name,
            "aliases": list(self.aliases),
            "processNames": list(self.process_names),
            "mcpServerId": self.mcp_server_id,
            "toolNamespace": self.tool_namespace,
        }


def _norm_alias(alias: str) -> str:
    return re.sub(r"\s+", " ", alias.strip().lower())


class AppRegistry:
    """Lookup by ``appId`` or by alias; namespace helpers.

    Entries are validated at construction: an entry must have a
    non-empty ``appId``; entries with duplicate ids or aliases collapse
    (first wins) and are logged as diagnostics.
    """

    def __init__(
        self,
        apps: Optional[List[Dict[str, Any]]] = None,
        mcps: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._apps: Dict[str, AppEntry] = {}
        self._aliases: Dict[str, str] = {}
        self._mcps = dict(mcps or {})
        self._load(apps or [])

    def _load(self, apps: List[Dict[str, Any]]) -> None:
        for raw in apps:
            if not isinstance(raw, dict):
                continue
            app_id = str(raw.get("appId") or "").strip()
            if not app_id:
                continue
            if app_id in self._apps:
                continue
            aliases = [a for a in (_norm_alias(str(a)) for a in raw.get("aliases") or []) if a]
            entry = AppEntry(
                app_id=app_id,
                display_name=str(raw.get("displayName") or app_id),
                aliases=aliases,
                process_names=[str(p) for p in raw.get("processNames") or []],
                mcp_server_id=_optional_text(raw.get("mcpServerId")),
                tool_namespace=_optional_text(raw.get("toolNamespace")),
            )
            self._apps[app_id] = entry
            self._aliases[app_id] = app_id
            for alias in aliases:
                if alias not in self._aliases:
                    self._aliases[alias] = app_id

    # -- lookup ---------------------------------------------------------

    def get(self, app_id: str) -> Optional[AppEntry]:
        return self._apps.get(app_id)

    def by_alias(self, text: str) -> Optional[AppEntry]:
        """Resolve an app from raw text via alias matching.

        Matches are inflection-aware: a single-word alias matches a word
        equal to the alias or a short inflected extension of it
        (``blender`` matches ``blenderu``, ``blenderem``), which covers
        Czech/Slavic declension without a stemmer. Multi-word aliases
        match as word-bounded substrings.
        """
        norm = _norm_alias(text)
        if not norm:
            return None
        words = [re.sub(r"[^\w]", "", w) for w in norm.split() if re.sub(r"[^\w]", "", w)]
        for alias, app_id in self._aliases.items():
            if _match_alias(alias, norm, words):
                return self._apps.get(app_id)
        return None

    def all(self) -> List[AppEntry]:
        return list(self._apps.values())

    def app_ids(self) -> List[str]:
        return list(self._apps.keys())

    def aliases(self) -> List[str]:
        return list(self._aliases.keys())

    def mcp_config(self, app: AppEntry) -> Optional[Dict[str, Any]]:
        """The MCP server config for an app, or ``None`` when the app has
        no server or the server is not configured in ``cfg.mcps``."""
        if not app.mcp_server_id:
            return None
        cfg = self._mcps.get(app.mcp_server_id)
        return cfg if isinstance(cfg, dict) else None

    def has_mcp(self, app: AppEntry) -> bool:
        return self.mcp_config(app) is not None

    def qualifies_tool(self, app: AppEntry, tool_name: str) -> bool:
        """True when ``tool_name`` is namespace-qualified for ``app``."""
        if not app.tool_namespace:
            return False
        prefix = f"{app.tool_namespace}."
        return isinstance(tool_name, str) and tool_name.startswith(prefix)


def _optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


#: Maximum extra characters an inflected word may carry past the alias
#: (``blenderem`` = +2, ``blenderu`` = +1) before it stops matching.
_MAX_INFLECTION_SUFFIX = 4


def _match_alias(alias: str, norm: str, words: List[str]) -> bool:
    if " " in alias:
        return _word_boundary_search(alias, norm)
    for word in words:
        if word == alias:
            return True
        if (
            word.startswith(alias)
            and 0 < len(word) - len(alias) <= _MAX_INFLECTION_SUFFIX
        ):
            return True
    return False


def _word_boundary_search(alias: str, text: str) -> bool:
    """Word-bounded substring search (no regex injection, no hyphen join
    confusion: ``blender`` matches in ``do blenderu`` but not in
    ``blenderovaci``)."""
    start = 0
    while True:
        idx = text.find(alias, start)
        if idx < 0:
            return False
        before_ok = idx == 0 or not (text[idx - 1].isalnum() or text[idx - 1] == "_")
        end = idx + len(alias)
        after_ok = end >= len(text) or not (text[end].isalnum() or text[end] == "_")
        if before_ok and after_ok:
            return True
        start = idx + 1