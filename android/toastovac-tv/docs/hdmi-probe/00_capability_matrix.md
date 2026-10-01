# Homatics HDMI / HDR capability matrix

Captured 2026-09-28 23:37–23:40 local, box `192.168.1.122:5555`,
firmware `UKG3.250803.001.8963` (Homatics/SEI804HM), sink SAMSUNG
(`Rx Product Code 0fa2`, 2019, 1420×800 mm).

Classification keys used below:

| Tag | Meaning |
|-----|---------|
| EDID | sink advertised it (amhdmitx `edid` / `hdr_cap` / `dc_cap`) |
| FW | firmware exposes the mode/format as selectable or listed |
| ACTIVE | currently driven on the HDMI link |
| TESTED | empirically switched and re-read from sysfs (none this pass) |

No undocumented sysfs writes. No second HEVC decoder. Firmware/root frozen.

## Current active link

| Field | Value | Source |
|-------|-------|--------|
| Physical mode | 2160p60 (VIC 97) | `amhdmitx0/disp_mode` = `VIC:97` |
| Refresh | 59.94 Hz | `dumpsys display` modeId 3 / `mActiveSfDisplayMode` 59.94006 |
| HDMI used / HPD | 1 / 1 | `hdmi_used`, `hpd_state` |
| Color / depth | **YCbCr 4:2:0, 10-bit** | `amhdmitx0/attr` = `420,10bit` |
| HDR on the wire | **not asserted** | SF `colorMode=NATIVE`, `dataspace=UNKNOWN`, `hdr metadata types=0`; Display `mActiveColorMode=0` (NATIVE only) |
| HDR conversion | passthrough, preferred type invalid | `mHdrConversionMode=HDR_CONVERSION_PASSTHROUGH` |
| ALLM | cap=1, mode=0 | `allm_cap` / `allm_mode` |
| Content type | cap=`game`, mode=`off` | `contenttype_*` |
| HDCP | 2.2 (also stores 1.4) | `hdcp_mode=22`, `hdcp_lstore=14+22` |
| Dolby Vision | sink does not support | `dv_cap` |
| Logical UI | 1920×1080 CLIENT, physical 3840×2160 | SF framebuffer 1920×1080, displaySpace 3840×2160 |
| SF HDR support flags | hdr10=true, hlg=true, hdr10plus=true, dv=false, wideColorGamut=false | Composition Display Color State |

Android HDR type IDs on this firmware (`supportedHdrTypes=[2,3,4]`):
HDR10 / HLG / HDR10+ (Dolby Vision type 1 is absent).

## Firmware-exposed timing (`disp_cap`) — 2160p family

| Mode | Listed in `disp_cap` | Android DisplayManager mode | Currently active |
|------|----------------------|-----------------------------|------------------|
| 2160p60 | FW | id=3 @ 59.94, id=4 @ 60.00 | **YES (59.94)** |
| 2160p50 | FW | id=5 @ 50.00 | no |
| 2160p30 | FW | id=6/7 @ 29.97/30 | no |
| 2160p25 | FW | id=8 @ 25 | no |
| 2160p24 | FW | id=9/10 @ 23.98/24 | no |
| 2160p120 | **not listed** | **not listed** | no |

Also listed (not cinematic target): 1080p/i 24–60, 720p 50/60, SD, SMPTE, a few VESA (`1440x900` / `1600x900` / `1680x1050` @60).

## Deep-color / packing (`dc_cap`) — firmware-exposed, not per-mode

`dc_cap` is a **flat sink+TX capability list**, not a timing×format matrix.
Do not read a row below as “this timing was measured at this packing”.

| Packing | 8-bit | 10-bit | 12-bit | Notes |
|---------|-------|--------|--------|-------|
| RGB | FW | FW | FW | `rgb,8/10/12bit` present. **No `rgb` currently active.** |
| YCbCr 4:4:4 | FW | FW | FW | `444,8/10/12bit` present. **Not active.** |
| YCbCr 4:2:2 | — | — | FW | **Only `422,12bit` is listed.** No `422,8bit` / `422,10bit` line. |
| YCbCr 4:2:0 | FW | **ACTIVE** | FW | `420,8/10/12bit`. Active = `420,10bit`. |

EDID ColorDeepSupport `0xb8` and Vendor MaxTMDSClock2 **600 MHz** / SCDC=1
are consistent with 4K60 deep color (and with 4:2:0 10-bit being the
safe default the firmware picked).

### Answers to the interesting combinations (exposure, not tested)

| Combination | Exposed? | Evidence |
|-------------|----------|----------|
| 2160p60 RGB 10-bit | FW yes, TESTED no | `rgb,10bit` in `dc_cap`; timing in `disp_cap` |
| 2160p60 YCbCr 4:4:4 10-bit | FW yes, TESTED no | `444,10bit` |
| 2160p60 YCbCr 4:2:2 10-bit | **not listed** | no `422,10bit` line |
| 2160p60 YCbCr 4:2:2 12-bit | **FW yes**, TESTED no | `422,12bit` is the only 422 entry |
| 2160p60 YCbCr 4:2:0 10-bit | **ACTIVE** | `attr=420,10bit` |
| 2160p60 YCbCr 4:2:0 12-bit | FW yes, TESTED no | `420,12bit` |

Homatics **does expose 4:2:2 12-bit** in `dc_cap`. It does **not**
currently drive it. It **does expose RGB and 4:4:4 at 8/10/12-bit**.
Whether the official Settings UI can select those packings is for the
user to check on the open Display & Sound screen — do not write `attr`.

## HDR (cinematic 4K60)

| Feature | EDID / FW | Active now |
|---------|-----------|------------|
| HDR10 / ST 2084 (PQ) | EDID yes (`SMPTE ST 2084: 1`) | no |
| HLG | EDID yes | no |
| Traditional SDR | EDID yes | **yes (implicit)** |
| Traditional HDR | EDID no | no |
| HDR10+ | EDID yes (`HDR10Plus Supported: 1`); SF `hdr10plus=true` | no |
| Dolby Vision | EDID/FW **no** (`The Rx don't support DolbyVision`, SF `dv=false`) | no |
| BT.2020 | EDID ColorMetry `0xc3` (includes BT2020 bits); SF `wideColorGamut=false` | not active |
| 10-bit HDMI | FW + **ACTIVE** (`420,10bit`) | yes (SDR 10-bit 420) |
| 12-bit HDMI | FW (`420/444/rgb/422,12bit`) | no |

Codec source bit depth ≠ HDMI output bit depth. A 10-bit HEVC Main10
decode can still leave the HDMI link at `420,10bit` SDR, which is the
current state. Switching the link to HDR PQ / BT.2020 is a firmware
policy + content-metadata path (`HDR_CONVERSION_PASSTHROUGH`); it is
**not** happening on the idle Settings UI.

Cinematic target `3840×2160 / 60 / HDR / BT.2020 / PQ / 10- or 12-bit`:

- Timing: firmware can do 2160p60. Active.
- 10-bit on the wire: active, but as **SDR 4:2:0**, not HDR.
- PQ / HLG / HDR10+: sink+FW advertise, **not active**.
- 12-bit: advertised, not active.
- BT.2020: EDID yes, Android color mode list is **NATIVE only**
  (`mSupportedColorModes=[0]`). That is a real discrepancy: HDMI EDID
  claims 2020, DisplayManager does not expose a BT.2020 color mode.

## Settings vs sysfs

Left open for the user:

`com.android.tv.settings/.device.displaysound.DisplaySoundActivity`

Do not trust a UI label until `amhdmitx0/attr` + `disp_mode` are re-read.
Known discrepancy already: Android reports color mode NATIVE / dataspace
UNKNOWN while the HDMI serializer is `420,10bit`. The UI will likely say
“4K / 60 Hz” without naming 4:2:0-10.

`/sys/class/display/{mode,vinfo,cap,frame_rate}` and DRM connector
`status/modes/edid` are **permission denied** to the shell. Usable HDMI
truth is under `/sys/class/amhdmitx/amhdmitx0/{disp_cap,dc_cap,hdr_cap,attr,edid,disp_mode}`.

## Overlay / HOME state after this pass

- `CompanionOverlayService` was **not running**. `am stopservice` confirmed.
- MENU no longer starts the overlay (code change, not yet on-box until next APK install; runtime overlay is already off).
- Stock launcher enabled. Toastovač still installed, still a HOME candidate.
- Foreground left at **Display & Sound** so the user can inspect resolution / HDR / color.

## Rescue commands

Stop overlay + stock HOME + Display & Sound:

```
powershell -File android\toastovac-tv\scripts\stock-ui.ps1
```

Restore Toastovač HOME later (overlay stays off):

```
powershell -File android\toastovac-tv\scripts\toastovac-home.ps1
```

One-shot ADB (same as the script):

```
adb -s 192.168.1.122:5555 shell am stopservice -a ai.toastovac.tv.STOP_OVERLAY -n ai.toastovac.tv/.CompanionOverlayService
adb -s 192.168.1.122:5555 shell pm enable com.google.android.tvlauncher
adb -s 192.168.1.122:5555 shell am force-stop ai.toastovac.tv
adb -s 192.168.1.122:5555 shell am start -n com.google.android.tvlauncher/.MainActivity
adb -s 192.168.1.122:5555 shell am start -n com.android.tv.settings/.device.displaysound.DisplaySoundActivity
```

## Raw logs

`android/toastovac-tv/docs/hdmi-probe/`

- `node_disp_cap.txt`, `node_dc_cap.txt`, `node_hdr_cap.txt`, `node_attr.txt`, `node_edid.txt`, `node_disp_mode.txt`, `node_dv_cap.txt`, `node_rawedid.txt`
- `dumpsys_display.txt`, `dumpsys_SurfaceFlinger.txt`
- `getprop_display.txt`, `service_list.txt`
- this file
