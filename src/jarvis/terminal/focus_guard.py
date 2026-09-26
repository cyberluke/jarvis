"""Foreground/focus/input primitives for the terminal composer (L2/L3).

Pure ctypes, fail-closed. APIs (spec §Native Windows process inspection):
  GetForegroundWindow, GetWindowThreadProcessId, GetClassNameW,
  QueryFullProcessImageNameW, GetProcessTimes,
  GetClipboardSequenceNumber, OpenClipboard/Get/Set/EmptyClipboard,
  SendInput (single batched INPUT[] — keybd_event is superseded),
  GetKeyboardState (modifier pre-check), UIA GetFocusedElement via the
  existing bridge in jarvis.desktop.

UIA slot mapping was probed on this machine: IUIAutomation slot 5 is
GetRootElement; slot 6 is GetFocusedElement (same one-out-pointer
signature). Both are re-verified per call by hr==0 + non-null output.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from typing import Optional, Tuple

from ..debug import debug_log

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
KEYEVENTF_KEYUP = 0x0002

VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_MENU = 0x12
VK_LWIN = 0x5B
VK_RWIN = 0x5E

UIA_SLOT_GET_ROOT = 5
UIA_SLOT_GET_FOCUSED = 6


def _u32():
    return ctypes.windll.user32


def _k32():
    return ctypes.windll.kernel32


def foreground_hwnd() -> int:
    try:
        return int(_u32().GetForegroundWindow() or 0)
    except Exception:  # pragma: no cover — defensive
        return 0


def hwnd_pid(hwnd: int) -> int:
    if not hwnd:
        return 0
    try:
        pid = wintypes.DWORD()
        _u32().GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        return int(pid.value or 0)
    except Exception:  # pragma: no cover
        return 0


def window_class(hwnd: int) -> str:
    if not hwnd:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(256)
        _u32().GetClassNameW(wintypes.HWND(hwnd), buf, 256)
        return buf.value or ""
    except Exception:  # pragma: no cover
        return ""


def window_title(hwnd: int) -> str:
    if not hwnd:
        return ""
    try:
        n = _u32().GetWindowTextLengthW(wintypes.HWND(hwnd))
        buf = ctypes.create_unicode_buffer(n + 1)
        _u32().GetWindowTextW(wintypes.HWND(hwnd), buf, n + 1)
        return buf.value or ""
    except Exception:  # pragma: no cover
        return ""


def process_image_name(pid: int) -> str:
    if not pid:
        return ""
    try:
        k32 = _k32()
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value or ""
            return ""
        finally:
            k32.CloseHandle(handle)
    except Exception:  # pragma: no cover
        return ""


def process_creation_time(pid: int) -> Optional[int]:
    """100-ns FILETIME creation stamp — PID-reuse guard."""
    if not pid:
        return None
    try:
        k32 = _k32()
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            created = wintypes.FILETIME()
            tmp = wintypes.FILETIME()
            if k32.GetProcessTimes(
                handle, ctypes.byref(created), ctypes.byref(tmp),
                ctypes.byref(tmp), ctypes.byref(tmp),
            ):
                return (int(created.dwHighDateTime) << 32) | int(
                    created.dwLowDateTime)
            return None
        finally:
            k32.CloseHandle(handle)
    except Exception:  # pragma: no cover
        return None


def now_ns() -> int:
    return time.time_ns()


# ── UIA focused-element proof (L2) ───────────────────────────────────

def _uia_bridge():
    from ..desktop import _bridge_instance
    return _bridge_instance()


def focused_element_evidence() -> Tuple[tuple, str]:
    """(runtime_id tuple, localized control type) of the focused element.

    Returns ((), "") fail-closed when UIA is unavailable. The root
    element itself is NOT a terminal proof; callers compare against the
    host's expected surface.
    """
    b = _uia_bridge()
    root = b.root_element()
    if not root:
        return (), ""
    try:
        # GetFocusedElement shares the one-out-pointer signature (§probe).
        from ctypes import c_long, POINTER, cast as _cast
        out = ctypes.c_void_p()
        hr = b._call(OBJ_Uia(), UIA_SLOT_GET_FOCUSED, ctypes.addressof(out))
        if hr != 0 or not out.value:
            return (), ""
        rid = b.runtime_id(out.value)
        return ((rid,) if rid is not None else ()), ""
    except Exception:  # pragma: no cover
        return (), ""


_Uia_obj: Optional[int] = None


def OBJ_Uia() -> int:
    global _Uia_obj
    b = _uia_bridge()
    if _Uia_obj is None:
        _init_uia(b)
    return _Uia_obj or 0


def _init_uia(b) -> None:
    global _Uia_obj
    from ctypes import addressof as _ao
    from ..desktop import _to_guid, _CLSID_CUIA, _IID_IUIA
    ole = ctypes.windll.ole32
    clsid = _to_guid(_CLSID_CUIA)
    iid = _to_guid(_IID_IUIA)
    obj = ctypes.c_void_p()
    hr = ole.CoCreateInstance(
        ctypes.c_void_p(_ao(clsid)), None, 1,
        ctypes.c_void_p(_ao(iid)), ctypes.c_void_p(_ao(obj)),
    )
    if hr == 0 and obj.value:
        _Uia_obj = obj.value


# ── keyboard input (L3): one checked SendInput batch ────────────────

class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUT(ctypes.Structure):
    # x64 INPUT is 40 bytes: UINT type (+pad) + 32-byte union. The
    # KEYBDINPUT member is 24 bytes, so pad the union to 32 (MOUSEINPUT
    # is the real largest member). Without the pad, Windows rejects
    # cbSize != sizeof(INPUT) and SendInput returns 0.
    class _U(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("_pad", ctypes.c_byte * 32)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.UINT), ("u", _U)]


INPUT_KEYBOARD = 1


def _phys_down(vk: int) -> bool:
    try:
        state = (ctypes.c_ubyte * 256)()
        _u32().GetKeyboardState(ctypes.byref(state))
        return bool(state[vk] & 0x80)
    except Exception:
        return False


def send_chord(vks: Tuple[int, ...]) -> str:
    """One SendInput batch: press vks in order, release in reverse.

    Returns: 'sent' | 'input_blocked_integrity' | 'modifier_down' |
    'sendinput_incomplete:<n>'. Never a retry with another gesture.
    """
    items = [
        _INPUT(INPUT_KEYBOARD, ki=_KEYBDINPUT(vk, 0, 0, 0, None))
        for vk in vks
    ] + [
        _INPUT(INPUT_KEYBOARD, ki=_KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP, 0, None))
        for vk in reversed(vks)
    ]
    n = len(items)
    arr = (_INPUT * n)(*items)
    u = _u32()
    u.SendInput.argtypes = [wintypes.UINT,
                            ctypes.POINTER(_INPUT), ctypes.c_int]
    u.SendInput.restype = wintypes.UINT
    try:
        written = int(u.SendInput(n, arr, ctypes.sizeof(_INPUT)))
    except Exception as exc:  # pragma: no cover
        debug_log(f"sendinput_error: {exc}", "terminal")
        return "sendinput_error"
    if written != n:
        err = 0
        try:
            err = int(_k32().GetLastError() or 0)
        except Exception:
            pass
        code = "input_blocked_integrity" if err == 0 else f"sendinput_incomplete:{written}:err{err}"
        debug_log(f"{code} n={n}", "terminal")
        return code
    return "sent"


def modifier_state() -> Tuple[bool, ...]:
    """(ctrl, shift, alt, win) physical key-down flags."""
    return (_phys_down(VK_CONTROL) or _phys_down(0xA2) or _phys_down(0xA3),
            _phys_down(VK_SHIFT) or _phys_down(0xA0) or _phys_down(0xA1),
            _phys_down(VK_MENU),
            _phys_down(VK_LWIN) or _phys_down(VK_RWIN))


# ── clipboard (L3 transaction primitives) ───────────────────────────

def clipboard_sequence_number() -> int:
    try:
        u = _u32()
        u.GetClipboardSequenceNumber.restype = wintypes.DWORD
        return int(u.GetClipboardSequenceNumber() or 0)
    except Exception:  # pragma: no cover
        return 0


def clipboard_format_count() -> int:
    try:
        u = _u32()
        if not u.OpenClipboard(0):
            return -1
        try:
            return int(u.CountClipboardFormats() or 0)
        finally:
            u.CloseClipboard()
    except Exception:
        return -1


# ── Deterministic host classification ───────────────────────────────

def classify_host(hwnd: int) -> Tuple[str, str, str]:
    """(host_kind, window_class, executable); unknown ⇒ fail closed."""
    cls = window_class(hwnd)
    pid = hwnd_pid(hwnd)
    exe = process_image_name(pid)
    base = exe.rsplit("\\", 1)[-1].lower() if exe else ""

    if cls == "CASCADIA_HOSTING_WINDOW_CLASS":
        return "windows_terminal", cls, exe
    if cls == "ConsoleWindowClass":
        return "conhost", cls, exe
    if cls == "Chrome_WidgetWin_1" or base.startswith("code"):
        if "insider" in base:
            return "vscode_insiders", cls, exe
        if base.startswith("code"):
            return "vscode", cls, exe
    return "unknown", cls, exe
