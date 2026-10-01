# 00 — Starting State (overnight autonomous run)

Timestamp: 2026-09-29 01:15 local (box TZ)
Box: 192.168.1.122:5555 (Homatics R Plus, Android 14, SDK 34)
PC sender host: 192.168.1.155

## Reported before this run

```text
LIVE HDR RUNTIME: PARTIAL
HwcVideo Blank (live), golden HDR10/HLG assets proven
HDMI attr=422,12bit VIC 97
CPU scene raster ~230–400 ms/frame; preroll ~15 fps
live bitrate telemetry ~0.1–0.3 Mbps
HOME = LiveHdrActivity, overlay OFF
```

## Verified at start

| Item | Value |
|------|-------|
| HDMI disp_mode | VIC:97 |
| HDMI attr | 422,12bit |
| hdr_cap | ST2084:1, HLG:1, HDR10+ :1 |
| Foreground | ai.toastovac.tv/.LiveHdrActivity (HOME) |
| PC IP | 192.168.1.155 (matches DEFAULT_HOST) |
| Server on :8768 | not running |
| Golden assets | docs/hdmi-probe/hdr10/toastovac_hdr10_4k60.mp4 + hlg present |
| logcat | `persist.log.tag=S` — app logs globally suppressed |
| ffmpeg | 7.1.1 with libvpl (QSV) |

## Root causes identified this run

1. Server mux treated each NAL as an AU; first picture was a non-key
   (TRAIL_N); bare SEI and split/merged pictures were sent; config only
   merged into later IDR packets. Android therefore received garbage AUs
   before any valid config+IDR — decoder never produced a picture.
2. NAL start-code scanner (server + diagnostic tools) treated a single
   `00 01` byte pair as a start code (needs ≥2 zero bytes). This corrupted
   NAL splitting of any payload containing `00 01`.
3. `persist.log.tag=S` suppressed all app logging — diagnostics needed
   per-tag `log.tag.*` overrides.

## Success condition met at end of phase

Live QSV stream → Amlogic decoder → HwcVideo UnBlank → DEVICE
→ BT2020_ITU_PQ → 3840×2160, identical to golden HDR10 proof.