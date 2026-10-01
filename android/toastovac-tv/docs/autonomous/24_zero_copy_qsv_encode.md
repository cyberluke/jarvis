# 24 — Zero-Copy QSV HEVC Main10 4K Encode: Working

**Date:** 2026-09-30
**Status:** WORKING — encoder produces valid decodable 4K30 Main10 HEVC with zero CPU pixels
**Machine:** Intel Meteor Lake (VEN_8086 DEV_7D67), libvpl 2.17 (mfx-gen), Rust 1.88 + windows-rs 0.61

## Goal

Complete the zero-copy native path: SceneGraph → Intel D3D11 render (RGBA16F, PQ BT.2020) →
compute convert into a GPU-resident P010 texture (UAV) → oneVPL QSV HEVC Main10 →
Annex-B stream → (next) TZHL → single Amlogic decoder → TV. No RTX, no readback,
`readbackBytesPerFrame = 0`.

## Result

The encoder is fully functional:

| Run | encoded fps | encode ms | frame ms | AUs | deadline misses |
|---|---|---|---|---|---|
| 10 f no-pace | 78.5 | 4.1 | 9.4 | 9 | — |
| 60 f no-pace | 104.2 | 6.7 | 9.2 | 60 | 0 |
| 61 f paced 4K30 | 29.8 | 0.20 | 0.91 | 60 | 2 |

ffprobe validation (test_paced.hevc): `codec=hevc, profile=Main 10, 3840x2160,
pix_fmt=yuv420p10le, level=150 (5.0)`, decode rc=0, `nb_read_frames=61`.

This replaces the P1 baseline (3.8 fps effective, 75 ms p50 readback) — a ~25×
throughput gain with zero readback.

## Root causes found and fixed (this session)

1. **QueryIOSurf -15 (every config)** — skipped entirely (oneVPL hello-encode doesn't
   call it either); fixed 8-surface pool. `TOASTOVAC_SKIP_IOSURF=1` is now the required path.
2. **EncodeFrameAsync -2** — the bitstream parameter is **mandatory**
   (`hevcehw_base_impl.cpp EncodeFrameCheck: MFX_CHECK_NULL_PTR2(bs, pEntryPoint)`).
   We passed `null_mut()`; now pass the real `mfxBitstream`.
3. **EncodeFrameAsync -5** — bitstream must hold the whole VBV buffer:
   `MaxLength >= BufferSizeInKB * 1000` (`hevcehw_base_legacy.cpp BLK_CheckBsData`).
   `BITSTREAM_CAP` 4 MiB → 16 MiB.
4. **SyncOperation -2 + tasks never completing** — the encode task resolves the input
   texture through the session allocator's **GetHDL**, which must *fill* the runtime's
   `mfxHDLPair` buffer with `{ first: texture, second: index }` (FFmpeg
   `hwcontext_qsv.c frame_get_hdl` convention). Our previous callbacks returned a
   dangling stack-local pair / NULL_PTR for the (then-empty) pair list, killing the task.
   Fix: `(*pair).first = mid; (*pair).second = 0;` where `mid` is the raw texture.
5. **Query with separate output -2** — `CheckBuffers` requires
   `out->NumExtParam == in->NumExtParam`; a zeroed output throws and the session
   wrapper catches it as NULL_PTR. Use in-place Query + re-assert
   IOPattern/NumExtParam/ExtParam (the runtime zeroes them in the capability rewrite).
6. **oneVPL 2.x status renumbering** — `WRN_IN_EXECUTION=1`, `WRN_DEVICE_BUSY=2`,
   `NOT_INITIALIZED=-8`, `MORE_DATA=-10`. Added a DEVICE_BUSY retry loop (FFmpeg style).

## Session / surface / allocator configuration

- Session: `MFXInitEx` impl 0x2, version 2.17, `impl_flags 0x302`; `SetHandle(D3D11) rc=0`
  (device has `VIDEO_SUPPORT | BGRA` for D2D, `ID3D10Multithread::SetMultithreadProtected(true)`).
- Custom `SetFrameAllocator` is registered but the runtime never invokes Alloc/GetHDL for
  the app-provided input (only GetHDL on our submitted surface mid during the task).
- Input surface: P010, `SRV|UAV` binds (no DECODER — matches FFmpeg's working external
  d3d11va upload textures), `MemType = DXVA2_PROCESSOR_TARGET | EXTERNAL_FRAME`,
  `MemId = raw ID3D11Texture2D*`.
- Params: HEVC Main10, P010 4K30, VBR 18000/28000, `InitialDelayInKB=3750`,
  `BufferSizeInKB=5000` (FFmpeg-verified effective values), `NumRefFrame=1`,
  `NumSlice=2`, `IOPattern=0x21` (IN_VIDEO_MEMORY | OUT_SYSTEM_MEMORY),
  `BitDepthLuma/Chroma=0` (runtime `CheckTargetBitDepth` rejects 10 in non-LowPower).
- Submit: serialized (one in-flight AU in the shared bitstream), drain via
  `SyncOperation` then read `DataOffset/DataLength` from `mfxBitstream`.

## Known limitations (documented)

- **Ext buffers not consumed by Init**: `AUDelimiter`, `RepeatPPS`, VSI and the HDR SEI
  buffers are attached with correct IDs/sizes and the runtime supports them
  (`m_ebCopySupported` lists CDOP/CDO2/VSIN), yet the output has no AUD NALs, PPS
  repeated only at IDR points, and `color_*` = unknown. This is a runtime-path
  behavior (FFmpeg on the same runtime also emits only `matrix_coeffs_present_flag`,
  not the full colour-description block). Needs either the `UpdateVideoParam` path or
  per-frame `mfxEncodeCtrl` ext attach.
- Standalone runs cap at 61 frames (stdin-EOF early-exit); the live pipeline feeds
  stdin continuously.
- QueryIOSurf remains -15; the fixed 8-surface pool is used.

## Artifacts

- `tools/live_hdr/native_engine/target/release/toastovac_gpu.exe`
- `results/zero_copy_qsv_encode.json` (measurements)
- Test streams under `%TEMP%\kilo\test_*.hevc`