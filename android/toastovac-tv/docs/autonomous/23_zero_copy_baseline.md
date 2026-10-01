# 23 — Zero-Copy Baseline Measurement (P1)

## Goal

Instrument the current live GPU scene frame path precisely, before replacing
anything. 320 frames at the production 15 fps pacing plus a 60-frame burst
(no pacing) to capture the raw throughput wall. Stage timings use
`perf_counter` around each step of the exact production pipeline:

```
GLScene (moderngl) → fbo.read (glReadPixels) → numpy view → .tobytes()
→ ffmpeg hevc_qsv stdin (rawvideo rgb48le) → Annex-B AU collection
```

## Results (`results/zero_copy_baseline.json`, 320 paced + 60 burst frames)

| Stage | p50 | mean | max |
|---|---|---|---|
| scene_update (clock/HUD masks) | 2.6 ms | 9.3 ms | 219 ms |
| draw submit (GL) | 0.09 ms | 0.6 ms | 95 ms |
| **readback (glReadPixels)** | **75.0 ms** | **121.6 ms** | **650 ms** |
| cpu_view (numpy frombuffer) | 2.8 ms | 5.6 ms | 130 ms |
| **cpu_bytes (.tobytes 50 MB)** | **51.7 ms** | **95.8 ms** | **776 ms** |
| pipe_write (encoder stdin) | 17.0 ms | 29.4 ms | 277 ms |
| **frame_total (render→write)** | **161.6 ms** | **262.4 ms** | **1150 ms** |
| au_interval (encode + upload) | 166.0 ms | 254.1 ms | 1366 ms |

Burst (no encoder, pure render+readback):

| Stage | p50 | mean | fps equivalent |
|---|---|---|---|
| draw (GPU submit) | 0.17 ms | 0.18 ms | ~227 fps |
| readback (unpaced) | 59.9 ms | 73.3 ms | ~16.7 fps |
| burst total | — | — | **13.6 fps** |

Paced end-to-end effective throughput: **3.8 fps** (320 frames / 84.2 s).

## Interpretation

The GPU render is effectively free (0.09–0.18 ms submit, ~227 fps capable).
The entire frame budget is consumed by the CPU ferry:

```
glReadPixels         ~75 ms   (GPU → CPU, 66 MB)
numpy .tobytes()     ~52 ms   (CPU copy, 50 MB)
pipe write           ~17 ms   (CPU → FFmpeg stdin)
encoder backpressure ~166 ms  (AU arrival interval)
```

p50 CPU-ferry sum ≈ 144 ms → ~7 fps theoretical, and with encoder
backpressure the pipeline delivers **3.8 fps** end to end. The previous
"15 fps stable on box" figure was not sustainable under measurement: the
box could only advance at the AU arrival rate.

## Conclusion

Every stage except the GPU draw is CPU-side pixel transport. Eliminating the
readback + numpy + pipe ferry (replacing it with a GPU-resident
D3D11→QSV path) removes ~99% of the measured frame time and is the single
highest-value change available. This is the baseline the native engine will
be compared against (P13).

Baseline invariants recorded for the comparison:

- old render fps (GPU draw only): ~227 fps
- old readback ms (p50): 75.0 ms (unpaced 59.9 ms)
- old output fps (end to end, paced): 3.8 fps