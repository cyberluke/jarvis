"""Tests for the Toastovač desktop control plane (v271-local/1).

Covers the protocol contract, pairing store, browser session registry,
app registry, MCP runtime registry, capability manifest, voice routing
(including inflection-aware aliases), the full tool-call validation
chain, the HTTP transport (auth, origins, SSE dispatch delivery), and
the voice dispatch flow.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import urllib.error
import urllib.request
from http.client import HTTPConnection
from types import SimpleNamespace

import pytest

from jarvis.control_plane.apps import AppRegistry
from jarvis.control_plane.capabilities import CapabilityManifestBuilder
from jarvis.control_plane.events import EventBus
from jarvis.control_plane.mcp_registry import (
    STATE_RUNNING,
    STATE_STOPPED,
    McpRuntimeRegistry,
)
from jarvis.control_plane.pairing import PairingStore
from jarvis.control_plane.protocol import (
    ErrorCode,
    PROTOCOL_VERSION,
    ProtocolError,
    make_envelope,
    make_error,
    make_result,
    parse_envelope,
)
from jarvis.control_plane.router import (
    ContinuityCache,
    V271_MARKER,
    VoiceRouteResolver,
)
from jarvis.control_plane.service import ControlPlane
from jarvis.control_plane.sessions import BrowserSessionRegistry
from jarvis.tools.external.mcp_runtime import shutdown_runtime


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _teardown_mcp_runtime():
    yield
    try:
        shutdown_runtime()
    except Exception:
        pass


def make_cfg(
    *,
    apps=None,
    mcps=None,
    voice_routing=True,
    pairing_store=None,
    port=0,
    allowed_origins=None,
):
    # Daemon-required fields (the wiring tests boot daemon.main()).
    try:
        from conftest import MockConfig

        _base = MockConfig()
        base = {
            k: getattr(_base, k)
            for k in dir(_base) if not k.startswith("_")
        }
    except Exception:  # pragma: no cover - fallback for standalone runs
        base = {
            "db_path": ":memory:",
            "sqlite_vss_path": None,
            "llm_chat_model": "gemma4-26b",
            "whisper_model": "small",
            "tts_enabled": False,
            "dictation_hotkey": "ctrl+alt+space",
        }
    cfg = dict(base)
    cfg.update(
        mcps=mcps or {},
        control_plane_enabled=True,
        control_plane_host="127.0.0.1",
        control_plane_port=port,
        control_plane_allowed_origins=allowed_origins or ["https://v271.cz"],
        control_plane_session_ttl_sec=120.0,
        control_plane_continuity_ttl_sec=600.0,
        control_plane_pairing_store=pairing_store or "",
        control_plane_apps=apps or [],
        control_plane_voice_routing=voice_routing,
        control_plane_dangerous_tool_patterns=[],
    )
    return SimpleNamespace(**cfg)


BLENDER_APP = {
    "appId": "blender",
    "displayName": "Blender",
    "aliases": ["blender"],
    "processNames": [],
    "mcpServerId": "blender-main",
    "toolNamespace": "blender",
}

BLENDER_MCPS = {
    "blender-main": {
        "command": "python",
        "args": ["-m", "blender_mcp_server"],
        "transport": "stdio",
    }
}


class FakeTTS:
    enabled = True

    def __init__(self):
        self.spoken = []

    def speak(self, text, language=None, **kwargs):
        self.spoken.append((text, language))


class FakeMCPClient:
    """In-process stand-in for MCPClient (list_tools / invoke_tool)."""

    def __init__(self, mcps):
        self.mcps = mcps
        self.invoked = []

    TOOLS = {
        "blender-main": [
            {"name": "get_scene_info", "description": "Current scene", "inputSchema": {}},
            {"name": "create_object", "description": "Create object", "inputSchema": {}},
            {"name": "exec_command", "description": "Run a command in Blender", "inputSchema": {}},
        ],
    }

    def list_tools(self, server_name):
        if server_name not in self.mcps:
            raise ValueError(f"Unknown MCP server '{server_name}'")
        return list(self.TOOLS.get(server_name, []))

    def invoke_tool(self, server_name, tool_name, arguments=None):
        self.invoked.append((server_name, tool_name, arguments))
        return {
            "content": [{"type": "text", "text": f"result:{tool_name}"}],
            "text": f"result:{tool_name}",
            "isError": False,
            "meta": None,
        }


@pytest.fixture
def fake_mcp_client(monkeypatch):
    def _factory(mcps):
        client = FakeMCPClient(mcps)
        monkeypatch.setattr(
            "jarvis.control_plane.mcp_registry.MCPClient",
            lambda _mcps: client,
        )
        return client

    return _factory


# ---------------------------------------------------------------------------
# protocol contract
# ---------------------------------------------------------------------------


class TestProtocol:
    def test_envelope_roundtrip(self):
        env = make_envelope("tool.call", {"a": 1}, msg_id="req-1", session_id="t1")
        assert env["protocol"] == PROTOCOL_VERSION
        assert env["id"] == "req-1"
        assert env["type"] == "tool.call"
        parsed = parse_envelope(env)
        assert parsed["payload"] == {"a": 1}
        assert parsed["sessionId"] == "t1"

    def test_unknown_type_rejected(self):
        with pytest.raises(ProtocolError) as exc:
            parse_envelope({"protocol": PROTOCOL_VERSION, "id": "x", "type": "nope"})
        assert exc.value.code == ErrorCode.INVALID_REQUEST

    def test_outbound_only_types_rejected(self):
        for msg_type in ("dispatch", "tool.result", "event"):
            with pytest.raises(ProtocolError) as exc:
                parse_envelope(
                    {"protocol": PROTOCOL_VERSION, "id": "x", "type": msg_type}
                )
            assert exc.value.code == ErrorCode.INVALID_REQUEST

    def test_unsupported_protocol_rejected(self):
        with pytest.raises(ProtocolError) as exc:
            parse_envelope({"protocol": "v1", "id": "x", "type": "hello"})
        assert exc.value.code == ErrorCode.PROTOCOL_UNSUPPORTED

    def test_error_envelope_shape(self):
        err = ProtocolError(ErrorCode.MCP_UNAVAILABLE, "boom")
        env = make_error(err, msg_id="req-1")
        assert env["ok"] is False
        assert env["type"] == "error"
        assert env["error"] == {
            "code": "MCP_UNAVAILABLE",
            "message": "boom",
            "retryable": True,
        }

    def test_retryable_flags(self):
        assert ProtocolError(ErrorCode.INVALID_REQUEST, "x").retryable is False
        assert ProtocolError(ErrorCode.SESSION_EXPIRED, "x").retryable is True
        assert ProtocolError(ErrorCode.MCP_NOT_RUNNING, "x").retryable is True

    def test_result_envelope_echoes_id(self):
        result = make_result("tool.result", {"ok": True}, msg_id="req-7")
        assert result["id"] == "req-7"
        assert result["ok"] is True
        assert result["type"] == "tool.result"


# ---------------------------------------------------------------------------
# pairing store
# ---------------------------------------------------------------------------


class TestPairingStore:
    def test_create_and_validate(self, tmp_path):
        store = PairingStore(str(tmp_path / "pairings.json"))
        pairing = store.get_or_create_pairing("u1")
        assert pairing["hostId"]
        assert pairing["pairingId"]
        assert len(pairing["secret"]) >= 32
        assert store.host_id == pairing["hostId"]
        assert store.validate_secret(pairing["secret"])["pairingId"] == pairing["pairingId"]

    def test_idempotent_pairing(self, tmp_path):
        store = PairingStore(str(tmp_path / "pairings.json"))
        first = store.get_or_create_pairing()
        second = store.get_or_create_pairing()
        assert first["pairingId"] == second["pairingId"]
        assert first["secret"] == second["secret"]

    def test_persistence_across_instances(self, tmp_path):
        path = str(tmp_path / "pairings.json")
        PairingStore(path).get_or_create_pairing("u1")
        reloaded = PairingStore(path)
        pairing = reloaded.get_or_create_pairing()
        assert reloaded.host_id
        assert reloaded.validate_secret(pairing["secret"]) is not None

    def test_invalid_secret(self, tmp_path):
        store = PairingStore(str(tmp_path / "p.json"))
        assert store.validate_secret("nope") is None
        assert store.validate_secret("") is None

    def test_clear(self, tmp_path):
        path = str(tmp_path / "p.json")
        store = PairingStore(path)
        pairing = store.get_or_create_pairing()
        store.clear()
        assert store.validate_secret(pairing["secret"]) is None


# ---------------------------------------------------------------------------
# browser session registry
# ---------------------------------------------------------------------------


class TestBrowserSessionRegistry:
    def test_register_and_pick_foreground(self):
        reg = BrowserSessionRegistry(session_ttl_sec=60)
        reg.register("t1", "u1", "p1", foreground=False)
        reg.register("t2", "u1", "p1", foreground=True)
        target = reg.pick_target()
        assert target.tab_session_id == "t2"

    def test_pick_most_recent_when_no_foreground(self):
        reg = BrowserSessionRegistry(session_ttl_sec=60)
        reg.register("t1", "u1", "p1")
        time.sleep(0.01)
        reg.register("t2", "u1", "p1")
        assert reg.pick_target().tab_session_id == "t2"

    def test_unregister(self):
        reg = BrowserSessionRegistry(session_ttl_sec=60)
        reg.register("t1", "u1", "p1")
        assert reg.unregister("t1", "p1") is True
        assert reg.pick_target() is None

    def test_pairing_scoping(self):
        reg = BrowserSessionRegistry(session_ttl_sec=60)
        reg.register("t1", "u1", "p1")
        # A different pairing cannot unregister or touch the session.
        assert reg.unregister("t1", "p2") is False
        assert reg.get_for_pairing("t1", "p2") is None
        assert reg.get_for_pairing("t1", "p1") is not None

    def test_expiry(self):
        reg = BrowserSessionRegistry(session_ttl_sec=0.05)
        reg.register("t1", "u1", "p1")
        assert reg.pick_target() is not None
        time.sleep(0.1)
        assert reg.pick_target() is None
        assert reg.get("t1") is None
        assert reg.exists("t1") is False  # pruned

    def test_touch_keeps_alive(self):
        reg = BrowserSessionRegistry(session_ttl_sec=0.1)
        reg.register("t1", "u1", "p1")
        time.sleep(0.06)
        assert reg.touch("t1", "p1") is not None
        time.sleep(0.06)
        assert reg.get("t1") is not None


# ---------------------------------------------------------------------------
# app registry
# ---------------------------------------------------------------------------


class TestAppRegistry:
    def test_lookup_by_id_and_alias(self):
        reg = AppRegistry([BLENDER_APP], BLENDER_MCPS)
        assert reg.get("blender").display_name == "Blender"
        assert reg.by_alias("zeptej se blenderu") is not None
        assert reg.by_alias("nic tady není") is None

    def test_inflected_alias_matching(self):
        reg = AppRegistry([BLENDER_APP], BLENDER_MCPS)
        for text in ("blender", "blenderu", "blenderem", "Blenderem"):
            assert reg.by_alias(text) is not None, text
        # A word that merely starts with the alias but is not an
        # inflection does not match.
        assert reg.by_alias("blenderovaci stroj") is None

    def test_namespace_qualification(self):
        reg = AppRegistry([BLENDER_APP], BLENDER_MCPS)
        app = reg.get("blender")
        assert reg.qualifies_tool(app, "blender.get_scene_info")
        assert not reg.qualifies_tool(app, "get_scene_info")
        assert not reg.qualifies_tool(app, "freecad.create")

    def test_mcp_missing(self):
        reg = AppRegistry([BLENDER_APP], {})
        app = reg.get("blender")
        assert reg.has_mcp(app) is False
        assert reg.mcp_config(app) is None

    def test_duplicate_apps_collapse(self):
        reg = AppRegistry([BLENDER_APP, dict(BLENDER_APP, displayName="Other")], {})
        assert len(reg.all()) == 1


# ---------------------------------------------------------------------------
# MCP runtime registry
# ---------------------------------------------------------------------------


class TestMcpRuntimeRegistry:
    def test_start_lists_tools_and_hashes(self, fake_mcp_client):
        fake_mcp_client(BLENDER_MCPS)
        reg = McpRuntimeRegistry(BLENDER_MCPS)
        status = reg.start("blender-main")
        assert status.state == STATE_RUNNING
        names = [t["name"] for t in status.tools]
        assert names == ["create_object", "exec_command", "get_scene_info"]
        assert status.schema_hash

    def test_stop_clears_state(self, fake_mcp_client):
        fake_mcp_client(BLENDER_MCPS)
        reg = McpRuntimeRegistry(BLENDER_MCPS)
        reg.start("blender-main")
        status = reg.stop("blender-main")
        assert status.state == STATE_STOPPED
        assert status.tools == []
        assert status.schema_hash is None

    def test_restart(self, fake_mcp_client):
        fake_mcp_client(BLENDER_MCPS)
        reg = McpRuntimeRegistry(BLENDER_MCPS)
        status = reg.restart("blender-main")
        assert status.state == STATE_RUNNING

    def test_unknown_server(self, fake_mcp_client):
        fake_mcp_client(BLENDER_MCPS)
        reg = McpRuntimeRegistry(BLENDER_MCPS)
        with pytest.raises(ProtocolError) as exc:
            reg.start("ghost")
        assert exc.value.code == ErrorCode.MCP_UNAVAILABLE

    def test_invoke_requires_running(self, fake_mcp_client):
        fake_mcp_client(BLENDER_MCPS)
        reg = McpRuntimeRegistry(BLENDER_MCPS)
        with pytest.raises(ProtocolError) as exc:
            reg.invoke("blender-main", "get_scene_info", {})
        assert exc.value.code == ErrorCode.MCP_NOT_RUNNING

    def test_invoke_returns_exact_result(self, fake_mcp_client):
        client = fake_mcp_client(BLENDER_MCPS)
        reg = McpRuntimeRegistry(BLENDER_MCPS)
        reg.start("blender-main")
        result = reg.invoke("blender-main", "get_scene_info", {"x": 1})
        assert result["text"] == "result:get_scene_info"
        assert result["isError"] is False
        assert client.invoked == [("blender-main", "get_scene_info", {"x": 1})]

    def test_tool_names_refresh(self, fake_mcp_client):
        fake_mcp_client(BLENDER_MCPS)
        reg = McpRuntimeRegistry(BLENDER_MCPS)
        assert "get_scene_info" in reg.tool_names("blender-main", refresh=True)

    def test_health_stopped(self, fake_mcp_client):
        fake_mcp_client(BLENDER_MCPS)
        reg = McpRuntimeRegistry(BLENDER_MCPS)
        health = reg.health("blender-main")
        assert health["healthy"] is False


# ---------------------------------------------------------------------------
# capability manifest
# ---------------------------------------------------------------------------


class TestCapabilityManifest:
    def test_manifest_shape(self):
        apps = AppRegistry([BLENDER_APP], BLENDER_MCPS)
        mcp = McpRuntimeRegistry(BLENDER_MCPS)
        sessions = BrowserSessionRegistry()
        pairings = PairingStore.__new__(PairingStore)
        pairings._host_id = "host-1"
        pairings._pairings = {}
        pairings._lock = threading.Lock()
        pairings._path = "unused.json"
        builder = CapabilityManifestBuilder(
            apps, mcp, sessions, pairings, dangerous_patterns=["exec"]
        )
        manifest = builder.build()
        assert manifest["protocol"] == PROTOCOL_VERSION
        assert manifest["hostId"] == "host-1"
        blender = manifest["apps"]["blender"]
        assert blender["mcp"] == {
            "serverId": "blender-main",
            "running": False,
            "state": "STOPPED",
            "schemaHash": None,
            "tools": [],
        }
        assert manifest["dangerousTools"] == []

    def test_dangerous_tools_identifiable(self):
        apps = AppRegistry([BLENDER_APP], BLENDER_MCPS)
        mcp = McpRuntimeRegistry(BLENDER_MCPS)
        # Seed the schema directly so no stdio is touched.
        status = mcp.status("blender-main")
        status.state = STATE_RUNNING
        status.tools = [
            {"name": "exec_command", "description": "run a shell", "inputSchema": {}},
            {"name": "get_scene_info", "description": "", "inputSchema": {}},
        ]
        sessions = BrowserSessionRegistry()
        pairings = PairingStore.__new__(PairingStore)
        pairings._host_id = "host-1"
        pairings._pairings = {}
        pairings._lock = threading.Lock()
        pairings._path = "unused.json"
        builder = CapabilityManifestBuilder(
            apps, mcp, sessions, pairings, dangerous_patterns=["exec", "shell"]
        )
        manifest = builder.build()
        blender = manifest["apps"]["blender"]
        assert blender["mcp"]["tools"] == [
            "blender.exec_command",
            "blender.get_scene_info",
        ]
        assert manifest["dangerousTools"] == ["blender.exec_command"]


# ---------------------------------------------------------------------------
# voice routing
# ---------------------------------------------------------------------------


class TestVoiceRouteResolver:
    def make_resolver(self, apps=None, sessions=None, continuity=None, enabled=True):
        return VoiceRouteResolver(
            apps or AppRegistry([BLENDER_APP], BLENDER_MCPS),
            sessions or BrowserSessionRegistry(),
            continuity or ContinuityCache(ttl_sec=600),
            enabled=enabled,
        )

    def test_explicit_app(self):
        resolver = self.make_resolver()
        route = resolver.resolve("Jarvisi, zeptej se Blenderu, co je ve scéne")
        assert route.routed
        assert route.app_id == "blender"
        assert route.reason == "explicit-app:blender"

    def test_generic_v271(self):
        resolver = self.make_resolver()
        route = resolver.resolve("pošli to do v271")
        assert route.routed
        assert route.generic_v271
        assert route.app_id is None

    def test_not_routed(self):
        resolver = self.make_resolver()
        route = resolver.resolve("jaké je dnes počasí")
        assert not route.routed

    def test_continuity_followup(self):
        continuity = ContinuityCache(ttl_sec=600)
        continuity.update("blender", agent_id="ag-1", chat_id="ch-1")
        resolver = self.make_resolver(continuity=continuity)
        route = resolver.resolve("a co ještě ve scéne?")
        assert route.routed
        assert route.app_id == "blender"
        assert route.reason == "continuity:blender"

    def test_continuity_expired(self):
        continuity = ContinuityCache(ttl_sec=0.05)
        continuity.update("blender")
        time.sleep(0.1)
        resolver = self.make_resolver(continuity=continuity)
        route = resolver.resolve("a co ještě?")
        assert not route.routed

    def test_disabled_resolver(self):
        resolver = self.make_resolver(enabled=False)
        assert not resolver.resolve("zeptej se blenderu").routed

    def test_pick_session_priority(self):
        sessions = BrowserSessionRegistry()
        sessions.register("t1", "u1", "p1", foreground=False)
        sessions.register("t2", "u1", "p1", foreground=True)
        resolver = self.make_resolver(sessions=sessions)
        assert resolver.pick_session().tab_session_id == "t2"


class TestContinuityCache:
    def test_update_and_get(self):
        cache = ContinuityCache(ttl_sec=60)
        cache.update("blender", "ag-1", "ch-1")
        entry = cache.get("blender")
        assert entry.agent_id == "ag-1"
        assert entry.last_chat_id == "ch-1"

    def test_v271_marker(self):
        cache = ContinuityCache(ttl_sec=60)
        cache.update(None, "ag-9", "ch-9")
        entry = cache.get(V271_MARKER)
        assert entry.agent_id == "ag-9"

    def test_update_keeps_unknown_fields(self):
        cache = ContinuityCache(ttl_sec=60)
        cache.update("blender", "ag-1", "ch-1")
        cache.update("blender")  # refresh without new ids
        entry = cache.get("blender")
        assert entry.agent_id == "ag-1"
        assert entry.last_chat_id == "ch-1"

    def test_latest(self):
        cache = ContinuityCache(ttl_sec=60)
        cache.update("blender", "ag-1", "ch-1")
        cache.update("freecad", "ag-2", "ch-2")
        latest = cache.latest()
        assert latest.app_id == "freecad"


# ---------------------------------------------------------------------------
# event bus
# ---------------------------------------------------------------------------


class TestEventBus:
    def test_publish_and_backlog(self):
        bus = EventBus()
        seq = bus.publish("t1", "dispatch", {"a": 1})
        assert seq == 1
        backlog = bus.backlog("t1")
        assert len(backlog) == 1
        assert backlog[0][1] == "dispatch"
        assert bus.sequence("t1") == 1

    def test_subscribe_replay(self):
        bus = EventBus()
        bus.publish("t1", "dispatch", {"a": 1})
        bus.publish("t1", "event", {"b": 2})
        q = bus.subscribe("t1", since=1)
        items = []
        while not q.empty():
            items.append(q.get_nowait())
        assert len(items) == 1
        assert items[0][1] == "event"

    def test_live_delivery(self):
        bus = EventBus()
        q = bus.subscribe("t1")
        bus.publish("t1", "dispatch", {"a": 1})
        seq, event_type, payload = q.get(timeout=2)
        assert event_type == "dispatch"
        assert payload == {"a": 1}

    def test_unsubscribe(self):
        bus = EventBus()
        q = bus.subscribe("t1")
        bus.unsubscribe("t1", q)
        bus.publish("t1", "dispatch", {})
        assert q.empty()


# ---------------------------------------------------------------------------
# control plane service (unit level)
# ---------------------------------------------------------------------------


def make_control_plane(tmp_path, **overrides):
    cfg = make_cfg(
        apps=[BLENDER_APP],
        mcps=BLENDER_MCPS,
        pairing_store=str(tmp_path / "pairings.json"),
        **overrides,
    )
    return ControlPlane(cfg)


class TestToolCallValidationChain:
    def _ready_plane(self, tmp_path, fake_mcp_client):
        fake_mcp_client(BLENDER_MCPS)
        plane = make_control_plane(tmp_path)
        plane.start()
        pairing = plane.pair()
        plane.register_session(
            pairing, {"tabSessionId": "tab-1", "userId": "u1", "foreground": True}
        )
        plane.mcp_start("blender")
        return plane, pairing

    def test_full_chain_success(self, tmp_path, fake_mcp_client):
        plane, pairing = self._ready_plane(tmp_path, fake_mcp_client)
        try:
            result = plane.tool_call(
                pairing,
                {
                    "tabSessionId": "tab-1",
                    "agentId": "ag-1",
                    "appId": "blender",
                    "toolCallId": "tc-1",
                    "toolName": "blender.get_scene_info",
                    "arguments": {},
                },
            )
            assert result["ok"] is True
            assert result["toolCallId"] == "tc-1"
            assert result["appId"] == "blender"
            assert result["toolName"] == "blender.get_scene_info"
            assert result["result"]["text"] == "result:get_scene_info"
        finally:
            plane.stop()

    def test_unpaired_session(self, tmp_path, fake_mcp_client):
        plane, pairing = self._ready_plane(tmp_path, fake_mcp_client)
        try:
            with pytest.raises(ProtocolError) as exc:
                plane.tool_call(
                    pairing,
                    {
                        "tabSessionId": "ghost",
                        "appId": "blender",
                        "toolCallId": "tc-1",
                        "toolName": "blender.get_scene_info",
                        "arguments": {},
                    },
                )
            assert exc.value.code == ErrorCode.SESSION_UNKNOWN
        finally:
            plane.stop()

    def test_session_from_other_pairing(self, tmp_path, fake_mcp_client):
        plane, pairing = self._ready_plane(tmp_path, fake_mcp_client)
        other = {"pairingId": "other", "secret": "x", "hostId": "h"}
        try:
            with pytest.raises(ProtocolError) as exc:
                plane.tool_call(
                    other,
                    {
                        "tabSessionId": "tab-1",
                        "appId": "blender",
                        "toolCallId": "tc-1",
                        "toolName": "blender.get_scene_info",
                        "arguments": {},
                    },
                )
            assert exc.value.code == ErrorCode.SESSION_UNKNOWN
        finally:
            plane.stop()

    def test_unknown_app(self, tmp_path, fake_mcp_client):
        plane, pairing = self._ready_plane(tmp_path, fake_mcp_client)
        try:
            with pytest.raises(ProtocolError) as exc:
                plane.tool_call(
                    pairing,
                    {
                        "tabSessionId": "tab-1",
                        "appId": "freecad",
                        "toolCallId": "tc-1",
                        "toolName": "freecad.x",
                        "arguments": {},
                    },
                )
            assert exc.value.code == ErrorCode.APP_UNKNOWN
        finally:
            plane.stop()

    def test_namespace_mismatch(self, tmp_path, fake_mcp_client):
        plane, pairing = self._ready_plane(tmp_path, fake_mcp_client)
        try:
            with pytest.raises(ProtocolError) as exc:
                plane.tool_call(
                    pairing,
                    {
                        "tabSessionId": "tab-1",
                        "appId": "blender",
                        "toolCallId": "tc-1",
                        "toolName": "freecad.create_object",
                        "arguments": {},
                    },
                )
            assert exc.value.code == ErrorCode.TOOL_NAMESPACE_MISMATCH
        finally:
            plane.stop()

    def test_tool_not_in_schema(self, tmp_path, fake_mcp_client):
        plane, pairing = self._ready_plane(tmp_path, fake_mcp_client)
        try:
            with pytest.raises(ProtocolError) as exc:
                plane.tool_call(
                    pairing,
                    {
                        "tabSessionId": "tab-1",
                        "appId": "blender",
                        "toolCallId": "tc-1",
                        "toolName": "blender.nonexistent",
                        "arguments": {},
                    },
                )
            assert exc.value.code == ErrorCode.TOOL_UNKNOWN
        finally:
            plane.stop()

    def test_mcp_not_running(self, tmp_path, fake_mcp_client):
        fake_mcp_client(BLENDER_MCPS)
        plane = make_control_plane(tmp_path)
        plane.start()
        pairing = plane.pair()
        plane.register_session(
            pairing, {"tabSessionId": "tab-1", "userId": "u1", "foreground": True}
        )
        try:
            with pytest.raises(ProtocolError) as exc:
                plane.tool_call(
                    pairing,
                    {
                        "tabSessionId": "tab-1",
                        "appId": "blender",
                        "toolCallId": "tc-1",
                        "toolName": "blender.get_scene_info",
                        "arguments": {},
                    },
                )
            assert exc.value.code == ErrorCode.MCP_NOT_RUNNING
        finally:
            plane.stop()


class TestEnvelopeEndpoint:
    def test_capabilities_envelope(self, tmp_path):
        plane = make_control_plane(tmp_path)
        pairing = plane.pair()
        result = plane.handle_message(
            {
                "protocol": PROTOCOL_VERSION,
                "id": "req-1",
                "type": "capabilities.get",
                "payload": {},
            },
            pairing,
        )
        assert result["ok"] is True
        assert result["id"] == "req-1"
        assert result["type"] == "capabilities.get"
        assert "apps" in result["payload"]

    def test_tool_call_envelope_returns_tool_result_type(
        self, tmp_path, fake_mcp_client
    ):
        fake_mcp_client(BLENDER_MCPS)
        plane = make_control_plane(tmp_path)
        plane.start()
        pairing = plane.pair()
        plane.register_session(
            pairing, {"tabSessionId": "tab-1", "userId": "u1", "foreground": True}
        )
        plane.mcp_start("blender")
        try:
            result = plane.handle_message(
                {
                    "protocol": PROTOCOL_VERSION,
                    "id": "req-2",
                    "type": "tool.call",
                    "payload": {
                        "tabSessionId": "tab-1",
                        "appId": "blender",
                        "toolCallId": "tc-2",
                        "toolName": "blender.get_scene_info",
                        "arguments": {},
                    },
                },
                pairing,
            )
            assert result["ok"] is True
            assert result["type"] == "tool.result"
            assert result["payload"]["toolCallId"] == "tc-2"
        finally:
            plane.stop()

    def test_error_envelope(self, tmp_path):
        plane = make_control_plane(tmp_path)
        result = plane.handle_message(
            {
                "protocol": PROTOCOL_VERSION,
                "id": "req-3",
                "type": "session.register",
                "payload": {},
            },
            None,
        )
        assert result["ok"] is False
        assert result["type"] == "error"
        assert result["error"]["code"] == ErrorCode.PAIRING_REQUIRED

    def test_malformed_envelope_never_raises(self, tmp_path):
        plane = make_control_plane(tmp_path)
        result = plane.handle_message({"bogus": True})
        assert result["ok"] is False
        assert result["error"]["code"] == ErrorCode.INVALID_REQUEST


class TestVoiceDispatch:
    def test_voice_route_dispatch_and_confirmation(self, tmp_path):
        plane = make_control_plane(tmp_path)
        tts = FakeTTS()
        plane.speak = tts.speak
        pairing = plane.pair()
        plane.register_session(
            pairing, {"tabSessionId": "tab-1", "userId": "u1", "foreground": True}
        )
        handled = plane.handle_voice_query(
            "Jarvisi, zeptej se Blenderu, co je ve scéne", language="cs"
        )
        assert handled is True
        events = plane.events.backlog("tab-1")
        assert len(events) == 1
        _, event_type, payload = events[0]
        assert event_type == "dispatch"
        assert payload["origin"] == "toastovac-voice"
        assert payload["appId"] == "blender"
        assert payload["replyMode"] == "voice-and-ui"
        assert "scéne" in payload["text"]
        assert tts.spoken == [("Posílám do Blender.", "cs")]

    def test_voice_followup_reuses_thread(self, tmp_path):
        plane = make_control_plane(tmp_path)
        tts = FakeTTS()
        plane.speak = tts.speak
        pairing = plane.pair()
        plane.register_session(
            pairing, {"tabSessionId": "tab-1", "userId": "u1", "foreground": True}
        )
        plane.report_continuity(
            pairing,
            {
                "tabSessionId": "tab-1",
                "appId": "blender",
                "agentId": "ag-7",
                "chatId": "ch-42",
            },
        )
        plane.handle_voice_query("a co ještě ve scéne?", language="cs")
        payload = plane.events.backlog("tab-1")[-1][2]
        assert payload["agentId"] == "ag-7"
        assert payload["chatId"] == "ch-42"

    def test_voice_unavailable_speaks_error(self, tmp_path):
        plane = make_control_plane(tmp_path)
        tts = FakeTTS()
        plane.speak = tts.speak
        handled = plane.handle_voice_query("zeptej se blenderu", language="cs")
        assert handled is True
        assert tts.spoken[0][0] == "Nemůžu odeslat otázku, v271 není dostupné."

    def test_voice_not_routed_returns_false(self, tmp_path):
        plane = make_control_plane(tmp_path)
        assert plane.handle_voice_query("jaké je dnes počasí") is False

    def test_voice_routing_disabled(self, tmp_path):
        plane = make_control_plane(tmp_path, voice_routing=False)
        assert plane.handle_voice_query("zeptej se blenderu") is False

    def test_voice_say_endpoint(self, tmp_path):
        plane = make_control_plane(tmp_path)
        tts = FakeTTS()
        plane.speak = tts.speak
        result = plane.speak_text({"text": "Výsledek je hotový.", "language": "cs"})
        assert result["ok"] is True
        assert result["spoken"] is True
        assert tts.spoken == [("Výsledek je hotový.", "cs")]

    def test_voice_say_rejects_non_string(self, tmp_path):
        plane = make_control_plane(tmp_path)
        with pytest.raises(ProtocolError) as exc:
            plane.speak_text({"text": 42})
        assert exc.value.code == ErrorCode.INVALID_REQUEST


# ---------------------------------------------------------------------------
# HTTP transport (end to end)
# ---------------------------------------------------------------------------


class TestHttpTransport:
    @pytest.fixture
    def running_plane(self, tmp_path, fake_mcp_client):
        fake_mcp_client(BLENDER_MCPS)
        plane = make_control_plane(tmp_path)
        plane.start()
        yield plane
        plane.stop()

    def _request(self, port, path, method="GET", body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=data,
            method=method,
            headers=headers or {},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_hello_no_auth(self, running_plane):
        port = running_plane.transport.port
        status, hello = self._request(port, "/desktop/v1/hello")
        assert status == 200
        assert hello["protocol"] == PROTOCOL_VERSION
        assert hello["hostId"]

    def test_pair_and_auth(self, running_plane):
        port = running_plane.transport.port
        status, pairing = self._request(port, "/desktop/v1/pair", "POST", {})
        assert status == 200
        token = pairing["secret"]
        status, caps = self._request(
            port, "/desktop/v1/capabilities", headers={"Authorization": f"Bearer {token}"}
        )
        assert status == 200
        assert "blender" in caps["apps"]

    def test_auth_required(self, running_plane):
        port = running_plane.transport.port
        status, body = self._request(port, "/desktop/v1/capabilities")
        assert status == 401
        assert body["error"]["code"] == ErrorCode.PAIRING_REQUIRED

    def test_bad_token(self, running_plane):
        port = running_plane.transport.port
        status, body = self._request(
            port,
            "/desktop/v1/capabilities",
            headers={"Authorization": "Bearer wrong"},
        )
        assert status == 401
        assert body["error"]["code"] == ErrorCode.PAIRING_INVALID

    def test_origin_rejected(self, running_plane):
        port = running_plane.transport.port
        status, pairing = self._request(port, "/desktop/v1/pair", "POST", {})
        status, body = self._request(
            port,
            "/desktop/v1/capabilities",
            headers={
                "Authorization": f"Bearer {pairing['secret']}",
                "Origin": "https://evil.example",
            },
        )
        assert status == 403
        assert body["error"]["code"] == ErrorCode.FORBIDDEN_ORIGIN

    def test_allowed_origin_gets_cors_header(self, running_plane):
        port = running_plane.transport.port
        _, pairing = self._request(port, "/desktop/v1/pair", "POST", {})
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/desktop/v1/capabilities",
            headers={
                "Authorization": f"Bearer {pairing['secret']}",
                "Origin": "https://v271.cz",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert resp.headers.get("Access-Control-Allow-Origin") == "https://v271.cz"

    def test_session_register_and_tool_call_over_http(
        self, running_plane, fake_mcp_client
    ):
        port = running_plane.transport.port
        _, pairing = self._request(port, "/desktop/v1/pair", "POST", {})
        auth = {"Authorization": f"Bearer {pairing['secret']}"}
        status, reg = self._request(
            port,
            "/desktop/v1/session/register",
            "POST",
            {"tabSessionId": "tab-1", "userId": "u1", "foreground": True},
            auth,
        )
        assert status == 200 and reg["registered"] is True
        status, started = self._request(port, "/desktop/v1/apps/blender/mcp/start", "POST", {}, auth)
        assert status == 200
        assert started["state"] == STATE_RUNNING
        status, result = self._request(
            port,
            "/desktop/v1/tool/call",
            "POST",
            {
                "tabSessionId": "tab-1",
                "agentId": "ag-1",
                "appId": "blender",
                "toolCallId": "tc-1",
                "toolName": "blender.get_scene_info",
                "arguments": {},
            },
            auth,
        )
        assert status == 200
        assert result["ok"] is True
        assert result["result"]["text"] == "result:get_scene_info"

    def test_mcp_lifecycle_errors(self, running_plane):
        port = running_plane.transport.port
        _, pairing = self._request(port, "/desktop/v1/pair", "POST", {})
        auth = {"Authorization": f"Bearer {pairing['secret']}"}
        status, body = self._request(
            port, "/desktop/v1/apps/ghost/mcp/start", "POST", {}, auth
        )
        assert status == 404
        assert body["error"]["code"] == ErrorCode.APP_UNKNOWN

    def test_unknown_path(self, running_plane):
        port = running_plane.transport.port
        status, body = self._request(port, "/desktop/v1/nope")
        assert status == 400
        assert body["error"]["code"] == ErrorCode.INVALID_REQUEST

    def test_envelope_over_http(self, running_plane):
        port = running_plane.transport.port
        _, pairing = self._request(port, "/desktop/v1/pair", "POST", {})
        status, result = self._request(
            port,
            "/desktop/v1/message",
            "POST",
            {
                "protocol": PROTOCOL_VERSION,
                "id": "req-99",
                "type": "capabilities.get",
                "payload": {},
            },
            {"Authorization": f"Bearer {pairing['secret']}"},
        )
        assert status == 200
        assert result["id"] == "req-99"
        assert result["ok"] is True

    def test_sse_dispatch_delivery(self, running_plane):
        """The browser's SSE stream receives the voice dispatch."""
        port = running_plane.transport.port
        _, pairing = self._request(port, "/desktop/v1/pair", "POST", {})
        auth = {"Authorization": f"Bearer {pairing['secret']}"}
        _, reg = self._request(
            port,
            "/desktop/v1/session/register",
            "POST",
            {"tabSessionId": "tab-1", "userId": "u1", "foreground": True},
            auth,
        )
        assert reg["registered"] is True

        received = queue.Queue()
        token = pairing["secret"]

        def _stream():
            conn = HTTPConnection("127.0.0.1", port, timeout=30)
            conn.request(
                "GET",
                f"/desktop/v1/events?tabSessionId=tab-1&token={token}",
                headers={"Origin": "https://v271.cz"},
            )
            resp = conn.getresponse()
            assert resp.status == 200
            assert resp.getheader("Content-Type") == "text/event-stream; charset=utf-8"
            line = resp.readline()
            while line:
                received.put(line.decode("utf-8", "replace"))
                if line.startswith(b"data: "):
                    break
                line = resp.readline()
            conn.close()

        thread = threading.Thread(target=_stream, daemon=True)
        thread.start()
        time.sleep(0.3)

        # Route a voice query; the dispatch must reach the stream.
        running_plane.handle_voice_query(
            "Jarvisi, zeptej se Blenderu, co je ve scéne", language="cs"
        )
        lines = []
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                lines.append(received.get(timeout=0.5))
            except queue.Empty:
                continue
            if any(line.startswith("data: ") and "dispatch" in line for line in lines):
                break
        assert any(
            line.startswith("data: ") and "dispatch" in line for line in lines
        ), lines
        # The SSE frame is two lines: "event: dispatch" then "data: {...}".
        event_name = next(
            line.split(":", 1)[1].strip() for line in lines if "event:" in line
        )
        data_line = next(line for line in lines if line.startswith("data: "))
        event = json.loads(data_line.split("data: ", 1)[1])
        assert event_name == "dispatch"
        assert event["type"] == "dispatch"
        assert event["payload"]["appId"] == "blender"
        thread.join(timeout=5)

    def test_sse_requires_registered_session(self, running_plane):
        port = running_plane.transport.port
        _, pairing = self._request(port, "/desktop/v1/pair", "POST", {})
        status, body = self._request(
            port,
            f"/desktop/v1/events?tabSessionId=ghost&token={pairing['secret']}",
        )
        assert status == 404
        assert body["error"]["code"] == ErrorCode.SESSION_UNKNOWN

    def test_voice_say_over_http(self, running_plane):
        port = running_plane.transport.port
        _, pairing = self._request(port, "/desktop/v1/pair", "POST", {})
        tts = FakeTTS()
        running_plane.speak = tts.speak
        status, result = self._request(
            port,
            "/desktop/v1/voice/say",
            "POST",
            {"text": "Hotovo.", "language": "cs"},
            {"Authorization": f"Bearer {pairing['secret']}"},
        )
        assert status == 200
        assert result["spoken"] is True
        assert tts.spoken == [("Hotovo.", "cs")]


# ---------------------------------------------------------------------------
# daemon wiring
# ---------------------------------------------------------------------------


class TestDaemonWiring:
    def test_daemon_starts_and_stops_control_plane(self):
        """``daemon.main(smoke_test=True)`` boots and stops the plane."""
        pytest.importorskip("huggingface_hub")  # full daemon dep set
        from unittest.mock import MagicMock, patch
        import io

        cfg = make_cfg(pairing_store=":memory:")  # ephemeral port via 0
        with patch("jarvis.daemon.load_settings", return_value=cfg), \
             patch("jarvis.daemon.Database") as mock_db, \
             patch("jarvis.daemon.initialize_mcp_tools", return_value=({}, {})), \
             patch("jarvis.daemon.DialogueMemory") as mock_dm, \
             patch("jarvis.daemon.get_location_context", return_value="Location: Test"), \
             patch("jarvis.daemon.create_tts_engine") as mock_tts, \
             patch("jarvis.daemon.VoiceListener") as mock_vl, \
             patch("jarvis.memory.graph.GraphMemoryStore") as mock_graph:
            mock_db_instance = MagicMock()
            mock_db.return_value = mock_db_instance
            mock_dm_instance = MagicMock()
            mock_dm.return_value = mock_dm_instance
            mock_tts_instance = MagicMock()
            mock_tts_instance.enabled = False
            mock_tts.return_value = mock_tts_instance
            mock_vl_instance = MagicMock()
            mock_vl.return_value = mock_vl_instance
            mock_graph_instance = MagicMock()
            mock_graph_instance.migrate_legacy_shape.return_value = False
            mock_graph.return_value = mock_graph_instance

            captured = io.StringIO()
            with patch("sys.stdout", captured):
                from jarvis.daemon import main

                main(smoke_test=True)

            output = captured.getvalue()
            assert "SMOKE_TEST_INIT_OK" in output
            assert "🕹️ Control plane" in output
            # The daemon accessor is reset after smoke shutdown.
            from jarvis import daemon

            assert daemon.get_control_plane() is None

    def test_daemon_disabled_prints_disabled(self):
        pytest.importorskip("huggingface_hub")
        from unittest.mock import MagicMock, patch
        import io

        cfg = make_cfg(port=0)
        cfg.control_plane_enabled = False
        with patch("jarvis.daemon.load_settings", return_value=cfg), \
             patch("jarvis.daemon.Database"), \
             patch("jarvis.daemon.initialize_mcp_tools", return_value=({}, {})), \
             patch("jarvis.daemon.DialogueMemory"), \
             patch("jarvis.daemon.get_location_context", return_value="Location: Test"), \
             patch("jarvis.daemon.create_tts_engine") as mock_tts, \
             patch("jarvis.daemon.VoiceListener"), \
             patch("jarvis.memory.graph.GraphMemoryStore"):
            mock_tts_instance = MagicMock()
            mock_tts_instance.enabled = False
            mock_tts.return_value = mock_tts_instance
            captured = io.StringIO()
            with patch("sys.stdout", captured):
                from jarvis.daemon import main

                main(smoke_test=True)
            assert "🕹️ Control plane disabled" in captured.getvalue()


class TestConfigDefaults:
    def test_control_plane_defaults_present(self, tmp_path, monkeypatch):
        import os

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))
        from jarvis.config import load_settings

        settings = load_settings()
        assert settings.control_plane_enabled is True
        assert settings.control_plane_host == "127.0.0.1"
        assert settings.control_plane_port == 27121
        assert settings.control_plane_allowed_origins == ["https://v271.cz"]
        assert settings.control_plane_apps == []
        assert settings.control_plane_voice_routing is True
        assert settings.control_plane_dangerous_tool_patterns

    def test_custom_apps_loaded(self, tmp_path, monkeypatch):
        import os

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "control_plane_apps": [
                        {
                            "appId": "blender",
                            "displayName": "Blender",
                            "aliases": ["blender"],
                            "mcpServerId": "blender-main",
                            "toolNamespace": "blender",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))
        from jarvis.config import load_settings

        settings = load_settings()
        assert settings.control_plane_apps[0]["appId"] == "blender"