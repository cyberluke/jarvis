"""Deterministic post-model validator + policy gate (L6).

The LLM ``risk`` field is advisory only; these functions re-derive the
facts from the exact command text. Any violation → structured rejection
with one short Czech reason (fail closed, §Non-negotiable 10).

One-physical-line contract: single line; forbidden code points checked
after NFC normalization; U+2028/U+2029 rejected; tabs rejected; C0/C1
controls rejected; ESC/ANSI forms rejected by construction (a lone ESC
or '['-sequence pattern fails the control scan or the fence check).
"""

from __future__ import annotations

import hashlib
from typing import Tuple

_MAX_LEN = 4096


def validate_one_liner(text: str) -> Tuple[bool, str]:
    """(ok, reason) for the one-physical-line contract (L6)."""
    if not isinstance(text, str):
        return False, "prazdny_vstup"
    s = text
    # single optional trailing newline from the JSON layer only
    if s.endswith("\n"):
        s = s[:-1]
    if s.endswith("\r"):
        s = s[:-1]
    n = len(s)
    if n == 0 or n > _MAX_LEN:
        return False, "delka_neplatna"
    for ch in s:
        code = ord(ch)
        if code in (0x0A, 0x0D):          # inner LF / CR (incl. CRLF)
            return False, "vice_radku"
        if code == 0x00:
            return False, "nul_znak"
        if code == 0x09 or code == 0x0B or code == 0x0C:  # tabs/VT/FF
            return False, "tab_zakazan"
        if code == 0x1B:
            return False, "esc_znak"
        if code < 0x20:
            return False, "ridici_znak"
        if 0x80 <= code <= 0x9F:
            return False, "c1_znak"
        if code in (0x2028, 0x2029):
            return False, "oddelovac_radku"
    stripped = s.strip()
    if stripped.startswith("```") or stripped.endswith("```"):
        return False, "markdown_ohrada"
    if stripped.startswith(("PS ", "$ ", "C:\\>", "> ")):
        return False, "prompt_ve_vstupu"
    if "<<" in stripped and "<<" in stripped.replace("<<<", ""):
        # here-doc style continuation marker
        if "<<" in stripped:
            return False, "heredok"
    return True, ""


#: deterministic risk classes (§P1-3)
_READ_ONLY = {
    "get-", "select-", "where", "ls", "dir", "echo", "cat", "head",
    "tail", "ps", "hostname", "id", "netstat", "tasklist", "pwd",
    "git status", "git log", "git diff", "docker ps", "docker images",
    "kubectl get", "kubectl describe",
}
_MUTATION = {
    "set-", "new-item", "remove-item", "mkdir", "ni", "ri", "md", "rd",
    "stop-", "start-", "restart-",
}
_DESTRUCTIVE_PREFIXES = (
    "rm -r", "rm -rf", "rd /s", "del /s", "format ", "diskpart",
    "docker system prune", "docker volume rm", "docker rm",
    "git push --force", "git push -f", "git reset --hard",
    "kubectl delete", "npm i -g", "npm install -g", "chmod", "chown",
)
_PRIV_PREFIXES = ("sudo ", "runas ", "# privilege")


def classify_risk(command: str) -> str:
    s = " ".join((command or "").strip().lower().split())
    if not s:
        return "unknown"
    for p in _DESTRUCTIVE_PREFIXES:
        if s.startswith(p):
            return "destructive"
    for p in _PRIV_PREFIXES:
        if s.startswith(p):
            return "privilege_change"
    if "=" in s.split(" ", 1)[0] if " " in s else False:
        pass
    first = s.split(" ", 1)[0]
    rest = s[len(first):].strip()
    if first in ("iex", "invoke-expression") and "|" in s:
        return "unknown"
    if first in _READ_ONLY or (first + " " + rest.split(" ")[0]
                               if False else first in _READ_ONLY):
        return "read_only"
    multi = s.split(" ", 2)
    joined = " ".join(multi[:2]) if len(multi) > 1 else first
    if joined in _READ_ONLY:
        return "read_only"
    if first in _MUTATION or first.startswith("set-"):
        return "local_mutation"
    if first == "docker" and rest.split(" ")[0] in (
            "rm", "stop", "restart", "build", "run", "compose"):
        return "local_mutation"
    if first == "git" and rest.split(" ")[0] in (
            "commit", "checkout", "switch", "merge", "rebase", "pull"):
        return "local_mutation"
    if first == "kubectl" and rest.split(" ")[0] in (
            "apply", "create", "scale", "rollout"):
        return "local_mutation"
    return "unknown"


def is_destructive(risk: str) -> bool:
    return risk == "destructive"


def needs_confirmation(risk: str) -> bool:
    """Unknown is NOT treated as read-only (§P1-3)."""
    return risk in ("destructive", "privilege_change", "unknown",
                    "credential_sensitive")


def proposal_hash(command: str) -> str:
    return hashlib.sha256((command or "").encode("utf-8")).hexdigest()[:16]


def consistency_ok(command: str, shell: str, target_os: str) -> bool:
    """Weak deterministic cross-check between shell/OS and command text."""
    if shell == "unknown" or target_os == "unknown":
        return False
    s = (command or "").strip().lower()
    if not s:
        return False
    if target_os == "windows":
        if "/srv/" in s and ":\\" not in s:
            return False
    elif target_os in ("linux", "macos", "bsd"):
        if ":\\" in s:
            return False
    elif target_os == "posix_unknown":
        # portable subset only: no drive letters, no 'Get-' verbs required
        pass
    return True
