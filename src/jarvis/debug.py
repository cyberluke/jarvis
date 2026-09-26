"""Debug logging utilities for Jarvis."""
import os
import sys
import threading
import time
from typing import Optional
from .config import load_settings

_RUNTIME_LOG_LOCK = threading.Lock()
_RUNTIME_LOG_PATH: Optional[str] = None
#: Runtime event log that persists across crashes. Always on (not gated by
#: voice_debug) for the operational categories so a lost runtime log never
#: happens again. stderr stays gated by voice_debug; the file is unconditional.
_RUNTIME_LOG_ENV = "JARVIS_RUNTIME_LOG"
_RUNTIME_LOG_MAX_BYTES = 5 * 1024 * 1024


def _runtime_log_path() -> str:
    """Resolve (once) the rolling runtime log path under the app log dir."""
    global _RUNTIME_LOG_PATH
    if _RUNTIME_LOG_PATH is None:
        try:
            override = os.environ.get(_RUNTIME_LOG_ENV)
            if override:
                _RUNTIME_LOG_PATH = override
            else:
                from desktop_app.paths import get_log_dir
                _RUNTIME_LOG_PATH = str(get_log_dir() / "jarvis_runtime.log")
        except Exception:
            # Fall back to a temp file when the app paths module is absent
            # (e.g. headless daemon-only runs).
            import tempfile
            _RUNTIME_LOG_PATH = os.path.join(
                tempfile.gettempdir(), "jarvis_runtime.log")
    return _RUNTIME_LOG_PATH


def _write_runtime_log(line: str) -> None:
    """Append one line to the rolling runtime log (bounded size)."""
    try:
        path = _runtime_log_path()
        with _RUNTIME_LOG_LOCK:
            try:
                if os.path.getsize(path) > _RUNTIME_LOG_MAX_BYTES:
                    # Rotate once: keep the tail half so the newest context
                    # survives instead of an ever-growing file.
                    with open(path, "rb") as fh:
                        fh.seek(-_RUNTIME_LOG_MAX_BYTES // 2, os.SEEK_END)
                        tail = fh.read()
                    with open(path, "wb") as fh:
                        fh.write(b"[... rotated ...]\n" + tail)
            except OSError:
                pass
            with open(path, "a", encoding="utf-8", errors="replace") as fh:
                fh.write(line + "\n")
    except Exception:
        pass


_last_check_time: float = 0.0
_cached_voice_debug: Optional[bool] = None
# One settings parse per minute, and only when the environment is silent about
# the flag. ``load_settings()`` rebuilds every default on each pass, so a very
# short TTL repeats that work on every ``debug_log`` call and dominates the
# frozen (windowed) boot before Qt's event loop starts.
_CACHE_TTL_SECONDS: float = 60.0

#: ``voice_debug`` is derived from this single variable in ``config.py``.
_DEBUG_ENV = "JARVIS_VOICE_DEBUG"


def _is_debug_enabled() -> bool:
    """The ``voice_debug`` flag, read from the environment when it is present.

    ``config.load_settings`` derives the field from ``JARVIS_VOICE_DEBUG`` only,
    so a direct read is the same answer at a fraction of the cost; the full
    parse is the fallback for the case where ``.env`` has not been loaded into
    the environment yet.
    """
    global _last_check_time, _cached_voice_debug
    raw = os.environ.get(_DEBUG_ENV)
    if raw is not None:
        return str(raw).strip() == "1"
    now = time.time()
    if _cached_voice_debug is None or (now - _last_check_time) > _CACHE_TTL_SECONDS:
        try:
            _cached_voice_debug = bool(load_settings().voice_debug)
        except Exception:
            _cached_voice_debug = False
        _last_check_time = now
    return bool(_cached_voice_debug)


def debug_log(message: str, category: str = "debug") -> None:
    """Unified debug logging function for Jarvis.

    Args:
        message: The debug message to log
        category: The log category (e.g., "debug", "voice", "echo", "tts", etc.)
    """
    # Always mirror operational events to the persistent runtime log so a
    # runtime failure is never lost; stderr stays gated by voice_debug.
    _write_runtime_log(f"{time.strftime('%H:%M:%S')} [{category:^10}] {message}")
    if not _is_debug_enabled():
        return
    try:
        print(f"[{category:^10}] {message}", file=sys.stderr)
    except Exception:
        pass
