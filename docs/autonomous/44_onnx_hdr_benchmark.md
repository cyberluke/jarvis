# 44 — HDRTVNet++ ONNX HDR Benchmark (XE / NPU / CPU)

## Model
- file: `tools/qsvenc/models/hdrtvnetpp/hdrtvnetpp_agcm_stable.onnx`
- SHA256: `CA3C243EC17F16D566D544DE5039BA4DD1504E608DC449CCB8A6DD506CE71957`
- runtime: QSVEncC 8.31 `--vpp-onnx` (OpenVINO), 1296x2304 → 1296x2304 (x1), RGB, fp16
- input/output signalling: SDR BT.709 limited → BT.2020nc/PQ HDR10 SEI

## Device identity (P16) — proven, no silent fallback
QSVEncC log lines (device pinned per run):
- XE:  `device=GPU` (Intel Xe via oneVPL, GPU 100 % during inference)
- NPU: `device=NPU [Intel(R) AI Boost] prec=f16`
- CPU: `device=CPU`

## Inference proof (P16) — PASS for all three
| metric | ONNX_XE (GPU) | ONNX_NPU | ONNX_CPU |
|---|---|---|---|
| frames encoded | 300 | 300 | 300 |
| fps | 2.20 | 5.07 | 3.24 |
| wall time (5 s clip) | ~136 s | ~59 s | ~93 s |
| device load | GPU 100 % | GPU 10–28 % (NPU does the work) | CPU 35.5 %, GPU 8.7 % |
| output | ONNX_XE.mp4 | ONNX_NPU.mp4 | ONNX_CPU.mp4 |

NPU and CPU outputs are near-identical (same model+precision path) — device
consistency proof. GPU (f16) deviates (hotter max, larger skin shift).

## Luminance vs WRAP ONLY (P14/P24)
| metric | WRAP | ONNX_XE | ONNX_NPU | ONNX_CPU |
|---|---|---|---|---|
| P50 nits | 20.5 | 71.5 | 69.5 | 69.5 |
| P90 nits | 79.5 | 360.5 | 345.5 | 345.5 |
| P99 nits | 99.5 | 962.5 | 943.5 | 943.5 |
| max nits | 180 | 4324 | 1448 | 1463 |
| >1000 nits | 0 % | 0.77 % | 0.52 % | 0.52 % |
| black p1/p5 (code) | 151/192 | 154/216 | 151/214 | 151/214 |
| skin hue delta | 0 | +5.04° | +1.58° | +1.48° |
| skin chroma | — | −2.6 % | +1.7 % | +1.4 % |
| edge overshoot | 0 % | 6.6 % | 4.4 % | 4.4 % |

## Temporal stability (P20)
ONNX path shows per-frame highlight variance: p99 CV 34.6 % vs 5.0 % for the
wrap source. The model amplifies frame-to-frame highlight changes (stage lights,
motion). Visible shimmer risk on TV; the LP path is stable (CV ~5 %).

## Verdict
ONNX_NPU is the best AI candidate: cleanest skin (+1.58°), blacks preserved,
fastest Intel device (5.07 fps), consistent with CPU. The AI keeps midtones
moderate (P50 69 nits) while pushing highlights (P99 943) — "structure first",
which complements the LP luminance expansion in the hybrid.

## Status
P15/P16 PASS (3 devices, real inference, output != wrap). P21 in
`results/onnx_hdr_benchmark.json`.