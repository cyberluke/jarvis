# 31 — Rational Frame Rate Support

**Date:** 2026-09-30

## Goal

Represent the frame rate as a **rational value** (`fpsNumerator` /
`fpsDenominator`) through the entire native scene path — never approximate
60000/1001 as 59.9/60, and never pace at one rate while stamping timestamps
for another.

## Rational flow (P5)

```text
cast.scene {fpsNumerator, fpsDenominator}
  → cast_host session_fraction()          (exact num/den, no float round-trip)
  → toastovac_gpu.exe --fps N --fps-den D (verified on the command line)
  → oneVPL FrameRateExtN / FrameRateExtD
  → pacing clock  deadline(n) = t0 + n*den/num  (integer ns)
  → scene clock   t = n*den/num                 (f64)
  → encoder PTS   n*90000*den/num               (90 kHz ticks, integer)
  → TZHL PTS      video_origin + n*den*1e6/num  (µs, integer)
  → status        fpsNumerator / fpsDenominator + requestedFps = num/den
```

## P5.1 — pacing clock (engine main.rs)

Before: `next_deadline += frame_interval` with
`frame_interval = Duration::from_secs_f64(den/num)` — cumulative float drift.

After: exact integer math

```rust
let deadline_ns = (n as u128 * fps_den as u128 * 1_000_000_000) / fps as u128;
let deadline = t0 + Duration::from_nanos(deadline_ns as u64);
```

For 60000/1001 the frame period is exactly `1001/60000 s = 16.683333… ms`,
never 16.666667. No accumulated drift over any session length.

## P5.2 — one rate everywhere

- **scene clock**: `t = n * den / num` (f64; f32 would lose precision above
  ~16.7M frames).
- **encoder timestamps**: `s.Data.TimeStamp = n*90000*den/num` (u128 → u64,
  90 kHz ticks). 60000/1001 yields 1501.5 → 1501/1502 alternating ticks
  (59.94 is not an integer in 90 kHz; the *rate* is the rational one).
- **TZHL PTS**: cast_host mux computes video PTS as
  `video_origin_us + n * den * 1_000_000 // num` (integer µs) for the native
  source; the decoder's presentation timeline therefore derives from the same
  rational rate as the encoder clock.
- **state feed**: `native_state_loop` paces at `t0 + n*den/num` and sends
  `t = n*den/num`.

## API

`cast.scene` accepts either form:

```json
{"action": "cast.scene", "fps": 60.0}
{"action": "cast.scene", "fpsNumerator": 60000, "fpsDenominator": 1001}
```

Status exposes:

```json
"fpsNumerator": 60000,
"fpsDenominator": 1001,
"requestedFps": 59.94005994005994,
"rateMode": "DISPLAY_NATIVE"
```

## Verified

- `cast.scene {fpsNumerator:60, fpsDenominator:1}` → engine cmdline
  `--fps 60 --fps-den 1 --gop 60 --pace`, produced 60.03.
- `cast.scene {fpsNumerator:60000, fpsDenominator:1001}` → engine cmdline
  `--fps 60000 --fps-den 1001 --gop 60 --pace`, produced 59.97,
  `requestedFps = 59.94005994005994` (exact).
- 10-min soak at 60/1 passed with the rational clock (report 30).
- Regression at 60/1 passed (report 28 update / P8).

## P6 — match-content frame-rate policy

| Scenario | Rate |
|---|---|
| native scene high-rate | 60/1 when display exposes true 60 (verified here); else 60000/1001 |
| native scene normal | 30/1 |
| 24p media | source timing / existing media policy |
| 59.94 source | 60000/1001 |
| 60.0 source | 60/1 only if display exposes true 60 |

Selection is source/display-aware; 59.94 is never forced onto a 60.000
display and 60.0 is never forced onto a 59.94-only display.

## Frozen rollback config (P0)

The working 60000/1001 configuration is preserved as the fallback profile:

```json
{
  "fpsNumerator": 60000,
  "fpsDenominator": 1001,
  "resolution": "3840x2160",
  "transfer": "PQ",
  "colorspace": "BT.2020",
  "profile": "Main10",
  "bsf": "hevc_metadata VUI rewrite (persistent)",
  "flip": "vertical pre-flip",
  "decoder": "c2.amlogic.hevc.decoder createCount=1"
}
```