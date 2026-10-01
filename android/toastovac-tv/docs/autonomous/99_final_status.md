# 99 — Final Status: Native Zero-Copy HDR Product Path

**Date:** 2026-09-30
**Status:** ✅ **WORKING — TRUE 60.000 AVAILABLE AND VERIFIED (P7 Outcome A)**
Native zero-copy HDR product path closed through display-rate closure.
Production high rate = **60/1**; 60000/1001 retained as the 59.94-only
fallback. "4K60 PARTIAL" is retired.

```text
NATIVE ZERO-COPY PATH
SceneGraph → Intel D3D11 RGBA16F → GPU compute RGB→P010 → oneVPL QSV HEVC Main10
→ persistent hevc_metadata bsf + HDR10 SEI injection → cast_host AU assembler
→ TZHL → one Amlogic decoder → HwcVideo DEVICE → Samsung HDR
readbackBytesPerFrame = 0 · RTX/NVIDIA/NVENC forbidden · one decoder (createCount=1)
```

## Image correctness

- SAR: `1:1` (AspectRatioW/H = 1/1, frozen)
- orientation mode: `VERTICAL_FLIP` (top↔bottom pre-flip; named, not "180°")
- device quirk: `HOMATICS_AMLOGIC_VIDEO_PLANE verticalPreFlip=true` (pipe mode);
  local `--out` files default NONE (never pre-flipped)
- full frame: `geomContentCrop=[0 0 3840 2160]`, buffer 4096×2304,
  `geomBufferTransform=0`, ROT_0

## HDR

- VUI source: `HEVC_METADATA_BSF` (persistent hevc_metadata; runtime drops the
  Init mfxExtVideoSignalInfo)
- primaries: bt2020 (9) · transfer: smpte2084/PQ (16) · matrix: bt2020nc (9) ·
  range: limited (video_full_range_flag=0) · video_format 5 · level 153
- static metadata: `HEVC_METADATA_BSF` SEI injection per VCL frame
  (payload 144 + 137; MaxCLL=1000, MaxFALL=400, BT.2020 mastering primaries,
  WP D65, L(1000 nits, 0.005 nits))
- SurfaceFlinger dataspace: `BT2020_ITU_PQ (298188800)` (verified during all
  soaks)
- Samsung: HDR signaled via VUI + dataspace (TV HDR switch; user-confirmed
  baseline). SF layer `hdr metadata types=0` in the TZHL path for native AND
  golden (control-tested) — a decoder/path characteristic, not a stream defect.

## 4K30 (P1) — PASS

- requested 30 · produced 30.00–30.05 · encoded 29.9+ · sent 29.99+ ·
  box output 29.88–30.23
- queue bounded (bsf queue 0, engine depth 1) · drops 0 · drop growth 0
- readback 0 · decoder createCount 1 · BT2020_ITU_PQ · orientation correct
- soak: 10 min PASS (pre-SEI) + 5 min PASS (final config with per-frame SEI)

## 4K60 (P2 → superseded by report 30) — PASS at 60/1

- **TRUE 60.000 Hz exists on this HDMI chain** (report 29): Android mode id=4
  = 3840×2160 @ 60.000004 (measured), alongside id=3 @ 59.94006; EDID VIC 97.
- **Display switched via supported non-root API**
  (`cmd display set-user-preferred-display-mode 3840 2160 60.000004 0`);
  SF VSYNC measured 16666666 ns = exactly 60.000 Hz.
- **10-minute 60/1 soak PASS** (results/60.0fps_hdr.json): produced 60.0,
  encoded 59.96+, sent 59.59+ (5 s sampler), box output avg 59.98, drop growth
  0, createCount 1, readback 0, HDR + orientation intact, no backlog, no
  collapse.
- App-level mode request added (P3.1/P3.2): `preferredDisplayModeId` (measured
  properties) + `Surface.setFrameRate(60f, FIXED_SOURCE, ALWAYS)` in
  LiveHdrActivity; mode survives app restart (verified).
- Historical 60.0-vs-59.94 collapse (report 27) was the display being left in
  its 59.94 boot mode — a display-mode artifact, not a hardware ceiling.

## Production decision (P9.2)

```json
{
  "productionRateN": 60,
  "productionRateD": 1,
  "productionFps": 60.0,
  "displayRefreshHz": 60.000004,
  "reason": "EDID VIC 97 native 60.000 mode exposed and accepted via supported API; 10-minute 60/1 soak PASS"
}
```

Fallback profile (59.94-only displays):

```json
{
  "productionRateN": 60000,
  "productionRateD": 1001,
  "productionFps": 59.94005994,
  "displayRefreshHz": 59.94006
}
```

## Rational frame rate (P5) — PASS

- Session rate is a rational (fpsNumerator/fpsDenominator) through
  cast.scene → cast_host → engine --fps/--fps-den → FrameRateExtN/D →
  pacing clock → status (report 31).
- Pacing clock: `deadline(n) = t0 + n*den/num` in integer nanoseconds (no
  float drift; 60000/1001 never approximated).
- Scene clock, encoder PTS (n*90000*den/num), TZHL PTS
  (origin + n*den*1e6/num) all derive from the same rational rate.
- Verified: `{60,1}` → produced 60.03; `{60000,1001}` → produced 59.97,
  requestedFps = 59.94005994005994 (exact).

## Transport

- BSF latency: per-AU P50 0.2 ms / P95 0.4 ms (60 fps; fixable metric artifact
  removed, backlog field exposed)
- AU assembler: mux queue 0, config/IDR re-derived per stream
- TZHL: v2 framing, TCP_NODELAY, single stream into the one decoder
- box reconnect: receiver auto-reconnects; host exposes sceneDesired /
  nativeProducerAlive / tzhlListening / boxConnected / clientCount /
  lastConnectedAt / lastDisconnectedAt / lastDisconnectReason /
  reconnectAttempts / boxStatsAgeSec

## Decoder

- codec: `c2.amlogic.hevc.decoder` · createCount: **1** (never 2, incl. P4 loop)
- HwcVideo: DEVICE (2) · dataspace BT2020_ITU_PQ · full 3840×2160 content crop
- never-drop input queue: `dequeueInputBuffer` retry with drain (120 ms budget),
  `inputFullEvents`/`inputFullDropped` counters — 0/0 in steady state (fixes the
  earlier blink/jump: silent AU drops are gone)

## Regression (P4/P8) — PASS

- native scene → Korean enhanced (original audio + burned Czech subs) →
  Interview Coach → native scene — at **60/1**
- decoder createCount 1 across all transitions; zero sustained drop growth at
  every stable step; HDR + correct orientation restored on return
- reconnect: cast_host restart → box auto-reconnects → native scene resumes
  (createCount 1, mode 4/60.000 held)
- audio-coherence drop gate (DecoderSession) prevents the source-switch drop
  storm

## Display / HDMI inventory (report 29)

- 19 modes (4K/1080p/720p × 24/25/30/50/59.94/60); true-60 4K = mode id 4
- EDID: SAM 0fa2, VIC 97 (2160p60), HDR/13, MaxTMDSClock2 600 MHz, hash
  8d57e19c06145b67ac5bdbe9dbcd2e7f
- HDMI attr: 422,12bit (unchanged across mode switch)

## Files changed (this run)

- `tools/live_hdr/native_engine/src/main.rs` — timeBeginPeriod(1) (1 ms timer),
  exact-rational pacing clock, f64 scene time.
- `tools/live_hdr/native_engine/src/qsv.rs` — rational encoder PTS
  (n*90000*den/num), fps/fps_den stored.
- `tools/live_hdr/native_engine/Cargo.toml` — Win32_Media feature.
- `tools/live_hdr/cast_host.py` — rational session rate (fpsNumerator/
  fpsDenominator), rational state-feed + TZHL PTS, status fields + rateMode,
  5 s sentFps sampler, 1 MiB socket buffers.
- `android/.../LiveHdrActivity.java` — preferredDisplayModeId (measured) +
  Surface.setFrameRate(60f) session-scoped mode request.
- `android/.../live/DecoderSession.java`, `LiveHdrReceiver.java` — never-drop
  input queue, 1 MiB recv buffer, inputFull counters (from the jitter run).
- `tools/live_hdr/soak_run.py`, `regression_run.py` — rational API, 5 s sampler.

## Reports

- `docs/autonomous/25_qsv_zero_copy.md` — frozen path, orientation profile,
  HDR sources, runtime quirks.
- `docs/autonomous/26_4k30_hdr.md` — 4K30 PASS, soaks, HWC/HDR evidence.
- `docs/autonomous/27_4k60_hdr.md` — 4K60 (historical P2 + Outcome A
  supersession).
- `docs/autonomous/28_zero_copy_regression.md` — P4 loop PASS + drop-storm fix.
- `docs/autonomous/29_display_refresh_inventory.md` — mode/EDID/HDMI inventory.
- `docs/autonomous/30_exact_60hz_attempt.md` — true-60 switch + 10-min soak PASS.
- `docs/autonomous/31_rational_framerate.md` — rational rate end to end.
- `docs/autonomous/99_final_status.md` — this report.
- `docs/autonomous/compat_qsv_runtime_quirks.md` — 9 frozen runtime quirks +
  diagnostic A/B assets.
- `results/qsv_zero_copy.json`, `results/4k30_hdr.json`,
  `results/4k60_hdr.json`, `results/59.94fps_hdr.json`,
  `results/60.0fps_hdr.json` (PASS), `results/exact_60hz_attempt.json`,
  `results/display_refresh_inventory.json`, `results/rational_framerate.json`,
  `results/zero_copy_regression.json`.

## Exact next highest-value step

None required — display-rate closure achieved (Outcome A). Ongoing work would
be productization: persist the app-level 60.000 display-mode request in the
normal (non-HOME) session path, and confirm the Samsung remembers the mode
across a full box power cycle (mode 4 is already the persisted
`mUserPreferredModeId`).