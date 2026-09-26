"""Jarvis OCR contract: engine-agnostic frozen structures + backends.

Everything above this layer depends on these structures only; upstream
vendored structures never leak out. Future backends
(``WindowsAiOcrBackend``, ``OpenVinoOcrBackend``, ``WindowsMlOcrBackend``)
plug in without touching Everywhere.
"""

from .base import (
    OcrDocument,
    OcrLine,
    OcrWord,
    Quad,
)
from .oneocr_backend import OneOcrBackend

__all__ = [
    "OcrDocument",
    "OcrLine",
    "OcrWord",
    "OneOcrBackend",
    "Quad",
]
