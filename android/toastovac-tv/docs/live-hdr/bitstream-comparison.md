# Live HDR bitstream comparison — golden HDR10 vs live hevc_qsv

Date: 2026-09-29
Golden asset: `android/toastovac-tv/docs/hdmi-probe/hdr10/toastovac_hdr10_4k60.mp4`
Golden elementary: extracted with `hevc_mp4toannexb` (temp: golden_4k60.h265)
Live capture: `docs/autonomous/logs/live_capture.hevc` (raw hevc_qsv stdout)

## Encoder command (live)

```text
ffmpeg -f rawvideo -pix_fmt rgb48le -s 3840x2160 -r 15 -i pipe:0
 -vf format=p010le -c:v hevc_qsv -profile:v main10 -preset veryfast
 -bf 0 -g 15 -forced_idr 1 -async_depth 2 -aud 1 -adaptive_i 0
 -scenario livestreaming -b:v 18M -maxrate 28M -bufsize 40M
 -color_primaries bt2020 -color_trc smpte2084 -colorspace bt2020nc -color_range tv
 -f hevc -bsf:v hevc_metadata=colour_primaries=9:transfer_characteristics=16:matrix_coefficients=9
 pipe:1
```

## ffprobe (both identical)

```text
codec_name=hevc  profile=Main 10  width=3840  height=2160
pix_fmt=yuv420p10le  color_space=bt2020nc
color_transfer=smpte2084  color_primaries=bt2020
```

PC decode check (`ffmpeg -f null`): no errors — live capture is valid HEVC.

## Golden first access units

```text
AU 0: [AUD,VPS,SPS,PPS,PREFIX_SEI,PREFIX_SEI,VPS,SPS,PPS,PREFIX_SEI,PREFIX_SEI,IDR_N_LP,...] 985 B
AU 1: [IDR_N_LP,RSV_NVCL44]  657 B
AU 2: [IDR_N_LP,RSV_NVCL41] 2143 B
AU 3: [RSV_VCL30] 27 B
AU 4: [SPS,SPS,EOB,EOB,EOB,BLA_W_LP] 5054 B
AU 5+: [AUD,TRAIL_R] ~343 B each (single slice per picture)
```

Golden traits: AUD at AU start, parameter sets repeated, first picture
is IDR_N_LP, pictures are single-slice, BLA pictures appear at GOP
boundaries, static scene → very small inter frames.

## Live hevc_qsv raw first access units (after -aud -adaptive_i 0)

```text
AU 0: [AUD,VPS,SPS,PPS,PREFIX_SEI,TRAIL_N] 151 B   ← first picture NOT key (quirk)
AU 1: [IDR_W_RADL] 13782 B
AU 2: [IDR_W_RADL] 14370 B                        ← 2-slice first IDR picture
AU 3: [AUD,PREFIX_SEI,TRAIL_N] 25 B
AU 4: [TRAIL_R] 370 B
...
AU 6: [BLA_W_RADL] 250 B                           ← BLA at GOP boundary (ok)
AU 10: [CRA_NUT,FD] 662 B                          ← CRA at GOP boundary (ok)
```

Live traits: AUD present, VPS/SPS/PPS once at start, first picture is a
non-key TRAIL_N (must be dropped for cold start), IDR pictures can be
multi-slice (2 slices), BLA/CRA appear at GOP boundaries despite
`-forced_idr` (QSV open-GOP recovery points; the golden decoder already
proved tolerant of BLA).

## Differences fixed by the AU assembler

| Aspect | Golden | Live raw | Assembler output |
|--------|--------|----------|------------------|
| First packet | config inside first AU | TRAIL_N picture first | CONFIG (VPS+SPS+PPS) |
| AU boundaries | AUD | AUD (+ slice flag) | AUD + first_slice flag |
| Picture split | never | 2-slice IDRs | never (merged via slice flag) |
| Config repetition | repeated | once at start | re-sent before every IRAP |
| Pre-IRAP garbage | none | first AU non-key | dropped |

## Wire packets observed after fix (TZHL :8768)

```text
PKT 1: flags=0x01 [VPS,SPS,PPS]                    CONFIG
PKT 2: flags=0x06 [AUD,PREFIX_SEI,IDR_W_RADL,IDR_W_RADL]  first IRAP AU (2 slices)
PKT 3..: flags=0x04 [AUD,PREFIX_SEI,TRAIL_R,TRAIL_R]      one picture per packet
(periodically) flags=0x01 [VPS,SPS,PPS] + flags=0x06 [AUD,SEI,IDR,IDR]  keyframe refresh
```

## Decoder / HWC outcome

Android MediaCodec `c2.amlogic.hevc.decoder` output format after first IDR:

```text
width=3840 height=2160 crop-right=3839 crop-bottom=2159
color-standard=6 (BT.2020) color-transfer=6 (ST.2084/PQ) color-range=2
hdr-static-info=25 bytes (mastering display SEI)
frame-rate=30
```

SurfaceFlinger (identical to golden HDR10 proof):

```text
usesDeviceComposition=true  usesClientComposition=false
displayFrame=[0 0 3840 2160]  sourceCrop=3840x2160
dataspace=BT2020_ITU_PQ (298188800)
HwcVideo--65536: UnBlank (plane z=130)
```

## Notes

- `-idr_interval begin_only` exists but would suppress periodic keyframes;
  we keep `-g 15 -forced_idr 1` so the stream recovers within ~1 s.
- Raw logs: `docs/autonomous/logs/{sf_live_unblank.txt, decoder_startup.txt, ffprobe_live.txt, live_capture.hevc}`