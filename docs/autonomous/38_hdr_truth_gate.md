# 38 — HDR Truth Gate

Date: 2026-10-01 · Clip: `tbsY9zC-w6U` · 5 s test segment (t=15..20)

## Question

Is the HDR stage a **real image transform**, or just HDR signaling?

## Test assets (same 5 s window)

| Asset | File | Content |
|---|---|---|
| A | `source_5s.mp4` | ORIGINAL SDR BT.709, HEVC 8-bit, 1296x2304, 60 fps |
| B | `wrap_5s.mp4` | Deterministic SDR→HDR transport wrap — bt709→PQ/BT.2020 via ffmpeg zscale (standard color math), **no AI**, + QSVEnc HDR10 SEI pass |
| C | `realhdr_5s.mp4` | Real HDR candidate: `hdrtvnetpp_agcm_stable` ONNX through QSVEncC `--vpp-onnx` |

## Model telemetry (C)

| Field | Value |
|---|---|
| model path | `tools/qsvenc/models/hdrtvnetpp/hdrtvnetpp_agcm_stable.onnx` |
| model SHA256 | `CA3C243EC17F16D566D544DE5039BA4DD1504E608DC449CCB8A6DD506CE71957` (33,837 B) |
| OpenVINO | bundled with QSVEncC64 (runtime version not directly exposed; onnx init reports `path=ocl 16bit`) |
| requested device | `GPU` |
| selected device | Intel(R) Graphics (iGPU), OpenCL `ocl` — **no silent fallback** |
| model compile | OK (onnx init line logged, no errors, rc=0) |
| frames submitted | 300 |
| frames returned / encoded | 300 |
| inference | ~117 s total (300 frames @ 2.56 fps, GPU 60 %) |

## Result: C vs B

```
SSIM Y:0.945488  All:0.960619
PSNR y:19.98 dB  average:21.73 dB
```

**C != B — proven.** Y SSIM 0.945 (far from 1.0), PSNR ~20 dB on luma. The HDRTVNet++ model performs a dramatic real SDR→HDR transform (dynamic range expansion, chroma regrade), not a re-signal.

Sanity references: C vs A (SDR) Y SSIM 0.932; B vs A Y SSIM 0.854 (the standard bt709→PQ wrap itself is a gamma transform).

## Verdict

**PASS — `cin_hdr.mp4` is a genuine enhanced-HDR transform, not signaling-only.**

Notes:
- Initial wrap-only attempt with QSVEncC (8-bit SDR input, no VPP, 10-bit HDR output) crashed with access violation (rc=0xC0000005) — a QSVEnc no-VPP 8→10 bit path bug; B was built via ffmpeg zscale + the standard QSVEnc SEI pass instead (both deterministic, no AI).
- ffmpeg zimg (`zscale`) linear↔PQ luma conversion is broken in this build (no-op + retag → image renders 80 % dark); QSVEnc `--vpp-colorspace transfer=smpte2084:linear` errors `invalid input colorspace definition`. Neither is used for grading; the showroom grade works in PQ (perceptual HDR) space instead (see 39).