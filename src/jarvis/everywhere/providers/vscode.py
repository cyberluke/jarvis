"""VS Code editor provider (existing VSIX semantic integration).

The VSIX is the only writer for ``vscode-editor`` snapshots. Revalidation at
apply time compares the captured document identity: URI, version, and the
original range text hash. Any mismatch is an explicit typed failure; the
broker never falls back to UIA or the clipboard.
"""

from __future__ import annotations

from typing import Dict, Tuple

PROVIDER_ID = "vsix-vscode"


def revalidate(snapshot: Dict, current: Dict) -> Tuple[bool, str]:
    """Same document URI, same version (or unchanged range), same text."""
    snap_tok = snapshot.get("provider_token") or {}
    cur_tok = (current or {}).get("provider_token") or {}
    if not isinstance(snap_tok, dict) or not isinstance(cur_tok, dict):
        return False, "PROVIDER_UNAVAILABLE"

    uri = snap_tok.get("document_uri")
    if not uri or cur_tok.get("document_uri") != uri:
        return False, "TARGET_CHANGED"

    expected_version = snap_tok.get("document_version")
    live_version = cur_tok.get("document_version")
    if expected_version is not None and live_version is not None:
        if str(expected_version) != str(live_version):
            return False, "DOCUMENT_CHANGED"

    if str(cur_tok.get("range_text_hash") or "") != str(
            snapshot.get("text_hash") or ""):
        return False, "SELECTION_CHANGED"

    if str(cur_tok.get("remote_authority") or "") != str(
            snap_tok.get("remote_authority") or ""):
        return False, "REMOTE_CONTEXT_CHANGED"

    return True, ""
