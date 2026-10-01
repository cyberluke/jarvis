"""Toaster Desktop Bridge (tdb/1) — loopback HTTP JSON + event stream.

Separate from the LAN subtitle / receiver static server:
- this listener binds 127.0.0.1 only
- every request requires Authorization: Bearer <session token>
- Cast protocol stays inside CastGateway
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from ..debug import debug_log
from .cast_gateway import PROTOCOL, error_body, get_gateway
from .voice_pe_contract import VOICE_PE_CAPABILITIES, VoicePeContract, handle_voice_pe

_server: Optional["TdbServer"] = None


def bridge_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(local) / "VIVERRA" / "Toastovac"


def bridge_path() -> Path:
    return bridge_dir() / "bridge.json"


def write_bridge_json(payload: Dict[str, Any]) -> None:
    path = bridge_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def remove_bridge_json() -> None:
    path = bridge_path()
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


class TdbServer:
    def __init__(self, cfg: Any = None, voice_pe: Any = None) -> None:
        self.cfg = cfg
        self.token = secrets.token_urlsafe(32)
        self.host = "127.0.0.1"
        self.port = 0
        self.started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.gateway = get_gateway(cfg)
        #: Voice PE public contract (``voice-pe/v1``). ``voice_pe`` is the
        #: daemon's VoicePEManager instance or ``None``; the router stays
        #: alive either way and reports ``enabled: false`` without a manager.
        self.voice_pe = VoicePeContract(voice_pe)

    def start(self) -> bool:
        if self._httpd is not None:
            return True
        try:
            httpd = ThreadingHTTPServer((self.host, 0), _make_handler(self))
        except OSError as exc:
            debug_log(f"TDB bind failed: {exc}", "cast")
            return False
        self._httpd = httpd
        self.port = int(httpd.server_address[1])
        self._thread = threading.Thread(target=httpd.serve_forever, name="tdb-http", daemon=True)
        self._thread.start()
        write_bridge_json({
            "schema": 1,
            "protocol": PROTOCOL,
            "pid": os.getpid(),
            "host": self.host,
            "port": self.port,
            "token": self.token,
            "startedAt": self.started_at,
            "capabilities": [
                "cast.discovery",
                "cast.session",
                "cast.youtube",
                "cast.shorts",
                "cast.playback",
                "cast.volume",
                "cast.receiver",
                *VOICE_PE_CAPABILITIES,
            ],
        })
        self.gateway.emit("bridge.ready", {"port": self.port})
        debug_log(f"TDB_REQUEST listener 127.0.0.1:{self.port}", "cast")
        print(f"  📺 Toaster Desktop Bridge tdb/1 on 127.0.0.1:{self.port}", flush=True)
        return True

    def stop(self) -> None:
        self.gateway.emit("bridge.shutdown", {})
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except Exception:
                pass
            self._httpd = None
        remove_bridge_json()


def _make_handler(server: TdbServer):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            return

        def _peer_ok(self) -> bool:
            host = self.client_address[0]
            return host in ("127.0.0.1", "::1", "localhost")

        def _auth_ok(self) -> bool:
            header = self.headers.get("Authorization") or ""
            if header == f"Bearer {server.token}":
                return True
            query = parse_qs(urlparse(self.path).query)
            token = (query.get("token") or [""])[0]
            return token == server.token

        def _read_json(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 1_000_000:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                return {}
            return data if isinstance(data, dict) else {}

        def _send(self, status: int, payload: Dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _guard(self) -> bool:
            if not self._peer_ok():
                self._send(403, error_body("AUTH_FAILED", "TDB is loopback only", False))
                return False
            if not self._auth_ok():
                self._send(401, error_body("AUTH_FAILED", "missing or invalid bearer token", False))
                return False
            return True

        def do_GET(self) -> None:  # noqa: N802
            if not self._guard():
                return
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)
            gw = server.gateway
            debug_log(f"TDB_REQUEST GET {path}", "cast")
            if path == "/tdb/v1/capabilities":
                caps = gw.capabilities()
                caps["voicePe"] = {
                    "available": server.voice_pe.available,
                    "capabilities": list(VOICE_PE_CAPABILITIES),
                }
                self._send(200, caps)
                return
            if path == "/tdb/v1/cast/devices":
                self._send(200, {"protocol": PROTOCOL, "devices": gw.devices()})
                return
            if path == "/tdb/v1/cast/sessions":
                self._send(200, gw.snapshot())
                return
            if path.startswith("/tdb/v1/cast/sessions/"):
                session_id = path.rsplit("/", 1)[-1]
                snap = gw.snapshot()
                if snap.get("sessionId") != session_id:
                    self._send(404, error_body("SESSION_LOST", "session not found"))
                    return
                self._send(200, snap)
                return
            if path == "/tdb/v1/events":
                since = int((query.get("since") or ["0"])[0] or 0)
                self._send(200, {"protocol": PROTOCOL, "events": gw.events_since(since)})
                return
            if path.startswith("/tdb/v1/voice-pe/"):
                action = path.rsplit("/", 1)[-1]
                result = handle_voice_pe("GET", action, {}, query, server.voice_pe)
                if result is None:
                    self._send(404, error_body("CAST_UNAVAILABLE", "unknown TDB route", False))
                    return
                self._send(200 if "error" not in result else 400, result)
                return
            if path == "/tdb/v1/events/stream":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                since = int((query.get("since") or ["0"])[0] or 0)
                last = since
                try:
                    while True:
                        events = gw.events_since(last)
                        for event in events:
                            last = int(event.get("eventId") or last)
                            chunk = f"data: {json.dumps(event)}\n\n".encode("utf-8")
                            self.wfile.write(chunk)
                            self.wfile.flush()
                        time.sleep(0.4)
                except Exception:
                    return
            self._send(404, error_body("CAST_UNAVAILABLE", "unknown TDB route", False))

        def do_POST(self) -> None:  # noqa: N802
            if not self._guard():
                return
            path = urlparse(self.path).path.rstrip("/")
            body = self._read_json()
            gw = server.gateway
            debug_log(f"TDB_REQUEST POST {path}", "cast")
            if path == "/tdb/v1/cast/discovery/refresh":
                self._send(200, {"protocol": PROTOCOL, "devices": gw.discover()})
                return
            if path.startswith("/tdb/v1/voice-pe/"):
                action = path.rsplit("/", 1)[-1]
                result = handle_voice_pe("POST", action, body, {}, server.voice_pe)
                if result is None:
                    self._send(404, error_body("CAST_UNAVAILABLE", "unknown TDB route", False))
                    return
                self._send(200 if "error" not in result else 400, result)
                return
            if path == "/tdb/v1/cast/sessions":
                device_id = str(body.get("deviceId") or body.get("device_id") or "")
                intent = body.get("intent") if isinstance(body.get("intent"), dict) else {}
                result = gw.create_session(device_id, intent)
                self._send(200 if "error" not in result else 400, result)
                return
            if "/tdb/v1/cast/sessions/" in path:
                parts = path.split("/")
                session_id = parts[5] if len(parts) > 5 else ""
                action = parts[6] if len(parts) > 6 else ""
                if action == "load":
                    result = gw.load(session_id, body.get("intent") if isinstance(body.get("intent"), dict) else body)
                elif action == "play":
                    result = gw.play(session_id)
                elif action == "pause":
                    result = gw.pause(session_id)
                elif action == "seek":
                    result = gw.seek(session_id, int(body.get("positionMs") or body.get("position_ms") or 0))
                elif action == "stop":
                    result = gw.stop(session_id)
                elif action == "volume":
                    result = gw.set_volume(
                        session_id,
                        float(body.get("volume") or 0),
                        body.get("muted"),
                    )
                else:
                    result = error_body("CAST_UNAVAILABLE", "unknown session command", False)
                self._send(200 if "error" not in result else 400, result)
                return
            self._send(404, error_body("CAST_UNAVAILABLE", "unknown TDB route", False))

        def do_DELETE(self) -> None:  # noqa: N802
            if not self._guard():
                return
            path = urlparse(self.path).path.rstrip("/")
            if path.startswith("/tdb/v1/cast/sessions/"):
                session_id = path.rsplit("/", 1)[-1]
                result = gw_disconnect(server, session_id)
                self._send(200 if "error" not in result else 400, result)
                return
            self._send(404, error_body("CAST_UNAVAILABLE", "unknown TDB route", False))

    return Handler


def gw_disconnect(server: TdbServer, session_id: str) -> Dict[str, Any]:
    return server.gateway.disconnect(session_id)


def start_tdb(cfg: Any = None, voice_pe: Any = None) -> Optional[TdbServer]:
    global _server
    if _server is not None:
        return _server
    instance = TdbServer(cfg, voice_pe=voice_pe)
    if not instance.start():
        return None
    _server = instance
    return instance


def stop_tdb() -> None:
    global _server
    if _server is not None:
        _server.stop()
        _server = None
