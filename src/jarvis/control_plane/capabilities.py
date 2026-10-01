"""Capability manifest builder for the desktop control plane.

``GET /desktop/v1/capabilities`` produces the snapshot the browser and
NAI OS use to know what the local machine can currently do. The manifest
is a view over the registries, never a source of truth itself.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

from .apps import AppRegistry
from .mcp_registry import McpRuntimeRegistry
from .pairing import PairingStore
from .protocol import PROTOCOL_VERSION
from .sessions import BrowserSessionRegistry


class CapabilityManifestBuilder:
    """Builds the §11 manifest (plus additive ``sessions`` /
    ``dangerousTools``) from live registry state."""

    def __init__(
        self,
        apps: AppRegistry,
        mcp: McpRuntimeRegistry,
        sessions: BrowserSessionRegistry,
        pairings: PairingStore,
        dangerous_patterns: Optional[List[str]] = None,
    ) -> None:
        self._apps = apps
        self._mcp = mcp
        self._sessions = sessions
        self._pairings = pairings
        self._dangerous = [
            re.compile(p, re.IGNORECASE)
            for p in (dangerous_patterns or [])
            if p
        ]

    def build(self) -> Dict[str, Any]:
        apps_manifest: Dict[str, Any] = {}
        dangerous_tools: List[str] = []
        for app in self._apps.all():
            app_entry: Dict[str, Any] = {
                "displayName": app.display_name,
                "aliases": list(app.aliases),
                "processNames": list(app.process_names),
                "running": _process_running(app.process_names),
            }
            mcp_part: Optional[Dict[str, Any]] = None
            if app.mcp_server_id:
                status = self._mcp.status(app.mcp_server_id)
                mcp_part = {
                    "serverId": app.mcp_server_id,
                    "running": status.state == "RUNNING",
                    "state": status.state,
                    "schemaHash": status.schema_hash,
                    "tools": [
                        _qualify(app.tool_namespace, t["name"])
                        for t in status.tools
                        if t.get("name")
                    ],
                }
                if self._dangerous:
                    for t in status.tools:
                        name = t.get("name") or ""
                        if self._is_dangerous(name, t.get("description") or ""):
                            dangerous_tools.append(
                                _qualify(app.tool_namespace, name)
                            )
            app_entry["mcp"] = mcp_part
            apps_manifest[app.app_id] = app_entry

        sessions_manifest = [
            {
                "tabSessionId": s.tab_session_id,
                "userId": s.user_id,
                "foreground": s.foreground,
                "workspaceId": s.workspace_id,
                "lastSeenAt": s.last_seen_at,
            }
            for s in self._sessions.all()
        ]
        return {
            "protocol": PROTOCOL_VERSION,
            "hostId": self._pairings.host_id,
            "apps": apps_manifest,
            "dangerousTools": sorted(set(dangerous_tools)),
            "sessions": sessions_manifest,
            "generatedAt": time.time(),
        }

    def _is_dangerous(self, name: str, description: str) -> bool:
        haystack = f"{name} {description}"
        return any(pat.search(haystack) for pat in self._dangerous)


def _qualify(namespace: Optional[str], tool_name: str) -> str:
    if not namespace:
        return tool_name
    return f"{namespace}.{tool_name}"


def _process_running(process_names: List[str]) -> Optional[bool]:
    """Best-effort foreground-process check via psutil.

    ``None`` when the app declares no process names; ``False`` when the
    process list cannot be read (honest failure, never a fake "running").
    """
    if not process_names:
        return None
    try:
        import psutil  # noqa: PLC0415

        wanted = {name.strip().lower() for name in process_names if name.strip()}
        if not wanted:
            return None
        for proc in psutil.process_iter(["name"]):
            try:
                name = proc.info.get("name")
                if name and name.lower() in wanted:
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return False
    except Exception:  # noqa: BLE001
        return None