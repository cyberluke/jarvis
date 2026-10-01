# 01 — Live bitstream repair (P0) + 02 — live HDR validation (P1)

## Goal

Make the live hevc_qsv stream decoder-visible on the Amlogic box: valid
Annex-B access units, deterministic CONFIG+IDR startup, HwcVideo UnBlank
in DEVICE composition with BT.2020 PQ — matching the proven golden HDR10
path.

## Starting state

HwcVideo Blank on live stream; golden HDR10/HLG assets proven; wire
packets malformed (bare SEI, non-key first picture, split/merged pictures).

## Changes made

### PC — `tools/live_hdr/server.py`
- Encoder: added `-aud 1`, `-adaptive_i 0`, `-scenario livestreaming`
  (deterministic AU delimiters and GOP behavior).
- Replaced per-NAL mux with a real Annex-B **AU assembler**:
  - start-code scanner requiring ≥2 zero bytes (`00 00 01` / `00 00 00 01`);
  - picture boundaries from AUD + `first_slice_segment_in_pic_flag`
    (slice-header bit), so multi-slice pictures stay in one AU and two
    pictures are never merged;
  - drops all AUs before the first IRAP picture (undecodable from cold);
  - emits `CONFIG` packet (latest VPS+SPS+PPS) before the first picture
    and before every subsequent IRAP;
  - config bytearray kept bounded to the latest VPS+SPS+PPS group.
- Wire sequence now: `CONFIG` → `IDR AU` → one AU per picture.

### Android — `android/toastovac-tv`
- `live/DecoderSession.java`: `FLAG_CONFIG` → `MediaCodec.BUFFER_FLAG_CODEC_CONFIG`;
  `FLAG_IDR` → `BUFFER_FLAG_KEY_FRAME`; logs first IDR.
- `live/LiveHdrReceiver.java`: instruments the first 16 AUs (index, flags,
  bytes, NAL type list) with a correct start-code scanner.
- `LiveHdrActivity.java`: P2 poster fallback — draws a status scene on the
  SurfaceView until the first decoder frame is presented (HOME never looks
  dead when the sender is down).
- Diagnostics: `persist.log.tag=S` on the box suppressed logging; enabled
  per-tag `log.tag.TOASTOVAC* V` for this session.

## Commands / probes used

```text
ffmpeg -c copy -bsf:v hevc_mp4toannexb  (golden elementary extraction)
tools/live_hdr/_live_capture.py          (raw hevc_qsv stdout capture)
tools/live_hdr/_au_inspect.py            (NAL/AU structure inspector)
tools/live_hdr/_wire_sink.py             (TZHL packet recorder)
ffprobe / ffmpeg -f null                 (live capture validity)
adb shell dumpsys SurfaceFlinger         (HWC/composition state)
adb shell logcat -s TOASTOVAC-DEC TOASTOVAC-LIVE
adb shell cat /sys/class/amhdmitx/amhdmitx0/{attr,disp_mode}
```

## Measured results

```text
Android AU trace (first valid startup):
  AU 0..9:  regular picture AUs (mid-stream connect, pre-keyframe)
  AU 10:    flags=0x01 [VPS,SPS,PPS]            CONFIG
  AU 11:    flags=0x06 [AUD,SEI,IDR,IDR]        FIRST_IDR bytes=34689
  AU 12+:   flags=0x04 [AUD,SEI,V1,V1]          one picture per packet

Decoder output format (after first IDR):
  width=3840 height=2160 color-standard=6 (BT.2020)
  color-transfer=6 (ST.2084/PQ) color-range=2
  hdr-static-info=25 bytes   frame-rate=30

SurfaceFlinger (live, matches golden):
  usesDeviceComposition=true  usesClientComposition=false
  displayFrame=[0 0 3840 2160]  sourceCrop=[0 0 3840 2160]
  dataspace=BT2020_ITU_PQ (298188800)
  HwcVideo--65536: UnBlank (plane z=130)
  SurfaceView(BLAST) layer: composition type=DEVICE, dataspace=BT2020_ITU_PQ

HDMI: attr=422,12bit  VIC:97
decoderCreateCount=1
```

## What is proven

- Live QSV stream is decoder-visible: HwcVideo UnBlank, DEVICE composition,
  full 4K sourceCrop/displayFrame, BT2020_ITU_PQ dataspace — identical to
  the golden HDR10 proof that drives Samsung HDR.
- Decoder singleton preserved (createCount=1).
- Live capture is valid HEVC Main10 4K BT.2020 PQ on the PC.

## What remains unproven

- Samsung-side ST.2084 indicator (TV OSD not readable over ADB; box-side
  signaling is identical to the golden path, which did trigger it).
- Long-run stability (P4 soak) and renderer fps (P3).

## Files changed

```text
tools/live_hdr/server.py          (AU assembler + encoder flags)
tools/live_hdr/_live_capture.py   (new, TEMP_DIAGNOSTIC)
tools/live_hdr/_au_inspect.py     (new, TEMP_DIAGNOSTIC)
tools/live_hdr/_wire_sink.py      (new, TEMP_DIAGNOSTIC)
android/toastovac-tv/src/ai/toastovac/tv/live/DecoderSession.java
android/toastovac-tv/src/ai/toastovac/tv/live/LiveHdrReceiver.java
android/toastovac-tv/src/ai/toastovac/tv/LiveHdrActivity.java
```

## Artifacts / logs

```text
docs/autonomous/logs/sf_live_unblank.txt
docs/autonomous/logs/decoder_startup.txt
docs/autonomous/logs/ffprobe_live.txt
docs/autonomous/logs/live_capture.hevc
docs/autonomous/logs/hdmi_state.txt
```

## Known issues

- Decoder reports output `frame-rate=30` (firmware default) though we feed
  15 fps — display timing, not a defect.
- Mid-stream connects recover at the next keyframe (~1 s) — acceptable.
- CPU raster still ~230–400 ms/frame (P3); preroll loop hides it at 15 fps.

## Next step

P4 soak (verify minutes-long stability, decoder count stays 1), then P3
GPU scene, then the product scenarios (Korean Short, Interview Coach,
Desktop cast).