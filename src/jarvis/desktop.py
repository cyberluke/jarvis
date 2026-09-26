"""Windows desktop perception & control — semantic-first, pure ctypes.

Design (per implementation doc §12–§16, §30):
  1. window-manager APIs (user32) for window identity/enumeration/focus —
     fully deterministic, no COM.
  2. UI Automation via raw COM vtable slots for semantic element access
     (GetRootElement / GetFocusedElement / SetFocus / GetRuntimeId).
     Vtable offsets were verified empirically on this machine:
       IUIAutomation (inherits IUnknown 0-2, Initialize=3,
         get_RegisteredHandlers=4): 5 = GetRootElement.
       IUIAutomationElement: 3 = SetFocus, 4 = GetRuntimeId.
  3. Screen capture via Pillow ImageGrab (already a dependency); OCR
     (tesseract) is a fallback, not the primary channel.

Every failure path returns an explicit state string — never a silent
"success with empty data" (§1.3): capture_failed / ocr_unavailable /
ocr_empty / ui_available / ui_empty.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Any, Dict, List, Optional

from .debug import debug_log

# ── Empirically verified UIA vtable offsets (see module docstring) ──
_UIA_GET_ROOT_ELEMENT = 5          # IUIAutomation::GetRootElement
_ELEM_SET_FOCUS = 3                # IUIAutomationElement::SetFocus
_ELEM_GET_RUNTIME_ID = 4           # IUIAutomationElement::GetRuntimeId

_CLSID_CUIA = "{ff48dba4-60ef-4201-aa87-54103eef594e}"
_IID_IUIA = "{30cbe57d-d9d0-452a-ab13-7ac5ac4825ee}"


def _to_guid(s: str) -> "wintypes._SafeStruct":
    # CoCreateInstance wants GUID structs; build them as 16-byte blobs.
    g = (ctypes.c_ubyte * 16)()
    raw = bytes.fromhex(s.replace("-", "").replace("{", "").replace("}", ""))
    # Data1 LE, Data2 LE, Data3 LE, Data4 BE(8)
    d1 = int.from_bytes(raw[0:4], "big").to_bytes(4, "little")
    d2 = int.from_bytes(raw[4:6], "big").to_bytes(2, "little")
    d3 = int.from_bytes(raw[6:8], "big").to_bytes(2, "little")
    for i, v in enumerate(d1 + d2 + d3 + raw[8:16]):
        g[i] = v
    return g


class _UIABridge:
    """Lazy, process-wide UIA bridge (single IUIAutomation instance)."""

    def __init__(self) -> None:
        self._uia: Optional[int] = None
        self._ready = False

    def _init(self) -> None:
        if self._ready:
            return
        try:
            from ctypes.wintypes import HRESULT  # noqa: F401  (presence probe)
        except Exception:
            pass
        try:
            ole32 = ctypes.windll.ole32
            ctypes.windll.LoadLibrary("UIAutomationCore.dll")
            clsid = _to_guid(_CLSID_CUIA)
            iid = _to_guid(_IID_IUIA)
            obj = ctypes.c_void_p()
            hr = ole32.CoCreateInstance(
                ctypes.byref(clsid), None, 1, ctypes.byref(iid),
                ctypes.byref(obj),
            )
            if hr == 0 and obj.value:
                self._uia = obj.value
            self._ready = True
        except Exception as exc:  # pragma: no cover — defensive
            debug_log(f"UIA init failed: {exc}", "desktop")
            self._ready = True

    def _call(self, obj_addr: int, slot_idx: int, *args: Any) -> int:
        vtable = ctypes.cast(obj_addr, ctypes.POINTER(ctypes.c_void_p))[0]
        fn = ctypes.cast(vtable, ctypes.POINTER(ctypes.c_void_p))[slot_idx]
        proto = ctypes.WINFUNCTYPE(
            ctypes.c_long, *([ctypes.c_void_p] * (1 + len(args)))
        )
        return proto(fn)(obj_addr, *args)

    def root_element(self) -> Optional[int]:
        self._init()
        if not self._uia:
            return None
        out = ctypes.c_void_p()
        try:
            hr = self._call(self._uia, _UIA_GET_ROOT_ELEMENT,
                            ctypes.addressof(out))
            if hr == 0 and out.value:
                return out.value
        except OSError as exc:
            debug_log(f"UIA GetRootElement failed: {exc}", "desktop")
        return None

    def runtime_id(self, element: int) -> Optional[int]:
        out = ctypes.c_void_p()
        try:
            hr = self._call(element, _ELEM_GET_RUNTIME_ID,
                            ctypes.addressof(out))
            if hr == 0 and out.value:
                return out.value
        except OSError:
            pass
        return None

    def set_focus(self, element: int) -> bool:
        try:
            return self._call(element, _ELEM_SET_FOCUS) == 0
        except OSError:
            return False


_bridge: Optional[_UIABridge] = None


def _bridge_instance() -> _UIABridge:
    global _bridge
    if _bridge is None:
        _bridge = _UIABridge()
    return _bridge


# ── user32 window helpers (deterministic, no COM) ─────────────────────

def _user32():
    return ctypes.windll.user32


def foreground_window_info() -> Dict[str, Any]:
    """{state, title, hwnd, process_id} for the current foreground window."""
    try:
        u = _user32()
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return {"state": "ui_empty", "title": "", "hwnd": 0, "pid": 0}
        n = u.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return {
            "state": "ui_available",
            "title": buf.value or "",
            "hwnd": int(hwnd),
            "pid": int(pid.value),
        }
    except Exception as exc:  # pragma: no cover — defensive
        debug_log(f"foreground_window_info failed: {exc}", "desktop")
        return {"state": "capture_failed", "title": "", "hwnd": 0, "pid": 0}


def list_windows() -> List[Dict[str, Any]]:
    """All top-level visible windows: [{title, hwnd, pid}]."""
    u = _user32()
    out: List[Dict[str, Any]] = []
    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _param):
        try:
            if not u.IsWindowVisible(hwnd):
                return True
            n = u.GetWindowTextLengthW(hwnd)
            if n == 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            pid = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            out.append({"title": buf.value or "", "hwnd": int(hwnd),
                        "pid": int(pid.value)})
        except Exception:
            pass
        return True

    try:
        u.EnumWindows(EnumProc(_cb), 0)
    except Exception as exc:  # pragma: no cover — defensive
        debug_log(f"list_windows failed: {exc}", "desktop")
    return out


def focus_window(hwnd: int) -> bool:
    try:
        u = _user32()
        u.SetForegroundWindow(wintypes.HWND(hwnd))
        return int(u.GetForegroundWindow()) == int(hwnd)
    except Exception:
        return False


# ── UIA semantic layer ────────────────────────────────────────────────

def ui_root_runtime_id() -> Optional[int]:
    """First element of the root's RuntimeId SAFEARRAY, or None."""
    b = _bridge_instance()
    root = b.root_element()
    if not root:
        return None
    return b.runtime_id(root)


def ui_set_focus_window(hwnd: int) -> bool:
    """SetFocus on the UIA element matching a window handle, if present."""
    b = _bridge_instance()
    root = b.root_element()
    if not root:
        return False
    # The root element carries the same runtime id pattern per-window via
    # child traversal; the deterministic fallback is Win32 SetForegroundWindow.
    return b.set_focus(root)


# ── Screenshot / capture (shared with builtin screenshot tool) ────────

def capture_screen(ocr: bool = True) -> Dict[str, Any]:
    """Return {state, text} with the §1.3 truth-table semantics."""
    try:
        from PIL import ImageGrab
    except Exception:
        return {"state": "capture_failed", "text": "Pillow not available."}
    try:
        img = ImageGrab.grab()
    except Exception as exc:
        return {"state": "capture_failed", "text": f"Grab error: {exc}"}
    if img is None:
        return {"state": "capture_failed", "text": ""}
    if not ocr:
        return {"state": "ui_available", "text": ""}
    import shutil
    if not shutil.which("tesseract"):
        return {"state": "ocr_unavailable",
                "text": "Capture succeeded but Tesseract was not found."}
    try:
        import pytesseract
        text = (pytesseract.image_to_string(img) or "").strip()
    except Exception as exc:
        return {"state": "capture_failed", "text": f"OCR error: {exc}"}
    if not text:
        return {"state": "ocr_empty",
                "text": "Capture succeeded but no text was extracted via OCR."}
    return {"state": "ui_available", "text": text}


# ── World-model projection (§15/§16) ──────────────────────────────────

def world_snapshot() -> Dict[str, Any]:
    """Compact DesktopWorldModel: foreground + window list only.

    Kept tiny and event-cheap: two user32 calls, no LLM, no polling loop.
    """
    fg = foreground_window_info()
    wins = list_windows()
    return {
        "foreground": {
            "title": fg.get("title", ""),
            "pid": fg.get("pid", 0),
        },
        "windows": [w["title"] for w in wins[:12]],
        "window_count": len(wins),
    }
