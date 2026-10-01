"""Loopback WebSocket server of the Voice PE WebAudio bridge.

Binds ``127.0.0.1`` only, requires a bearer token and an allowed ``Origin``,
and serves at most one client. The satellite microphone signal is tapped off
the Voice PE device's ``AudioIngress`` by ``VoicePEFrameTap`` and streamed as
framed PCM16LE over ``v271-webaudio/1`` (see ``voice_pe_bridge.spec.md``).

The server owns an asyncio loop thread of its own, mirroring
``VoicePEManager``; the tap's callbacks run on the manager loop and only
wake this loop through ``call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from ..debug import debug_log
from .protocol import (
    CLOSE_BUSY,
    CLOSE_ORIGIN_REJECTED,
    CLOSE_UNAUTHORISED,
)
from .session import BridgeSession
from .sink import VoicePEFrameTap

#: How often the server retries attaching the tap while no satellite exists.
ATTACH_RETRY_S = 3.0

#: Default frame budget of the tap's bounded queue (200 frames = 6.4 s).
DEFAULT_MAX_FRAMES = 200


def origin_allowed(origin: Optional[str], allowed: List[str]) -> bool:
    """Match one ``Origin`` header against the configured allow list.

    Entries are ``scheme://host`` or ``scheme://host:port``; a ``*`` port
    matches any port of that scheme+host. An exact ``"*"`` entry allows
    everything (explicit wildcard only). ``None`` (no Origin header) is never
    allowed here; the caller decides separately.
    """
    if not origin:
        return False
    if "*" in allowed:
        return True
    for entry in allowed:
        entry = (entry or "").strip()
        if not entry:
            continue
        if entry == origin:
            return True
        try:
            scheme, rest = entry.split("://", 1)
            host, _, port = rest.partition(":")
            origin_scheme, origin_rest = origin.split("://", 1)
            origin_host, _, origin_port = origin_rest.partition(":")
        except ValueError:
            continue
        if scheme != origin_scheme or host != origin_host:
            continue
        if port in ("", "*") or port == origin_port:
            return True
    return False


class VoicePEBridgeServer:
    """One loopback WebSocket bridge per daemon process."""

    def __init__(
        self,
        cfg: Any,
        manager: Any,
        device_key: str = "",
    ) -> None:
        self._cfg = cfg
        self._manager = manager
        self._device_key = str(device_key or "")
        self._host = "127.0.0.1"
        self._port = int(getattr(cfg, "voice_pe_bridge_port", 27123) or 27123)
        # ``0`` means an ephemeral port (tests); negative or oversized ports
        # fall back to the default.
        if self._port < 0 or self._port > 65535:
            self._port = 27123
        self._token = str(getattr(cfg, "voice_pe_bridge_token", "") or "")
        if not self._token:
            import os

            self._token = os.environ.get("JARVIS_VOICE_PE_BRIDGE_TOKEN", "")
        raw_origins = getattr(cfg, "voice_pe_bridge_allowed_origins", None)
        self._allowed_origins = (
            [str(o) for o in raw_origins] if isinstance(raw_origins, list) else []
        )
        if not self._allowed_origins:
            self._allowed_origins = ["https://v271.cz"]
        self._max_clients = max(
            1, int(getattr(cfg, "voice_pe_bridge_max_clients", 1) or 1)
        )
        self._max_frames = max(
            1,
            int(
                getattr(cfg, "voice_pe_bridge_buffer_frames", DEFAULT_MAX_FRAMES)
                or DEFAULT_MAX_FRAMES
            ),
        )

        self.tap = VoicePEFrameTap(max_frames=self._max_frames)
        self.wake: Optional[asyncio.Event] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ws_server: Optional[Any] = None
        self._start_done = threading.Event()
        self._running = False
        self._idle_event: Optional[asyncio.Event] = None
        self._clients: set = set()
        self._client_slots = 0
        self._attached = False
        self._attach_task: Optional[asyncio.Task] = None
        self._serve_task: Optional[asyncio.Task] = None
        self._metrics: Dict[str, Any] = {
            "connections": 0,
            "reconnects": 0,
            "rejected_auth": 0,
            "rejected_origin": 0,
            "rejected_busy": 0,
            "client_peaks": 0,
        }

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._cfg, "voice_pe_bridge_enabled", False))

    @property
    def running(self) -> bool:
        return bool(self._running and self._loop is not None)

    @property
    def port(self) -> int:
        return self._port

    @property
    def clients(self) -> int:
        return len(self._clients)

    # -- lifecycle (thread + loop, like VoicePEManager) ----------------------

    def start(self, timeout_s: float = 10.0) -> bool:
        """Boot the bridge loop thread and wait until it serves."""
        if self._thread is not None or self._running:
            return True
        if not self._token:
            debug_log(
                "voice_pe_bridge: no token configured, refusing to start "
                "(set voice_pe_bridge_token or JARVIS_VOICE_PE_BRIDGE_TOKEN)",
                category="warn",
            )
            return False
        self._start_done.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="voice_pe_bridge", daemon=True
        )
        self._thread.start()
        return bool(self._start_done.wait(timeout_s))

    def stop(self) -> None:
        """Stop the loop, the server and the tap attachment."""
        self._running = False
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(self._wake_idle)
            except Exception:
                pass
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)

    def _wake_idle(self) -> None:
        if self._idle_event is not None:
            self._idle_event.set()

    def _run_loop(self) -> None:
        asyncio.run(self._astart())

    async def _astart(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.wake = asyncio.Event()
        self.tap.set_notify(
            lambda: self._loop.call_soon_threadsafe(self._wake_from_tap)
        )
        from websockets.asyncio.server import serve

        self._running = True
        self._ws_server = await serve(
            self._handle,
            self._host,
            self._port,
            max_size=2 ** 20,
            ping_interval=20.0,
            ping_timeout=20.0,
        )
        sockets = self._ws_server.sockets or []
        if sockets:
            self._port = int(sockets[0].getsockname()[1])
        self._attach_task = asyncio.ensure_future(self._attach_loop())
        debug_log(
            f"🗂️ Voice PE bridge listening on ws://{self._host}:{self._port} "
            f"(v271-webaudio/1)",
            category="voice_pe_bridge",
        )
        self._start_done.set()
        self._idle_event = asyncio.Event()
        try:
            await self._idle_event.wait()  # released by ``stop()``
        finally:
            self._running = False
            if self._attach_task is not None:
                self._attach_task.cancel()
            if self._ws_server is not None:
                self._ws_server.close()
                await self._ws_server.wait_closed()
            for client in list(self._clients):
                try:
                    await client.disconnect(code=1001, reason="bridge stopped")
                except Exception:
                    pass
            self._clients.clear()
            if self._attached and self._manager is not None:
                self._manager.detach_audio_sink(self.tap, self._device_key)
                self._attached = False
            self.tap.set_notify(None)

    def _wake_from_tap(self) -> None:
        if self.wake is not None:
            self.wake.set()

    # -- tap attachment ------------------------------------------------------

    async def _attach_loop(self) -> None:
        """Keep the tap attached once a satellite connection exists."""
        while self._running:
            if not self._attached and self._manager is not None:
                if self._manager.attach_audio_sink(self.tap, self._device_key):
                    self._attached = True
                    debug_log(
                        "🗂️ Voice PE bridge: tap attached",
                        category="voice_pe_bridge",
                    )
                    info = self._device_info(None)
                    for client in list(self._clients):
                        try:
                            await client.announce_device(info)
                        except Exception:
                            pass
            await asyncio.sleep(ATTACH_RETRY_S)

    # -- connection handling --------------------------------------------------

    async def _handle(self, ws) -> None:
        """One upgrade: origin, token, slot, then the session."""
        self._metrics["connections"] = int(self._metrics["connections"]) + 1
        request = getattr(ws, "request", None)
        headers = request.headers if request is not None else None
        origin = headers.get("Origin") if headers is not None else None
        if not origin_allowed(origin, self._allowed_origins):
            self._metrics["rejected_origin"] = (
                int(self._metrics["rejected_origin"]) + 1
            )
            await self._close(ws, CLOSE_ORIGIN_REJECTED, "origin not allowed")
            return
        token = ""
        if headers is not None:
            auth = headers.get("Authorization", "") or ""
            if auth.startswith("Bearer "):
                token = auth[7:].strip()
        if not token or token != self._token:
            self._metrics["rejected_auth"] = int(self._metrics["rejected_auth"]) + 1
            await self._close(ws, CLOSE_UNAUTHORISED, "unauthorised")
            return
        if not self._acquire_slot():
            self._metrics["rejected_busy"] = int(self._metrics["rejected_busy"]) + 1
            await self._close(ws, CLOSE_BUSY, "another client is connected")
            return
        session = BridgeSession(
            ws,
            self,
            resolve_device=self._resolve_device,
            device_info=self._device_info,
        )
        self._clients.add(session)
        try:
            await session.run()
        finally:
            self._clients.discard(session)
            self._release_slot()

    def _acquire_slot(self) -> bool:
        if self._client_slots >= self._max_clients:
            return False
        self._client_slots += 1
        self._metrics["client_peaks"] = max(
            int(self._metrics["client_peaks"]), self._client_slots
        )
        return True

    def _release_slot(self) -> None:
        self._client_slots = max(0, self._client_slots - 1)

    @staticmethod
    async def _close(ws, code: int, reason: str) -> None:
        try:
            await ws.close(code=code, reason=reason)
        except Exception:
            pass

    # -- device resolution -----------------------------------------------------

    def _resolve_device(self, device_id: str) -> Optional[Dict[str, Any]]:
        """Resolve a client's ``deviceId`` (or the default) to a snapshot."""
        if self._manager is None:
            return None
        device = self._manager.device(device_id) if device_id else self._manager.device(self._device_key)
        return self._snapshot(device)

    def _device_info(self, device_id: Optional[str]) -> Optional[Dict[str, Any]]:
        """Device snapshot for ``hello``/``state``, or ``None`` when absent."""
        if self._manager is None:
            return None
        device = self._manager.device(str(device_id or "") or self._device_key)
        return self._snapshot(device)

    @staticmethod
    def _snapshot(device) -> Optional[Dict[str, Any]]:
        if device is None:
            return None
        identity = getattr(device, "identity", None) or {}
        mac = str(identity.get("mac_address", "") or "").replace(":", "").lower()
        name = str(
            identity.get("node_name", "")
            or identity.get("friendly_name", "")
            or getattr(device, "_host", "")
        )
        return {
            "deviceId": mac or name,
            "name": name,
            "state": str(getattr(device, "state", "") or ""),
        }

    # -- diagnostics -----------------------------------------------------------

    def metrics(self) -> Dict[str, Any]:
        out = dict(self._metrics)
        out["clients"] = len(self._clients)
        out["attached"] = self._attached
        out["tap"] = self.tap.metrics()
        out["sessions"] = [client.metrics() for client in self._clients]
        return out

    def health(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "running": self.running,
            "host": self._host,
            "port": self._port,
            "token": bool(self._token),
            "allowed_origins": list(self._allowed_origins),
            "max_clients": self._max_clients,
            "attached": self._attached,
            "clients": len(self._clients),
            "metrics": self.metrics(),
        }