# 45 — Phase 1 SDR→HDR Showroom Engine

## Objective
Stop treating SDR→HDR as a colours-space/tagging problem; build a real
SDR→HDR remaster with the existing stack only (ONNX/OpenVINO + FFmpeg 9.0.2/
libplacebo + Intel Xe/NPU/CPU + QSV). Result must be immediately visible on
the 65" Samsung at sofa distance — a spotlight materially brighter than a face.

## Final candidate set (identical 5 s frames, t=15–20 of source)
| candidate | recipe | P50 | P90 | P99 | max | >1000 | skinΔhue | p99CV |
|---|---|---|---|---|---|---|---|---|
| WRAP ONLY | BT.709→PQ retag, no enhancement (P11) | 20 | 79 | 99 | 180 | 0 % | 0 | 5.0 % |
| LP_A | libplacebo peak1000 + gamut expansion | 203 | 780 | 983 | 1558 | 0.36 % | +4.25° | 4.8 % |
| LP_B | libplacebo peak1200 + sat1.06/contrast1.03 | 264 | 1004 | 1277 | 2003 | 10.3 % | +4.25° | 5.1 % |
| LP_C | libplacebo peak1500 + sat1.1/gamma0.97 | 294 | 1162 | 1478 | 2369 | 15.9 % | +4.40° | 5.0 % |
| ONNX_XE | HDRTVNet++ GPU fp16 | 71 | 360 | 962 | 4324 | 0.77 % | +5.04° | 34 % |
| ONNX_NPU | HDRTVNet++ NPU fp16 | 69 | 345 | 943 | 1448 | 0.52 % | +1.58° | 35 % |
| HYBRID_B | ONNX_NPU → libplacebo contrast1.1/sat1.12 | 111 | 626 | 1862 | 3249 | 3.8 % | +2.18° | 37 % |

## TV comparison (P22/P23) — LIVE
`showroom_phase1_compare.mp4` (3840x2160 HEVC Main10 BT.2020/PQ + HDR10 SEI,
4 × 5 s sequential full-screen with labels):
1. WRAP ONLY → 2. LIBPLACEBO SHOWROOM → 3. HDRTVNET NPU → 4. HYBRID SHOWROOM
Instant-switch sources deployed as `enhanced/showroom_{a,b,c}.mp4`
(a=LP_B, b=ONNX_NPU, c=HYBRID_B) via the cast host `cast.showroom`.
Verified: TV `HDR current type: HDR10-GAMMA_ST2084`, box connected (clients=1),
avg 7.9 Mbps, idr/cfg incrementing, no drops/errors in host log.

## Winner selection (pending user TV verdict, P25)
- LIBPLACEBO best: **LP_B** — largest clean luminance jump, temporally stable.
- HDRTVNET++ best: **ONNX_NPU** — cleanest skin, blacks preserved, 5.07 fps.
- HYBRID: **HYBRID_B** — AI structure + libplacebo presentation; strongest
  combination candidate (P17), but carries the ONNX temporal highlight variance.
- After user choice: `SHOWROOM_HDR_V1` → full-clip render `enhanced/cin_showroom_hdr.mp4`
  → optional ladder (cleanup → CAS → colour finish).

## Hard rules honoured
- Intel only: Xe iGPU + NPU + CPU proven; **no NVIDIA execution** (Vulkan
  adapter pinned to 8086:7d67; ONNX devices GPU/NPU/CPU identity verified).
- No silent fallback: every candidate either ran on the requested device or
  failed (none failed silently).
- FFmpeg 7.1.1 preserved (`tools/ffmpeg/7.1/`); 9.0.2 isolated under
  `tools/ffmpeg/9.0.2-stock/`.
- QSV/oneVPL HEVC Main10 final encode + existing HDR10/TZHL transport unchanged.

## Status
Phase 1: **PASS (candidates + TV validation complete; winner pending user verdict)**.
Full numbers in `results/phase1_showroom_hdr.json`.