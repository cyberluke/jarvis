# Desktop control plane spec

## Purpose

Toastovač is the **local desktop control plane** for the v271 web
assistant. A paired v271.cz browser tab can ask Toastovač to run local
desktop MCP tools (Blender, FreeCAD, OrcaSlicer, ...) and Toastovač can
route voice utterances to the v271 agent that owns the focused tab. The
browser tab receives dispatches through the local protocol and calls its
own normal v271 APIs; Toastovač never keyboard-automates the browser.

The transport-neutral wire contract is `v271-local/1`. Domain services
depend on `LocalProtocolTransport` (or the equivalent interface), never
on browser-specific fetch/WebSocket code, so a future
`BrowserExtensionTransport`, `NativeMessagingTransport`, or
`DeviceRelayTransport` can replace the HTTP transport without touching
the domain.

## Ownership

Toastovač owns: Voice PE ingress, voice routing, foreground process
identity, the local app runtime registry, the local MCP server
lifecycle, `tools/list`, MCP call execution, the browser pairing/session
registry, the local capability manifest, and optional TTS reply.

Toastovač does NOT own: v271 agents, prompts, workflows, or DB; NAI OS
catalog/installer UI.

## P0 transport

Loopback HTTP (`127.0.0.1`, default port `27121`) with these bindings:

| Method | Path | Purpose |
|--------|------|---------|
| GET  | `/desktop/v1/hello` | Protocol greeting (no auth) |
| POST | `/desktop/v1/pair` | Create or fetch the host pairing (no auth) |
| POST | `/desktop/v1/session/register` | Register a paired tab |
| POST | `/desktop/v1/session/unregister` | Forget a tab |
| POST | `/desktop/v1/session/continuity` | Report appId/agentId/chatId for follow-ups |
| GET  | `/desktop/v1/capabilities` | Capability manifest |
| GET  | `/desktop/v1/apps` | App runtime registry |
| GET  | `/desktop/v1/apps/{appId}` | One app entry |
| POST | `/desktop/v1/apps/{appId}/mcp/start` | Start the app's MCP server |
| POST | `/desktop/v1/apps/{appId}/mcp/stop` | Stop the app's MCP server |
| POST | `/desktop/v1/apps/{appId}/mcp/restart` | Restart the app's MCP server |
| GET  | `/desktop/v1/apps/{appId}/mcp/tools` | Live tool schema of the app's MCP server |
| POST | `/desktop/v1/tool/call` | Execute a local MCP tool (validation chain) |
| POST | `/desktop/v1/message` | Raw `v271-local/1` envelope endpoint |
| GET  | `/desktop/v1/events` | SSE stream of events for one tab (dispatch delivery) |
| POST | `/desktop/v1/voice/say` | Browser → local TTS (additive, optional reply) |

## Protocol primitives

Required primitives: `hello`, `pair`, `session.register`,
`session.unregister`, `capabilities.get`, `dispatch`, `tool.call`,
`tool.result`, `event`. Additive (documented, transport-neutral):
`session.continuity`. `dispatch` is outbound-only from Toastovač; an
inbound `dispatch` envelope is rejected with `INVALID_REQUEST`.

### Envelope

```json
{
  "protocol": "v271-local/1",
  "id": "req-...",
  "type": "tool.call",
  "timestamp": "...",
  "sessionId": "...",
  "payload": {}
}
```

Result envelope mirrors the request id and type (`tool.call` → `tool.result`,
all others echo their own type). Error envelope:

```json
{
  "protocol": "v271-local/1",
  "id": "req-...",
  "type": "error",
  "ok": false,
  "error": {"code": "MCP_UNAVAILABLE", "message": "...", "retryable": true}
}
```

Canonical error codes: `INVALID_REQUEST`, `PROTOCOL_UNSUPPORTED`,
`FORBIDDEN_ORIGIN`, `PAIRING_REQUIRED`, `PAIRING_INVALID`,
`SESSION_UNKNOWN`, `SESSION_EXPIRED`, `APP_UNKNOWN`, `MCP_UNAVAILABLE`,
`MCP_NOT_RUNNING`, `TOOL_UNKNOWN`, `TOOL_NAMESPACE_MISMATCH`,
`V271_BROWSER_UNAVAILABLE`, `INTERNAL_ERROR`.

## Pairing

`PairingStore` persists one `hostId` and one pairing per host in a JSON
file next to the daemon config (`control_plane_pairing_store`). Pairing
creates `{hostId, pairingId, secret}` (`secrets.token_urlsafe(32)`).
`POST /desktop/v1/pair` is idempotent: it returns the existing pairing
when one exists. Every other endpoint requires the secret as
`Authorization: Bearer <secret>` (SSE accepts `?token=` because
`EventSource` cannot set headers). Toastovač never receives or stores
v271 cookies. All browser-origin requests must carry an Origin listed in
`control_plane_allowed_origins`; there is no wildcard CORS.

## Browser sessions

`BrowserSessionRegistry` keeps one entry per paired tab: `tabSessionId`,
`userId`, `foreground`, `workspaceId?`, `lastSeenAt`, plus the owning
`pairingId` (a session registered under one pairing cannot be touched
with another pairing's secret). Registration updates `lastSeenAt`;
sessions older than `control_plane_session_ttl_sec` are pruned and count
as unavailable.

Voice routing priority (§7):

```text
explicit selected tab (v1: none)
→ foreground paired tab
→ most recent paired tab
```

If none exists the resolver returns `V271_BROWSER_UNAVAILABLE`.

## App runtime registry

`AppRegistry` is built from `control_plane_apps` (runtime metadata only;
NAI OS owns marketplace/install metadata). Entry shape:

```json
{
  "appId": "blender",
  "displayName": "Blender",
  "aliases": ["blender"],
  "processNames": ["blender.exe"],
  "mcpServerId": "blender-main",
  "toolNamespace": "blender"
}
```

The MCP server command/args/env live in the existing `cfg.mcps`
(`mcpServerId` must name one of its keys). Lookup works by `appId` or by
alias. `mcpServerId` and `toolNamespace` may be absent on an app whose
tools are not exposed (the manifest then reports `mcp: null`).

## MCP runtime registry

`McpRuntimeRegistry` wraps the existing persistent MCP runtime
(`jarvis.tools.external.mcp_runtime`) and tracks per-server status:
`STOPPED | STARTING | RUNNING | ERROR`, `lastError`, tool schema cache,
`schemaHash` (sha256 over the canonical tool list), best-effort PID
(psutil), `startedAt`, `lastHealthAt`.

- `start` = `list_tools` through the persistent runtime (spawns/reuses
  the stdio worker) and caches the schema.
- `stop` = `mcp_runtime.stop_server(server_id)` (new public API on the
  runtime) and drops the schema.
- `restart` = stop + start.
- `health` = cheap worker-alive probe; a dead worker with `RUNNING`
  state triggers one `list_tools` probe with a 10 s budget.
- Tools are seeded at service start from the daemon's discovery cache
  (`registry.get_cached_mcp_tools()`, `server__tool` keys) so the first
  capability manifest needs no round trip. A fresh `list_tools` refreshes
  the schema lazily on `GET .../mcp/tools`.

Tool names are exposed namespace-qualified (`blender.get_scene_info`).
Dangerous tools stay identifiable: a tool whose name or description
matches `control_plane_dangerous_tool_patterns` is reported in the
manifest's `dangerousTools` list.

## Tool call validation chain

`POST /desktop/v1/tool/call` with `{tabSessionId, agentId, appId,
toolCallId, toolName, arguments}` validates, in order:

1. paired browser session (registered, same pairing, not expired) —
   `SESSION_UNKNOWN` / `SESSION_EXPIRED`
2. registered app — `APP_UNKNOWN`
3. registered MCP (`app.mcpServerId` in `cfg.mcps`) — `MCP_UNAVAILABLE`
4. tool exists in the current schema — `TOOL_UNKNOWN`
5. namespace matches the app (`toolName` starts with
   `{app.toolNamespace}.`) — `TOOL_NAMESPACE_MISMATCH`

The response preserves `toolCallId`, `appId`, `toolName` and carries the
exact structured MCP result (`{content, text, isError, meta}` from
`MCPClient.invoke_tool`). Success is never fabricated.

## Capability manifest

`GET /desktop/v1/capabilities` returns the §11 shape:

```json
{
  "protocol": "v271-local/1",
  "hostId": "...",
  "apps": {
    "blender": {
      "running": true,
      "mcp": {"running": true, "schemaHash": "...", "tools": ["blender.get_scene_info"]}
    }
  },
  "dangerousTools": ["blender.exec_command"],
  "sessions": [{"tabSessionId": "...", "userId": "...", "foreground": true, "lastSeenAt": "..."}]
}
```

`apps[].running` is the foreground-process check (psutil scan of
`processNames`); it is `null` when the app declares no `processNames`.
`sessions` and `dangerousTools` are additive. The manifest is also what
the browser uses to know what the local machine can currently do.

## Voice dispatch

`VoiceRouteResolver` runs after final ASR (single hook in
`VoiceListener._dispatch_query`, which every voice path funnels through:
local mic, Voice PE satellite, commit button, collection timeout). It is
deterministic — no LLM round trip:

1. normalize (lowercase, collapse whitespace)
2. explicit app: first app whose alias matches word-bounded in the text
3. generic v271: `v271` appears in the text
4. continuity follow-up: no explicit match, but the last routed target is
   younger than `control_plane_continuity_ttl_sec`
5. no match → not routed, the local reply engine handles the query

A routed query selects a browser session (§7 priority) and publishes a
`dispatch` event to it:

```json
{
  "type": "dispatch",
  "payload": {
    "origin": "toastovac-voice",
    "appId": "blender",
    "agentId": "...",
    "chatId": "...",
    "text": "co je teď ve scéně",
    "replyMode": "voice-and-ui"
  }
}
```

`agentId`/`chatId` come from the thread-continuity cache (reported by
the browser via `session.continuity`; v271 remains authoritative). After
dispatch Toastovač speaks a short confirmation (cs/en, else the app
name) and the query is NOT run against the local engine. If no browser
session exists the user gets the `V271_BROWSER_UNAVAILABLE` error spoken
and the query is not run locally either — the utterance was explicitly
addressed to v271.

## Events

`EventBus` keeps a bounded ring per `tabSessionId` (200 entries) and
live subscriber queues. `GET /desktop/v1/events?tabSessionId=...` is
SSE: events are pushed as they arrive, heartbeat comments every 15 s,
`since` replays the backlog after a reconnect so a dispatch is not lost
while the stream was down. `dispatch` events target one tab; the ring
and stream are scoped to that tab's `tabSessionId`.

## Thread continuity

`ContinuityCache` stores only `{appId, agentId, lastChatId, timestamp}`
per routed target (app id, or the `v271` marker for the generic
target), with `control_plane_continuity_ttl_sec` expiry. The browser
reports the created v271 turn via `POST /desktop/v1/session/continuity`
(`{tabSessionId, appId?, agentId?, chatId?}`) so voice follow-ups reuse
the same thread.

## Security

Loopback bind only, paired-session auth (Bearer secret), explicit
Origin allowlist, no wildcard CORS, no generic shell endpoint, no
browser cookies, dangerous tools identifiable in capability metadata,
request body size cap (1 MB). The pairing store file is written with
owner-only permissions when the platform supports it.

## Future seams

`LocalProtocolTransport` is the domain seam; `LocalHttpTransport` is the
only implementation in P0. Reserved additive message types (not
implemented): `resource.list`, `resource.read`, `subscription.open`,
`subscription.close`, `binary.upload`, `binary.download`.

## Non-goals

No NAI catalog cards, no winget/scoop/choco UI, no v271 custom-agent
API, no v271 DB binding, no AI SDK workflow changes, no keyboard
automation of v271 (no focus/paste/Enter/scrape).