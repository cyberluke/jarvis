# 20 — Desktop cast: GPU-native capture (P3)

## Capture backend

**DXGI Desktop Duplication** via `dxcam` 0.3.0 (GPU-native frame acquisition)
replaces gdigrab. The installed ffmpeg 7.1.1 essentials build has no
`d3d11hw`/`ddagrab` demuxer, so the DXGI path is driven from the host process:

```text
DXGI Output Duplication (dxcam, GPU copy)
→ raw rgb24 frames (pipe)
→ format=p010le
→ Intel hevc_qsv Main10
→ TZHL :8768
→ c2.amlogic.hevc.decoder (createCount=1)
→ SurfaceView / HwcVideo
```

P3.1 — the desktop producer feeds the SAME `_Q → mux → TZHL` transport as
every other source; no encoder/transport code was duplicated.

## Resolution truth (P3.2)

The active desktop is genuinely **3840×2160** (verified two ways this run:
ffmpeg gdigrab reports "Capturing whole desktop as 3840x2160x32" and dxcam
reports `Res:(3840, 2160) Primary:True`). The cmd builder reads the real
output size at build time and only letterboxes (never stretches) if it were
smaller. No invented resolution.

## SDR/HDR truth (P3.3)

The desktop is SDR and is cast as SDR: the encoder tags bt709/bt709 (never
PQ), matching the proven SDR path. No SDR frame is labeled PQ.

## Metrics (P3.4) — 30 fps target reached

```text
capture_fps        : 30.02   (paced delivery; dxcam thread runs at 60)
encoder_fps        : 30.0    (idr delta 10 per 10 s at g=30)
capture_latency_ms : 10.69
dropped_frames     : 0
queue_depth        : 0
frames_written     : 1650
```

First attempt delivered ~26–27 fps (capture-bound); the fix was to pace the
pump at exactly 30 fps with the dxcam thread targeting 60 (always a fresh
frame). 60 fps is a documented next step (encoder measured 35–36 fps at 4K
veryfast standalone, so a 60 fps push needs encoder tuning first).

## Box rendering evidence

- `/cast/status`: `source=desktop`, `clients=1`, idr advancing
- box: `ai.toastovac.tv/.LiveHdrActivity` focused; `SurfaceView BLAST
  Consumer` layer active (4096×2304); decoder `createCount=1`
- host log: `stat src=desktop:desktop ... clients=1`

## Evidence

- `results/desktop_wgc.json`
- `/cast/status.desktop` (`capture_backend`, `capture_fps`,
  `capture_latency_ms`, `dropped_frames`, `queue_depth`)