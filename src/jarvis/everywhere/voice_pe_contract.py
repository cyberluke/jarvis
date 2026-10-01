"""Voice PE public API contract (``voice-pe/v1``) served over ``tdb/1``.

Toastovač is the first-class Voice PE implementation: it owns the ESPHome
Native API connections, sessions, TTS, LED ring and media player. NAI OS owns
*detection* (its launcher plugin discovers and tracks the device) and must NOT
reimplement device operation. This module is the public contract for live
operation: NAI OS (and any other loopback peer) calls these endpoints and
Toastovač runs them on the existing ``VoicePEManager`` — no duplicated
protocol code anywhere.

Every endpoint is thin: it validates the payload, resolves the device through
the manager's ``device()`` rules (node name, friendly name, MAC, host; empty =
single attached satellite) and calls an existing manager method. Nothing here
talks to the satellite directly.

Security follows the rest of ``tdb/1``: loopback bind + bearer token are
enforced by the HTTP handler before this router runs.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, Optional, Tuple

#: Capability ids advertised in ``bridge.json`` and ``/tdb/v1/capabilities``.
VOICE_PE_CAPABILITIES = (
    "voice-pe.status",
    "voice-pe.discover",
    "voice-pe.announce",
    "voice-pe.playback",
    "voice-pe.volume",
    "voice-pe.led",
    "voice-pe.calibrate",
    "voice-pe.mirror",
    "voice-pe.restart",
)

#: Canonical error codes of the voice-pe section.
ERROR_NO_MANAGER = "VOICE_PE_UNAVAILABLE"
ERROR_NO_DEVICE = "VOICE_PE_DEVICE_UNKNOWN"
ERROR_INVALID = "VOICE_PE_INVALID_REQUEST"

#: How long one operation may run on the manager loop.
_OP_TIMEOUT_S = 20.0

DeviceKey = str


class ManagerUnavailable(RuntimeError):
    """The Voice PE integration is disabled or the manager is missing."""


def _error(code: str, message: str) -> Dict[str, Any]:
    return {"error": {"code": code, "message": message, "retryable": False}}


def _ok(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"ok": True, **payload}


def _device_view(device: Any) -> Dict[str, Any]:
    """One renderable device entry: identity + config + live connection state.

    Only existing manager/device accessors are used — this is an assembly
    view, not a new protocol implementation.
    """
    identity = dict(getattr(device, "identity", {}) or {})
    config = getattr(device, "config", None)
    connection = device.health_snapshot() if hasattr(device, "health_snapshot") else {}
    media = {}
    media_ctrl = getattr(device, "media", None)
    if media_ctrl is not None and hasattr(media_ctrl, "snapshot"):
        media = media_ctrl.snapshot() or {}
    caps = getattr(device, "capabilities", None)
    features = list(caps.names()) if caps is not None and hasattr(caps, "names") else []
    host = str(getattr(config, "host", "")) if config is not None else ""
    port = int(getattr(config, "port", 0) or 0) if config is not None else 0
    name = identity.get("node_name") or identity.get("friendly_name") or host
    return {
        "id": str(identity.get("mac_address") or name),
        "name": str(name),
        "host": host,
        "port": port,
        "macAddress": str(identity.get("mac_address", "")),
        "identity": identity,
        "features": features,
        "connection": connection,
        "media": media,
    }


class VoicePeContract:
    """Router for the ``/tdb/v1/voice-pe/*`` section.

    ``manager`` may be ``None`` (Toastovač without the Voice PE integration):
    the router then answers status with ``enabled: false`` and every
    operation with ``VOICE_PE_UNAVAILABLE`` instead of failing the request.
    """

    def __init__(self, manager: Any = None) -> None:
        self.manager = manager

    @property
    def available(self) -> bool:
        """Whether the Voice PE integration is present in this daemon."""
        return self.manager is not None

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------

    def _await(self, coro: Any) -> Any:
        """Run one coroutine on the manager loop, bounded like the CLI."""
        manager = self.manager
        loop = getattr(manager, "_loop", None)
        if loop is None:
            raise ManagerUnavailable("voice-pe manager loop is not running")
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=_OP_TIMEOUT_S)

    def _require_manager(self) -> Any:
        if self.manager is None:
            raise ManagerUnavailable("voice-pe manager unavailable")
        return self.manager

    def _resolve(self, device: DeviceKey) -> Any:
        manager = self._require_manager()
        found = manager.device(device)
        if found is None:
            raise LookupError("no such device")
        return found

    # ------------------------------------------------------------------
    # GET section
    # ------------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        """Authoritative live state for the NAI OS detection plugin."""
        manager = self.manager
        if manager is None:
            return {"enabled": False, "devices": [], "metrics": {}}
        health = manager.health() if hasattr(manager, "health") else {}
        metrics = manager.metrics() if hasattr(manager, "metrics") else {}
        # ``devices`` is a property on the real manager; accept a method too.
        raw_devices = getattr(manager, "devices", None)
        device_list = raw_devices() if callable(raw_devices) else (raw_devices or [])
        devices = [_device_view(device) for device in device_list]
        return {
            "enabled": bool(getattr(manager, "enabled", False)),
            "devices": devices,
            "metrics": metrics,
            "health": health,
        }

    def devices(self) -> Dict[str, Any]:
        return {"devices": self.status().get("devices", [])}

    def media_state(self, device: DeviceKey = "") -> Dict[str, Any]:
        manager = self._require_manager()
        try:
            found = self._resolve(device)
        except LookupError as err:
            return _error(ERROR_NO_DEVICE, str(err))
        state = manager.media_state(self._key_for(found)) if hasattr(manager, "media_state") else {}
        return {"device": self._key_for(found), "media": state or {}}

    # ------------------------------------------------------------------
    # POST section
    # ------------------------------------------------------------------

    def discover(self) -> Dict[str, Any]:
        manager = self.manager
        if manager is None or not hasattr(manager, "aasync_discover"):
            return _error(ERROR_NO_MANAGER, "voice-pe manager unavailable")
        try:
            found = self._await(manager.aasync_discover())
        except Exception as err:  # noqa: BLE001 - contract boundary
            return _error(ERROR_INVALID, f"discover failed: {err}")
        return {"found": list(found or [])}

    def announce(self, device: DeviceKey, text: str, start_conversation: bool = True) -> Dict[str, Any]:
        if not text or not str(text).strip():
            return _error(ERROR_INVALID, "text is required")
        try:
            found = self._resolve(device)
        except ManagerUnavailable as err:
            return _error(ERROR_NO_MANAGER, str(err))
        except LookupError as err:
            return _error(ERROR_NO_DEVICE, str(err))
        manager = self.manager
        try:
            ok = self._await(manager.announce(
                self._key_for(found), str(text),
                start_conversation=bool(start_conversation),
            ))
        except Exception as err:  # noqa: BLE001
            return _error(ERROR_INVALID, f"announce failed: {err}")
        return _ok({"device": self._key_for(found), "announced": bool(ok)})

    def play(self, device: DeviceKey, url: str) -> Dict[str, Any]:
        if not url or "://" not in str(url):
            return _error(ERROR_INVALID, "url is required")
        return self._run_media("play", device, url=url)

    def pause(self, device: DeviceKey) -> Dict[str, Any]:
        return self._run_media("pause", device)

    def resume(self, device: DeviceKey) -> Dict[str, Any]:
        return self._run_media("resume", device)

    def stop(self, device: DeviceKey) -> Dict[str, Any]:
        return self._run_media("stop", device)

    def volume(self, device: DeviceKey, volume: float) -> Dict[str, Any]:
        try:
            value = float(volume)
        except (TypeError, ValueError):
            return _error(ERROR_INVALID, "volume must be a number")
        if not 0.0 <= value <= 1.0:
            return _error(ERROR_INVALID, "volume must be between 0 and 1")
        return self._run_media("volume", device, volume=value)

    def mute(self, device: DeviceKey, muted: bool) -> Dict[str, Any]:
        return self._run_media("mute", device, muted=bool(muted))

    def led(self, device: DeviceKey, rgb=None, brightness=None) -> Dict[str, Any]:
        manager = self._require_manager()
        if not hasattr(manager, "set_led"):
            return _error(ERROR_NO_MANAGER, "voice-pe manager unavailable")
        try:
            found = self._resolve(device)
        except LookupError as err:
            return _error(ERROR_NO_DEVICE, str(err))
        try:
            ok = self._await(manager.set_led(self._key_for(found), rgb, brightness))
        except Exception as err:  # noqa: BLE001
            return _error(ERROR_INVALID, f"led failed: {err}")
        return _ok({"device": self._key_for(found), "led": bool(ok)})

    def calibrate(self, device: DeviceKey) -> Dict[str, Any]:
        """Audio calibration probe built from existing operations only.

        Speaks a short calibration line and pulses the LED ring, both through
        the manager's own announce/set_led — no new satellite protocol code.
        """
        try:
            found = self._resolve(device)
        except ManagerUnavailable as err:
            return _error(ERROR_NO_MANAGER, str(err))
        except LookupError as err:
            return _error(ERROR_NO_DEVICE, str(err))
        key = self._key_for(found)
        manager = self.manager
        results: Dict[str, Any] = {"device": key}
        try:
            results["announced"] = bool(self._await(manager.announce(
                key, "Calibration test", start_conversation=False,
            )))
        except Exception as err:  # noqa: BLE001
            results["announced"] = False
            results["announceError"] = str(err)
        try:
            results["led"] = bool(self._await(manager.set_led(key, (0.55, 0.0, 1.0), 0.66)))
        except Exception as err:  # noqa: BLE001
            results["led"] = False
            results["ledError"] = str(err)
        return _ok(results)

    def mirror(self, text: str) -> Dict[str, Any]:
        manager = self.manager
        if manager is None or not hasattr(manager, "mirror_local"):
            return _error(ERROR_NO_MANAGER, "voice-pe manager unavailable")
        if not text or not str(text).strip():
            return _error(ERROR_INVALID, "text is required")
        try:
            count = int(manager.mirror_local(str(text)))
        except Exception as err:  # noqa: BLE001
            return _error(ERROR_INVALID, f"mirror failed: {err}")
        return _ok({"satellites": count})

    def restart(self) -> Dict[str, Any]:
        """Restart all managed connections (the reconnect path).

        Toastovač's manager owns the reconnect logic; NAI OS just asks for a
        fresh connection cycle and then re-reads status.
        """
        manager = self._require_manager()
        try:
            manager.restart()
        except Exception as err:  # noqa: BLE001
            return _error(ERROR_INVALID, f"restart failed: {err}")
        return _ok({"restarted": True})

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    #: Contract operation -> VoicePEManager method name.
    _MEDIA_OPS: Dict[str, str] = {
        "play": "play",
        "pause": "pause_media",
        "resume": "resume_media",
        "stop": "stop_media",
        "volume": "set_volume",
        "mute": "set_muted",
    }

    def _run_media(self, op: str, device: DeviceKey, **kwargs: Any) -> Dict[str, Any]:
        manager = self._require_manager()
        method_name = self._MEDIA_OPS.get(op)
        if method_name is None or not hasattr(manager, method_name):
            return _error(ERROR_NO_MANAGER, "voice-pe manager unavailable")
        try:
            found = self._resolve(device)
        except LookupError as err:
            return _error(ERROR_NO_DEVICE, str(err))
        key = self._key_for(found)
        coro = getattr(manager, method_name)(key, **kwargs)
        try:
            ok = self._await(coro)
        except Exception as err:  # noqa: BLE001
            return _error(ERROR_INVALID, f"{op} failed: {err}")
        return _ok({"device": key, "ok": bool(ok)})

    @staticmethod
    def _key_for(device: Any) -> str:
        identity = getattr(device, "identity", {}) or {}
        host = getattr(getattr(device, "config", None), "host", "")
        return str(identity.get("mac_address") or identity.get("node_name") or host or "")


#: Route table used by the tdb HTTP handler: method -> (prefix, handler).
#: Every handler receives ``(contract, body, query)``; GET handlers ignore
#: ``body``, POST handlers ignore ``query``.
GET_HANDLERS: Dict[str, Callable[..., Dict[str, Any]]] = {
    "status": lambda c, b, q: c.status(),
    "devices": lambda c, b, q: c.devices(),
    "media-state": lambda c, b, q: c.media_state((q.get("device") or [""])[0]),
}

POST_HANDLERS: Dict[str, Callable[..., Dict[str, Any]]] = {
    "discover": lambda c, b, q: c.discover(),
    "announce": lambda c, b, q: c.announce(
        str(b.get("device") or ""), str(b.get("text") or ""),
        start_conversation=bool(b.get("startConversation", True)),
    ),
    "play": lambda c, b, q: c.play(str(b.get("device") or ""), str(b.get("url") or "")),
    "pause": lambda c, b, q: c.pause(str(b.get("device") or "")),
    "resume": lambda c, b, q: c.resume(str(b.get("device") or "")),
    "stop": lambda c, b, q: c.stop(str(b.get("device") or "")),
    "volume": lambda c, b, q: c.volume(str(b.get("device") or ""), b.get("volume", 0)),
    "mute": lambda c, b, q: c.mute(str(b.get("device") or ""), b.get("muted", False)),
    "led": lambda c, b, q: c.led(
        str(b.get("device") or ""), b.get("rgb"), b.get("brightness"),
    ),
    "calibrate": lambda c, b, q: c.calibrate(str(b.get("device") or "")),
    "mirror": lambda c, b, q: c.mirror(str(b.get("text") or "")),
    "restart": lambda c, b, q: c.restart(),
}


def handle_voice_pe(
    method: str,
    action: str,
    body: Dict[str, Any],
    query: Dict[str, list],
    contract: VoicePeContract,
) -> Optional[Dict[str, Any]]:
    """Dispatch one ``/tdb/v1/voice-pe/{action}`` request.

    Returns ``None`` when ``action`` is not part of the contract (the caller
    then answers 404 with its generic route error). Errors are returned as
    ``{"error": {...}}`` payloads so the HTTP layer can pick the status code.
    """
    if method not in ("GET", "POST"):
        return None
    table = GET_HANDLERS if method == "GET" else POST_HANDLERS
    handler = table.get(action)
    if handler is None:
        return None
    try:
        return handler(contract, body, query)
    except Exception as err:  # noqa: BLE001 - contract boundary
        return _error(ERROR_INVALID, str(err))


__all__ = [
    "ERROR_INVALID",
    "ERROR_NO_DEVICE",
    "ERROR_NO_MANAGER",
    "VOICE_PE_CAPABILITIES",
    "VoicePeContract",
    "handle_voice_pe",
]