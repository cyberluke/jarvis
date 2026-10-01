# 29 — Display Refresh Inventory (Homatics + Samsung HDMI chain)

**Date:** 2026-09-30
**Box:** 192.168.1.122:5555 (Homatics, Android 14, logd dead)
**Host:** LAN 192.168.1.155
**Scope:** Read-only capability discovery. No root, no sysfs writes, no EDID/firmware changes.

## Question

Does the Homatics + Samsung HDMI chain expose and accept a **true 60.000 Hz**
2160p mode, or is `60000/1001` (59.9400599… Hz) the only real high-rate timing?

## Answer: TRUE 60.000 IS EXPOSED AND ACCEPTED

Android's DisplayManager exposes **both** rates at 3840×2160:

| App-facing modeId | SF DisplayMode id | Width×Height | refreshRate (fps) | Classification |
|---|---|---|---|---|
| 3 | 2 | 3840×2160 | **59.94006** | 60000/1001 |
| **4** | **3** | 3840×2160 | **60.000004** | **60/1 TRUE** |
| 5 | 4 | 3840×2160 | 50.0 | 50/1 |
| 6 | 5 | 3840×2160 | 29.97003 | 30000/1001 |
| 7 | 6 | 3840×2160 | 30.000002 | 30/1 |
| 8 | 7 | 3840×2160 | 25.0 | 25/1 |
| 9 | 8 | 3840×2160 | 23.976025 | 24000/1001 |
| 10 | 9 | 3840×2160 | 24.000002 | 24/1 |

Same dual-rate pattern exists at 1920×1080 (modeId 1/2) and 1280×720
(modeId 17/18): each has a 59.94006 sibling and a 60.000004 sibling.

## Mode switch (supported, non-root)

```powershell
adb -s 192.168.1.122:5555 shell cmd display set-user-preferred-display-mode 3840 2160 60.000004 0
# -> True
```

Verified after switch (`dumpsys display`):

- `mActiveModeId = 4` (was 3)
- `mUserPreferredModeId = 4`
- `DisplayDeviceInfo … modeId 4, renderFrameRate 60.000004, defaultModeId 4`
- `mActiveSfDisplayMode = DisplayMode{id=3, 3840×2160, refreshRate=60.000004, group=10}`
- SF **VSYNC period: 16666666 ns = exactly 60.000 Hz** (was 16683333 = 59.94)

## P1.1 — nominal vs actual

The mode IDs above are Android's **measured** floating-point refresh values,
not UI labels. `60.000004` is the integer `60/1` timing (float epsilon from
1e6 ns/60 division); `59.94006` is `60000/1001`. Distinction is numerical,
not label-based.

## P1.2 — EDID (readable text form, /sys/class/amhdmitx/amhdmitx0/edid)

- Manufacturer: SAM, product 0fa2, name SAMSUNG, EDID 1.3, week 1 / 2019
- Physical size: 1420 × 800 mm
- **EDID hash (sha256, first 32): `8d57e19c06145b67ac5bdbe9dbcd2e7f`**
- CTA VIC list: `97 16 31 4 19 5 20 32 33 34 93 94 95 96 101 102 98 100 7 22 3 18 63 64`
  - **VIC 97 = 3840×2160@60 (true 60.000 timing)** ← the 4K60 claim
  - VIC 16 = 1080p60, VIC 31 = 1080p30, VIC 4 = 720p60, VIC 5 = 1080i60
  - VIC 93–96, 101, 102, 98, 100 = 4K 24/25/30/50/100/120 family
- HDR: `HDR/13 DeepColor` present; ColorMetry 0xc3
- HDMI: `MaxTMDSClock2 600 MHz`, `MaxFRLRate 0` (HDMI 2.0-class link, not 2.1 FRL)
- `ALLM: 1`, SCDC present
- No DSC, no FRL → the 60.000 mode runs within TMDS 600 MHz (HDMI 2.0 4:2:0/4:2:2 10/12-bit budget)

## HDMI current state

- `/sys/class/amhdmitx/amhdmitx0/attr` = `422,12bit` (unchanged across the
  59.94 → 60.000 switch)
- disp_cap lists `2160p60hz`, `2160p50hz`, `smpte60hz`, etc.

## HDR capability (unchanged across switch)

- `HdrCapabilities{mSupportedHdrTypes=[2, 3, 4], mMaxLuminance=1000.0, mMaxAverageLuminance=1000.0}`
- `mHdrConversionMode = HDR_CONVERSION_PASSTHROUGH`
- `isForceSdr = false`
- Supported HDR types: 2 (HDR10), 3 (HLG), 4 (HDR10+) per platform constants.

## Display identity

- `DeviceProductInfo{name=SAMSUNG, manufacturerPnpId=SAM, productId=4002, manufactureDate=2019 w1}`
- `3840×2160, density 640, colorMode 0 (NATIVE), supportedColorModes [0]`
- Physical output is 4K (`mCurrentDisplayRect=3840x2160`); the `wm size`
  1920×1080 override is a TV-interface setting, not the HDMI mode.
- Boot display mode: `3840 2160 59.94006` (modeId 3, the factory default).

## Conclusion

```json
{
  "true60Exists": true,
  "true60ModeId": 4,
  "true60Refresh": 60.000004,
  "true60VsyncNanos": 16666666,
  "rate594ModeId": 3,
  "rate594Refresh": 59.94006,
  "currentModeId": 4,
  "currentRefresh": 60.000004,
  "edidHash": "8d57e19c06145b67ac5bdbe9dbcd2e7f",
  "edidVic97_2160p60": true,
  "hdmi": "422,12bit",
  "hdr": "PASSTHROUGH HDR10/HLG/HDR10+",
  "switchMechanism": "cmd display set-user-preferred-display-mode (non-root, supported API)",
  "status": "TRUE_60_ACCEPTED"
}
```

**This is the P3 path.** The prior `4K60 PARTIAL` diagnosis (60.0 stream vs
59.94 display) is resolved at the display layer: the chain genuinely exposes
and runs 60.000 Hz when requested through a supported API.