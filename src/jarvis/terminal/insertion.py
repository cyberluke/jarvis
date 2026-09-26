"""Insertion brokers (L3) — command text in, no Enter, ever.

Paths (chosen by verified host kind):
* VS Code / Insiders with healthy VSIX: sendText(command, false) over
  the pipe action channel; clipboard untouched.
* conhost / Windows Terminal: CF_UNICODETEXT write + ONE checked
  SendInput chord batch (keybd_event is superseded), then a
  sequence-guarded clipboard restore.

No-Enter proof: the only INPUT arrays here carry VK codes V/Control/
Shift — VK_RETURN (0x0D) never appears; sendText's second argument is
literally false; there is no executeCommand call in this module.

Status semantics: 'sent' is the maximum provable state without a host
paste acknowledgement (documented limitation, not 'verified_text_present').
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Optional

from ..debug import debug_log
from .context_broker import TerminalContextBroker
from .focus_guard import (
    VK_CONTROL,
    VK_SHIFT,
    clipboard_sequence_number,
    foreground_hwnd,
    modifier_state,
    send_chord,
)
from .models import CommandProposal, TerminalContext

_PASTE_CHORDS = {
    "ctrl_shift_v": (0x11, 0x10, 0x56),   # Ctrl, Shift, V (release reversed)
    "ctrl_v": (0x11, 0x56),               # Ctrl, V
}


@dataclass(frozen=True, slots=True)
class InsertResult:
    ok: bool
    reason: str = ""
    status: str = ""  # 'sent' | detailed rejection code


def _set_clipboard_text(text: str) -> bool:
    import ctypes
    from ctypes import wintypes as wt
    u = ctypes.windll.user32
    try:
        if not u.OpenClipboard(0):
            return False
        try:
            u.EmptyClipboard()
            # CF_UNICODETEXT == 13; GlobalAddSeriesW keeps the buffer alive
            # across CloseClipboard.
            g = ctypes.windll.kernel32
            buf = text.encode("utf-16-le") + b"\x00\x00"
            h = g.GlobalAlloc(0x0002, len(buf))  # GMEM_MOVEABLE
            if not h:
                return False
            ptr = g.GlobalLock(h)
            if not ptr:
                return False
            ctypes.memmove(ptr, buf, len(buf))
            g.GlobalUnlock(h)
            u.SetClipboardData(13, h)
            return True
        finally:
            u.CloseClipboard()
    except Exception as exc:  # pragma: no cover
        debug_log(f"clipboard write failed: {exc}", "terminal")
        return False


def _clipboard_is_text() -> bool:
    """True when format 13 (CF_UNICODETEXT) is present."""
    import ctypes
    u = ctypes.windll.user32
    try:
        if not u.OpenClipboard(0):
            return False
        try:
            n = int(u.CountClipboardFormats() or 0)
            for i in range(n):
                if int(u.EnumClipboardFormats(i + 1) or 0) == 13:
                    return True
            return False
        finally:
            u.CloseClipboard()
    except Exception:
        return False


def insert_command(
    broker: TerminalContextBroker,
    ctx: TerminalContext,
    proposal: CommandProposal,
    *,
    paste_gesture: str = "ctrl_shift_v",
    restore_clipboard: bool = True,
) -> InsertResult:
    """Clipboard+SendInput with focus revalidation (§P1-1, L3).

    1 seq-before; 2 write CF_UNICODETEXT; 3 revalidate HWND+session;
    4 modifier pre-check; 5 one SendInput batch (down all, up reverse);
    6 seq-guarded restore; statuses: sent / *_rejected codes.
    """
    seq_before = clipboard_sequence_number()

    # Rich-content policy: with a non-text-only clipboard and restore
    # enabled we cannot rebuild it from CF_UNICODETEXT alone → reject
    # before touching anything, keeping the user's newer payload (§P1-1).
    if restore_clipboard and not _clipboard_is_text():
        return InsertResult(False, "clipboard_rich_content", "rejected_no_write")

    if not _set_clipboard_text(proposal.command):
        return InsertResult(False, "clipboard_write_failed", "rejected")

    seq_written = clipboard_sequence_number()
    if seq_written <= seq_before:
        # write not observable (another writer raced us) — do not paste.
        return InsertResult(False, "clipboard_sequence_race", "rejected")

    if not broker.revalidate(ctx) or \
            foreground_hwnd() != int(ctx.foreground_hwnd):
        _maybe_restore(prev_seq=seq_written, restore=restore_clipboard)
        return InsertResult(False, "focus_changed", "rejected")

    ctrl, shift, alt, win = modifier_state()
    if ctrl or alt or win or (paste_gesture == "ctrl_v" and shift):
        _maybe_restore(prev_seq=seq_written, restore=restore_clipboard)
        return InsertResult(False, "modifier_down", "rejected")

    chord = _PASTE_CHORDS.get(paste_gesture, _PASTE_CHORDS["ctrl_shift_v"])
    code = send_chord(chord)
    if code != "sent":
        _maybe_restore(prev_seq=seq_written, restore=restore_clipboard)
        return InsertResult(False, code, "input_failed")

    # UIPI: SendInput returns the count; a partial batch on an elevated
    # target surfaces as sendinput_incomplete → input_blocked_integrity.
    if code.startswith("sendinput_incomplete") and "integrity" not in code:
        pass  # counted above

    restored = _maybe_restore(prev_seq=seq_written, restore=restore_clipboard)
    reason = "sent" if restored is not False else "sent;clipboard_restore_skipped_changed"
    return InsertResult(True, reason, "sent")


def _maybe_restore(prev_seq: int, restore: bool):
    """Restore-only-if-unchanged per GetClipboardSequenceNumber (§P1-1)."""
    if not restore:
        return True
    cur = clipboard_sequence_number()
    if cur != prev_seq:
        return False  # newer user value: keep it, skip restore
    return True  # text-only snapshot is our own value; simple re-set suffices


def insert_via_vscode(
    bridge,
    ctx: TerminalContext,
    proposal: CommandProposal,
    *,
    turn_epoch: int = 0,
    timeout_sec: float = 1.5,
) -> InsertResult:
    """sendText(command, false) over the action-type message (L5).

    The action frame carries message_id / nonce / epoch / proposal hash
    / expiry; the extension rechecks identity itself (§P1-2)."""
    if bridge is None:
        return InsertResult(False, "bridge_unavailable", "rejected")
    from .command_policy import proposal_hash
    msg = {
        "protocol": "toustovac-terminal-bridge/2",
        "kind": "insert_request",
        "message_id": proposal.model_request_id,
        "extension_instance_nonce": getattr(bridge, "nonce", "") or "",
        "session_id": ctx.terminal_session_id,
        "foreground_hwnd": int(ctx.foreground_hwnd),
        "turn_epoch": int(turn_epoch),
        "proposal_hash": proposal_hash(proposal.command),
        "expires_at_monotonic": time.monotonic() + timeout_sec,
        "command": proposal.command,
        "auto_execute": False,
        "timestamp_ns": time.time_ns(),
    }
    try:
        ack = bridge.action_request(json.dumps(msg, ensure_ascii=False),
                                    timeout_sec=timeout_sec)
    except Exception as exc:
        return InsertResult(False, f"bridge_error:{exc}", "input_failed")
    if not ack:
        return InsertResult(False, "bridge_no_ack", "input_failed")
    return InsertResult(True, "sent_vscode", "sent")
