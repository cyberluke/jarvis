# Voice PE WebAudio Bridge (`v271-webaudio/1`)

Connect Home Assistant Voice PE directly to the V271 browser/PWA chat
composer without a Windows virtual microphone.

```text
Home Assistant Voice PE
  -> Toastovač local service
       -> framed PCM/audio stream
       -> local session/auth
  -> V271 PWA / NAI Browser
       -> VoicePeAudioSource
       -> AudioWorklet
       -> jitter/ring buffer
       -> resampler if needed
       -> Silero ONNX VAD
       -> recorder / STT / composer
```

A Windows virtual microphone is not part of this path: the microphone never
touches a capture device, so the PWA needs no fake `MediaStream` either.

## Phase 0 audit (recorded findings)

| Area | Finding |
|---|---|
| Voice PE stream | `aioesphomeapi` Native API on TCP 6053; microphone rides the API when `API_AUDIO` is set, UDP otherwise; the device callback feeds `AudioIngress.put(data, data2, stream)` |
| PCM format | PCM16LE, 16 kHz, mono; 512 samples per block (1024 bytes); channel 0 = enhanced (XMOS), channel 1 = raw; the lock picks one channel per stream |
| DSP before delivery | `host_raw_aec` (default) pushes the chosen channel through the native AEC3 lane and delivers cleaned float32 blocks; `device_enhanced` delivers the raw PCM16LE bytes |
| Existing taps | `AudioIngress` had none; this bridge adds `attach_sink`/`detach_sink` with `on_frame`/`on_stream_start`/`on_stream_end` callbacks (append-only, zero cost when no sink) |
| Queue discipline | Existing ingress queue is bounded (`voice_pe_audio_queue_ms`, default 300 ms, drop-oldest); the bridge tap adds its own bounded outbound queue so no unbounded growth exists anywhere on the path |
| V271 composer | The V271 client (velo) has no microphone/audio code today: no `getUserMedia`, no `MediaStream`, no Silero/ONNX. The PWA side implements `ComposerAudioSource`, the AudioWorklet and the Silero feed against this contract |
| BrowserOS/PWA | A public web origin cannot reach the Voice PE on the LAN. The bridge therefore binds loopback only; the PWA connects to `ws://127.0.0.1:<port>/voice-pe/v1` |
| Local auth | Bearer token (`voice_pe_bridge_token` or `JARVIS_VOICE_PE_BRIDGE_TOKEN`) plus an explicit `Origin` allow list, mirroring the control-plane boundary (loopback only, explicit origins, no wildcard CORS) |

## Transport

Secure WebSocket (wss is unnecessary on loopback; the connection is
`ws://127.0.0.1:<port>/voice-pe/v1`). WebRTC is not introduced: the jitter
handling belongs to the PWA's AudioWorklet ring buffer and the transport is
already frame-addressed, so RTCPeerConnection would add machinery without
media benefit.

## Wire contract

### Handshake

1. Client opens `ws://127.0.0.1:<port>/voice-pe/v1` with
   `Authorization: Bearer <token>` and a browser-sent `Origin` header.
2. Server validates origin, then token, then the single-client slot:
   - bad origin → close `4003 origin not allowed`;
   - bad/missing token → close `4001 unauthorised`;
   - slot busy → close `4004 another client is connected`.
3. On success the server sends `hello` (JSON text frame):

```json
{
  "type": "hello",
  "protocol": "v271-webaudio/1",
  "sessionId": "<hex>",
  "device": {"deviceId": "..", "name": "..", "state": ".."} | null,
  "format": {
    "sampleRate": 16000, "channels": 1,
    "sampleType": "pcm16le", "endianness": "little",
    "frameSamples": 512, "frameDurationMs": 32
  },
  "streamState": "idle" | "streaming",
  "tsMs": <ms since bridge start>
}
```

`device` is `null` while no satellite is attached; the client shows
unavailable and never falls back to another source. A `state` message with a
`device` field is pushed when a satellite becomes available.

### Client messages

| Message | Meaning |
|---|---|
| `{"type":"subscribe","deviceId":"<mac-or-name>"}` | Bind to a device (empty = the configured/first satellite); re-anchors the sequence; answered with `state` or `error {"code":"no_device"}` |

The stream is one-way (Voice PE -> PWA); binary frames from the client are
ignored.

### Audio frames (binary)

```
offset  size  field
0       4     sequence number, u32 LE
4       4     timestamp ms since bridge start, u32 LE
8       1     flags
9       3     reserved (zero)
12+           PCM16LE payload
```

| Flag | Value | Meaning |
|---|---|---|
| `DISCONTINUITY` | 0x01 | this frame follows a gap or a reconnect |
| `END_OF_STREAM` | 0x02 | zero-payload terminator of a microphone stream |
| `RESYNC` | 0x04 | sequence anchor of a fresh connection/stream |

### Server messages

| Message | Meaning |
|---|---|
| `state` | `streamState` + `sessionGeneration` (+ `device` when known): emitted on subscribe, on stream start/end and when a device appears |
| `resync` | `{"fromSeq": null, "toSeq": N}` right after connect; anchors the client's jitter buffer |
| `discontinuity` | `{"reason": "gap"\|"dropped", "fromSeq", "toSeq", "droppedFrames"}`: a skipped sequence (gap) or frames the bounded queue dropped under backpressure |
| `error` | `{"code": "no_device"\|"unsupported"\|"busy"\|"internal", "message"}` |

### Stream lifecycle

Each satellite pipeline run is one microphone stream:

- `on_stream_start` (a new `StreamId` binds) → `state {"streamState":"streaming","sessionGeneration":N}`;
- frames flow with monotonic sequence numbers and ms timestamps;
- `on_stream_end` (the EOS marker is queued) → `state {"streamState":"idle",...}` followed by one zero-payload `END_OF_STREAM` frame;
- the next stream starts with `RESYNC|DISCONTINUITY` flags and a `resync` message, so the client never mistakes utterance boundaries for silence gaps.

## Source semantics

- `streamState` is the satellite microphone state, not the whole voice session: `streaming` means PCM is flowing.
- A reconnect (new WebSocket) gets a fresh `sessionId` and re-anchors via `resync`; frames that were dropped while no client was connected are reported through `discontinuity {"reason":"dropped"}` with the delta, then the stream continues.
- `sessionGeneration` counts satellite runs; audio of an older run is never replayed after a new run opened (the tap forwards only the currently delivered signal).

## Backpressure

- The tap queue is bounded (`voice_pe_bridge_buffer_frames`, default 200 frames = 6.4 s); overflow drops the oldest frames and counts them.
- The writer drains in bounded cycles (512 frames) and never blocks the Voice PE callback: the tap's `on_frame` only appends and bumps counters.
- Producer/consumer position, depth, drops, gaps and reconnect counts are all in `metrics()`/`health()`.

## Conversion

One conversion only, at the tap-to-wire boundary: the `device_enhanced`
path already produces PCM16LE bytes (passed through), the host-AEC lane
produces float32 which is converted once to PCM16LE (`samples_to_pcm16`).
No resampling happens in the bridge: the format is declared in `hello` and
the client's worklet handles any rate conversion. No fake `MediaStream` is
created on the Toastovač side; if a V271 component strictly needs one, it is
built in the PWA from the AudioWorklet node through
`MediaStreamAudioDestinationNode`.

## Security

- Loopback bind only (`127.0.0.1`); a peer address check additionally
  rejects non-loopback remotes.
- Bearer token required; missing token disables the server with a warning
  (no silent fallback to open access).
- Explicit `Origin` allow list (`voice_pe_bridge_allowed_origins`,
  default `["https://v271.cz"]`); `scheme://host:*` matches any port.
- One client at a time (`voice_pe_bridge_max_clients`, default 1); a second
  connection is closed with `4004`.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `voice_pe_bridge_enabled` | `false` | master switch |
| `voice_pe_bridge_port` | `27123` | loopback port (`0` = ephemeral, tests) |
| `voice_pe_bridge_token` | `""` | bearer token; `JARVIS_VOICE_PE_BRIDGE_TOKEN` overrides in-process |
| `voice_pe_bridge_allowed_origins` | `["https://v271.cz"]` | `Origin` allow list |
| `voice_pe_bridge_max_clients` | `1` | concurrent clients |
| `voice_pe_bridge_buffer_frames` | `200` | tap queue budget (~6.4 s) |
| `voice_pe_bridge_device` | `""` | satellite to bridge (name/MAC); empty = the attached one |

## PWA behaviour (V271 side, implemented against this contract)

- `ComposerAudioSource` abstraction with `BrowserMicrophoneSource` and
  `ToastovacVoicePeSource`; the source selector offers `Microphone -> System
  microphone / Home Assistant Voice PE`.
- `ToastovacVoicePeSource` opens the WebSocket, verifies `hello.format`,
  feeds the AudioWorklet from binary frames, exposes `state()` and
  `onFrame(...)`, and reconnects with backoff; it never falls back to the
  laptop microphone.
- AudioWorklet owns the bounded ring/jitter buffer, fixed-size frames and
  underrun/overrun metrics; heavy DSP stays off the main thread.
- Silero ONNX VAD consumes the worklet's normalized PCM directly (no
  fake-MediaStream round trip).
- Disconnect/reconnect is visible in the composer and safe (the jitter
  buffer is flushed on `resync`/`discontinuity`).

## Acceptance mapping

| # | Criterion | Evidence |
|---|---|---|
| 1 | Browser mic still works | untouched: the bridge is a tap, the listener path is unchanged |
| 2 | Voice PE selectable source | `hello` + `subscribe` contract; `state`/`device` messages |
| 3 | Live audio in the PWA | framed PCM stream with seq/ts; EOS terminator |
| 4 | Silero gets PCM directly | PCM16LE frames -> worklet -> normalized float32 (no MediaStream) |
| 5 | Worklet buffering off main thread | PWA-side component against this contract |
| 6 | Synthetic MediaStream compatibility-only | PWA-side adapter (`MediaStreamAudioDestinationNode`) |
| 7 | PWA mode in BrowserOS | loopback WS reachable from the installed PWA (origin allow list) |
| 8 | Disconnect/reconnect visible, safe | `resync` + `discontinuity` + close codes; no silent fallback |
| 9 | Bounded buffers proven | tap metrics: `frames_queued` <= `max_frames`, `dropped_frames` counts |
| 10 | No Windows virtual microphone | the bridge is a direct WS path; `virtual_microphone_enabled` is independent |

## Observability

`debug_log` lines carry `category=voice_pe_bridge`: server start, tap
attach, rejections. `health()` exposes enabled/running/port/token/origins/
clients/attached; `metrics()` folds in per-session counters (frames, bytes,
gaps, dropped) and the tap's depth/drop counts.