"""OneOCR — vendored, modified fork of bropines/oneocr-onnx-python.

Upstream commit: 75cc12666503425ffd6ea0cb052c0bcaaff21058 (MIT).
See ``third_party/oneocr/UPSTREAM.md`` for provenance.

Modifications against upstream:
  * corrected classifier-index -> script mapping (no Bengali; Greek/Thai/
    Hebrew/Tamil per the Java reference ``oneocr/onnx/ScriptGroup``);
  * recognizer confidence is ``exp(logsoftmax)`` of the winning CTC token
    (the model output is already log-softmax; no second softmax);
  * detector consumes all three FPN levels and the real quadrilateral
    ``bbox_deltas`` outputs, with deterministic cross-level merge;
  * single deterministic asset root (package-relative), no ``~/.config``
    search, no runtime downloads;
  * explicit single-provider ONNX Runtime sessions (no implicit fallback).
"""

from .common import (
    LANG_TO_SCRIPT,
    SCRIPT_BY_NAME,
    SCRIPT_METADATA,
    OcrResult,
    Quad,
    Word,
    Line,
)
from .engine import OneOCR

__all__ = [
    "OneOCR",
    "OcrResult",
    "Quad",
    "Word",
    "Line",
    "SCRIPT_METADATA",
    "SCRIPT_BY_NAME",
    "LANG_TO_SCRIPT",
]
