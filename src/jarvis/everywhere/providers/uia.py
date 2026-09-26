"""Generic Windows UI Automation provider.

Revalidates the minimal generic identity at apply time: same target process,
same HWND/control identity, same selected-text hash, still editable. A
password control is never read (``IsPassword``), and a control the UIA tree
dropped is ``TARGET_GONE``. No clipboard-extraction fallback: failure is the
answer with an explicit code.
"""

from __future__ import annotations

from typing import Dict, Tuple

PROVIDER_ID = "uia"


def revalidate(snapshot: Dict, current: Dict) -> Tuple[bool, str]:
    """Strict generic-control identity check (§Context transactions)."""
    if not isinstance(snapshot, dict) or not isinstance(current, dict):
        return False, "PROVIDER_UNAVAILABLE"

    if current.get("gone"):
        return False, "TARGET_GONE"

    if snapshot.get("process_id") is not None and \
            current.get("process_id") is not None and \
            int(snapshot["process_id"]) != int(current["process_id"]):
        return False, "TARGET_CHANGED"

    if snapshot.get("hwnd") is not None and current.get("hwnd") is not None \
            and int(snapshot["hwnd"]) != int(current["hwnd"]):
        return False, "TARGET_CHANGED"

    snap_tok = snapshot.get("provider_token") or {}
    cur_tok = current.get("provider_token") or {}
    if not isinstance(snap_tok, dict) or not isinstance(cur_tok, dict):
        return False, "PROVIDER_UNAVAILABLE"

    runtime_id = snap_tok.get("runtime_id")
    if runtime_id and str(cur_tok.get("runtime_id") or "") != str(runtime_id):
        return False, "TARGET_CHANGED"

    if str(cur_tok.get("selected_text_hash") or "") != str(
            snapshot.get("text_hash") or ""):
        return False, "SELECTION_CHANGED"

    if bool(snapshot.get("editable", False)) and not bool(
            current.get("editable", False)):
        return False, "READ_ONLY_TARGET"

    if cur_tok.get("is_password"):
        return False, "PASSWORD_FIELD"

    return True, ""
