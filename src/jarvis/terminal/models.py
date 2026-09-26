"""Normalized, immutable data models for the Terminal Command Composer.

Only these structures cross the core pipeline (broker → composer →
policy gate → insertion → observer → memory). Loose dicts stay inside
individual adapters and are converted at the adapter boundary.

Canonical value sets (L1 remediation):

  host_kind:   windows_terminal | conhost | vscode | vscode_insiders | unknown
  shell:       pwsh | powershell | cmd | bash | sh | zsh | fish | wsl | unknown
  target_os:   windows | linux | macos | bsd | posix_unknown | unknown
  transport:   local | vscode_remote_ssh | ssh | wsl | unknown
  evidence:    PROVEN | PARTIAL | AMBIGUOUS | STALE | UNAVAILABLE

Remote identity is split (§P0-2): remote_kind (e.g. "ssh-remote") is
NOT unique; remote_authority (e.g. "ssh-remote+opteron") is the unique
target. Missing authority on a remote terminal = incomplete context.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

HOST_KINDS = ("windows_terminal", "conhost", "vscode", "vscode_insiders", "unknown")
SHELLS = ("pwsh", "powershell", "cmd", "bash", "sh", "zsh", "fish", "wsl", "unknown")
TARGET_OSES = ("windows", "linux", "macos", "bsd", "posix_unknown", "unknown")
TRANSPORTS = ("local", "vscode_remote_ssh", "ssh", "wsl", "unknown")
EVIDENCE_LEVELS = ("PROVEN", "PARTIAL", "AMBIGUOUS", "STALE", "UNAVAILABLE")


class ResultKind(Enum):
    """Provider resolution outcomes (§Resolution contract)."""

    MATCH = "match"
    NO_MATCH = "no_match"
    AMBIGUOUS = "ambiguous"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class TerminalContext:
    """One evidence-complete view of the verified terminal at one instant."""

    provider: str
    terminal_session_id: str
    foreground_hwnd: int
    host_pid: int
    shell_pid: Optional[int]
    host_kind: str
    shell: str
    target_os: str
    transport: str
    executable: Optional[str]
    shell_version: Optional[str]
    cwd: Optional[str]
    hostname: Optional[str]
    window_title: str
    focused_control_kind: str
    shell_integration_ready: bool
    confidence: float
    observed_at_ns: int
    evidence: tuple[str, ...]
    # L1 additions — defaults keep positional construction valid.
    remote_kind: Optional[str] = None
    remote_authority: Optional[str] = None
    remote_host_fingerprint: Optional[str] = None
    evidence_level: str = "PROVEN"


@dataclass(frozen=True, slots=True)
class TerminalFocusProof:
    """Focus evidence bound to one instant, checked at compose AND insert (§P0-4)."""

    foreground_hwnd: int
    foreground_pid: int
    focused_runtime_id: tuple
    focused_control_kind: str
    focused_class_name: str
    provider: str
    terminal_session_id: str
    observed_at_ns: int
    verdict: str  # one of EVIDENCE_LEVELS


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """Uniform adapter return: exactly one ResultKind plus payload."""

    kind: ResultKind
    context: Optional[TerminalContext] = None
    reason: str = ""
    candidates: tuple[TerminalContext, ...] = ()


@dataclass(frozen=True, slots=True)
class CommandProposal:
    """Validated, schema-constrained output of the composer."""

    command: str
    shell: str
    target_os: str
    risk: str
    destructive: bool
    uses_memory_keys: tuple[str, ...]
    missing_inputs: tuple[str, ...]
    model_request_id: str


@dataclass(frozen=True, slots=True)
class TerminalExecutionRecord:
    """What the user actually executed (may differ from the proposal)."""

    terminal_session_id: str
    proposed_command: Optional[str]
    executed_command: str
    exit_code: Optional[int]
    cwd_before: Optional[str]
    cwd_after: Optional[str]
    output_tail: str
    started_at_ns: int
    ended_at_ns: Optional[int]
    command_confidence: str
    # M0.4 semantics: native -> exit_code authoritative; powershell ->
    # captured success flag authoritative (exit_code None); unknown ->
    # never promoted.
    execution_kind: str = "unknown"
    success: Optional[bool] = None


@dataclass(frozen=True, slots=True)
class EntityRecord:
    """Typed terminal entity with provenance (§TerminalSessionMemory)."""

    key: str
    value: str
    source: str            # terminal_output | shell_integration | user
    command_id: str
    confidence: float
    observed_at_ns: int
