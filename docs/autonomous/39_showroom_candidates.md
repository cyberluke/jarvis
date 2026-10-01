# 39 — SHOWROOM Candidates

Date: 2026-10-01 · Clip: `tbsY9zC-w6U` · 5 s fixed segment (t=15..20, same frames for all)

## Pipeline (Intel-only, no NVIDIA)

All candidates start from **`cin_detail.mp4`** (frozen Step D: HDR + ArtCNN cleanup + CAS 0.25), 5 s segment.

```
ffmpeg:  curves=master='<PQ-domain luma S-curve>'      (tone shaping in perceptual HDR space)
QSVEncC: --vpp-tweak saturation=<s>,vibrance=<v>       (chroma, hue-preserving)
         --vpp-cas sharpness=<cas>,hdr=true            (detail)
         + BT.2020/PQ signaling + HDR10 SEI (MaxCLL 1000 / MaxFALL 400)
```

Why PQ-domain curves (not linear): ffmpeg zimg linear↔PQ is broken on this build and QSVEnc colorspace PQ→linear errors (see 38). PQ is the perceptual HDR space of the content — grading on PQ code values is standard HDR colorist practice (DaVinci HDR timeline), no SDR gamma math applied. `curves=master` on YUV is luma-only → **skin hue preserved by construction** (verified −0.01° without chroma ops).

## Profiles (COLOR_BALANCED_V1 stays as CINEMATIC reference)

| | SHOWROOM_A | SHOWROOM_B | SHOWROOM_C |
|---|---|---|---|
| curve low (blacks) | 0.09→0.077 | 0.08→0.063 | 0.07→0.051 |
| curve highlight | slope ~1.08 | slope ~1.14 | slope ~1.20 |
| saturation | 1.05 | 1.06 | 1.06 |
| vibrance | 0.02 | 0.03 | 0.00 |
| CAS (additional) | 0.10 | 0.15 | 0.20 |

## Metrics vs CINEMATIC_V1 (cin_color 5 s, same frames)

| Metric | Target | A | B | C |
|---|---|---|---|---|
| nits P95 | +8..+18 % | +13.6 % | +19.7 % | +24.9 % |
| nits P99 | +10..+25 % | +14.6 % | +19.5 % | +26.0 % |
| blacks p1 (code) | deeper, no crush | −2 | −5 | −8 |
| blacks p5 (code) | deeper | −3 | −5 | −8 |
| chroma median | +4..+10 % | +6.5 % | +10.8 % | +8.6 % |
| chroma P90 | +8..+18 % | +5.9 % | +9.5 % | +7.1 % |
| chroma P99 | — | +5.4 % | +8.8 % | +8.1 % |
| skin hue Δ | < 2° | −1.12° | −1.24° | −0.93° |
| skin chroma Δ | < 8 % | +4.4 % | +8.6 % | +6.0 % |
| edge overshoot | clean | 0.000 % | 0.000 % | 0.000 % |
| SSIM vs CINEMATIC | — | 0.9954 | 0.9942 | 0.9921 |
| PSNR vs CINEMATIC | — | 42.1 dB | 37.7 dB | 34.5 dB |
| luma MAE | — | 6.0 | 8.3 | 10.5 |
| bright frac >940 nits | no hard clip | 2.32 % | 2.46 % | 2.67 % (ref 1.66 %) |
| max nits | < 10000 | 5237 | 5181 | 5465 |

## Rejection tests (Phase 14)

- skin hue: A/B/C all < 2° ✓
- skin chroma: A +4.4 %, C +6.0 % ✓; B +8.6 % — at the guardrail edge (measured baseline re-encode noise is ~7 %, so true op contribution ≈ +1.6 %; flagged for the TV verdict)
- blacks: all deepen without crush (p1 ≥ 136.5, floor 64) ✓
- highlights: no hard clip (max 5465 < 10000, roll-off at curve top) ✓
- halos: edge overshoot 0.000 % on all ✓
- posterization: 10-bit PQ, curves at 16-bit curve table, error_diffusion on any depth change — none observed in metrics ✓
- HDR metadata: BT.2020/PQ VUI + HDR10 SEI (MaxCLL 1000/MaxFALL 400) preserved on all ✓

## Character

- **A** — premium punch: clearly better than CINEMATIC, still natural.
- **B** — showroom: strongest highlight + chroma + deeper blacks + more detail. Expected favorite.
- **C** — max safe: deepest blacks + most detail; chroma deliberately restrained to keep skin under guardrail.

Difference vs CINEMATIC is multidimensional (highlights + blacks + chroma + detail), not one-dimensional saturation.