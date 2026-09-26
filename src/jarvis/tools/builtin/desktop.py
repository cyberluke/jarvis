"""Desktop perception / control tool (Slice G).

Thin dispatch over ``jarvis.desktop`` (semantic-first: window-manager and
UI Automation before pixels; OCR is the fallback). One allow-listed action
enum keeps the schema small for the router and for small models.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ...debug import debug_log
from ..base import Tool, ToolContext
from ..types import ToolExecutionResult


class DesktopTool(Tool):
    """Structured Windows desktop context & control."""

    @property
    def name(self) -> str:
        return "desktopTool"

    @property
    def description(self) -> str:
        return (
            "Structured Windows desktop context and control. Args "
            "{action}. Actions: foreground (focused window title+pid), "
            "windows (list visible windows), focus (bring a window "
            "forward by hwnd from windows/foreground), snapshot (compact "
            "world model), capture (screenshot + OCR fallback). Prefer "
            "this over screenshot for window/app questions."
        )

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["foreground", "windows", "focus", "snapshot",
                             "capture"],
                    "description": "Which desktop operation to run.",
                },
                "hwnd": {
                    "type": "integer",
                    "description": "Window handle for focus (from foreground/windows).",
                },
            },
            "required": ["action"],
        }

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        from ... import desktop as _d

        action = str((args or {}).get("action", "")).strip().lower()
        context.user_print(f"🖥️ Desktop: {action}")
        try:
            if action == "foreground":
                info = _d.foreground_window_info()
                if info.get("state") != "ui_available":
                    return ToolExecutionResult(
                        success=False, reply_text=None,
                        error_message="No foreground window.",
                    )
                return ToolExecutionResult(
                    success=True,
                    reply_text=(
                        f"Foreground: {info['title']} "
                        f"(hwnd {info['hwnd']}, pid {info['pid']})"
                    ),
                )
            if action == "windows":
                wins = _d.list_windows()
                if not wins:
                    return ToolExecutionResult(
                        success=False, reply_text=None,
                        error_message="No visible windows found.",
                    )
                lines = [f"{w['hwnd']}: {w['title']}" for w in wins[:12]]
                return ToolExecutionResult(
                    success=True, reply_text="\n".join(lines)
                )
            if action == "focus":
                hwnd = (args or {}).get("hwnd")
                if hwnd is None:
                    return ToolExecutionResult(
                        success=False, reply_text=None,
                        error_message="focus needs an hwnd argument.",
                    )
                ok = _d.focus_window(int(hwnd))
                return ToolExecutionResult(
                    success=ok,
                    reply_text=(
                        f"Window {hwnd} is now in foreground." if ok
                        else f"Could not bring window {hwnd} to foreground."
                    ),
                )
            if action == "snapshot":
                snap = _d.world_snapshot()
                fg = snap["foreground"]["title"] or "(none)"
                return ToolExecutionResult(
                    success=True,
                    reply_text=(
                        f"Foreground: {fg}; {snap['window_count']} "
                        f"visible window(s)."
                    ),
                )
            if action == "capture":
                cap = _d.capture_screen(ocr=True)
                state = cap.get("state", "capture_failed")
                text = cap.get("text", "")
                if state == "capture_failed":
                    return ToolExecutionResult(
                        success=False, reply_text=None,
                        error_message=text or "Capture failed.",
                    )
                # ocr_empty / ocr_unavailable: capture itself succeeded →
                # truthful success with an explicit note (§1.3).
                return ToolExecutionResult(success=True, reply_text=text)
            return ToolExecutionResult(
                success=False,
                reply_text=(
                    f"Neznámá akce '{action}'. Povolené: foreground, "
                    "windows, focus, snapshot, capture."
                ),
            )
        except Exception as exc:  # pragma: no cover — defensive
            debug_log(f"desktop tool error: {exc}", "tools")
            return ToolExecutionResult(
                success=False, reply_text=None, error_message=str(exc)
            )
