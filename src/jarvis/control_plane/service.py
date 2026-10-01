"""ControlPlane facade: the Toastovač desktop control plane.

Owns the registries (pairing, browser sessions, apps, MCP), the event
bus, the HTTP transport, and the voice-dispatch path. The daemon starts
one instance when ``control_plane_enabled`` is set and hands it the TTS
engine so the browser can ask Toastovač to speak and the voice path can
speak dispatch confirmations.

Everything here is transport-neutral: HTTP is just one
``LocalProtocolTransport`` implementation.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from ..debug import debug_log
from .apps import AppRegistry
from .capabilities import CapabilityManifestBuilder
from .events import EventBus
from .mcp_registry import McpRuntimeRegistry, STATE_RUNNING
from .pairing import PairingStore
from .protocol import (
    ErrorCode,
    PROTOCOL_VERSION,
    ProtocolError,
    make_error,
    make_result,
    parse_envelope,
)
from .router import ContinuityCache, V271_MARKER, VoiceRouteResolver
from .sessions import BrowserSessionRegistry
from .transport import LocalHttpTransport, LocalProtocolTransport

_DEFAULT_DANGEROUS_PATTERNS = (
    "exec",
    "shell",
    "bash",
    "cmd",
    "powershell",
    "command",
    "kill",
    "shutdown",
    "reboot",
    "format",
    "delete",
    "remove",
    "drop",
    "truncate",
    "rm ",
    "rmdir",
    "del ",
)


class ControlPlane:
    """Local desktop control plane (v271-local/1)."""

    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg
        pairing_store = str(
            getattr(cfg, "control_plane_pairing_store", "") or ""
        ).strip()
        if not pairing_store:
            pairing_store = _default_pairing_store_path()
        self.pairings = PairingStore(pairing_store)
        ttl = float(getattr(cfg, "control_plane_session_ttl_sec", 120.0) or 120.0)
        self.sessions = BrowserSessionRegistry(session_ttl_sec=ttl)
        mcps = getattr(cfg, "mcps", {}) or {}
        apps_raw = getattr(cfg, "control_plane_apps", None) or []
        self.apps = AppRegistry(apps_raw, mcps)
        self.mcp = McpRuntimeRegistry(mcps)
        continuity_ttl = float(
            getattr(cfg, "control_plane_continuity_ttl_sec", 600.0) or 600.0
        )
        self.continuity = ContinuityCache(ttl_sec=continuity_ttl)
        dangerous = getattr(cfg, "control_plane_dangerous_tool_patterns", None)
        self.manifest = CapabilityManifestBuilder(
            self.apps,
            self.mcp,
            self.sessions,
            self.pairings,
            dangerous_patterns=(
                list(dangerous) if isinstance(dangerous, list) and dangerous
                else list(_DEFAULT_DANGEROUS_PATTERNS)
            ),
        )
        self.events = EventBus()
        #: Optional TTS speak callable, injected by the daemon:
        #: ``speak(text, language=...)``. ``None`` disables speech.
        self.speak: Optional[Callable[..., None]] = None
        self.router = VoiceRouteResolver(
            self.apps,
            self.sessions,
            self.continuity,
            enabled=bool(getattr(cfg, "control_plane_voice_routing", True)),
        )
        host = str(getattr(cfg, "control_plane_host", "127.0.0.1") or "127.0.0.1")
        raw_port = getattr(cfg, "control_plane_port", 27121)
        try:
            port = int(raw_port) if raw_port is not None else 27121
        except (TypeError, ValueError):
            port = 27121
        # 0 = ephemeral port (tests); the config loader already rejects
        # non-positive ports so production config always pins a real one.
        origins = getattr(cfg, "control_plane_allowed_origins", None)
        self.transport: LocalProtocolTransport = LocalHttpTransport(
            self,
            host=host,
            port=port,
            allowed_origins=(
                list(origins) if isinstance(origins, list) else []
            ),
        )
        self._started = False

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        """Start the transport and seed MCP schemas from the daemon cache."""
        if self._started:
            return
        self._started = True
        try:
            from ..tools.registry import get_cached_mcp_tools  # noqa: PLC0415

            self.mcp.seed_from_daemon_cache(get_cached_mcp_tools())
        except Exception as exc:  # noqa: BLE001
            debug_log(
                f"control plane MCP cache seed skipped: {exc}", "control_plane"
            )
        self.transport.start()

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        self.transport.stop()

    # -- REST bindings --------------------------------------------------

    def hello(self) -> Dict[str, Any]:
        return {
            "protocol": PROTOCOL_VERSION,
            "hostId": self.pairings.host_id,
            "transports": ["http"],
            "port": self.transport.port,
            "endpoints": [
                "/desktop/v1/hello",
                "/desktop/v1/pair",
                "/desktop/v1/session/register",
                "/desktop/v1/session/unregister",
                "/desktop/v1/session/continuity",
                "/desktop/v1/capabilities",
                "/desktop/v1/apps",
                "/desktop/v1/apps/{appId}",
                "/desktop/v1/apps/{appId}/mcp/start",
                "/desktop/v1/apps/{appId}/mcp/stop",
                "/desktop/v1/apps/{appId}/mcp/restart",
                "/desktop/v1/apps/{appId}/mcp/tools",
                "/desktop/v1/tool/call",
                "/desktop/v1/message",
                "/desktop/v1/events",
                "/desktop/v1/voice/say",
            ],
        }

    def pair(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        pairing = self.pairings.get_or_create_pairing(user_id)
        return {
            "hostId": pairing["hostId"],
            "pairingId": pairing["pairingId"],
            "secret": pairing["secret"],
        }

    def register_session(self, pairing: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
        tab_session_id = _require_text(body, "tabSessionId")
        user_id = _optional_text(body.get("userId")) or ""
        foreground = bool(body.get("foreground", False))
        workspace_id = _optional_text(body.get("workspaceId"))
        session = self.sessions.register(
            tab_session_id,
            user_id,
            pairing["pairingId"],
            foreground=foreground,
            workspace_id=workspace_id,
        )
        return {
            "tabSessionId": session.tab_session_id,
            "userId": session.user_id,
            "foreground": session.foreground,
            "workspaceId": session.workspace_id,
            "lastSeenAt": session.last_seen_at,
            "registered": True,
        }

    def unregister_session(self, pairing: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
        tab_session_id = _require_text(body, "tabSessionId")
        self.sessions.unregister(tab_session_id, pairing["pairingId"])
        return {"tabSessionId": tab_session_id, "unregistered": True}

    def report_continuity(self, pairing: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
        tab_session_id = _require_text(body, "tabSessionId")
        session = self._require_session(pairing, tab_session_id)
        app_id = _optional_text(body.get("appId"))
        agent_id = _optional_text(body.get("agentId"))
        chat_id = _optional_text(body.get("chatId"))
        if app_id is not None and self.apps.get(app_id) is None:
            raise ProtocolError(ErrorCode.APP_UNKNOWN, f"Unknown app {app_id!r}")
        self.continuity.update(app_id, agent_id, chat_id)
        self.sessions.touch(tab_session_id, pairing["pairingId"])
        return {
            "tabSessionId": session.tab_session_id,
            "appId": app_id,
            "agentId": agent_id,
            "chatId": chat_id,
            "stored": True,
        }

    def capabilities(self) -> Dict[str, Any]:
        return self.manifest.build()

    def list_apps(self) -> List[Dict[str, Any]]:
        return [app.to_dict() for app in self.apps.all()]

    def get_app(self, app_id: str) -> Dict[str, Any]:
        app = self.apps.get(app_id)
        if app is None:
            raise ProtocolError(ErrorCode.APP_UNKNOWN, f"Unknown app {app_id!r}")
        return app.to_dict()

    def _app_require_mcp(self, app_id: str):
        app = self.apps.get(app_id)
        if app is None:
            raise ProtocolError(ErrorCode.APP_UNKNOWN, f"Unknown app {app_id!r}")
        if not app.mcp_server_id:
            raise ProtocolError(
                ErrorCode.MCP_UNAVAILABLE, f"App {app_id!r} has no MCP server"
            )
        if not self.apps.has_mcp(app):
            raise ProtocolError(
                ErrorCode.MCP_UNAVAILABLE,
                f"MCP server {app.mcp_server_id!r} is not configured",
            )
        return app

    def mcp_start(self, app_id: str) -> Dict[str, Any]:
        app = self._app_require_mcp(app_id)
        status = self.mcp.start(app.mcp_server_id)
        return _status_payload(app, status)

    def mcp_stop(self, app_id: str) -> Dict[str, Any]:
        app = self._app_require_mcp(app_id)
        status = self.mcp.stop(app.mcp_server_id)
        return _status_payload(app, status)

    def mcp_restart(self, app_id: str) -> Dict[str, Any]:
        app = self._app_require_mcp(app_id)
        status = self.mcp.restart(app.mcp_server_id)
        return _status_payload(app, status)

    def mcp_tools(self, app_id: str) -> Dict[str, Any]:
        app = self._app_require_mcp(app_id)
        tools = self.mcp.tools(app.mcp_server_id, refresh=True)
        status = self.mcp.status(app.mcp_server_id)
        return {
            "appId": app.app_id,
            "serverId": app.mcp_server_id,
            "state": status.state,
            "schemaHash": status.schema_hash,
            "tools": [
                {
                    "name": f"{app.tool_namespace}.{t['name']}"
                    if app.tool_namespace
                    else t["name"],
                    "description": t.get("description"),
                    "inputSchema": t.get("inputSchema"),
                }
                for t in tools
            ],
        }

    # -- tool execution -------------------------------------------------

    def tool_call(self, pairing: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
        """The §13 validation chain, then the real MCP call."""
        tab_session_id = _require_text(body, "tabSessionId")
        app_id = _require_text(body, "appId")
        tool_call_id = _require_text(body, "toolCallId")
        tool_name = _require_text(body, "toolName")
        arguments = body.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST, "'arguments' must be an object"
            )

        # 1. paired browser session
        self._require_session(pairing, tab_session_id)

        # 2. registered app
        app = self.apps.get(app_id)
        if app is None:
            raise ProtocolError(ErrorCode.APP_UNKNOWN, f"Unknown app {app_id!r}")

        # 3. registered MCP
        if not app.mcp_server_id or not self.apps.has_mcp(app):
            raise ProtocolError(
                ErrorCode.MCP_UNAVAILABLE,
                f"App {app_id!r} has no configured MCP server",
            )

        # 5. namespace matches app (checked before schema so a wrong
        #    namespace never triggers a server round trip)
        if not self.apps.qualifies_tool(app, tool_name):
            raise ProtocolError(
                ErrorCode.TOOL_NAMESPACE_MISMATCH,
                f"Tool {tool_name!r} is not in the {app.tool_namespace!r} namespace",
            )
        server_tool = tool_name[len(f"{app.tool_namespace}."):]

        # 4. tool exists in the current schema
        state = self.mcp.status(app.mcp_server_id).state
        if state != STATE_RUNNING:
            raise ProtocolError(
                ErrorCode.MCP_NOT_RUNNING,
                f"MCP server {app.mcp_server_id!r} is not running",
            )
        schema = self.mcp.tool_names(app.mcp_server_id)
        if server_tool not in schema:
            # One lazy refresh: the daemon may have started the server
            # after our last schema snapshot.
            schema = self.mcp.tool_names(app.mcp_server_id, refresh=True)
            if server_tool not in schema:
                raise ProtocolError(
                    ErrorCode.TOOL_UNKNOWN,
                    f"Tool {tool_name!r} is not in the current schema",
                )

        result = self.mcp.invoke(app.mcp_server_id, server_tool, arguments)
        return {
            "toolCallId": tool_call_id,
            "appId": app_id,
            "toolName": tool_name,
            "ok": not bool(result.get("isError", False)),
            "result": result,
        }

    # -- envelope endpoint ----------------------------------------------

    def handle_message(
        self, raw: Any, pairing: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Process one ``v271-local/1`` envelope; always returns an
        envelope (result or error). Never raises for contract problems."""
        try:
            envelope = parse_envelope(raw)
            msg_id = envelope["id"]
            msg_type = envelope["type"]
            session_id = envelope.get("sessionId")
            payload = envelope["payload"]
            if msg_type == "hello":
                return make_result(
                    "hello", self.hello(), msg_id=msg_id, session_id=session_id
                )
            if msg_type == "pair":
                result = self.pair(_optional_text(payload.get("userId")))
                return make_result(
                    "pair", result, msg_id=msg_id, session_id=session_id
                )
            if pairing is None:
                raise ProtocolError(
                    ErrorCode.PAIRING_REQUIRED,
                    "This primitive requires a paired session",
                )
            if msg_type == "session.register":
                result = self.register_session(pairing, payload)
                return make_result(
                    "session.register",
                    result,
                    msg_id=msg_id,
                    session_id=session_id,
                )
            if msg_type == "session.unregister":
                result = self.unregister_session(pairing, payload)
                return make_result(
                    "session.unregister",
                    result,
                    msg_id=msg_id,
                    session_id=session_id,
                )
            if msg_type == "session.continuity":
                result = self.report_continuity(pairing, payload)
                return make_result(
                    "session.continuity",
                    result,
                    msg_id=msg_id,
                    session_id=session_id,
                )
            if msg_type == "capabilities.get":
                return make_result(
                    "capabilities.get",
                    self.capabilities(),
                    msg_id=msg_id,
                    session_id=session_id,
                )
            if msg_type == "tool.call":
                result = self.tool_call(pairing, payload)
                # Spec §4: a tool.call is answered with type tool.result.
                return make_result(
                    "tool.result",
                    result,
                    msg_id=msg_id,
                    session_id=session_id,
                )
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST, f"Unhandled primitive {msg_type!r}"
            )
        except ProtocolError as exc:
            return make_error(exc, msg_id=_envelope_id_or_none(raw))
        except Exception as exc:  # noqa: BLE001
            debug_log(f"control plane message error: {exc}", "control_plane")
            return make_error(
                ProtocolError(
                    ErrorCode.INTERNAL_ERROR, "Internal control plane error"
                ),
                msg_id=_envelope_id_or_none(raw),
            )

    # -- voice ----------------------------------------------------------

    def handle_voice_query(
        self,
        text: str,
        *,
        tts: Any = None,
        language: Optional[str] = None,
    ) -> bool:
        """Route one final-ASR utterance to the paired v271 browser.

        Returns True when the utterance was handled by the control plane
        (dispatch sent, or an unavailable error spoken); False when the
        local reply engine should handle it. Called from
        ``VoiceListener._dispatch_query``.
        """
        if not self.router.enabled:
            return False
        route = self.router.resolve(text)
        if not route.routed:
            return False
        session = self.router.pick_session()
        speak = tts if tts is not None else self.speak
        if session is None:
            message = _unavailable_message(language)
            debug_log(
                f"control plane: voice route {route.reason} but no browser "
                f"session ({ErrorCode.V271_BROWSER_UNAVAILABLE})",
                "control_plane",
            )
            if speak is not None:
                try:
                    speak(message, language=language)
                except Exception as exc:  # noqa: BLE001
                    debug_log(
                        f"control plane unavailable speech failed: {exc}",
                        "control_plane",
                    )
            return True

        entry = self.continuity.get(route.app_id)
        payload: Dict[str, Any] = {
            "origin": "toastovac-voice",
            "appId": route.app_id,
            "agentId": entry.agent_id if entry else None,
            "chatId": entry.last_chat_id if entry else None,
            "text": text,
            "replyMode": "voice-and-ui",
        }
        self.events.publish(session.tab_session_id, "dispatch", payload)
        # Thread continuity: remember this target so follow-ups reuse it.
        self.continuity.update(
            route.app_id,
            agent_id=entry.agent_id if entry else None,
            chat_id=entry.last_chat_id if entry else None,
        )
        debug_log(
            f"control plane: dispatched voice query to tab "
            f"{session.tab_session_id} ({route.reason})",
            "control_plane",
        )
        confirmation = _confirmation_message(
            route.app_display_name, route.generic_v271, language
        )
        if speak is not None:
            try:
                speak(confirmation, language=language)
            except Exception as exc:  # noqa: BLE001
                debug_log(
                    f"control plane confirmation speech failed: {exc}",
                    "control_plane",
                )
        return True

    def speak_text(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Browser → Toastovač TTS (optional reply, step 11 of the P0
        scenario). Additive endpoint."""
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST, "'text' must be a non-empty string"
            )
        language = _optional_text(body.get("language"))
        spoken = False
        if self.speak is not None:
            try:
                self.speak(text, language=language)
                spoken = True
            except Exception as exc:  # noqa: BLE001
                debug_log(f"control plane voice/say failed: {exc}", "control_plane")
        return {"ok": True, "spoken": spoken, "text": text}

    # -- internals ------------------------------------------------------

    def _require_session(
        self, pairing: Dict[str, Any], tab_session_id: str
    ) -> Any:
        session = self.sessions.get_for_pairing(tab_session_id, pairing["pairingId"])
        if session is not None:
            return session
        # Distinguish "never seen" from "expired" without leaking which
        # session belongs to which pairing.
        if self.sessions.get(tab_session_id) is None and self.sessions.exists(
            tab_session_id
        ):
            raise ProtocolError(
                ErrorCode.SESSION_EXPIRED, "Tab session has expired"
            )
        raise ProtocolError(
            ErrorCode.SESSION_UNKNOWN, "Tab session is not registered"
        )


def _status_payload(app: Any, status: Any) -> Dict[str, Any]:
    return {
        "appId": app.app_id,
        "serverId": app.mcp_server_id,
        "state": status.state,
        "lastError": status.last_error,
        "schemaHash": status.schema_hash,
        "pid": status.pid,
        "toolCount": len(status.tools),
    }


def _require_text(body: Dict[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError(
            ErrorCode.INVALID_REQUEST, f"'{key}' must be a non-empty string"
        )
    return value.strip()


def _optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _envelope_id_or_none(raw: Any) -> Optional[str]:
    if isinstance(raw, dict) and isinstance(raw.get("id"), str):
        return raw["id"]
    return None


def _confirmation_message(
    app_display_name: Optional[str], generic: bool, language: Optional[str]
) -> str:
    target = app_display_name or "v271"
    lang = (language or "").lower()
    if lang.startswith("cs"):
        return f"Posílám do {target}."
    if lang.startswith("en"):
        return f"Sending to {target}."
    return f"→ {target}"


def _unavailable_message(language: Optional[str]) -> str:
    lang = (language or "").lower()
    if lang.startswith("cs"):
        return "Nemůžu odeslat otázku, v271 není dostupné."
    if lang.startswith("en"):
        return "Cannot send the question, v271 is not available."
    return "v271 is not available."


def _default_pairing_store_path() -> str:
    from ..config import default_config_path  # noqa: PLC0415

    return str(default_config_path().parent / "control_plane_pairings.json")