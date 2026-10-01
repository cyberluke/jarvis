"""CastGateway — façade over the existing CastManager / CastSenderService.

Toastovač remains the only Google Cast owner. This module normalizes
sessions, devices, and errors for the Toaster Desktop Bridge (tdb/1).
It does not reimplement mDNS, Cast sockets, or the YouTube wire protocol.
"""

from __future__ import annotations

import secrets
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from ..debug import debug_log
from .cast_manager import CastManager, CastMode, ensure_manager

PROTOCOL = "tdb/1"

STATES = (
    "IDLE",
    "DISCOVERING",
    "CONNECTING",
    "LAUNCHING",
    "BUFFERING",
    "PLAYING",
    "PAUSED",
    "STOPPED",
    "ENDED",
    "ERROR",
    "DISCONNECTED",
)

ERROR_CODES = {
    "TOASTER_OFFLINE",
    "CAST_UNAVAILABLE",
    "NO_CAST_DEVICES",
    "DEVICE_OFFLINE",
    "SESSION_LAUNCH_FAILED",
    "RECEIVER_UNAVAILABLE",
    "MEDIA_LOAD_FAILED",
    "CAST_PROTOCOL_ERROR",
    "SESSION_LOST",
    "UNSUPPORTED_MEDIA",
    "AUTH_FAILED",
}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def error_body(code: str, message: str, retryable: bool = True) -> Dict[str, Any]:
    return {
        "error": {
            "code": code if code in ERROR_CODES else "CAST_PROTOCOL_ERROR",
            "message": message,
            "retryable": retryable,
        }
    }


def validate_intent(intent: Dict[str, Any]) -> Optional[str]:
    if not isinstance(intent, dict):
        return "intent must be an object"
    source = str(intent.get("source") or "")
    kind = str(intent.get("kind") or "")
    video_id = str(intent.get("videoId") or intent.get("video_id") or "")
    url = str(intent.get("canonicalUrl") or intent.get("canonical_url") or "")
    if source != "youtube":
        return "unsupported source"
    if kind not in ("video", "short"):
        return "kind must be video or short"
    if not video_id or not video_id.replace("-", "").replace("_", "").isalnum():
        return "invalid videoId"
    if url and ("youtube.com" not in url and "youtu.be" not in url):
        return "canonicalUrl must be a YouTube URL"
    return None


def _player_state_to_tdb(raw: Optional[str]) -> str:
    value = (raw or "").upper()
    mapping = {
        "PLAYING": "PLAYING",
        "PAUSED": "PAUSED",
        "BUFFERING": "BUFFERING",
        "IDLE": "STOPPED",
        "UNKNOWN": "CONNECTING",
    }
    return mapping.get(value, "LAUNCHING" if value else "IDLE")


class CastGateway:
    """Single façade consumed by TDB. Wraps CastManager; does not own Cast sockets."""

    def __init__(self, cfg: Any = None) -> None:
        self._cfg = cfg
        self._lock = threading.RLock()
        self._manager: CastManager = ensure_manager(cfg)
        self._session_id: Optional[str] = None
        self._state = "IDLE"
        self._device: Optional[Dict[str, Any]] = None
        self._media: Optional[Dict[str, Any]] = None
        self._devices: List[Dict[str, Any]] = []
        self._event_id = 0
        self._events: List[Dict[str, Any]] = []
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []

    def subscribe(self, listener: Callable[[Dict[str, Any]], None]) -> None:
        with self._lock:
            self._listeners.append(listener)

    def emit(self, event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self._event_id += 1
            envelope = {
                "protocol": PROTOCOL,
                "eventId": self._event_id,
                "type": event_type,
                "timestamp": _now_iso(),
                "payload": payload,
            }
            self._events.append(envelope)
            if len(self._events) > 256:
                self._events = self._events[-256:]
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(envelope)
            except Exception as exc:
                debug_log(f"TDB emit listener failed: {exc}", "cast")
        debug_log(f"CAST_STATE type={event_type}", "cast")
        return envelope

    def events_since(self, since: int) -> List[Dict[str, Any]]:
        with self._lock:
            return [item for item in self._events if int(item.get("eventId") or 0) > since]

    def capabilities(self) -> Dict[str, Any]:
        receiver_mode = str(getattr(self._cfg, "cast_receiver_mode", "existing") or "existing")
        app_id = str(getattr(self._cfg, "cast_receiver_app_id", "") or "")
        return {
            "protocol": PROTOCOL,
            "product": "toastovac",
            "version": "tdb/1",
            "cast": {
                "available": True,
                "discovery": True,
                "youtube": True,
                "shorts": True,
                "receiver": {
                    "mode": receiver_mode,
                    "appIdConfigured": bool(app_id),
                    "appId": app_id or None,
                    "url": str(getattr(self._cfg, "cast_receiver_url", "") or "") or None,
                },
                "commands": ["play", "pause", "resume", "seek", "stop", "volume", "mute"],
            },
        }

    def discover(self, timeout: float = 5.0) -> List[Dict[str, Any]]:
        self._state = "DISCOVERING"
        self.emit("cast.device.updated", {"state": "DISCOVERING"})
        debug_log("CAST_DISCOVERY_STARTED", "cast")
        raw = self._manager.discover_devices()
        devices = []
        for item in raw:
            device_id = str(item.get("uuid") or item.get("name") or secrets.token_hex(4))
            devices.append({
                "id": device_id,
                "name": item.get("name") or "Cast device",
                "host": item.get("host") or "",
                "port": int(item.get("port") or 8009),
                "model": item.get("model") or "Chromecast",
                "online": True,
                "lastSeenAt": _now_iso(),
                "capabilities": ["video", "audio"],
            })
            debug_log(f"CAST_DEVICE_FOUND {device_id}", "cast")
            self.emit("cast.device.added", {"device": devices[-1]})
        with self._lock:
            self._devices = devices
            if self._state == "DISCOVERING":
                self._state = "IDLE"
        return devices

    def devices(self) -> List[Dict[str, Any]]:
        with self._lock:
            if not self._devices:
                return self.discover()
            return list(self._devices)

    def _resolve_device(self, device_id: str) -> Optional[Dict[str, Any]]:
        for device in self.devices():
            if device["id"] == device_id or device["name"] == device_id:
                return device
        return None

    def create_session(self, device_id: str, intent: Dict[str, Any]) -> Dict[str, Any]:
        problem = validate_intent(intent)
        if problem:
            self._state = "ERROR"
            return error_body("UNSUPPORTED_MEDIA", problem, False)
        device = self._resolve_device(device_id)
        if device is None:
            return error_body("DEVICE_OFFLINE", "Cast device not found")
        video_id = str(intent.get("videoId") or intent.get("video_id") or "")
        kind = str(intent.get("kind") or "video")
        session_id = f"cast-{secrets.token_hex(6)}"
        with self._lock:
            self._session_id = session_id
            self._state = "LAUNCHING"
            self._device = device
            self._media = {
                "source": "youtube",
                "kind": kind,
                "videoId": video_id,
                "title": intent.get("title") or video_id,
                "thumbnailUrl": intent.get("thumbnailUrl") or intent.get("thumbnail_url") or "",
            }
        self.emit("cast.session.created", {"sessionId": session_id, "deviceId": device["id"]})
        debug_log(f"CAST_SESSION_CREATE {session_id} device={device['id']}", "cast")
        debug_log("CAST_RECEIVER_LAUNCH youtube", "cast")
        ok = self._manager.start_session(
            CastMode.YOUTUBE,
            device_name=device["name"],
            video_id=video_id,
        )
        if not ok:
            with self._lock:
                self._state = "ERROR"
            self.emit("cast.session.error", {"sessionId": session_id, "code": "SESSION_LAUNCH_FAILED"})
            return error_body("SESSION_LAUNCH_FAILED", f"Could not launch receiver on {device['name']}.")
        with self._lock:
            self._state = "BUFFERING"
        debug_log(f"CAST_MEDIA_LOAD youtube:{video_id}", "cast")
        self.emit("cast.session.state", {"sessionId": session_id, "state": "BUFFERING"})
        self.emit("cast.session.media", {"sessionId": session_id, "media": self._media})
        return self.snapshot()

    def load(self, session_id: str, intent: Dict[str, Any]) -> Dict[str, Any]:
        if not self._session_ok(session_id):
            return error_body("SESSION_LOST", "Unknown Cast session")
        return self.create_session((self._device or {}).get("id") or "", intent)

    def play(self, session_id: str) -> Dict[str, Any]:
        if not self._session_ok(session_id):
            return error_body("SESSION_LOST", "Unknown Cast session")
        debug_log("CAST_COMMAND play", "cast")
        if not self._manager.resume():
            return error_body("CAST_PROTOCOL_ERROR", "play failed")
        self._state = "PLAYING"
        self.emit("cast.session.state", {"sessionId": session_id, "state": "PLAYING"})
        return self.snapshot()

    def pause(self, session_id: str) -> Dict[str, Any]:
        if not self._session_ok(session_id):
            return error_body("SESSION_LOST", "Unknown Cast session")
        debug_log("CAST_COMMAND pause", "cast")
        if not self._manager.pause():
            return error_body("CAST_PROTOCOL_ERROR", "pause failed")
        self._state = "PAUSED"
        self.emit("cast.session.state", {"sessionId": session_id, "state": "PAUSED"})
        return self.snapshot()

    def seek(self, session_id: str, position_ms: int) -> Dict[str, Any]:
        if not self._session_ok(session_id):
            return error_body("SESSION_LOST", "Unknown Cast session")
        debug_log(f"CAST_COMMAND seek {position_ms}", "cast")
        if not self._manager.seek(max(0, position_ms) / 1000.0):
            return error_body("CAST_PROTOCOL_ERROR", "seek failed")
        self.emit("cast.session.position", {"sessionId": session_id, "positionMs": position_ms})
        return self.snapshot()

    def stop(self, session_id: str) -> Dict[str, Any]:
        if session_id and not self._session_ok(session_id):
            return error_body("SESSION_LOST", "Unknown Cast session")
        debug_log("CAST_SESSION_END stop", "cast")
        self._manager.stop_session()
        with self._lock:
            ended = self._session_id
            self._session_id = None
            self._state = "STOPPED"
            self._media = None
        self.emit("cast.session.ended", {"sessionId": ended, "state": "STOPPED"})
        return self.snapshot()

    def set_volume(self, session_id: str, volume: float, muted: Optional[bool] = None) -> Dict[str, Any]:
        if not self._session_ok(session_id):
            return error_body("SESSION_LOST", "Unknown Cast session")
        level = 0.0 if muted else max(0.0, min(1.0, float(volume)))
        debug_log(f"CAST_COMMAND volume={level}", "cast")
        if not self._manager.set_volume(level):
            return error_body("CAST_PROTOCOL_ERROR", "volume failed")
        self.emit("cast.session.volume", {"sessionId": session_id, "volume": level, "muted": bool(muted)})
        return self.snapshot()

    def disconnect(self, session_id: Optional[str] = None) -> Dict[str, Any]:
        return self.stop(session_id or self._session_id or "")

    def snapshot(self) -> Dict[str, Any]:
        sender = {}
        try:
            sender = self._manager.status.get("cast_sender") or {}
        except Exception:
            sender = {}
        with self._lock:
            state = self._state
            if sender.get("connected") and sender.get("player_state"):
                state = _player_state_to_tdb(str(sender.get("player_state")))
                self._state = state
            return {
                "protocol": PROTOCOL,
                "sessionId": self._session_id,
                "state": state if self._session_id else "IDLE",
                "device": self._device,
                "media": self._media,
                "positionMs": int(float(sender.get("current_time") or 0) * 1000),
                "durationMs": int(float(sender.get("duration") or 0) * 1000),
                "volume": float(sender.get("volume") or 0),
                "muted": float(sender.get("volume") or 0) <= 0.001,
                "receiverMode": str(getattr(self._cfg, "cast_receiver_mode", "existing") or "existing"),
                "updatedAt": _now_iso(),
            }

    def _session_ok(self, session_id: str) -> bool:
        with self._lock:
            return bool(self._session_id) and self._session_id == session_id


_gateway: Optional[CastGateway] = None


def get_gateway(cfg: Any = None) -> CastGateway:
    global _gateway
    if _gateway is None:
        _gateway = CastGateway(cfg)
    return _gateway
