"""Frozen, engine-agnostic OCR structures shared by every Jarvis backend."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Tuple

#: Explicit failure reasons (typed, never a generic string).
OCR_MODEL_BUNDLE_INVALID = "OCR_MODEL_BUNDLE_INVALID"
OCR_PROVIDER_UNAVAILABLE = "OCR_PROVIDER_UNAVAILABLE"
OCR_BACKEND_UNAVAILABLE = "OCR_BACKEND_UNAVAILABLE"
OCR_NO_TEXT = "OCR_NO_TEXT"


@dataclass(frozen=True)
class Quad:
    """A real quadrilateral: four (x, y) corners in reading order."""

    x1: float
    y1: float
    x2: float
    y2: float
    x3: float
    y3: float
    x4: float
    y4: float

    def as_rect(self) -> Tuple[float, float, float, float]:
        """Axis-aligned (x, y, w, h) enclosure of the quad."""
        xs = (self.x1, self.x2, self.x3, self.x4)
        ys = (self.y1, self.y2, self.y3, self.y4)
        x, y = min(xs), min(ys)
        return x, y, max(xs) - x, max(ys) - y


@dataclass(frozen=True)
class OcrWord:
    text: str
    confidence: float
    quad: Optional[Quad] = None


@dataclass(frozen=True)
class OcrLine:
    text: str
    quad: Optional[Quad] = None
    words: Tuple[OcrWord, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class OcrDocument:
    """One frozen OCR observation of an image."""

    text: str
    lines: Tuple[OcrLine, ...] = field(default_factory=tuple)
    rotation_deg: float = 0.0
    backend: str = "oneocr"

    @property
    def word_count(self) -> int:
        return sum(len(line.words) for line in self.lines)
