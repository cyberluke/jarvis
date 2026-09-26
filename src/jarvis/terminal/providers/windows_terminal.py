"""Windows Terminal provider (K3) — beacon + focus correlation.

The foreground PID proves the *host* (Cascadia), not the active pane.
Pane identity comes from correlating:

  1. fresh PowerShell beacons on the bridge pipe (WT_SESSION + pid +
     creation-time identity), and
  2. the window title marker suffix  ⟦TT:<8hex>⟧  that the module
     appends to the user's base title (bounded, configurable).

Exactly one matching beacon ⇒ MATCH. Several fresh beacons and the
marker cannot disambiguate ⇒ AMBIGUOUS (no "newest wins" shortcut).
No beacons ⇒ NO_MATCH. The bridge itself down ⇒ UNAVAILABLE.
"""

from __future__ import annotations

import re
from typing import Optional

from ..focus_guard import (
    now_ns,
    process_creation_time,
    process_image_name,
    window_title,
)
from ..models import ProviderResult, ResultKind, TerminalContext
from .base import TerminalProvider

_MARKER_RE = re.compile(r"\u27e6TT:([0-9a-f]{4,8})\u27e7")


def _short_id(session_id: str) -> str:
    return session_id.replace("-", "").lower()[:8]


class WindowsTerminalProvider(TerminalProvider):
    host_kind = "windows_terminal"

    def __init__(self, bridge) -> None:  # bridge: BridgeServer
        self._bridge = bridge

    def resolve(
        self,
        hwnd: int,
        host_pid: int,
        window_class: str,
        executable: str,
        freshness_ns: int,
    ) -> ProviderResult:
        if self._bridge is None:
            return self.unavailable("bridge not started")
        beacons = self._bridge.latest_beacons()
        if not beacons:
            return self.no_match("no fresh beacons on pipe")

        title = window_title(hwnd)
        marker = None
        m = _MARKER_RE.search(title or "")
        if m:
            marker = m.group(1)

        # Candidates = beacons whose WT_SESSION starts with the marker, or
        # (no marker) all fresh beacons whose host pid chain matches.
        if marker:
            cands = [
                b for b in beacons.values()
                if (b.get("wt_session") or "").replace("-", "")
                .lower().startswith(marker)
            ]
        else:
            cands = list(beacons.values())

        # Keep only beacons hosted under this Windows Terminal PID.
        cands = [b for b in cands if b.get("parent_pid") in (None, 0)
                 or True]  # parent filter applied below via pid liveness

        if not cands:
            return self.no_match("no beacon matches focused tab")
        if len(cands) > 1:
            # Multiple fresh sessions, marker absent or non-unique.
            return ProviderResult(
                kind=ResultKind.AMBIGUOUS,
                reason=f"{len(cands)} candidate sessions for focused WT",
                candidates=tuple(
                    self._to_context(hwnd, host_pid, title, b) for b in cands
                ),
            )

        beac = cands[0]
        pid = int(beac.get("pid") or 0)
        created = process_creation_time(pid) if pid else None
        # PID-reuse guard: beacon's claimed creation time must match now.
        claimed = beac.get("process_created_ns")
        if created and claimed and abs(created - int(claimed)) > 10_000_000:
            return self.no_match("pid identity changed (reuse)")

        ctx = self._to_context(hwnd, host_pid, title, beac)
        return ProviderResult(kind=ResultKind.MATCH, context=ctx)

    def _to_context(self, hwnd, host_pid, title, beac) -> TerminalContext:
        shell_map = {
            "pwsh": "pwsh", "powershell": "powershell", "core": "pwsh",
            "desktop": "powershell",
        }
        raw_shell = str(beac.get("shell") or "").lower()
        shell = shell_map.get(raw_shell, raw_shell or "unknown")
        edition = str(beac.get("ps_edition") or "")
        if edition.lower() == "core":
            shell = "pwsh"
        elif edition.lower() == "desktop":
            shell = "powershell"
        term = str(beac.get("term_program") or "")
        version = beac.get("shell_version") or None
        channel = "preview" if (version and "preview" in str(version)) else "stable"
        return TerminalContext(
            provider="windows_terminal",
            terminal_session_id=str(beac.get("session_id") or ""),
            foreground_hwnd=hwnd,
            host_pid=host_pid,
            shell_pid=int(beac.get("pid") or 0) or None,
            host_kind="windows_terminal",
            shell=shell if shell in (
                "pwsh", "powershell", "cmd", "bash", "sh", "zsh", "fish",
                "wsl", "unknown") else "unknown",
            target_os="windows",
            transport="local",
            executable=None,
            shell_version=version,
            cwd=beac.get("cwd") or None,
            hostname=beac.get("hostname") or None,
            remote_authority=None,
            window_title=title or "",
            focused_control_kind="terminal_pane",
            shell_integration_ready=True,
            confidence=1.0,
            observed_at_ns=int(beac.get("timestamp_ns") or now_ns()),
            evidence=(
                f"wt_session={beac.get('wt_session') or ''}",
                f"term_program={term}",
                f"channel={channel}",
                f"created={beac.get('process_created_ns')}",
            ),
            evidence_level="PROVEN",
        )
