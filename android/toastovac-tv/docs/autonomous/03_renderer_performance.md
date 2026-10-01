# 03 — Renderer performance

## Goal
Move the live scene off the 230–400 ms/frame full-CPU raster without lowering
resolution or switching SDR.

## Starting state
Preroll of 30 static frames looped at 15 fps. Clock/Spark not live.

## Changes
`tools/live_hdr/hdr_scene.py`: `LiveScene` caches the static 4K PQ layer once
and redraws only clock text, Spark, and the moving highlight. Text glyphs are
cached. `server.py` / `cast_host.py` feed live frames (no preroll loop).

## Measured
```text
render_ms ≈ 20–45 (typical ~25)
requested fps = 15 (headroom for 30 later; not claimed)
resolution still 3840×2160 PQ RGB48 → P010 QSV
```

## Proven
Live clock + Spark at useful realtime on CPU without a GPU engine.

## Unproven
30/60 fps; D3D/Vulkan path. No existing GPU scene renderer in-repo to reuse.

## Next
Keep CPU incremental path; GPU only if 30+ fps is required.
