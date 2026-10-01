# 25 — Zero-Copy QSV HEVC Main10 4K HDR: Product Path Frozen

**Date:** 2026-09-30
**Status:** WORKING — native zero-copy path frozen and codified (P0)
**Machine:** Intel Meteor Lake (VEN_8086 DEV_7D67), libvpl 2.17 (mfx-gen), Rust 1.88 + windows-rs 0.61
**Box:** Homatics R Plus (Android 14), one `c2.amlogic.hevc.decoder`, HwcVideo DEVICE

## Goal

Freeze the now-correct live picture as the golden native baseline and codify
every proven quirk so later work never "cleans up" a working accommodation.

```text
SceneGraph → Intel D3D11 RGBA16F → GPU compute RGB→P010 → oneVPL QSV HEVC Main10
→ persistent hevc_metadata bsf + HDR10 SEI injection → cast_host AU assembler
→ TZHL → one Amlogic decoder → HwcVideo DEVICE → Samsung HDR
readbackBytesPerFrame = 0, RTX/NVIDIA/NVENC = forbidden
```

## Frozen picture invariants (P0)

| Item | Value |
|---|---|
| SAR | `AspectRatioW = 1`, `AspectRatioH = 1` |
| HDR VUI | `colour_primaries=9, transfer_characteristics=16, matrix_coefficients=9, video_full_range_flag=0, video_format=5, level=153` |
| Orientation quirk | Homatics/Amlogic video plane requires a **vertical pre-flip** of the native stream |
| SurfaceFlinger | `dataspace=BT2020_ITU_PQ (298188800)`, `composition type=DEVICE (2)`, `geomContentCrop=[0 0 3840 2160]`, `geomBufferTransform=0` |

## Orientation quirk — named and scoped (P0.1/P0.2)

The correction is a **VERTICAL_FLIP** (top↔bottom), NOT "180°" — they differ by
a horizontal mirror. The quirk is scoped to the live box target, not a universal
renderer truth. The native scene stays logically upright; the shader pre-flips
only for the box.

Resolution precedence (scene.rs `resolve_flip_mode`):

```text
explicit --flip arg (none | vertical | rotate180)
→ TOASTOVAC_FLIP env (0=none, 1=vertical, 2=rotate180)   [explicit override]
→ display-profile default:
    pipe mode  → HOMATICS_AMLOGIC_VIDEO_PLANE verticalPreFlip=true
    --out file → verticalPreFlip=false (local files never pre-flipped)
→ NONE
```

Verified: file mode → `effectiveMode=NONE target=LOCAL_FILE`; pipe mode →
`VERTICAL_FLIP target=HOMATICS_AMLOGIC_VIDEO_PLANE`; `--flip vertical` on file
mode overrides to VERTICAL. The engine emits a `DISPLAY_PROFILE` event with the
resolved profile; cast_host exposes it as `orientation` in `/cast/status`.

`MediaFormat.KEY_ROTATION = 0` on the decoder stays neutral (P0.4). The working
correction is upstream pre-flip, never Android rotation metadata.

## HDR signaling sources (P1.2/P1.3)

- `hdrVuiSource = HEVC_METADATA_BSF` — the mfx-gen runtime ignores the Init
  `mfxExtVideoSignalInfo` buffer (no VUI colour description from the encoder),
  so the persistent `hevc_metadata` bsf rewrites the SPS VUI
  (BT.2020 / PQ / BT.2020nc / limited / video_format=5 / level 5.1). One
  persistent process, never per-frame spawns. Measured P50 ≈ 0.15 ms,
  P95 ≈ 0.3 ms at 4K30.
- `hdrSeiSource = HEVC_METADATA_BSF` — the Init `mfxExtMasteringDisplayColourVolume`
  / `mfxExtContentLightLevelInfo` buffers are ignored too (trace_headers: the
  engine's own SEI carries only buffering-period + pic-timing). The HDR10 SEI
  (content-light type 144 + mastering-display type 137, two NALs, 2-byte header
  `0x4E 0x01`, G/B/R primaries order) is injected per VCL frame in cast_host,
  byte layout matched to the golden clip. MaxCLL=1000, MaxFALL=400,
  BT.2020 mastering primaries, WP D65, L(1000 nits, 0.005 nits). Verified on
  the wire: both payload types present on every frame; parsed by
  `trace_headers`.
- The box's SF layer reports `hdr metadata types=0` in the TZHL live path for
  BOTH the native stream and the golden clip (control test) — the static-
  metadata layer surfacing difference is a decoder/path characteristic, not a
  stream defect. The TV HDR switch is driven by the VUI + `BT2020_ITU_PQ`
  dataspace (active, user-confirmed HDR).

## Session FPS — one source of truth (P1.1)

`POST /cast {"action":"cast.scene","fps":N}` (default 30, `TOASTOVAC_FPS` env
override) flows into `toastovac_gpu.exe --fps N --gop N --pace`, the state-feed
pacing, and the status endpoint. Status never displays configured FPS as
measured: it distinguishes `requestedFps` / `producedFps` (engine render
metrics) / `encodedFps` (engine AU metrics) / `sentFps` (host wire counter) /
`boxOutputFps` (box decoder output rate via `/cast/boxstats`).

## Runtime quirks (frozen, see compat_qsv_runtime_quirks.md)

QueryIOSurf -15 → fixed 8-surface pool · mandatory non-null bitstream pointer ·
`MaxLength ≥ BufferSizeInKB×1000` (16 MiB) · GetHDL fills runtime-owned
`mfxHDLPair` · in-place Query + re-assert IOPattern/ExtParam · status 2 =
`MFX_WRN_DEVICE_BUSY` retried · HEVC SAR 1:1 · runtime drops Init ext buffers
(VUI + HDR SEI) → bsf + SEI injection · Amlogic video plane vertical pre-flip.

## Files changed

- `native_engine/src/scene.rs` — FlipMode enum (NONE/VERTICAL_FLIP/ROTATE_180),
  `resolve_flip_mode` precedence, HLSL flip comment.
- `native_engine/src/main.rs` — `--flip` arg, DISPLAY_PROFILE event, `--frames`
  respected in file mode (EOF early-exit only when unlimited).
- `cast_host.py` — session FPS (set_session_fps/current_fps), `--gop N`,
  `_inject_hdr10_sei` + `_hdr10_sei_nal`, BSF latency instrumentation,
  engine metrics capture (kind 2), sentFps sampler, `/cast/boxstats` endpoint,
  adb box activation (`_activate_box_live`), reconnect status fields,
  `nobuffer/low_delay` bsf flags.
- `soak_run.py` — soak harness writing `results/{fps}fps_hdr.json`.
- `LiveHdrActivity.java` + `live/BoxStatsReporter.java` — box → host telemetry.