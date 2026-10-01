# 43 — libplacebo Inverse Tone Mapping (Intel Xe Vulkan)

## What was tested
FFmpeg 9.0.2 `vf_libplacebo` (libplacebo v7.371.0) on Intel Xe (Vulkan 8086:7d67),
fixed 5 s SDR test source `showroom/source_5s_sdr.mp4` (1296x2304@60, BT.709).

## Empirical findings (this build)
1. **Tone-map function selection is a no-op without `inverse_tonemapping=true`**
   — `spline`, `clip`, `bt.2446a` all produce byte-identical frames for a
   plain SDR→PQ conversion (each maps to the same ~2× perceptual lift).
2. **`inverse_tonemapping=true` expands to the output colour-space peak**
   (PQ = 10 000 nits nominal): spline → P50 480 nits / 40 % pixels >1000 nits
   (too hot for a 1000-nit TV); bt.2446a even hotter. Function choice DOES
   change output in this mode, but the target peak cannot be capped through
   this filter (FFmpeg strips mastering-display side data when output tags
   change; `pl_options` exposes no `target_peak` key).
3. **`contrast_recovery` is ineffective in this FFmpeg integration** — the
   libplacebo feature-map input (`args->feature_map`) is not wired by
   `vf_libplacebo`, so the option has zero effect (verified byte-identical
   outputs with 0.0 and 0.3).
4. **Working controlled expansion: mastering-display metadata on the INPUT.**
   `-mastering_display "…L(<peak>0000,1)"` makes libplacebo interpret the SDR
   signal as a `<peak>`-nit signal; the SDR→PQ re-encode then expands luma
   linearly by `<peak>/100`. Deterministic, temporally stable, no AI:
   - peak 500 → P50 101 nits (5×), P99 491, max 764, 0 % >1000
   - peak 1000 → P50 203 nits (10×), P99 983, max 1558, 0.36 % >1000
   - peak 1200 → P50 264 nits (12×), P99 1277, max 2003, 10.3 % >1000
   - peak 3000 → P50 613 nits (30×), P99 2955, max 4809, 38 % >1000
5. **`gamut_expansion=on` (extra_opts) works** — boosts chroma (+20–40 %
   median) and slightly lifts luma; required for showroom colour pop.
6. **`color_adjustment` works** — `saturation=`, `contrast=`, `gamma=` on the
   filter shape chroma/midtones (separator `:`; comma breaks parsing).

## Candidate recipes (LP_A/B/C)
```
ffmpeg -mastering_display "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(<peak>0000,1)" \
  -init_hw_device vulkan:0 -i source_5s_sdr.mp4 \
  -vf "libplacebo=tonemapping=spline:contrast_recovery=0.3:gamut_mode=perceptual:format=yuv420p10le:\
       color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc:peak_detect=false:\
       extra_opts=gamut_expansion=on[:saturation=…:contrast=…]" -c:v ffv1 out.mkv
# then QSVEncC → HEVC Main10 BT.2020/PQ + HDR10 SEI (max-cll 1000,400)
```
- LP_A = peak 1000 (Natural HDR): P50 203 / P90 780 / P99 983 / max 1558,
  >1000 0.36 %, skin hue +4.25°, chroma +22.8 %, black p1/p5 248/317, p99 CV 4.8 % (stable)
- LP_B = peak 1200 + saturation 1.06 + contrast 1.03 (Showroom HDR):
  P50 264 / P90 1004 / P99 1277 / max 2003, >1000 10.3 %, skin +4.25°,
  chroma +30.4 %, black 264/335, p99 CV 5.1 % (stable)
- LP_C = peak 1500 + saturation 1.1 + gamma 0.97 (Max safe):
  P50 294 / P90 1162 / P99 1478 / max 2369, >1000 15.9 %, skin +4.40°,
  chroma +38 %, black 265/337, p99 CV 5.0 % (stable)

## Guardrail assessment (P24)
- Blacks: LP family lifts dark pixels (+100 code ≈ +30 nits) — visible but the
  source is a purple-lit stage scene, not true black; acceptable, watch on TV.
- Skin: hue +4.2–4.4° (gamut-mapping to BT.2020) — tolerable for stage light.
- Clipping: 0.36–16 % >1000 nits by candidate strength (TV rolls off above 1000).
- Temporal: p99 CV 4.8–5.1 % ≈ source variance — no pumping/flicker.

## Status
P12/P13 PASS (three libplacebo candidates, real expansion). P20 stability PASS
for the LP family. Full numbers in `results/libplacebo_inverse_tonemap.json`.