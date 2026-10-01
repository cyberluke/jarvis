"""MCP runtime registry for the desktop control plane.

Wraps the persistent MCP runtime (``jarvis.tools.external.mcp_runtime``)
with a per-server status view: state machine (STOPPED → STARTING →
RUNNING → ERROR), tool schema cache, schema hash, best-effort PID, and
the lifecycle operations the control plane API exposes (start/stop/
restart/health/tools/invoke).

The actual stdio sessions are owned by the shared persistent runtime, so
a control-plane ``start`` after the daemon's own discovery is a no-op on
the wire — the worker is reused.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..debug import debug_log
from ..tools.external.mcp_client import MCPClient
from ..tools.external import mcp_runtime as _runtime_mod
from .protocol import ErrorCode, ProtocolError

STATE_STOPPED = "STOPPED"
STATE_STARTING = "STARTING"
STATE_RUNNING = "RUNNING"
STATE_ERROR = "ERROR"

_HEALTH_PROBE_TIMEOUT_SEC = 10.0
_PID_CACHE_TTL_SEC = 5.0


@dataclass
class McpServerStatus:
    server_id: str
    state: str = STATE_STOPPED
    last_error: Optional[str] = None
    tools: List[Dict[str, Any]] = field(default_factory=list)
    schema_hash: Optional[str] = None
    pid: Optional[int] = None
    started_at: Optional[float] = None
    last_health_at: Optional[float] = None


class McpRuntimeRegistry:
    """Per-server status + lifecycle over the shared persistent runtime."""

    def __init__(self, mcps: Dict[str, Any]) -> None:
        self._mcps = dict(mcps or {})
        self._status: Dict[str, McpServerStatus] = {}
        self._lock = threading.RLock()
        self._pid_cache: Dict[str, tuple] = {}

    # -- status ---------------------------------------------------------

    def status(self, server_id: str) -> McpServerStatus:
        with self._lock:
            status = self._status.get(server_id)
            if status is None:
                status = McpServerStatus(server_id=server_id)
                self._status[server_id] = status
            status.pid = self._pid_for(server_id, status.state)
            return status

    def status_dict(self, server_id: str) -> Dict[str, Any]:
        status = self.status(server_id)
        return {
            "serverId": status.server_id,
            "state": status.state,
            "lastError": status.last_error,
            "tools": [
                {
                    "name": t.get("name"),
                    "description": t.get("description"),
                    "inputSchema": t.get("inputSchema"),
                }
                for t in status.tools
            ],
            "schemaHash": status.schema_hash,
            "pid": status.pid,
            "startedAt": status.started_at,
            "lastHealthAt": status.last_health_at,
        }

    def seed_from_daemon_cache(self, cached_tools: Dict[str, Any]) -> None:
        """Seed tool schemas from the daemon's discovery cache.

        ``cached_tools`` maps ``server__tool`` → ToolSpec. Only servers
        configured in ``cfg.mcps`` are considered; state is derived from
        the live worker. No network/stdio work happens here.
        """
        if not self._mcps or not cached_tools:
            return
        with self._lock:
            for server_id in self._mcps:
                if server_id in self._status:
                    continue
                tools: List[Dict[str, Any]] = []
                prefix = f"{server_id}__"
                for key, spec in cached_tools.items():
                    if isinstance(key, str) and key.startswith(prefix):
                        tools.append(
                            {
                                "name": key[len(prefix):],
                                "description": getattr(spec, "description", ""),
                                "inputSchema": getattr(spec, "inputSchema", None),
                            }
                        )
                if not tools:
                    continue
                status = McpServerStatus(server_id=server_id)
                status.tools = sorted(tools, key=lambda t: t["name"])
                status.schema_hash = _schema_hash(status.tools)
                if _runtime_mod.is_server_running(server_id):
                    status.state = STATE_RUNNING
                    status.started_at = time.time()
                self._status[server_id] = status

    # -- lifecycle ------------------------------------------------------

    def start(self, server_id: str) -> McpServerStatus:
        cfg = self._require_cfg(server_id)
        with self._lock:
            status = self._status.setdefault(server_id, McpServerStatus(server_id))
            status.state = STATE_STARTING
            status.last_error = None
        try:
            client = MCPClient(self._mcps)
            tools = client.list_tools(server_id)
            normalized = [
                {
                    "name": str(t.get("name") or ""),
                    "description": t.get("description") or "",
                    "inputSchema": t.get("inputSchema") or {},
                }
                for t in tools
                if t.get("name")
            ]
            normalized.sort(key=lambda t: t["name"])
            with self._lock:
                status.tools = normalized
                status.schema_hash = _schema_hash(normalized)
                status.state = STATE_RUNNING
                status.started_at = time.time()
                status.last_error = None
            debug_log(
                f"control plane: MCP server '{server_id}' started "
                f"({len(normalized)} tools)",
                "control_plane",
            )
            return status
        except ProtocolError:
            raise
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                status.state = STATE_ERROR
                status.last_error = str(exc) or type(exc).__name__
            debug_log(
                f"control plane: MCP server '{server_id}' start failed: {exc}",
                "control_plane",
            )
            raise ProtocolError(
                ErrorCode.MCP_UNAVAILABLE,
                f"MCP server '{server_id}' could not start: {exc}",
            ) from exc

    def stop(self, server_id: str) -> McpServerStatus:
        with self._lock:
            status = self._status.setdefault(server_id, McpServerStatus(server_id))
        _runtime_mod.stop_server(server_id)
        with self._lock:
            status.state = STATE_STOPPED
            status.tools = []
            status.schema_hash = None
            status.started_at = None
            status.last_error = None
            self._pid_cache.pop(server_id, None)
        debug_log(f"control plane: MCP server '{server_id}' stopped", "control_plane")
        return status

    def restart(self, server_id: str) -> McpServerStatus:
        self.stop(server_id)
        return self.start(server_id)

    def health(self, server_id: str) -> Dict[str, Any]:
        """Cheap health probe: live worker → RUNNING; a dead worker with
        cached RUNNING state triggers one bounded ``list_tools`` probe."""
        status = self.status(server_id)
        if status.state != STATE_RUNNING:
            return {"serverId": server_id, "healthy": False, "state": status.state}
        status.last_health_at = time.time()
        if _runtime_mod.is_server_running(server_id):
            return {"serverId": server_id, "healthy": True, "state": STATE_RUNNING}
        # Worker died silently: try one bounded probe to resurrect it.
        try:
            cfg = self._require_cfg(server_id)
            runtime = _runtime_mod.get_runtime()
            runtime.list_tools(server_id, cfg, timeout=_HEALTH_PROBE_TIMEOUT_SEC)
            with self._lock:
                status.state = STATE_RUNNING
            return {"serverId": server_id, "healthy": True, "state": STATE_RUNNING}
        except ProtocolError:
            raise
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                status.state = STATE_ERROR
                status.last_error = str(exc) or type(exc).__name__
            return {"serverId": server_id, "healthy": False, "state": STATE_ERROR}

    # -- tools / invoke -------------------------------------------------

    def tools(self, server_id: str, *, refresh: bool = False) -> List[Dict[str, Any]]:
        """Return the cached tool schema, refreshing when requested.

        When ``refresh`` is set (or nothing is cached yet), a live
        ``list_tools`` round trip replaces the cache. Raises
        ``ProtocolError(MCP_UNAVAILABLE)`` when the server cannot be
        reached.
        """
        self._require_cfg(server_id)
        with self._lock:
            status = self._status.setdefault(server_id, McpServerStatus(server_id))
            cached = list(status.tools)
        if cached and not refresh:
            return cached
        return self.start(server_id).tools

    def tool_names(self, server_id: str, *, refresh: bool = False) -> List[str]:
        return [t["name"] for t in self.tools(server_id, refresh=refresh)]

    def invoke(self, server_id: str, tool_name: str, arguments: Any) -> Dict[str, Any]:
        """Execute a tool through the shared persistent runtime.

        Returns the exact structured MCP result dict (``{content, text,
        isError, meta}``). Success is never fabricated: tool-level errors
        surface inside the result (``isError``), session failures raise
        ``ProtocolError(MCP_NOT_RUNNING)``.
        """
        self._require_cfg(server_id)
        if self.status(server_id).state != STATE_RUNNING:
            raise ProtocolError(
                ErrorCode.MCP_NOT_RUNNING,
                f"MCP server '{server_id}' is not running",
            )
        if not isinstance(arguments, dict):
            arguments = {}
        try:
            client = MCPClient(self._mcps)
            return client.invoke_tool(server_id, tool_name, arguments)
        except ProtocolError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ProtocolError(
                ErrorCode.MCP_NOT_RUNNING,
                f"MCP tool '{tool_name}' failed: {exc}",
            ) from exc

    # -- internals ------------------------------------------------------

    def _require_cfg(self, server_id: str) -> Dict[str, Any]:
        cfg = self._mcps.get(server_id)
        if not isinstance(cfg, dict):
            raise ProtocolError(
                ErrorCode.MCP_UNAVAILABLE,
                f"MCP server '{server_id}' is not configured",
            )
        return cfg

    def _pid_for(self, server_id: str, state: str) -> Optional[int]:
        """Best-effort subprocess PID via psutil command-line match.

        The ``mcp`` SDK does not expose the spawned process handle, so
        the PID is resolved by matching the configured command against
        running processes, cached briefly. ``None`` is honest: the
        worker may be alive without an identifiable PID.
        """
        if state != STATE_RUNNING:
            self._pid_cache.pop(server_id, None)
            return None
        now = time.time()
        cached = self._pid_cache.get(server_id)
        if cached is not None and now - cached[0] < _PID_CACHE_TTL_SEC:
            return cached[1]
        pid: Optional[int] = None
        try:
            import psutil  # noqa: PLC0415

            cfg = self._mcps.get(server_id) or {}
            command = str(cfg.get("command") or "").strip()
            if command:
                command_base = command.replace(".cmd", "").replace(".exe", "").lower()
                for proc in psutil.process_iter(["pid", "cmdline"]):
                    try:
                        cmdline = proc.info.get("cmdline") or []
                        if not cmdline:
                            continue
                        first = str(cmdline[0]).lower()
                        joined = " ".join(str(c) for c in cmdline).lower()
                        if first == command.lower() or (
                            command_base and command_base in joined
                        ):
                            pid = proc.info["pid"]
                            break
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
        except Exception:  # noqa: BLE001
            pid = None
        self._pid_cache[server_id] = (now, pid)
        return pid


def _schema_hash(tools: List[Dict[str, Any]]) -> str:
    """sha256 over the canonical tool list (names + schemas)."""
    canonical = json.dumps(
        [
            {
                "name": t.get("name"),
                "description": t.get("description"),
                "inputSchema": t.get("inputSchema"),
            }
            for t in tools
        ],
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()