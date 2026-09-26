"""Direct ssh.exe transport provider (K4 companion).

`ssh.exe` proves the transport only; a Linux hostname still does not
prove Bash (§Direct ssh.exe). Shell identity resolves from, in order:

  1. a fresh first-party remote beacon (term_program 'ssh'),
  2. the user-confirmed shell stored for this exact (host, session)
     pair (phrase mapping: "pro Bash" -> bash, etc.),
  3. otherwise NO_MATCH — the composer asks one short Czech question and
     stores the answer bound to this session only.
"""

from __future__ import annotations

from typing import Dict, Optional

from ..focus_guard import now_ns
from ..models import ProviderResult, ResultKind, TerminalContext
from .base import TerminalProvider

_CONFIRMED = {
    "bash": "bash", "sh": "sh", "zsh": "zsh", "fish": "fish",
    "pwsh": "pwsh", "powershell": "powershell", "cmd": "cmd",
}


class SshProvider(TerminalProvider):
    host_kind = "unknown"

    def __init__(
        self,
        bridge,
        host_shell_map: Optional[Dict[str, str]] = None,
        session_confirmed: Optional[Dict[str, str]] = None,
    ) -> None:
        self._bridge = bridge
        #: per-host SSH shell mapping from settings (e.g. {"opteron": "bash"})
        self._host_shell_map = dict(host_shell_map or {})
        #: session-scoped user confirmations (exact session id only)
        self._session_confirmed = dict(session_confirmed or {})

    def resolve(
        self,
        hwnd: int,
        host_pid: int,
        window_class: str,
        executable: str,
        freshness_ns: int,
    ) -> ProviderResult:
        beac: Optional[dict] = None
        if self._bridge is not None:
            for b in self._bridge.latest_beacons().values():
                if str(b.get("term_program") or "").lower() == "ssh":
                    beac = b
                    break

        if beac is not None:
            shell = str(beac.get("shell") or "").lower()
            if shell in _CONFIRMED:
                return ProviderResult(
                    kind=ResultKind.MATCH,
                    context=self._context(hwnd, host_pid, shell, beac),
                )

        # No beacon: only a user confirmation for THIS session counts.
        for sid, shell in self._session_confirmed.items():
            if shell in _CONFIRMED and sid:
                return ProviderResult(
                    kind=ResultKind.MATCH,
                    context=self._context(
                        hwnd, host_pid, _CONFIRMED[shell],
                        {"session_id": sid},
                    ),
                )
        return self.no_match("ssh transport proven; remote shell not provable")

    def _context(self, hwnd, host_pid, shell, beac) -> TerminalContext:
        # §P0-3: shell family narrows but does not prove the OS. A
        # beacon-reported target_os wins; otherwise POSIX shells map to
        # posix_unknown (portable subset), never 'linux' by assumption.
        reported = str(beac.get("target_os") or "").strip().lower()
        if reported in ("windows", "linux", "macos", "bsd"):
            target = reported
        elif shell in ("pwsh", "powershell", "cmd"):
            target = "windows"
        else:
            target = "posix_unknown"
        host = beac.get("hostname") or None
        return TerminalContext(
            provider="ssh",
            terminal_session_id=str(beac.get("session_id") or ""),
            foreground_hwnd=hwnd,
            host_pid=host_pid,
            shell_pid=int(beac.get("pid") or 0) or None,
            host_kind="conhost",
            shell=shell,
            target_os=target,
            transport="ssh",
            executable=None,
            shell_version=beac.get("shell_version") or None,
            cwd=beac.get("cwd") or None,
            hostname=host,
            window_title="",
            focused_control_kind="console",
            shell_integration_ready=True,
            confidence=1.0 if reported else 0.8,
            observed_at_ns=int(beac.get("timestamp_ns") or now_ns()),
            evidence=("transport=ssh", f"shell={shell}",
                      f"os={'proven' if reported else 'posix_subset'}"),
            remote_kind="ssh",
            remote_authority=f"ssh+{host}" if host else None,
            evidence_level="PROVEN" if reported else "PARTIAL",
        )
