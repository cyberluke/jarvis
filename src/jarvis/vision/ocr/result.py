"""Conversion from vendored OneOCR structures to the Jarvis OCR contract."""

from __future__ import annotations

from ..._vendor.oneocr.common import OcrResult
from .base import OcrDocument, OcrLine, OcrWord, Quad


def _quad_from(bbox) -> Quad | None:
    if bbox is None:
        return None
    return Quad(
        x1=float(bbox.x1), y1=float(bbox.y1),
        x2=float(bbox.x2), y2=float(bbox.y2),
        x3=float(bbox.x3), y3=float(bbox.y3),
        x4=float(bbox.x4), y4=float(bbox.y4),
    )


def document_from_result(result: OcrResult, backend: str = "oneocr") -> OcrDocument:
    """Map a vendored ``OcrResult`` onto the frozen Jarvis structures."""
    lines: list[OcrLine] = []
    for line in result.lines:
        words = tuple(
            OcrWord(text=w.text,
                    confidence=float(w.confidence),
                    quad=_quad_from(w.bbox))
            for w in line.words
        )
        lines.append(OcrLine(
            text=line.text,
            quad=_quad_from(line.bbox),
            words=words,
        ))
    return OcrDocument(
        text=result.full_text,
        lines=tuple(lines),
        rotation_deg=float(result.image_angle),
        backend=backend,
    )
