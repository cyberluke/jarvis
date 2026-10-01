# Phase 2 firmware / root escape hatch (FROZEN)

Status: **frozen 2026-09-28**. Do not decrypt boot, patch Magisk,
modify vbmeta, USB-burn, or continue UVM/HwcVideo experiments unless
1080p overlay quality is proven unacceptable in real use.

Production architecture is hybrid (see `RenderTarget`):
Dashboard paints 3840×2160; Companion Overlay paints 1920×1080 RGBA.

## Why this path exists

RGBA UI buffers on the Homatics Box R 4K Plus (SEI804HM / YYJ,
`UKG3.250803.001.8963`) are allocated at 3840×2160 and geometrically
mapped 1:1, but SurfaceFlinger **CLIENT composition** targets a
1920×1080 framebuffer (`ro.surface_flinger.max_graphics_height=1080`
+ `config_maxUiWidth=1920` from `com.android.tv.overlay.framework`).
HWC then upscales that 1080p client target to the 2160p60 panel.

True 1:1 4K is only granted to the **HwcVideo / DEVICE** plane
(MediaCodec / UVM decoder buffers). App-owned YUV that looks like
video either stays CLIENT (YV12) or **kernel-panics** (NV12 or
`HW_VIDEO_DECODER` usage → UVM ioctl without a live decoder).

Root would lift the CLIENT ceiling (`resetprop max_graphics_*`,
`config_maxUiWidth=0`, `mesondisplay.cfg` 2160p) so RGBA overlays
retain per-pixel alpha at 4K. That is the only remaining way to get
true-4K transparent UI over Kodi/YouTube.

## Box / AVB facts (do not re-probe unless hardware changed)

```
adb -s 192.168.1.122:5555
ro.boot.verifiedbootstate=green
ro.boot.flash.locked=1
ro.boot.vbmeta.device_state=locked
ro.boot.avb_version=1.2
ro.boot.veritymode=enforcing
ro.boot.dynamic_partitions=true
non-A/B (no ro.boot.slot_suffix)
bootloader=01.01.260521.191028
fingerprint=Homatics/SEI804HM/YYJ:14/UKG3.250803.001/8963:user/release-keys
CPU=armeabi-v7a only
```

Network fastboot is unreachable. Rescue is USB Burning Mode
(reset pinhole + power).

## Recovery cartridge (already downloaded, SHA1 verified)

Internet Archive item `sei-804-hm-14.8.8963-usb-stick-burn-tool`

- File: `SEI804HM 14.8.8963 Usb Stick Burn Tool.zip`
- SHA1: `194206ed7f62f06e910350bb03ac04cdee612e39`
- Local copy (agent temp, not in git):
  `%TEMP%\kilo\fw\SEI804HM-14.8.8963.zip`
  extracted: `%TEMP%\kilo\fw\Usb stick Burn Tool\`
  parsed parts: `%TEMP%\kilo\fw\part\`

Contents: `aml_autoscript`, `aml_sdc_burn.ini` (erase_flash=1,
erase_bootloader=1, reboot=1), `aml_upgrade_package.img` (1.76 GB).

**Caveat:** vbmeta inside the cartridge is `AVB0` / avbtool 1.2.0
but branded `RockTek/SEI804RT` (same YYJ / 8963 platform). Device
digest `ro.boot.vbmeta.digest` does **not** match the cartridge
vbmeta SHA-256. Same SEI 8963 family, not a byte-identical OTA image.

Burn procedure (stock, no patch) — USB stick FAT32, three files at
root, hold reset pinhole, plug power, wait ~5 min. Same-build flash
keeps apps; downgrade wipes.

## Classifier evidence (do not repeat live)

Pulled (readable): `mapper.arm.so`, mapper@4.0-impl-arm, gralloc
capability XML (`/vendor/etc/gralloc/{gpu,vpu,dpu,cam,dpu_aeu}.xml`).
`hwcomposer.amlogic.so` is `vendor_file` — adb pull denied.

`am_gralloc_is_video_decoder_*_usage` predicates (Thumb):
full = priv30+priv31; quarter = 22+30; 1/16 = 22+15; replace = 22
without 9/28; OSD = 9+30+31. Video → `/dev/uvm` ioctl `0xC0085509`.

Live A2 (`YuvPlaneProbeActivity`, keep installed as diagnostic only):

| Buffer | Result |
|---|---|
| YV12 4K app-owned | DEVICE in SF, CLIENT→OSD 1080p in HWC. Safe. |
| NV12 4K app-owned | kernel panic (`oops:_fatal_exception`) |
| any + `HW_VIDEO_DECODER` (bit 22) | kernel panic |

Do **not** launch NV12 / bit-22 variants on a living-room box.

## If this hatch is ever reopened

1. Decrypt SC2 `AML ` boot/super/recovery payloads (not XOR).
2. Magisk-patch boot; vbmeta flags=2 (disable verification).
3. Repack with `ampack` (7Ji). Never flash a non-matching build.
4. USB burn, then `resetprop` + overlay override.
5. Re-measure CLIENT target with `dumpsys SurfaceFlinger`.

Until then: Dashboard 4K buffer + Overlay 1080p alpha + native 4K
MediaCodec video is the product path.
