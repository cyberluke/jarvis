# Compatibility facts — native zero-copy QSV HDR path (P5 freeze)

Durable hardware/runtime quirks proven on this machine + box. **Do not "fix"
these because they look unconventional — runtime evidence wins.** Each entry
records the observed behavior, the evidence, and the working accommodation.

## 1. oneVPL `MFXVideoENCODE_QueryIOSurf` returns -15 on this runtime
   → fixed 8-surface input pool
   Even the exact FFmpeg working config (P010 4K30 Main10 VBR 18000/28000,
   3750/5000 InitialDelay/BufferSize, refs=1 slices=2, IOPattern 0x21, no
   ext buffers) is rejected with MFX_ERR_INVALID_VIDEO_PARAM. The oneVPL
   hello-encode sample never calls QueryIOSurf. Accommodation: skip it and
   build a fixed pool of 8 P010 surfaces (qsv.rs, TOASTOVAC_SKIP_IOSURF).

## 2. `MFXVideoENCODE_EncodeFrameAsync` requires a real non-null bitstream pointer
   Passing null for the `bs` argument is rejected (hevcehw_base_impl.cpp
   EncodeFrameCheck: MFX_CHECK_NULL_PTR2(bs, ...)). The shared 16 MiB
   bitstream (qsv.rs `BITSTREAM_CAP`) is always passed.

## 3. Bitstream `MaxLength` must cover `BufferSizeInKB × 1000`
   hevcehw_base_legacy.cpp BLK_CheckBsData requires MaxLength >= the whole
   VBV buffer (5000 KB → 5,000,000 bytes). 16 MiB covers the 5000 KB buffer.

## 4. `GetHDL` must fill runtime-owned `mfxHDLPair` storage
   The runtime passes a buffer for an mfxHDLPair; fill `{first: ID3D11Texture2D*,
   second: array index}`. `mid` IS the raw texture pointer (external app
   surface). Without this the encode task cannot resolve the input texture and
   never produces an AU (hwcontext_qsv.c ffmpeg convention).

## 5. `MFXVideoENCODE_Query` is used in-place (same struct in/out)
   The Windows mfx-gen runtime returns 0 only for in-place Query; a separate
   zeroed output struct throws (MFX_ERR_NULL_PTR, hevcehw_base_legacy.cpp
   CheckBuffers requires out->NumExtParam == in->NumExtParam). The in-place
   call rewrites IOPattern/NumExtParam, so they are re-asserted afterwards.

## 6. Status 2 = `MFX_WRN_DEVICE_BUSY` must be retried
   The HW is still working on a previous task; retry the submit with a short
   sleep (qsvenc.c does the same). Never treat it as an error.

## 7. HEVC SAR must be 1:1
   `mfxFrameInfo.AspectRatioW/H` were 16/9 (a leftover); the Amlogic decoder
   stretched/interleaved the frame (upper-half bug). 1:1 is the fixed value.

## 8. The Intel runtime does not reliably emit the requested VUI here
   `mfxExtVideoSignalInfo` (and AUDelimiter / RepeatPPS / the Init
   `mfxExtMasteringDisplayColourVolume` / `mfxExtContentLightLevelInfo`)
   Init ext buffers are dropped: no VUI colour description and no HDR SEI
   from the encoder (verified with trace_headers — the engine's SEI carries
   only buffering-period + pic-timing; the box layer reported
   `hdr metadata types=0` vs golden=3). Accommodations (bitstream-only):
   - VUI source = persistent ffmpeg `hevc_metadata` bsf (BT.2020/PQ/BT.2020nc,
     limited, video_format=5, level 153) — `hdrVuiSource=HEVC_METADATA_BSF`.
   - HDR static metadata = deterministic SEI injection on IDR AUs in
     cast_host (`_hdr10_sei_nal`), byte layout matched to the golden HDR10
     clip (two prefix-SEI NALs: content-light type 144 then mastering-display
     type 137, G/B/R primaries order, 2-byte NAL header 0x4E 0x01) —
     `hdrSeiSource=HEVC_METADATA_BSF`; MaxCLL=1000, MaxFALL=400, BT.2020
     mastering primaries, WP D65, L(1000 nits, 0.005 nits).

## 9. The Homatics/Amlogic live video plane requires a vertical source pre-flip
   The 4K HEVC video plane vertically flips THIS native encoder's stream
   (proven against ffmpeg hevc_qsv streams — golden, golden+bsf, goldenqsv,
   live-scene re-encode — which do NOT flip). The correction is a VERTICAL
   pre-flip (top↔bottom), not a 180° rotation (they differ by a horizontal
   mirror). Scoped to the live box target via a display profile:
   - pipe mode → `displayProfile.target=HOMATICS_AMLOGIC_VIDEO_PLANE,
     verticalPreFlip=true`
   - local `--out` encodes → `verticalPreFlip=false` (no flip)
   - precedence: explicit `--flip` arg → `TOASTOVAC_FLIP` env (0=none,
     1=vertical, 2=rotate180) → profile default → NONE
   The native scene stays logically upright; the shader pre-flips only for
   the box (scene.rs `u_flip`, modes named NONE / VERTICAL_FLIP / ROTATE_180).
   `MediaFormat.KEY_ROTATION=0` on the decoder stays neutral — the correction
   is upstream, never Android rotation metadata.

---

## Diagnostic A/B assets (P0.3) — what each proves

| Asset | Purpose |
|---|---|
| `tools/live_hdr/hdr10_golden.mp4` | Golden HDR10 clip (3840×2160 Main10 60fps, bt2020nc/smpte2084, level 5.1, DCI-P3 mastering, MaxCLL=1000/MaxFALL=400). Ground truth for color metadata byte layout and the box's correct orientation baseline. |
| `golden` source | Stream-copy of the golden clip through the same TZHL path → isolates bitstream-vs-path (decoder/display) effects. |
| `goldenbsf` source | Golden + the same `hevc_metadata` bsf → proves the bsf-modified SPS is not what the Amlogic decoder dislikes (orientation stays correct). |
| `goldenqsv` source | Golden re-encoded with the same Meteor Lake QSV encoder via ffmpeg → proves the flip is not QSV-encoder-wide; `GOLDEN_QSV_SRC` overrides the input, `GOLDEN_QSV_SLICES=1` mirrors the engine's NumSlice=2. |
| `VideoProbeActivity` clip modes | `clip=live` → `live_plain.mp4`, `clip=livehdr` → `live_hdr.mp4` (SAR-fixed + color-VUI test clips) for the MediaPlayer/MediaCodec probe path. |

These are diagnostics only — none are wired into runtime behavior of the
product path (`native`).