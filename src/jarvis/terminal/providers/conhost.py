"""Classic console (conhost) provider — standalone PowerShell support (K2).

The foreground HWND belongs to the console window; its PID is normally
conhost.exe (or the in-probe pseudoconsole). The real shell is found by
walking the process tree once (Toolhelp snapshot) to the unique child
that is a recognized shell executable. PowerShell 7 Preview is detected
by path evidence ("7-preview" segment) plus file-version metadata —
never by a hardcoded path (§Standalone PowerShell).

No window-title guessing: titles are user-controlled. If the tree is
not a unique, provable shell chain, fail closed with NO_MATCH.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import Structure, wintypes
from typing import Dict, List, Optional, Tuple

from ..focus_guard import (
    now_ns,
    process_creation_time,
    process_image_name,
    window_title,
)
from ..models import ProviderResult, ResultKind, TerminalContext
from .base import TerminalProvider

# Toolhelp snapshot constants.
TH32CS_SNAPPROCESS = 0x00000002


class _PROCESSENTRY32W(Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def _snapshot_processes() -> Dict[int, Tuple[int, str]]:
    """pid → (parent_pid, exe_name_lower) via one Toolhelp snapshot."""
    k32 = ctypes.windll.kernel32
    out: Dict[int, Tuple[int, str]] = {}
    entry = _PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap in (0, -1, ctypes.c_void_p(-1).value):
        return out
    try:
        if not k32.Process32FirstW(snap, ctypes.byref(entry)):
            return out
        while True:
            pid = int(entry.th32ProcessID)
            ppid = int(entry.th32ParentProcessID)
            name = (entry.szExeFile or "").lower()
            out[pid] = (ppid, name)
            if not k32.Process32NextW(snap, ctypes.byref(entry)):
                break
    finally:
        k32.CloseHandle(snap)
    return out


_SHELL_NAMES = {
    "pwsh.exe": "pwsh",
    "powershell.exe": "powershell",
    "cmd.exe": "cmd",
    "bash.exe": "bash",
    "sh.exe": "sh",
    "zsh.exe": "zsh",
    "fish.exe": "fish",
    "wsl.exe": "wsl",
    "ssh.exe": "ssh",
}


def _descendant_shell(
    root_pid: int, table: Dict[int, Tuple[int, str]], max_depth: int = 6
) -> List[Tuple[int, str, str]]:
    """All (pid, shell_family, exe) shells in the subtree of root_pid."""
    children: Dict[int, List[int]] = {}
    for pid, (ppid, _name) in table.items():
        children.setdefault(ppid, []).append(pid)
    found: List[Tuple[int, str, str]] = []
    stack: List[Tuple[int, int]] = [(root_pid, 0)]
    while stack:
        pid, depth = stack.pop()
        if depth > max_depth:
            continue
        for child in children.get(pid, ()):
            _ppid, name = table.get(child, (0, ""))
            if name in _SHELL_NAMES:
                found.append((child, _SHELL_NAMES[name], process_image_name(child)))
            stack.append((child, depth + 1))
    return found


def _normalize_pwsh(exe_path: str, version: Optional[str]) -> Tuple[str, str]:
    """(shell, channel) — PowerShell 7 and 7-preview share family 'pwsh'.

    Channel evidence is the executable path (…\\PowerShell\\7-preview\\) or
    a '-' prerelease suffix in the reported version. Nothing hardcoded:
    both markers are read from the observed process image (spec §Standalone).
    """
    lower = (exe_path or "").lower()
    text = f"{lower} {version or ''}"
    channel = "preview" if "preview" in text else "stable"
    return "pwsh", channel


class ConHostProvider(TerminalProvider):
    """Foreground classic console: resolve the unique shell descendant."""

    host_kind = "conhost"

    def resolve(
        self,
        hwnd: int,
        host_pid: int,
        window_class: str,
        executable: str,
        freshness_ns: int,
    ) -> ProviderResult:
        table = _snapshot_processes()
        if not table or host_pid <= 0:
            return self.unavailable("process snapshot empty")

        shells = _descendant_shell(host_pid, table)
        # A console whose foreground PID *is* the shell (Win-Terminal-less
        # pwsh self-host / GetConsoleProcessList-style layout) still counts.
        host_name = (table.get(host_pid, (0, ""))[1] or "").lower()
        if host_name in _SHELL_NAMES:
            shells.insert(0, (host_pid, _SHELL_NAMES[host_name], executable))

        # Drop wsl.exe/ssh.exe hop entries: they prove transport, not shell.
        named = [s for s in shells if s[1] not in ("wsl", "ssh")]
        if not named:
            # Only a transport hop is visible (e.g. wsl.exe with a hidden
            # child shell) — the shell identity is unproven. Fail closed.
            if shells:
                hop = shells[0]
                return self.no_match(
                    f"transport hop only ({hop[1]}); shell not provable"
                )
            return self.no_match("no shell descendant of console host")

        uniq = {s[1] for s in named}
        if len(named) > 1 and len(uniq) > 1:
            return self.no_match("multiple different shells under console")
        shell_pid, shell, exe = named[0]

        version: Optional[str] = None
        # File-version metadata comes from the observed exe path only.
        ps_version = _pwsh_version_from_path(exe)
        if ps_version:
            version = ps_version
        shell, channel = (
            _normalize_pwsh(exe, version) if shell == "pwsh" else (shell, "stable")
        )

        created = process_creation_time(shell_pid)
        session_id = f"conhost:{shell_pid}:{created or 0}"

        ctx = TerminalContext(
            provider="conhost",
            terminal_session_id=session_id,
            foreground_hwnd=hwnd,
            host_pid=host_pid,
            shell_pid=shell_pid,
            host_kind="conhost",
            shell=shell,
            target_os="windows",
            transport="local",
            executable=exe or None,
            shell_version=version,
            cwd=None,
            hostname=None,
            remote_authority=None,
            window_title=window_title(hwnd),
            focused_control_kind="console",
            shell_integration_ready=False,
            confidence=1.0,
            observed_at_ns=now_ns(),
            evidence=(
                f"class={window_class}",
                f"host_pid={host_pid}",
                f"shell_pid={shell_pid}",
                f"exe={exe}",
                f"channel={channel}",
                f"created={created}",
            ),
            evidence_level="PROVEN",
        )
        return ProviderResult(kind=ResultKind.MATCH, context=ctx)


def _pwsh_version_from_path(exe_path: str) -> Optional[str]:
    """Best-effort version/channel metadata from the observed exe path.

    Uses the PowerShell-style folder segment (7 / 7-preview); a full file
    version is taken from the exe's version resource when readable.
    """
    if not exe_path or not os.path.isfile(exe_path):
        # Path may be unavailable for non-local processes.
        return None
    try:
        import ctypes as _c

        size = _c.GetFileVersionInfoSizeW(exe_path, None)
        if not size:
            return None
        buf = _c.create_string_buffer(size)
        if not _c.GetFileVersionInfoW(exe_path, 0, size, buf):
            return None
        val = _c.c_void_p()
        length = _c.c_uint()
        if _c.VerQueryValueW(buf, "1", _c.byref(val), _c.byref(length)) and val.value:
            return _c.wstring_at(val.value, max(int(length.value) - 1, 1))
    except Exception:
        pass
    return None
