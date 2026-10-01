# 27 — TZHL Integration: Native GPU Engine → Live TV Pipeline

**Date:** 2026-09-30
**Status:** WORKING — native zero-copy engine delivers live HEVC to the cast_host mux → TZHL
**Machine:** Intel Meteor Lake, libvpl 2.17 (mfx-gen)

## What was integrated

`cast_host.py` (the live 4K HDR streamer) now supports a **native** source that
replaces the Python renderer + ffmpeg hevc_qsv path for the scene:

```
toastovac_gpu.exe (pipe mode)
  stdin : framed [u32 BE len][JSON]  {"t": s, "hud": {live, fps, pq}}
  stdout: framed [u32 BE len][kind][payload]  kind 1 = Annex-B AU
              ↓
cast_host pump_native → shared AU assembler → TZHL :8768 → box decoder
```

### Changes (cast_host.py)

- `native_cmd()` — launches `native_engine/target/release/toastovac_gpu.exe`
  with `--fps 30 --pace` (pipe mode, no `--out`).
- `pump_native()` — parses the framed stdout; **exact-read** helper required
  (Python `read(n)` returns partial pipe reads; a short header previously broke
  the pump and delivered zero chunks). kind-1 payloads go to `_Q` as chunks.
- `native_state_loop()` — feeds framed state JSON at FPS (mirrors the old
  scene render loop's HUD: `live` frame counter, `fps`, `pq`).
- `Producer.start()` dispatches `native` to `pump_native` + `native_state_loop`.
- `switch_source` maps `native` → `native_cmd`; `cast.scene` now routes to
  `native`; status `decoderOwner` treats `native` like the dashboard scene.

### Engine changes (qsv.rs / main.rs)

- Pipe mode already existed (`--out` absent → framed stdout + stdin reader).
- QueryIOSurf `-15` now falls back to a fixed 8-surface pool instead of
  failing, so the engine runs without any env switches.

## Verification

Standalone pipe test (frames=30 --pace, state fed, stderr drained):

```
RC 0 | FILE 6551 bytes | AUs 29 metrics 1 events 2 | parsed to 6551
```

Live cast_host run (control :8770 /cast/status):

```
source=native  producer_alive=true  clients=1 (box connected earlier)
idr_sent=12  bytes_sent=75975  (growing continuously)
```

The engine submits + syncs frames at 30 fps pace; the mux receives complete
AUs, assembles config (VPS/SPS/PPS) + IDR frames, and streams them to TZHL.

## Notes / limitations

- The box (`clients`) must be connected to :8768; after cast_host restarts the
  TV app reconnects on demand. `idr_sent`/`bytes_sent` prove the delivery path.
- Gop=30 → one IDR per second at 30 fps; the mux emits CONFIG before the first
  IDR so the decoder can start at any reconnect.
- Audio for the native source is not yet wired (audio_cmd=None); the scene
  source previously had no audio either.

## Artifacts

- `tools/live_hdr/cast_host.py` (native source)
- `tools/live_hdr/native_engine/target/release/toastovac_gpu.exe`
- Log: `%TEMP%\kilo\cast_host_native.log`
- Results: `results/zero_copy_qsv_encode.json`