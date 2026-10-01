# 47 — QSV anti-ringing pass (SHOWROOM_D final master encode)

Date: 2026-10-01 · Clip: `tbsY9zC-w6U` · Source: `intermediates/D.s1_remap.mkv` (clean stage-1, max 1212 nits)

## Problem

The phase-1 final encodes used QSVEncC **ICQ 23**. For SHOWROOM_D the clean
stage-1 master peaks at **1212 nits**, but the ICQ23 encode produced isolated
highlight spikes up to **2345 nits** (HEVC edge ringing on the steepened
highlight gradients). The encode was changing the luminance envelope.

## Sweep

Encoded the 300-frame stage-1 master with QSVEncC 8.31 (HEVC Main10,
yuv420, bt2020nc/smpte2084, max-cll 1000,400, MD L(10000000,1)) across RC modes
and measured per-mode: post-encode full-res max (ringing), PSNR-Y (10-bit,
peak 1023, whole clip), luma MAE, and bitrate.

| mode | bitrate | size | max (nits) | PSNR-Y | luma MAE |
|---|---|---|---|---|---|
| ICQ 23 (baseline) | 10.9 Mbps | 6.5 MB | **2345** | — | — |
| ICQ 18 | 14.5 Mbps | 8.6 MB | 1881 | 53.71 dB | 1.381 |
| ICQ 15 | 17.8 Mbps | 10.6 MB | 1712 | 54.81 dB | 1.238 |
| ICQ 12 | 23.5 Mbps | 14.0 MB | 1558 | 55.56 dB | 1.160 |
| CQP (QP 22, driver min) | 34.4 Mbps | 20.5 MB | 1389 | 56.97 dB | 1.005 |
| **CQP 22 + `--quality best`** | **34.5 Mbps** | **20.5 MB** | **1360** | **57.00 dB** | **1.002** |

Notes:

- Percentiles (P50/P90/P95/P99) are identical across every mode
  (122.5/540.5/666.5/962.5) — the luminance envelope is untouched; only the
  ringing spikes shrink.
- The QSV driver clamps CQP QP to a minimum of 22
  (`videoPrm.mfx.QPx value changed 18 -> 22 by driver`), so CQP 20/18/15/12 all
  encoded at QP 22 (identical 34.4 Mbps output).
- ffmpeg's `psnr` filter silently down-converts to 8-bit (reports ~27 dB for
  every encode — meaningless); PSNR-Y here is computed manually on raw 10-bit
  luma with peak 1023.

## Decision

**CQP (QP 22) + `--quality best`** is the final master encode:

```
QSVEncC64.exe -i D.s1_remap.mkv --avsw -c hevc --profile main10
  --output-depth 10 --output-csp yuv420
  --colormatrix bt2020nc --colorprim bt2020 --transfer smpte2084
  --max-cll 1000,400 --master-display G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,1)
  --cqp 18 --quality best -o SHOWROOM_D.mp4
```

Result: post-encode max **1360 nits** (+12 % over stage-1 1212, vs +93 % for
ICQ23), PSNR-Y 57.00 dB (highest), luma MAE 1.002 (lowest), temporal p99 CV
18.27 % (unchanged). Bitrate 34.5 Mbps is the price of a clean master — the
box streamed 34 Mbps cin content earlier without issue.

ICQ 12 remains the "compact master" alternative (23.5 Mbps, max 1558) if the
34 Mbps master is too heavy for delivery.

## Deployed

- `SHOWROOM_D.mp4` rebuilt with the CQP master encode (20.5 MB, 300 f).
- `enhanced/showroom_c.mp4` = final SHOWROOM_D (instant-switch c).
- `enhanced/showroom_compare.mp4` = 3-way deck rebuilt with the final D in
  segment 3 (LIBPLACEBO B → HDRTVNET NPU → SHOWROOM D).
- Stream verified: `src=showroom:tbsY9zC-w6U:c`, clients=1, idr advancing,
  avg 13.6 Mbps, 0 drops; compare deck re-streamable via `name=compare`.

## Artifacts

- `%TEMP%\kilo\showroom\phase1\SHOWROOM_D.mp4` (final master)
- `%TEMP%\kilo\showroom\phase1\SHOWROOM_D_qsv_final.log` (CQP confirm: I/P/B 22,
  MaxCLL 1000/400, 34465 kbps)
- `%TEMP%\kilo\showroom\phase1\qsv_{icq18,icq15,icq12,cqp20,cqp18,cqp15,cqp12,cqp18best}.log`
  (sweep evidence; videos removed after measurement)