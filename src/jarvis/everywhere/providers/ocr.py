"""OCR region provider (explicit Alt+drag capture).

An OCR region is a pixel observation, not an editable control: its replace
capability is ``COPY_ONLY`` and Insert is not offered. Revalidation checks
that the live OCR state still names the same backend and region; the image
buffer itself is discarded after recognition.
"""

from __future__ import annotations

from typing import Dict, Tuple

PROVIDER_ID = "ocr"

KNOWN_BACKENDS = ("windows-ai", "windows-media", "oneocr")


def revalidate(snapshot: Dict, current: Dict) -> Tuple[bool, str]:
    """Same backend, same physical rectangle; pixels never re-insert."""
    snap_tok = snapshot.get("provider_token") or {}
    cur_tok = (current or {}).get("provider_token") or {}
    if not isinstance(snap_tok, dict) or not isinstance(cur_tok, dict):
        return False, "PROVIDER_UNAVAILABLE"

    backend = str(snap_tok.get("ocr_backend") or "")
    if backend not in KNOWN_BACKENDS:
        return False, "OCR_BACKEND_UNAVAILABLE"
    if str(cur_tok.get("ocr_backend") or "") != backend:
        return False, "OCR_BACKEND_UNAVAILABLE"

    snap_rect = snap_tok.get("physical_rect")
    cur_rect = cur_tok.get("physical_rect")
    if snap_rect is not None and cur_rect is not None:
        if [float(v) for v in snap_rect] != [float(v) for v in cur_rect]:
            return False, "SELECTION_CHANGED"

    if str(cur_tok.get("recognized_text_hash") or "") != str(
            snapshot.get("text_hash") or ""):
        return False, "SELECTION_CHANGED"

    # Pixels cannot be written back into the source (§Screen Reading).
    if str(snapshot.get("replace_capability") or "COPY_ONLY") != "COPY_ONLY":
        return False, "REPLACE_UNSUPPORTED"

    return True, ""
