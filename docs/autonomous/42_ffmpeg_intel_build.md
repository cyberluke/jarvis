# 42 — FFmpeg Intel Build Assessment

## Result: NOT REQUIRED
The P1 binary-first policy succeeded: the newest Gyan FULL Windows x64 release
build (FFmpeg 9.0.2, 2026-09-19) contains the complete required feature set
(P2 gate PASS). No source compile, no BtbN fallback, no master/nightly needed.

## Would-be build plan (documented per P4–P8, not executed)
- source: https://github.com/BtbN/FFmpeg-Builds (libplacebo recipe under Vulkan deps)
- configure intent: `--enable-vulkan --enable-libplacebo --enable-libvpl --enable-opencl`
  plus `--enable-lcms2 --enable-libzimg`
- CPU optimization: `-O3 -march=native -mtune=native -flto` (Core Ultra 9 285K, x86-64)
- PGO: optional, only after green functional build
- rationale for skipping: stock binary is statically linked, self-contained, and
  matches the required API surface exactly (libplacebo v7.371.0, Vulkan 1.4.356,
  QSV/libvpl 2.17, OpenCL, lcms2, zimg)

## GPU build / runtime notes (P8) — applied to the stock binary
- Vulkan device forced to Intel Xe via `-init_hw_device vulkan:0` (NVIDIA rejected)
- libplacebo reuses one Vulkan device per process; shader cache available
  (`shader_cache=` option) but not persisted yet (per-run compile ~0.3–1.3 s)
- GPU-resident path: `N->V` filter, frames stay on GPU through the filter;
  software readback at the output (yuv420p10le) is the only CPU copy
- measured stage throughput (1296x2304, inverse tonemap + gamut expansion,
  Intel Xe): ~40 fps with ffv1 lossless output, ~52–60 fps to null

## Status
P4–P8: PASS-BY-EXEMPTION (binary-first). No local optimized build produced;
the stock full build is used for all phase-1 candidates. Provenance in
`results/ffmpeg_intel_build.json`.