"""Local protocol transports for the desktop control plane.

Domain services depend on ``LocalProtocolTransport``, never on
browser-specific fetch/WebSocket code. P0 ships only
``LocalHttpTransport`` (loopback HTTP + SSE); future transports
(native messaging, device relay) implement the same interface and are
swapped in without touching the domain.

Security (spec §20): loopback bind only, paired-session auth (Bearer
secret; SSE accepts ``?token=`` because EventSource cannot set headers),
explicit Origin allowlist, no wildcard CORS, no generic shell endpoint,
1 MB request body cap.
"""

from __future__ import annotations

import json
import re
import threading
import time
from abc import ABC, abstractmethod
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Pattern, Tuple
from urllib.parse import parse_qs, urlparse

from ..debug import debug_log
from .protocol import (
    ErrorCode,
    ProtocolError,
    make_envelope,
    make_error,
    make_result,
)

_MAX_BODY_BYTES = 1024 * 1024
_SSE_HEARTBEAT_SEC = 15.0

_STATUS_BY_CODE = {
    ErrorCode.INVALID_REQUEST: 400,
    ErrorCode.PROTOCOL_UNSUPPORTED: 400,
    ErrorCode.FORBIDDEN_ORIGIN: 403,
    ErrorCode.PAIRING_REQUIRED: 401,
    ErrorCode.PAIRING_INVALID: 401,
    ErrorCode.SESSION_UNKNOWN: 404,
    ErrorCode.SESSION_EXPIRED: 409,
    ErrorCode.APP_UNKNOWN: 404,
    ErrorCode.MCP_UNAVAILABLE: 503,
    ErrorCode.MCP_NOT_RUNNING: 503,
    ErrorCode.TOOL_UNKNOWN: 404,
    ErrorCode.TOOL_NAMESPACE_MISMATCH: 400,
    ErrorCode.V271_BROWSER_UNAVAILABLE: 503,
    ErrorCode.INTERNAL_ERROR: 500,
}


class LocalProtocolTransport(ABC):
    """Domain seam for v271-local/1 message delivery.

    Implementations map envelopes onto a concrete wire (HTTP today,
    native messaging / relay later) and expose the P0 REST bindings.
    """

    @abstractmethod
    def start(self) -> None:
        """Begin listening. Idempotent."""

    @abstractmethod
    def stop(self) -> None:
        """Stop listening and release the port. Idempotent."""

    @property
    @abstractmethod
    def port(self) -> Optional[int]:
        """The bound port (``None`` while stopped)."""

    @property
    @abstractmethod
    def running(self) -> bool:
        """True while the transport is accepting connections."""


class LocalHttpTransport(LocalProtocolTransport):
    """Loopback HTTP/SSE transport (P0).

    ``service`` is duck-typed against ``ControlPlane``: hello, pair,
    session registration, continuity, capabilities, apps, MCP
    lifecycle, tool calls, the envelope endpoint, voice replies, and
    the event bus.
    """

    def __init__(
        self,
        service: Any,
        *,
        host: str = "127.0.0.1",
        port: int = 27121,
        allowed_origins: Optional[List[str]] = None,
    ) -> None:
        self._service = service
        self._host = host
        self._port = int(port)
        self._allowed_origins = set(allowed_origins or [])
        self._server: Optional[_ControlPlaneServer] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._stopping = threading.Event()

    # -- LocalProtocolTransport -----------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._server is not None:
                return
            server = _ControlPlaneServer(
                (self._host, self._port),
                _ControlPlaneHandler,
                service=self._service,
                allowed_origins=self._allowed_origins,
                stopping=self._stopping,
            )
            self._stopping.clear()
            self._server = server
            thread = threading.Thread(
                target=server.serve_forever,
                name="control-plane-http",
                daemon=True,
            )
            thread.start()
            self._thread = thread
            debug_log(
                f"control plane HTTP transport on "
                f"http://{self._host}:{server.server_address[1]}",
                "control_plane",
            )

    def stop(self) -> None:
        with self._lock:
            server = self._server
            self._server = None
        if server is None:
            return
        self._stopping.set()
        try:
            server.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            server.server_close()
        except Exception:  # noqa: BLE001
            pass
        debug_log("control plane HTTP transport stopped", "control_plane")

    @property
    def port(self) -> Optional[int]:
        server = self._server
        if server is None:
            return None
        return int(server.server_address[1])

    @property
    def running(self) -> bool:
        return self._server is not None and not self._stopping.is_set()


class _ControlPlaneServer(ThreadingHTTPServer):
    """Threading loopback server carrying the service + policy context."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: Tuple[str, int],
        handler_cls: type,
        *,
        service: Any,
        allowed_origins: set,
        stopping: threading.Event,
    ) -> None:
        self.service = service
        self.allowed_origins = allowed_origins
        self.stopping = stopping
        super().__init__(server_address, handler_cls)

    def handle_error(self, request, client_address) -> None:  # noqa: N802
        # Broken pipes on SSE disconnects are normal; log everything else.
        import sys

        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


_GET_ROUTES: List[Tuple[Pattern, str]] = [
    (re.compile(r"^/desktop/v1/hello$"), "hello"),
    (re.compile(r"^/desktop/v1/capabilities$"), "capabilities"),
    (re.compile(r"^/desktop/v1/apps$"), "apps_list"),
    (re.compile(r"^/desktop/v1/apps/(?P<app_id>[^/]+)$"), "app_get"),
    (
        re.compile(r"^/desktop/v1/apps/(?P<app_id>[^/]+)/mcp/tools$"),
        "mcp_tools",
    ),
    (re.compile(r"^/desktop/v1/events$"), "events"),
]

_POST_ROUTES: List[Tuple[Pattern, str]] = [
    (re.compile(r"^/desktop/v1/pair$"), "pair"),
    (re.compile(r"^/desktop/v1/session/register$"), "session_register"),
    (re.compile(r"^/desktop/v1/session/unregister$"), "session_unregister"),
    (re.compile(r"^/desktop/v1/session/continuity$"), "session_continuity"),
    (
        re.compile(
            r"^/desktop/v1/apps/(?P<app_id>[^/]+)/mcp/(?P<action>start|stop|restart)$"
        ),
        "mcp_lifecycle",
    ),
    (re.compile(r"^/desktop/v1/tool/call$"), "tool_call"),
    (re.compile(r"^/desktop/v1/message$"), "message"),
    (re.compile(r"^/desktop/v1/voice/say$"), "voice_say"),
]

#: Routes that do not require a pairing secret.
_NO_AUTH = frozenset({"hello", "pair"})


class _ControlPlaneHandler(BaseHTTPRequestHandler):
    server_version = "ToastovacControlPlane/1"
    protocol_version = "HTTP/1.1"

    # -- helpers --------------------------------------------------------

    @property
    def _cp(self) -> "_ControlPlaneServer":
        return self.server  # type: ignore[return-value]

    def log_message(self, fmt: str, *args) -> None:  # noqa: N802
        debug_log(
            f"control plane HTTP: {self.address_string()} - {fmt % args}",
            "control_plane",
        )

    def _read_body(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length < 0 or length > _MAX_BODY_BYTES:
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST, "Request body too large"
            )
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST, f"Invalid JSON body: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST, "JSON body must be an object"
            )
        return data

    def _check_origin(self) -> None:
        origin = self.headers.get("Origin")
        if not origin:
            return  # non-browser client (curl, native) — token auth applies
        if origin not in self._cp.allowed_origins:
            raise ProtocolError(
                ErrorCode.FORBIDDEN_ORIGIN,
                f"Origin {origin!r} is not allowed",
            )

    def _pairing(self) -> Dict[str, Any]:
        """Resolve the Bearer secret to a pairing; raises 401 otherwise."""
        auth = self.headers.get("Authorization") or ""
        scheme, _, secret = auth.partition(" ")
        if scheme.lower() != "bearer" or not secret.strip():
            raise ProtocolError(ErrorCode.PAIRING_REQUIRED, "Bearer token required")
        pairing = self._cp.service.pairings.validate_secret(secret.strip())
        if pairing is None:
            raise ProtocolError(ErrorCode.PAIRING_INVALID, "Unknown pairing secret")
        return pairing

    def _send_json(self, status: int, data: Dict[str, Any]) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors_headers()
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_error(self, error: ProtocolError) -> None:
        self._send_json(
            _STATUS_BY_CODE.get(error.code, 500),
            make_error(error),
        )

    def _send_cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        if origin and origin in self._cp.allowed_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    # -- HTTP verbs -----------------------------------------------------

    def do_OPTIONS(self) -> None:  # noqa: N802
        origin = self.headers.get("Origin")
        if origin and origin not in self._cp.allowed_origins:
            self._send_error(
                ProtocolError(
                    ErrorCode.FORBIDDEN_ORIGIN, f"Origin {origin!r} is not allowed"
                )
            )
            return
        self.send_response(204)
        self._send_cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers", "Authorization, Content-Type"
        )
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._check_origin()
            path = urlparse(self.path).path
            for pattern, name in _GET_ROUTES:
                match = pattern.match(path)
                if match:
                    self._handle_get(name, match.groupdict())
                    return
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST, f"Unknown GET path {path!r}"
            )
        except ProtocolError as exc:
            self._send_error(exc)
        except Exception as exc:  # noqa: BLE001
            debug_log(f"control plane GET error: {exc}", "control_plane")
            self._send_error(
                ProtocolError(
                    ErrorCode.INTERNAL_ERROR, "Internal control plane error"
                )
            )

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._check_origin()
            path = urlparse(self.path).path
            for pattern, name in _POST_ROUTES:
                match = pattern.match(path)
                if match:
                    self._handle_post(name, match.groupdict())
                    return
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST, f"Unknown POST path {path!r}"
            )
        except ProtocolError as exc:
            self._send_error(exc)
        except Exception as exc:  # noqa: BLE001
            debug_log(f"control plane POST error: {exc}", "control_plane")
            self._send_error(
                ProtocolError(
                    ErrorCode.INTERNAL_ERROR, "Internal control plane error"
                )
            )

    # -- GET handlers ---------------------------------------------------

    def _handle_get(self, name: str, groups: Dict[str, str]) -> None:
        svc = self._cp.service
        if name == "hello":
            self._send_json(200, svc.hello())
            return
        if name == "events":
            # SSE streams are opened by EventSource, which cannot set an
            # Authorization header — the pairing secret travels as
            # ``?token=`` here. The Origin check above and the loopback
            # bind keep this acceptable.
            pairing = self._pairing_from_query()
            self._handle_events(pairing)
            return
        pairing = self._pairing()
        if name == "capabilities":
            self._send_json(200, svc.capabilities())
        elif name == "apps_list":
            self._send_json(200, {"apps": svc.list_apps()})
        elif name == "app_get":
            self._send_json(200, svc.get_app(groups["app_id"]))
        elif name == "mcp_tools":
            self._send_json(200, svc.mcp_tools(groups["app_id"]))
        else:  # pragma: no cover
            raise ProtocolError(ErrorCode.INVALID_REQUEST, f"Unhandled GET {name}")

    def _pairing_from_query(self) -> Dict[str, Any]:
        query = parse_qs(urlparse(self.path).query)
        token = (query.get("token") or [""])[0]
        if not token:
            raise ProtocolError(ErrorCode.PAIRING_REQUIRED, "Pairing token required")
        pairing = self._cp.service.pairings.validate_secret(token)
        if pairing is None:
            raise ProtocolError(ErrorCode.PAIRING_INVALID, "Unknown pairing secret")
        return pairing

    def _handle_events(self, pairing: Dict[str, Any]) -> None:
        """SSE stream for one tab."""
        query = parse_qs(urlparse(self.path).query)
        tab_session_id = (query.get("tabSessionId") or [""])[0]
        since_raw = (query.get("since") or ["0"])[0]
        if not tab_session_id:
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST, "tabSessionId query parameter required"
            )
        try:
            since = max(0, int(since_raw))
        except (TypeError, ValueError):
            since = 0
        session = self._cp.service.sessions.get_for_pairing(
            tab_session_id, pairing["pairingId"]
        )
        if session is None:
            raise ProtocolError(
                ErrorCode.SESSION_UNKNOWN, "Tab session is not registered"
            )
        q = self._cp.service.events.subscribe(tab_session_id, since)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self._send_cors_headers()
            self.end_headers()
            self._stream_events(tab_session_id, q)
        finally:
            self._cp.service.events.unsubscribe(tab_session_id, q)

    def _stream_events(
        self,
        tab_session_id: str,
        q: "queue.Queue",
    ) -> None:
        import queue as _queue

        last_heartbeat = time.time()
        while not self._cp.stopping.is_set():
            try:
                seq, event_type, payload = q.get(timeout=1.0)
            except _queue.Empty:
                if time.time() - last_heartbeat >= _SSE_HEARTBEAT_SEC:
                    try:
                        self._write_sse(b": hb\n\n")
                    except _SSEClosed:
                        return
                    last_heartbeat = time.time()
                continue
            event = make_envelope(
                event_type,
                payload,
                msg_id=f"evt-{seq}",
                session_id=tab_session_id,
            )
            try:
                self._write_sse(
                    f"event: {event_type}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n".encode(
                        "utf-8"
                    )
                )
            except _SSEClosed:
                return
            last_heartbeat = time.time()

    def _write_sse(self, chunk: bytes) -> None:
        try:
            self.wfile.write(chunk)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            raise _SSEClosed()

    # -- POST handlers --------------------------------------------------

    def _handle_post(self, name: str, groups: Dict[str, str]) -> None:
        svc = self._cp.service
        if name == "pair":
            body = self._read_body()
            self._send_json(200, svc.pair(body.get("userId")))
            return
        pairing = self._pairing()
        if name == "session_register":
            self._send_json(200, svc.register_session(pairing, self._read_body()))
        elif name == "session_unregister":
            body = self._read_body()
            self._send_json(200, svc.unregister_session(pairing, body))
        elif name == "session_continuity":
            self._send_json(200, svc.report_continuity(pairing, self._read_body()))
        elif name == "mcp_lifecycle":
            action = groups["action"]
            app_id = groups["app_id"]
            if action == "start":
                result = svc.mcp_start(app_id)
            elif action == "stop":
                result = svc.mcp_stop(app_id)
            else:
                result = svc.mcp_restart(app_id)
            self._send_json(200, result)
        elif name == "tool_call":
            self._send_json(200, svc.tool_call(pairing, self._read_body()))
        elif name == "message":
            envelope = self._read_body()
            result = svc.handle_message(envelope, pairing)
            self._send_json(200, result)
        elif name == "voice_say":
            self._send_json(200, svc.speak_text(self._read_body()))
        else:  # pragma: no cover
            raise ProtocolError(ErrorCode.INVALID_REQUEST, f"Unhandled POST {name}")


class _SSEClosed(Exception):
    """Internal: the SSE client disconnected."""