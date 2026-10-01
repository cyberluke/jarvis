# 46 — SHOWROOM_D: AI structure + deterministic nit-targeted luminance remap

Date: 2026-10-01 · Clip: `tbsY9zC-w6U` · Phase-1 engine, 5 s window (t=15–20)

## Why D

User verdict on phase-1 candidates:

- `ONNX_NPU` (HDRTVNet++ NPU) — best structure and skin, but P50 69.5 nits is
  too dark and per-frame p99 CV 34.6 % (highlight variance/shimmer risk).
- `LP_B` (libplacebo metadata expansion) — great showroom punch and temporal
  stability, but it is a *linear* expansion that does not understand the scene.
- `HYBRID_SHOWROOM_B` — overdriven (P99 1862 / max 3249 nits): "luminance
  flamethrower" for a 1000-nit Samsung. Retired.
- `WRAP ONLY` — control, retired.

Final architecture (user-specified): **AI decides WHERE, algorithm decides HOW
MANY nits.**

```
SDR
  → HDRTVNet++ NPU        "what is highlight / skin / shadow"
  → deterministic luminance remap   "how many nits to give it"
  → libplacebo gamut / presentation
  → QSV HEVC Main10 HDR10
```

No PQ-space `contrast=` amplifier. The remap is an explicit luminance curve with
target nit values and a soft knee around 800–1000 nits.

## Pipeline (built & verified)

Source: `ONNX_NPU.mp4` (HDRTVNet++ NPU fp16, already bt2020/pq 10-bit tv-range,
AI gamut expansion baked into the model: `matrix=bt709 -> matrix_out=bt2020`).

Stage 1 — FFmpeg 9.0.2 (Intel Xe Vulkan):

```
format=gbrp10le,
curves=r='<PQ signal points>':g='<same>':b='<same>':interp=pchip:plot=showroom_D_curve.gp,
format=yuv420p10le,
libplacebo=tonemapping=none:gamut_mode=perceptual:format=yuv420p10le:
          color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc:
          peak_detect=false:extra_opts=gamut_expansion=on:saturation=1.04
→ intermediates/D.s1_remap.mkv (ffv1, lossless)
```

Stage 2 — QSVEncC 8.31: HEVC Main10, yuv420, bt2020nc/smpte2084,
`--max-cll 1000,400`, mastering display `L(10000000,1)` → `SHOWROOM_D.mp4`.

### The luminance curve (explicit nit targets)

PCHIP (Fritsch–Carlson, monotone, no overshoot) through 18 control points,
computed in PQ signal domain (SMPTE 2084 inverse EOTF):

| input nits | output nits | note |
|---|---|---|
| 0 | 0 | black stays black |
| 1 | 1.2 | |
| 3 | 4 | |
| 8 | 11 | |
| 20 | 30 | shadow lift (moderate) |
| 45 | 80 | |
| **69.5** | **130** | ONNX P50 → target 100–180 |
| 120 | 210 | |
| 200 | 330 | |
| **345.5** | **540** | ONNX P90 → target 400–650 |
| 550 | 760 | |
| 750 | 920 | knee entry |
| **943.5** | **950** | ONNX P99 → target 850–1000 |
| 1150 | 1030 | **soft knee ~800–1000** |
| 1448 | 1150 | ONNX max → target 1100–1200 |
| 2200 | 1180 | roll-off |
| 4000 | 1200 | |
| 10000 | 1205 | hard cap ≈ 1200 nits |

Key property: the curve *lifts mids* (P50 ×1.87, P90 ×1.56) and *rolls off the
top* above the knee, capping speculars at ≈1200 nits — exactly the opposite of
a PQ-space contrast boost, and it measurably *reduces* highlight temporal
variance (p99 CV 34.6 % → 18.3 %).

### Why RGB-domain curves (engineering finding)

The `curves` filter in FFmpeg 9 normalizes its x/y points in the **signal
domain** (for `gbrp10le` full-range RGB: `x = PQ signal V = code/1023`; for
limited-range YUV input the observed mapping is `(code−64)/876`), not `code/1023`.
Points computed as `pq_inv(nits/10000)` land exactly on target (P50 125.5 vs
130 predicted, P90 540.5 vs 540, P99 962.5 vs 950).

Applying the same curve to R/G/B in `gbrp10le` (canonical master curve) keeps
chroma consistent: skin hue shift **+0.37°** (vs **+2.74°** when `master` was
applied in YUV mode — the YUV path visibly perturbs chroma/hue). The RGB path
also lifts blacks less (p1 +4 code vs +15).

## Metrics (half-res pipeline, same as all phase-1 candidates)

| metric | ONNX_NPU | SHOWROOM_D | target |
|---|---|---|---|
| P50 | 69.5 | **125.5** | 100–180 ✓ |
| P90 | 345.5 | **540.5** | 400–650 ✓ |
| P95 | 460.5 | 674.5 | — |
| P99 | 943.5 | **962.5** | 850–1000 ✓ |
| >1000 nits | 0.523 % | **0.278 %** | ↓ (was 0.52 %) |
| 800–1000 nits | 1.491 % | 3.076 % | highlight band enriched |
| >600 nits | 3.279 % | 7.219 % | |
| black p1 / p5 (code) | 151.5 / 214.5 | 156.5 / 225.5 | +5 / +11 (minimal lift) |
| skin hue delta | — | **+0.93°** | preserves NPU skin |
| skin chroma delta | — | +2.5 % | mild |
| edge overshoot | — | **0.000 %** | ✓ |
| temporal p99 CV | 34.6 % | **18.3 %** | shimmer reduced |
| temporal p99 mean | — | 845 nits | |

Full-res ground truth (whole clip, no scaling):

| file | P50 | P90 | P95 | P99 | max |
|---|---|---|---|---|---|
| ONNX_NPU | 66.5 | 345.5 | 456.5 | 943.5 | 2046* |
| SHOWROOM_D (stage-1, clean) | 122.5 | 540.5 | 666.5 | 962.5 | **1212** |
| SHOWROOM_D (final master, CQP) | 122.5 | 540.5 | 666.5 | 962.5 | **1360** |
| LP_B (final QSV) | 255.5 | 1004.5 | 1162.5 | 1277.5 | 2833* |

\* final-encode max is QSV edge ringing; the original ICQ23 encode of D peaked
at 2345 nits, which the **anti-ringing pass (doc 47)** fixed: final master uses
**CQP (QP 22) + `--quality best`** → post-encode max 1360 nits (+12 % over
stage-1), PSNR-Y 57.0 dB, 34.5 Mbps. Half-res metric scripts also under-report
the input max (1448 half-res vs 2046 full-res ONNX) and can show bicubic
overshoot.

## TV

New 3-way compare deployed and streaming on the 65" Samsung (HDR10):

```
1. LIBPLACEBO B  (LP_B)
2. HDRTVNET NPU  (ONNX_NPU)
3. SHOWROOM D    (SHOWROOM_D)
```

- `enhanced/showroom_compare.mp4` = new 15 s 3-way deck (3840x2160, HEVC
  Main10 bt2020nc/smpte2084, HDR10 SEI max-cll 1000,400), loops.
- `enhanced/showroom_c.mp4` = SHOWROOM_D (instant-switch c replaces HYBRID_B).
- Cast host: `src=showroom:tbsY9zC-w6U:compare`, clients=1 (box 192.168.1.122),
  idr/cfg incrementing, avg ≈12 Mbps, 0 drops/errors.
- Sink verified: Samsung 65", `HdrCapabilities{mSupportedHdrTypes=[2,3,4],
  mMaxLuminance=1000.0}`, `HDR_CONVERSION_PASSTHROUGH`, 4K60 mode active;
  video presented on the AMLogic video plane (vendor HDR path, same pipeline
  that verified `HDR10-GAMMA_ST2084` in doc 40).

## Delivered artifacts

- `SHOWROOM_D.mp4` (final master — anti-ringing CQP encode, doc 47)
- `intermediates/D.s1_remap.mkv` (lossless stage-1)
- `build_showroom_d.py` (reproducible: `--sat 1.04 --tag D`), `showroom_D_curve.gp`
- `showroom_phase1_v2_compare.mp4` + `intermediates/compare2.fg`
- `frames/seg2_{lp,onnx,d}.png` (segment proof frames)
- `results/phase1_showroom_d.json`

## Pending

User TV verdict on the 3-way deck (loop + instant-switch a/b/c). On verdict:
render winner full-clip (P25) via `enhanced/cin_showroom_hdr.mp4`.