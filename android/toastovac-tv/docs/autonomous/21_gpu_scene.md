# 21 — Live SceneGraph GPU renderer (P4)

## Backend

`tools/live_hdr/gpu_scene.py` — **OpenGL via moderngl 5.12** on the Intel GPU
(the smallest available GPU path; D3D11/12/Vulkan would not reduce the
readback wall). The SceneGraph semantic state (clock, Spark, blob, hud, static
layer) is consumed by fragment shaders; the CPU rasterizer is not
micro-optimized further.

## Semantic SceneGraph preserved (P4.2)

Renderer is a pure consumer of the same state the CPU renderer used
(clock / Spark / blob / text / hud). `render(t, hud)` returns the identical
PQ-encoded Rec.2020 RGB48 uint16 format, so the encoder/transport path is
unchanged and the interview scene keeps working on top.

## Minimal GPU port (P4.3)

- near-black background, cyan/lavender chips, PQ ramp, 1 px hairlines,
  wordmark → composited ONCE into a static texture
- clock, Spark, moving HDR blob, hud → fragment shaders per frame
- full 3840×2160 physical resolution (unchanged)

## Performance (measured)

```text
GPU draw alone          : 4.4 ms/frame  (~227 fps GPU capability)
full render + readback  : 41.9 ms/frame (23.9 fps)
readback                : RGBA16UI raw uint16 (66 MB) — zero numpy conversion
scene pipeline          : 15 fps stable on the box (frames advancing, clients=1)
```

The first implementation (float16 readback + numpy conversion) measured
4.2 fps; the bottleneck was the Python float conversion (145–210 ms/frame),
eliminated by rendering to an integer **RGBA16UI** target — the readback
bytes are already the RGB48 values.

## HDR preserved (P4.4)

High-precision PQ values (full 16-bit in the render target), BT.2020 primaries
and PQ transfer tags on the QSV encoder unchanged — 60 fps would never be
bought by dropping to SDR. Box evidence: `SurfaceView` active, HwcVideo
DEVICE path, same as the golden HDR proof.

## Honest target status

- **≥30 fps first target: not reached via Python readback on this iGPU**
  (glReadPixels wall ≈ 42 ms at 4K RGBA16UI).
- The genuine path to 30–60 fps is **zero-copy GL→QSV** (D3D11 texture
  sharing into `hevc_qsv` `hw_frames`). This is a native interop task and is
  the documented next step — not attempted this run.
- The GPU renderer IS the live scene path now (no fallback triggered) at the
  proven 15 fps pipeline rate with 1.6× render headroom, freeing the CPU.

## Evidence

- `results/gpu_scene.json`
- host log: no `GPU_SCENE_UNAVAILABLE`, `stat src=scene:live ... clients=1`
- box SurfaceFlinger: SurfaceView layer active