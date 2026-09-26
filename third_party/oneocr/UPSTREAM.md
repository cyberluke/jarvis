# OneOCR upstream provenance

Project: bropines/oneocr-onnx-python
Upstream commit: 75cc12666503425ffd6ea0cb052c0bcaaff21058
Upstream date: 2026-07-01
License: MIT
Local integration: modified vendored fork

## Scope of the vendored code

`src/jarvis/_vendor/oneocr/` contains the production runtime only:
`common.py`, `vocab.py`, `detector.py`, `classifier.py`,
`recognizer.py`, `corrector.py`, `engine.py`.

Extraction/preparation tooling lives in `tools/oneocr/`
(`extractor.py`, `prepare_assets.py`, `inspect_models.py`) and is not
imported by the runtime.

## Modifications against upstream

- Corrected classifier-index → script mapping:
  1 CJK (32632), 2 Cyrillic (548), 3 Latin (415), 4 Arabic (221),
  5 Devanagari (237), 6 Greek (244), 7 Thai (199), 8 Hebrew (201),
  9 Tamil (179). There is no Bengali recognizer in this model
  generation. Reference: `oneocr/onnx` Java `ScriptGroup.java`.
- Recognizer confidence = `exp(logsoftmax)` of the winning CTC token
  (no second softmax over the output).
- Detector uses all three FPN levels (2, 3, 4) and the real
  quadrilateral `bbox_deltas` outputs, with deterministic cross-level
  merge (descending confidence; suppress at >30 % overlap of the
  smaller line's area).
- Deterministic workspace-relative asset resolution
  (`_vendor/oneocr/assets`); no `~/.config/oneocr` search; no runtime
  downloads.
- Single explicit ONNX Runtime execution provider; no implicit
  fallback chain.
- Thread-safe lazy recognizer session creation.

## License notes

The upstream Python source is MIT (see `LICENSE` next to this file).

The extracted model weights are NOT covered by that MIT license: they
are Microsoft's proprietary OneOCR weights shipped inside the Windows
`Microsoft.ScreenSketch` package. The MIT license of the Python wrapper
does not grant redistribution rights for the Microsoft model assets.
Keep that distinction when redistributing.
