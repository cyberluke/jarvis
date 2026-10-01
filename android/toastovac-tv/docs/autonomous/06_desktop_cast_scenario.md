# 06 — Desktop cast

## Goal
`cast.desktop` → Windows capture → Intel encode → same TZHL / decoder.

## Path
```text
ffmpeg gdigrab (primary 1920×1080 SDR)
 → scale+pad 3840×2160 (letterbox)
 → format=p010le
 → hevc_qsv Main10
 → TZHL :8768
 → c2.amlogic.hevc.decoder (createCount=1)
 → HwcVideo UnBlank DEVICE
```

## First failure
Forced `gdigrab -video_size 3840x2160` + `format=nv12` →
`hevc_qsv: Current profile is unsupported`.

## Fix
Native 1080p grab, letterbox into 4K P010. After 10 s:
`producer_alive=true`, 13 012 076 bytes, 21 IDRs, HwcVideo UnBlank.

## Honest limits
Source is 1080p SDR. HDMI stayed 422,12bit VIC 97. Decoder color metadata
from the previous PQ session can remain sticky until process restart.

## Artifacts
`results/desktop_cast_result.json`
