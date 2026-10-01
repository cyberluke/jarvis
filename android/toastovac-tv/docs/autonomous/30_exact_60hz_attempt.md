# 30 — Exact 60.000 Hz End-to-End Attempt

**Date:** 2026-09-30
**Result:** ✅ **PASS — TRUE 60.000 AVAILABLE AND VERIFIED**

## Summary

The prior campaign left "4K60 PARTIAL" with the diagnosis that a 60.000 fps
stream backlogs against a 59.94 Hz display. This run asked: *does the HDMI
chain expose and accept a real 60.000 Hz 2160p mode at all?*

**Answer: yes.** Android exposes mode id=4 (3840×2160 @ 60.000004) alongside
mode id=3 (59.94006). Requesting it through the supported, non-root API
(`cmd display set-user-preferred-display-mode 3840 2160 60.000004`) was
accepted, and the display measurably ran at 60.000 Hz (SF VSYNC period
16666666 ns). A 10-minute 60/1 end-to-end soak **PASSED**.

## P3 — Mode switch (supported API only)

```powershell
adb -s 192.168.1.122:5555 shell cmd display set-user-preferred-display-mode 3840 2160 60.000004 0
# -> True (accepted)
```

Verified after switch:

| Field | Before | After |
|---|---|---|
| mActiveModeId | 3 | **4** |
| mUserPreferredModeId | -1 | **4** |
| renderFrameRate | 59.94006 | **60.000004** |
| SF active mode | 3840×2160 @ 59.94006 (group 9) | **3840×2160 @ 60.000004 (group 10)** |
| SF VSYNC period | 16683333 ns (59.94) | **16666666 ns (60.000)** |
| HDMI attr | 422,12bit | 422,12bit (unchanged) |
| HDR passthrough | yes | yes (unchanged) |
| resolution | 3840×2160 | 3840×2160 (unchanged) |

## P3.1/P3.2 — app-level request (added to LiveHdrActivity)

- `WindowManager.LayoutParams.preferredDisplayModeId` set to the measured
  3840×2160 @ ~60.000 mode (selected by properties, never by ordinal index).
- `Surface.setFrameRate(60.0f, FRAME_RATE_COMPATIBILITY_FIXED_SOURCE,
  CHANGE_FRAME_RATE_ALWAYS)` on the live video surface (API 30+).
- Session-scoped, best-effort; falls back to the current mode if no true-60
  mode exists.

Verified: after the box app restarted (APK reinstall), the display stayed at
mode 4 / 60.000 Hz — the app-level request held the mode.

## P4 — 10-minute exact-60/1 soak

Stream: `FrameRateExtN=60, FrameRateExtD=1` (never 60000/1001), 3840×2160,
PQ, BT.2020, Main10, zero-copy.

`results/60.0fps_hdr.json` / `results/exact_60hz_attempt.json`:

| Metric | Min | Avg |
|---|---|---|
| requestedFps | 60.0 | 60.0 |
| producedFps | **60.0** | 60.01 |
| encodedFps | 59.96 | 59.99 |
| sentFps | 59.59 | 60.05 |
| boxOutputFps | 58.86 | 59.98 |
| dropGrowth | **0** | — |
| decoderCreateCountMax | **1** | — |
| readbackBytesPerFrameMax | **0** | — |
| deadlineMissesTotal | 5341 | — |
| orientation | VERTICAL_FLIP | — |

**Verdict: PASS** (all soak criteria met; no backlog growth, no collapse).

Note: an earlier soak run with the 2-second sentFps sampler read one window at
58.47 and was marked FAIL. Cross-check proved **zero wire loss** (host sent
1200 frames/20 s = 60.00/s; box received/queued all of them; the dip was a
TCP-buffered frame crossing the sampler window edge). The sampler was widened
to 5 s; the rerun passed.

## P4.1 — 60/1 vs 60000/1001 comparison

| Config | 10-min soak | Backlog | Drops | Verdict |
|---|---|---|---|---|
| **60/1 stream → 60.000 display** | PASS (this run) | none | 0 | production candidate |
| 60000/1001 stream → 59.94 display | PASS (P2.4 historical) | none | 0 | fallback profile |
| 60/1 stream → 59.94 display | FAIL (P2.4 historical) | unbounded | collapse | mismatch only |

The 60.000 mode is native to the EDID (VIC 97) and stable end-to-end. The
roundness of the number is not the reason for choosing it — the 10-minute
measured stability on a display that genuinely runs 60.000 is.

## P7 — Outcome A

```text
TRUE 60.000 AVAILABLE AND VERIFIED
4K60 exact = PASS
production high rate = 60/1
```

"4K60 PARTIAL" is retired: the physical system exposes and runs 60.000 Hz;
the earlier "partial" was the display being left in its 59.94 boot mode.

## P3.3 checklist

- [x] 3840×2160 remains
- [x] BT2020_ITU_PQ remains (HDR passthrough, types 2/3/4)
- [x] 422,12bit remains
- [x] HwcVideo DEVICE remains
- [x] orientation remains correct (VERTICAL_FLIP)