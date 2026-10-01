# Voice PE public API contract (`voice-pe/v1` over `tdb/1`)

Toastovač is the first-class Voice PE implementation. It owns the ESPHome
Native API connections, onboarding/PSK, discovery and reconnect, the Voice
Assistant session protocol, microphone ingress, streamed TTS, media player,
LED ring and button events (`integrations/voice_pe/`, see
`voice_pe.spec.md`).

NAI OS owns **detection**: its launcher plugin discovers the device on the
LAN, tracks configuration state, and renders the hardware card. NAI OS must
**not** reimplement device operation.

This document is the public API contract between the two for **live
operation**: NAI OS (or any loopback peer) calls the endpoints below and
Toastovač executes them on the existing `VoicePEManager`. The wire transport
is the existing Toaster Desktop Bridge (`tdb/1`): loopback HTTP, bearer
token from `%LOCALAPPDATA%\VIVERRA\Toastovac\bridge.json`, same auth rules
as the cast section.

## Transport

- Base path: `http://127.0.0.1:<port>/tdb/v1/voice-pe/...`
- Auth: `Authorization: Bearer <token>` from the bridge descriptor.
- Bodies: JSON objects, max 1 MB (enforced by the tdb handler).
- Errors: `{"error": {"code": ..., "message": ..., "retryable": false}}`
  with HTTP 400. Unknown routes → 404.

## Device key

Every operation takes an optional `device` key. Resolution follows the
manager rules: node name, friendly name, MAC address (with or without `:`)
or host; an empty key resolves to the single attached satellite (or fails
with `VOICE_PE_DEVICE_UNKNOWN` when none/ambiguous).

## Endpoints

### GET `/tdb/v1/voice-pe/status`

Authoritative live state for the NAI OS detection plugin.

```json
{
  "enabled": true,
  "devices": [
    {
      "id": "aa:bb:cc:dd:ee:ff",
      "name": "Voice Assistant 1234",
      "host": "192.168.1.50",
      "port": 6053,
      "macAddress": "aa:bb:cc:dd:ee:ff",
      "identity": {"node_name": "...", "friendly_name": "...", "model": "..."},
      "features": ["voice_assistant", "api_audio", "speaker", "announce"],
      "connection": {"connected": true, "authenticated": true,
                     "device_state": "READY", "session_state": "IDLE",
                     "voice_features": [...], "error": "", "...": "..."},
      "media": {"state": "IDLE", "volume": 0.66, "muted": false}
    }
  ],
  "metrics": {"device_count": 1},
  "health": {"enabled": true, "devices": [...], "metrics": {...}}
}
```

`connection` is the device's `health_snapshot()`; `media` the media-player
snapshot. When the integration is disabled the response is
`{"enabled": false, "devices": [], "metrics": {}}`.

### GET `/tdb/v1/voice-pe/devices`

`{"devices": [...]}` — same device views as status.

### POST `/tdb/v1/voice-pe/discover`

`{}` → triggers the manager's mDNS discovery pass; returns
`{"found": [...]}` (discovery results, not attached devices).

### POST `/tdb/v1/voice-pe/announce`

```json
{"device": "...", "text": "Hello", "startConversation": true}
```

Synthesises and announces over the Voice Assistant RPC. Returns
`{"ok": true, "device": "...", "announced": true}`.

### Media playback

| Endpoint | Body | Effect |
|---|---|---|
| POST `/tdb/v1/voice-pe/play` | `{"device", "url"}` | play media URL |
| POST `/tdb/v1/voice-pe/pause` | `{"device"}` | pause |
| POST `/tdb/v1/voice-pe/resume` | `{"device"}` | resume |
| POST `/tdb/v1/voice-pe/stop` | `{"device"}` | stop |
| GET `/tdb/v1/voice-pe/media-state?device=` | — | `{"device", "media": {...}}` |

### Volume / mute

| Endpoint | Body |
|---|---|
| POST `/tdb/v1/voice-pe/volume` | `{"device", "volume": 0.0..1.0}` |
| POST `/tdb/v1/voice-pe/mute` | `{"device", "muted": true\|false}` |

### LED

POST `/tdb/v1/voice-pe/led` — `{"device", "rgb": [r,g,b], "brightness": 0..1}`.
Both fields optional (manager defaults). Returns `{"ok": true, "led": true}`.

### POST `/tdb/v1/voice-pe/calibrate`

`{"device"}` — audio calibration probe built from existing operations only:
announces a short test line and pulses the LED ring. Returns
`{"ok": true, "device", "announced": bool, "led": bool}`. No new satellite
protocol code.

### POST `/tdb/v1/voice-pe/mirror`

`{"text": "..."}` — hand one locally spoken line to every idle satellite.
Returns `{"ok": true, "satellites": <count>}`.

## Capabilities

Advertised in `bridge.json` `capabilities` and the tdb capabilities
manifest: `voice-pe.status`, `voice-pe.discover`, `voice-pe.announce`,
`voice-pe.playback`, `voice-pe.volume`, `voice-pe.led`,
`voice-pe.calibrate`, `voice-pe.mirror`.

## Non-goals (V1)

- Pairing / PSK provisioning stays in Toastovač's own onboarding
  (`jarvis voice-pe pair`) — NAI OS only records its launcher-level config.
- Audio stream taps (the WebAudio bridge for v271) are a separate contract.
- NAI OS keeps its mDNS/TCP detection plugin; this contract does not replace
  detection, it replaces *operation*.

## Error codes

`VOICE_PE_UNAVAILABLE` (manager disabled/missing), `VOICE_PE_DEVICE_UNKNOWN`
(device key did not resolve), `VOICE_PE_INVALID_REQUEST` (bad payload or
operation failure).