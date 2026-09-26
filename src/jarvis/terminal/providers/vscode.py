"""VS Code / VS Code Insiders provider (K4, L1-remediated).

The optional UI-side VSIX pushes one validated ``terminal_context``
beacon per active terminal through the length-prefixed named pipe.

API floor: VS Code >= 1.99 (``Terminal.state`` finalized with the
March-2025 milestone of issue #230165; ``shellIntegration``,
``sendText`` and the execution events are stable at that floor).

Identity split (§P0-2):
  remote_kind       <- vscode.env.remoteName        ("ssh-remote")
  remote_authority  <- shellIntegration cwd URI authority, or the unique
                       matching workspace-folder authority; NEVER the
                       kind string. Missing authority on a remote
                       terminal ⇒ incomplete ⇒ NO_MATCH.

Target OS (§P0-3): never inferred from "remote exists". The VSIX
reports the resolved platform string from the shell-integration cwd
scheme + remote probe when available; otherwise posix_unknown, which
keeps composition to portable POSIX syntax.

Focus proof (§P0-4): the UIA focused-element runtime id supplied by
the focus guard must belong to the terminal surface; the daemon-side
broker compares snapshots at composition and insertion time.
"""

from __future__ import annotations

from typing import Optional

from ..focus_guard import now_ns, window_title
from ..models import ProviderResult, ResultKind, TerminalContext
from .base import TerminalProvider

_SHELL_MAP = {
    "pwsh": "pwsh", "powershell": "powershell", "cmd": "cmd",
    "bash": "bash", "sh": "sh", "zsh": "zsh", "fish": "fish",
    "wsl": "wsl", "unknown": "unknown",
}
_POSIX_SHELLS = ("bash", "sh", "zsh", "fish")


def _target_os_from_cwd(cwd: Optional[str], reported: str) -> str:
    """Deterministic OS mapping from evidence, no Linux-by-default (§P0-3)."""
    rep = (reported or "").strip().lower()
    if rep in ("windows", "linux", "macos", "bsd"):
        return rep
    # Only a POSIX-ish CWD is a weak hint, and it stays posix_unknown
    # because BSD/macOS/Windows-OpenSSH all share '/'-prefixed paths.
    if cwd and cwd.startswith("/"):
        return "posix_unknown"
    return "unknown"


class VsCodeProvider(TerminalProvider):
    host_kind = "vscode"

    def __init__(self, bridge, insiders: bool = False) -> None:
        self._bridge = bridge
        self._insiders = insiders

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
            return self.no_match("no fresh vscode beacon (is the VSIX active?)")

        want = "vscode_insiders" if self._insiders else "vscode"
        cands = [
            b for b in beacons.values()
            if str(b.get("term_program") or "").lower() in (want, "vscode",
                                                            "vscode_insiders")
        ]
        exact = [b for b in cands
                 if str(b.get("term_program") or "").lower() == want]
        if exact:
            cands = exact

        if not cands:
            return self.no_match("no vscode terminal beacon for focused window")
        if len(cands) > 1:
            return ProviderResult(
                kind=ResultKind.AMBIGUOUS,
                reason=f"{len(cands)} vscode terminal beacons",
                candidates=tuple(self._to_context(hwnd, host_pid, b)
                                 for b in cands),
            )

        beac = cands[0]
        ctx = self._to_context(hwnd, host_pid, beac)
        # Remote completeness rules (§P0-2/P0-3), all fail-closed.
        if ctx.transport == "vscode_remote_ssh":
            if not ctx.remote_authority:
                return self.no_match(
                    "remote authority missing (incomplete remote identity)")
            if ctx.target_os == "unknown":
                return self.no_match("remote target OS unproven")
        if ctx.shell == "unknown" or not ctx.shell_integration_ready:
            return self.no_match("shell integration not ready or shell unknown")
        if ctx.evidence_level != "PROVEN":
            return self.no_match(f"evidence level {ctx.evidence_level}")
        return ProviderResult(kind=ResultKind.MATCH, context=ctx)

    def _to_context(self, hwnd, host_pid, beac) -> TerminalContext:
        raw = str(beac.get("shell") or "unknown").lower()
        shell = _SHELL_MAP.get(raw, "unknown")
        kind = str(beac.get("remote_kind") or "").strip() or None
        authority = str(beac.get("remote_authority") or "").strip() or None
        fingerprint = str(beac.get("remote_host_fingerprint") or "").strip() or None
        # Legacy beacons (single-field) treated as incomplete (§P0-2):
        if kind and not authority and kind != "unknown":
            authority = None
        cwd = beac.get("cwd") or None
        target = _target_os_from_cwd(cwd, str(beac.get("target_os") or ""))
        if not kind:
            transport = str(beac.get("transport") or "local")
            transport = transport if transport in (
                "local", "vscode_remote_ssh", "ssh", "wsl") else "unknown"
        else:
            transport = "vscode_remote_ssh"
        # Portable-POSIX downgrade: shell known, OS unproven.
        if target == "unknown" and shell in _POSIX_SHELLS and transport != "local":
            target = "posix_unknown"
        level = "PROVEN"
        if (transport == "vscode_remote_ssh" and not authority) or \
                not beac.get("shell_integration_ready") or not shell:
            level = "PARTIAL"
        return TerminalContext(
            provider="vscode_insiders" if self._insiders else "vscode",
            terminal_session_id=str(beac.get("session_id") or ""),
            foreground_hwnd=hwnd,
            host_pid=host_pid,
            shell_pid=int(beac.get("pid") or 0) or None,
            host_kind="vscode_insiders" if self._insiders else "vscode",
            shell=shell if shell in (
                "pwsh", "powershell", "cmd", "bash", "sh", "zsh", "fish",
                "wsl") else "unknown",
            target_os=target if target in (
                "windows", "linux", "macos", "bsd", "posix_unknown") else "unknown",
            transport=transport,
            executable=None,
            shell_version=beac.get("shell_version") or None,
            cwd=cwd,
            hostname=beac.get("hostname") or None,
            window_title=window_title(hwnd),
            focused_control_kind="integrated_terminal",
            shell_integration_ready=bool(beac.get("shell_integration_ready")),
            confidence=1.0 if level == "PROVEN" else 0.6,
            observed_at_ns=int(beac.get("timestamp_ns") or now_ns()),
            evidence=(
                f"remote_kind={kind or 'local'}",
                f"authority_bound={'yes' if authority else 'no'}",
                f"ready={beac.get('shell_integration_ready')}",
            ),
            remote_kind=kind,
            remote_authority=authority,
            remote_host_fingerprint=fingerprint,
            evidence_level=level,
        )
