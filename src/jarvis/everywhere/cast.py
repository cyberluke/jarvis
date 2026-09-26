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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from ..debug import debug_log

_PORT = 8765
_MAX_LINES = 20

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

_server: Optional["SubtitleCastServer"] = None


def push_subtitle(text: str) -> None:
    """Update the line shown on the casting page (called per subtitle)."""
    global _latest
    with _state_lock:
        _latest = (text or "").strip()


def _current() -> str:
    with _state_lock:
        return _latest


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        if self.path not in ("/", "/index.html"):
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
        print(f"  📺 Subtitles casting at {self.url} (open on the TV browser)",
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
