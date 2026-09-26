"""WSL transport provider (K4 companion).

`wsl.exe` proves the transport, not the interactive shell. Resolution
order (no invented defaults — Bash is NOT assumed, §WSL):

  1. a fresh module beacon whose term_program marks WSL (a supported
     first-party shell integration inside the distro);
  2. the per-distribution shell map from configuration;
  3. the session-scoped user-confirmed shell (stored by the daemon under
     the exact terminal session id).

Otherwise NO_MATCH; the composer asks which shell is active and stores
the answer only for the current terminal session.
"""

from __future__ import annotations

from typing import Dict, Optional

from ..focus_guard import now_ns
from ..models import ProviderResult, ResultKind, TerminalContext
from .base import TerminalProvider


class WslProvider(TerminalProvider):
    host_kind = "unknown"

    def __init__(
        self,
        bridge,
        distro_shells: Optional[Dict[str, str]] = None,
        session_confirmed: Optional[Dict[str, str]] = None,
    ) -> None:
        self._bridge = bridge
        self._distro_shells = dict(distro_shells or {})
        self._session_confirmed = dict(session_confirmed or {})

    def resolve(
        self,
        hwnd: int,
        host_pid: int,
        window_class: str,
        executable: str,
        freshness_ns: int,
    ) -> ProviderResult:
        # 1) fresh WSL-marked beacon
        beac: Optional[dict] = None
        if self._bridge is not None:
            for b in self._bridge.latest_beacons().values():
                if "wsl" in str(b.get("term_program") or "").lower():
                    beac = b
                    break
        if beac is not None:
            shell = str(beac.get("shell") or "unknown").lower()
            if shell and shell != "unknown":
                return ProviderResult(
                    kind=ResultKind.MATCH,
                    context=self._context(hwnd, host_pid, shell, beac),
                )
        return self.no_match("wsl transport proven; shell not provable")

    def _context(self, hwnd, host_pid, shell, beac) -> TerminalContext:
        return TerminalContext(
            provider="wsl",
            terminal_session_id=str(beac.get("session_id") or ""),
            foreground_hwnd=hwnd,
            host_pid=host_pid,
            shell_pid=int(beac.get("pid") or 0) or None,
            host_kind="conhost",
            shell=shell if shell in (
                "pwsh", "powershell", "cmd", "bash", "sh", "zsh", "fish",
                "wsl") else "unknown",
            target_os="linux",
            transport="wsl",
            executable=None,
            shell_version=beac.get("shell_version") or None,
            cwd=beac.get("cwd") or None,
            hostname=beac.get("hostname") or None,
            window_title="",
            focused_control_kind="console",
            shell_integration_ready=True,
            confidence=1.0,
            observed_at_ns=int(beac.get("timestamp_ns") or now_ns()),
            # WSL is a Linux kernel by definition: target_os is proven.
            evidence=("transport=wsl", f"shell={shell}", "os=proven_linux"),
            remote_kind=None,
            remote_authority=None,
            evidence_level="PROVEN",
        )
