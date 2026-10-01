# 41 — FFmpeg Upgrade (7.1.1 → 9.0.2)

## Decision
P0/P1 binary-first policy followed. **No source compile required** — the newest
suitable stable Windows full binary satisfies the complete P2 capability gate.

## Versions
- old: `7.1.1-essentials_build-www.gyan.dev` (WinGet, no libplacebo, no Vulkan)
- new: `9.0.2-full_build-www.gyan.dev` (release 2026-09-19, source commit `946fcce07b`)
- provider: Gyan (https://www.gyan.dev/ffmpeg/builds/), `ffmpeg-release-full.7z`
- SHA256 (archive): `f0e46253c70dfe902bac915dfb4224f0cf1b7c6eeab9da2ccc9a5581f9a71b13`
- binary path: `tools/ffmpeg/9.0.2-stock/ffmpeg-9.0.2-full_build/bin/ffmpeg.exe`
- old binary preserved: `tools/ffmpeg/7.1/ffmpeg.exe` (87,429,632 B, WinGet 7.1.1 essentials)

## Capability gate (P2) — PASS
- `libplacebo` filter: YES (`N->V`, libplacebo v7.371.0, API v371)
- `inverse_tonemapping`: YES
- `contrast_recovery` / `contrast_smoothness`: YES (0–3 / 1–32)
- `tonemapping` functions: auto, clip, st2094-40, st2094-10, bt.2390, bt.2446a,
  spline, reinhard, mobius, hable, gamma, linear
- `peak_detect` + `smoothing_period` + `scene_threshold_low/high` + `percentile`: YES
- `gamut_mode` (perceptual/clip/relative/saturation/absolute/desaturate/darken/warn/linear): YES
- `gamut_expansion`: YES (via `extra_opts=gamut_expansion=on`)
- `shader_cache`: YES (`shader_cache=` option)
- BT.2020 primaries / SMPTE ST.2084 / 10-bit output: YES
- Vulkan / OpenCL / QSV (libvpl) hwaccels: YES
- lcms2, zimg: enabled in build

## Vulkan adapter (P3) — Intel Xe PROVEN, forced
Without `-init_hw_device vulkan:0` libplacebo auto-selects the **NVIDIA RTX 4090**.
Forcing device 0:
- Device Name: `Intel(R) Graphics`
- Device ID: `8086:7d67` (Arrow Lake Xe iGPU)
- Vulkan API: 1.4.356 (libplacebo restricts to 1.3.0)
- Driver: 32.0.101.8991, shaderc SPIR-V 1.6
- NVIDIA used: false (no fallback — adapter pinned)

## FFmpeg 9 filtergraph escaping note
FFmpeg 9 changed colon handling in filter args: drive-letter colons in filter
values need **double-backslash** escaping (`C\\:/Windows/...`), not single.

## Status
P0–P3 PASS. P4–P8 (compile / CPU-opt / PGO) NOT NEEDED — binary-first satisfied.
P27 provenance recorded in `results/ffmpeg_upgrade.json`.