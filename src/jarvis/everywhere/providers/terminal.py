"""Terminal provider (existing terminal composer / PowerShell bridge).

Covers ``vscode-terminal``, ``powershell`` and ``windows-terminal`` snapshots.
Revalidation keeps the existing shell-aware identity: session id, shell kind,
local/remote context, and the prompt generation / line revision. A Windows
command must never land in a remote Linux shell because focus moved, so the
remote identity is part of the strict comparison.
"""

from __future__ import annotations

from typing import Dict, Tuple

PROVIDER_ID = "terminal-bridge"


def revalidate(snapshot: Dict, current: Dict) -> Tuple[bool, str]:
    """Same session, shell, remote identity and prompt/line revision."""
    snap_tok = snapshot.get("provider_token") or {}
    cur_tok = (current or {}).get("provider_token") or {}
    if not isinstance(snap_tok, dict) or not isinstance(cur_tok, dict):
        return False, "PROVIDER_UNAVAILABLE"

    session = snap_tok.get("terminal_session_id") or snap_tok.get("session_id")
    if not session or str(cur_tok.get("terminal_session_id")
                          or cur_tok.get("session_id") or "") != str(session):
        return False, "TARGET_CHANGED"

    shell = snap_tok.get("shell")
    if shell and str(cur_tok.get("shell") or "") != str(shell):
        return False, "TARGET_CHANGED"

    if str(cur_tok.get("remote_kind") or "") != str(
            snap_tok.get("remote_kind") or ""):
        return False, "REMOTE_CONTEXT_CHANGED"
    if str(cur_tok.get("remote_authority") or "") != str(
            snap_tok.get("remote_authority") or ""):
        return False, "REMOTE_CONTEXT_CHANGED"

    for key in ("prompt_generation", "command_line_revision"):
        expected = snap_tok.get(key)
        live = cur_tok.get(key)
        if expected is not None and live is not None \
                and str(expected) != str(live):
            return False, "SELECTION_CHANGED"

    return True, ""
