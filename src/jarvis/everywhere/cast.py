"""AI Subtitles web-cast — serve live subtitles to any device on the LAN.

A tiny HTTP server in the daemon that serves a single auto-updating page
(showing the latest subtitle line in large text). Point any device on the
same Wi-Fi (an Android TV's browser, a phone) at the printed URL. No custom
receiver app, no Chromecast handshake — just a page that refreshes itself.

This is the receiver-free path: the TV's built-in browser is the renderer.
"""

from __future__ import annotations

import socket
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Deque, Optional, Tuple
from urllib.parse import urlparse

from ..debug import debug_log

_PORT = 8765
_MAX_LINES = 20
_SRT_WINDOW_SEC = 120.0   # rolling SRT covers the last 2 minutes
_SRT_MAX_CUES = 300
_SRT_DEFAULT_DUR_MS = 5000

_PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="1">
<style>
html,body{margin:0;height:100%;background:#000;color:#fff;
font-family:sans-serif;display:flex;align-items:flex-end;justify-content:center;}
#sub{font-size:7vw;text-align:center;padding:2vh 4vw;line-height:1.3;
text-shadow:0 2px 8px #000;}
</style></head>
<body><div id="sub">{text}</div></body></html>"""

# Shared latest-subtitle state (written by the subtitles service).
_state_lock = threading.Lock()
_latest = ""

# Rolling subtitle ring for the live SRT feed (Kodi / external players).
# Each entry: (start_offset_sec, text, duration_ms). Offsets are anchored
# on the monotonic clock at the first push after reset_subtitles(), which
# tracks the live translation stream (arrival time ~ media time).
_ring_lock = threading.Lock()
_ring: Deque[Tuple[float, str, int]] = deque(maxlen=_SRT_MAX_CUES)
_ring_anchor: Optional[float] = None

_server: Optional["SubtitleCastServer"] = None


def push_subtitle(text: str, duration_ms: int = _SRT_DEFAULT_DUR_MS) -> None:
    """Update the line shown on the casting page (called per subtitle)."""
    global _latest, _ring_anchor
    clean = (text or "").strip()
    with _state_lock:
        _latest = clean
    with _ring_lock:
        now = time.monotonic()
        if _ring_anchor is None:
            _ring_anchor = now
        if clean:
            _ring.append((now - _ring_anchor, clean, max(500, int(duration_ms))))


def reset_subtitles() -> None:
    """Re-anchor the SRT timeline (call right before starting playback)."""
    global _ring_anchor
    with _ring_lock:
        _ring.clear()
        _ring_anchor = None


def _current() -> str:
    with _state_lock:
        return _latest


def _fmt_srt_ts(seconds: float) -> str:
    seconds = max(0.0, seconds)
    ms = int(round((seconds - int(seconds)) * 1000))
    s = int(seconds) % 60
    m = (int(seconds) // 60) % 60
    h = int(seconds) // 3600
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _srt_body() -> str:
    """Rolling SRT built from the recent subtitle ring (windowed)."""
    with _ring_lock:
        now = time.monotonic()
        anchor = _ring_anchor if _ring_anchor is not None else now
        elapsed = now - anchor
        items = [
            (start, text, dur)
            for (start, text, dur) in _ring
            if start + dur / 1000.0 >= elapsed - _SRT_WINDOW_SEC
        ]
    out = []
    for idx, (start, text, dur) in enumerate(items, 1):
        end = start + dur / 1000.0
        out.append(
            f"{idx}\n{_fmt_srt_ts(start)} --> {_fmt_srt_ts(end)}\n{text}\n"
        )
    return "\n".join(out)


_RECEIVER_ROOT = Path(__file__).resolve().parent / "cast_receiver"
_RECEIVER_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".woff2": "font/woff2",
}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        parsed = urlparse(self.path)
        if parsed.path.startswith("/tdb"):
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"TDB is loopback-only")
            return
        if parsed.path in ("/", "/index.html", "/subtitles"):
            body = _PAGE.replace("{text}", _current()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/subtitles.srt":
            body = _srt_body().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path.startswith("/cast-receiver"):
            self._serve_receiver(parsed.path[len("/cast-receiver"):] or "/")
            return
        self.send_response(404)
        self.end_headers()

    def _serve_receiver(self, rel: str) -> None:
        rel = rel.lstrip("/") or "index.html"
        if ".." in rel:
            self.send_response(400)
            self.end_headers()
            return
        root = _RECEIVER_ROOT / "dist" if (_RECEIVER_ROOT / "dist").is_dir() else _RECEIVER_ROOT
        target = (root / rel).resolve()
        if not str(target).startswith(str(root.resolve())):
            self.send_response(403)
            self.end_headers()
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            self.send_response(404)
            self.end_headers()
            return
        body = _PAGE.replace("{text}", _current()).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # quiet
        pass


class SubtitleCastServer:
    """Threaded HTTP server on the LAN, one page, auto-refreshing."""

    def __init__(self, port: int = _PORT) -> None:
        self.port = port
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        ip = _lan_ip()
        return f"http://{ip}:{self.port}/"

    def start(self) -> bool:
        if self._httpd is not None:
            return True
        try:
            self._httpd = ThreadingHTTPServer(("0.0.0.0", self.port), _Handler)
        except OSError as exc:
            debug_log(f"subtitle cast: bind failed: {exc}", "everywhere")
            return False
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="subtitle-cast", daemon=True)
        self._thread.start()
        debug_log(f"subtitle cast: serving {self.url}", "everywhere")
        try:
            print(f"  📺 Subtitles casting at {self.url} (open on the TV browser)",
                  flush=True)
        except UnicodeEncodeError:  # cp1252 console
            print(f"  Subtitles casting at {self.url} (open on the TV browser)",
                  flush=True)
        return True

    def stop(self) -> None:
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except Exception:
                pass
            self._httpd = None


def _lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def get_cast_server() -> SubtitleCastServer:
    global _server
    if _server is None:
        _server = SubtitleCastServer()
    return _server
