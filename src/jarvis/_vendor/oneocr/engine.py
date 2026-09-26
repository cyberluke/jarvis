"""OneOCR orchestrator engine (vendored, modified fork of upstream
``oneocr/engine.py``).

Jarvis-specific contract:
  * exactly one asset location: ``<package>/_vendor/oneocr/assets`` —
    no search, no ``~/.config`` fallback, no runtime downloads;
  * the bundle is verified once per process (manifest, sizes, SHA-256);
    any mismatch raises ``OCR_MODEL_BUNDLE_INVALID``;
  * one explicit execution provider, never a fallback chain
    (``OCR_PROVIDER_UNAVAILABLE`` when the selected provider is absent);
  * a public :meth:`OneOCR.preload` pays session startup cost up front.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional

import onnxruntime as ort
from PIL import Image

from .common import (
    LANG_TO_SCRIPT,
    SCRIPT_BY_NAME,
    SCRIPT_METADATA,
    Line,
    OcrResult,
    Quad,
    Word,
)
from .corrector import OrientationCorrector
from .classifier import ScriptClassifier
from .detector import TextDetector
from .recognizer import TextRecognizer
from .vocab import Vocabulary

# Workspace-relative deterministic resolution (no search, no override).
ASSET_ROOT = Path(__file__).resolve().parent / "assets"
MODEL_ROOT = ASSET_ROOT / "models"

#: Jarvis config name -> ONNX Runtime execution provider.
PROVIDER_BY_NAME = {
    "cpu": "CPUExecutionProvider",
    "directml": "DmlExecutionProvider",
    "cuda": "CUDAExecutionProvider",
    "openvino": "OpenVINOExecutionProvider",
}

_MIN_DIM = 50
_MAX_DIM = 10000
_CROP_PADDING = 2

# Bundle validation happens once per process, per asset root.
_VALIDATED_ROOTS: set[str] = set()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_bundle() -> None:
    """Verify manifest + every listed asset (size and SHA-256)."""
    key = str(ASSET_ROOT)
    if key in _VALIDATED_ROOTS:
        return
    manifest_path = ASSET_ROOT / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError("OCR_MODEL_BUNDLE_INVALID")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RuntimeError("OCR_MODEL_BUNDLE_INVALID") from None
    models = manifest.get("models")
    if not isinstance(models, dict) or not models:
        raise RuntimeError("OCR_MODEL_BUNDLE_INVALID")
    for rel, meta in models.items():
        path = MODEL_ROOT / rel
        if not path.is_file():
            raise RuntimeError("OCR_MODEL_BUNDLE_INVALID")
        if not isinstance(meta, dict):
            raise RuntimeError("OCR_MODEL_BUNDLE_INVALID")
        if int(meta.get("size", -1)) != path.stat().st_size:
            raise RuntimeError("OCR_MODEL_BUNDLE_INVALID")
        if str(meta.get("sha256", "")) != _sha256(path):
            raise RuntimeError("OCR_MODEL_BUNDLE_INVALID")
    _VALIDATED_ROOTS.add(key)


def _resolve_provider(provider: str) -> list[str]:
    name = str(provider or "cpu").strip().lower()
    ep = PROVIDER_BY_NAME.get(name, str(provider).strip())
    available = ort.get_available_providers()
    if ep not in available:
        raise RuntimeError("OCR_PROVIDER_UNAVAILABLE")
    return [ep]


class OneOCR:
    """The OneOCR pipeline over ONNX Runtime: detect, classify, recognize."""

    def __init__(
        self,
        provider: str = "cpu",
        max_lines: int = 1000,
        max_side: int = 1536,
        score_threshold: float = 0.5,
        link_threshold: float = 0.0,
        default_language: Optional[str] = None,
        default_rotation: Optional[int] = None,
    ) -> None:
        _validate_bundle()
        self.providers = _resolve_provider(provider)

        self.max_lines = max_lines
        self.max_side = max_side
        self.score_threshold = score_threshold
        self.link_threshold = link_threshold
        self.default_language = default_language
        self.default_rotation = default_rotation

        # Vocabularies (small, eager) + detector/classifier.
        self.vocabs = {}
        for script_id, info in SCRIPT_METADATA.items():
            name = info["name"]
            self.vocabs[name] = Vocabulary.load_clean(
                MODEL_ROOT / "vocab" / f"vocab_{name}.txt")

        self.detector = TextDetector(
            MODEL_ROOT / "detector" / "text_detector.onnx",
            providers=self.providers)
        self.classifier = ScriptClassifier(
            MODEL_ROOT / "classifier" / "script_classifier.onnx",
            providers=self.providers)
        self.recognizer = TextRecognizer(
            MODEL_ROOT, self.vocabs, providers=self.providers)

    # ── lifecycle ─────────────────────────────────────────────────────
    def __enter__(self) -> "OneOCR":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def close(self) -> None:
        pass

    def preload(self, languages: Optional[list[str]] = None) -> list[str]:
        """Load recognizer sessions + vocabularies now; return loaded names."""
        names = list(languages) if languages else list(SCRIPT_BY_NAME)
        loaded = []
        for language in names:
            if language is None:
                continue
            try:
                name, _ = self._resolve_language(str(language))
            except ValueError:
                continue
            self.recognizer._get_session(name)
            loaded.append(name)
        return loaded

    # ── helpers ───────────────────────────────────────────────────────
    def _resolve_language(self, language: str) -> tuple[str, int]:
        lang_lower = str(language).lower().strip()
        script_name = LANG_TO_SCRIPT.get(lang_lower, lang_lower)
        if script_name not in SCRIPT_BY_NAME:
            available = sorted(SCRIPT_BY_NAME.keys())
            raise ValueError(
                f"Unsupported language/script: '{language}'. "
                f"Available script groups: {available}")
        _, vocab_size = SCRIPT_BY_NAME[script_name]
        return script_name, vocab_size

    @staticmethod
    def _map_point_back(x: float, y: float, angle: float,
                        width: int, height: int) -> tuple[float, float]:
        if angle == 90.0:
            return y, height - x
        if angle == 180.0:
            return width - x, height - y
        if angle == 270.0:
            return width - y, x
        return x, y

    def _map_box_back(self, box, angle, width, height):
        out = []
        for i in range(4):
            out.extend(self._map_point_back(box[2 * i], box[2 * i + 1],
                                            angle, width, height))
        return out

    # ── pipeline ──────────────────────────────────────────────────────
    def recognize(
        self,
        image: Image.Image,
        language: Optional[str] = None,
        rotation: Optional[int] = None,
        max_side: Optional[int] = None,
        score_threshold: Optional[float] = None,
        link_threshold: Optional[float] = None,
    ) -> OcrResult:
        if any(d < _MIN_DIM or d > _MAX_DIM for d in image.size):
            raise ValueError(
                f"Image dimensions {image.size} out of supported range "
                f"({_MIN_DIM}-{_MAX_DIM} px).")

        orig_w, orig_h = image.size

        # Rotation: explicit > default > four-way auto search.
        target_rotation = rotation if rotation is not None \
            else self.default_rotation
        if target_rotation is not None:
            if target_rotation not in (0, 90, 180, 270):
                raise ValueError(
                    f"Invalid rotation angle: {target_rotation}. "
                    "Must be 0, 90, 180, or 270.")
            angle = float(target_rotation)
        else:
            angle = OrientationCorrector.estimate(
                image, self.detector, self.classifier)

        upright_img = (image if angle == 0.0
                       else image.rotate(angle, expand=True))

        target_max_side = max_side if max_side is not None else self.max_side
        levels = self.detector.run(upright_img, max_side=target_max_side)
        is_vertical = self.detector.vertical_layout(
            levels[0],
            score_threshold if score_threshold is not None
            else self.score_threshold)

        target_score = (score_threshold if score_threshold is not None
                        else self.score_threshold)
        target_link = (link_threshold if link_threshold is not None
                       else self.link_threshold)

        per_level = [
            self.detector.segment(m, is_vertical, target_score, target_link)
            for m in levels
        ]
        shapes = self.detector.merge_levels(per_level)
        # Reading order: horizontal lines top->bottom; vertical lines
        # right->left (standard CJK column order).
        shapes.sort(
            key=(lambda s: s.bounds[0]) if is_vertical
            else (lambda s: s.bounds[1]),
            reverse=is_vertical,
        )

        target_lang = language if language is not None else self.default_language
        fixed_lang_info = None
        if target_lang is not None:
            fixed_lang_info = self._resolve_language(target_lang)

        lines_out = []
        for shape in shapes[:self.max_lines]:
            x, y, w, h = shape.bounds
            crop_x1 = max(0, int(x) - _CROP_PADDING)
            crop_y1 = max(0, int(y) - _CROP_PADDING)
            crop_x2 = min(upright_img.width, int(x + w) + _CROP_PADDING)
            crop_y2 = min(upright_img.height, int(y + h) + _CROP_PADDING)
            if crop_x2 - crop_x1 < 1 or crop_y2 - crop_y1 < 1:
                continue

            crop = upright_img.crop((crop_x1, crop_y1, crop_x2, crop_y2))
            processed_crop = (crop.rotate(90, expand=True) if is_vertical
                              else crop)

            if fixed_lang_info is not None:
                lang_name, vocab_size = fixed_lang_info
            else:
                script_id, _ = self.classifier.classify(processed_crop)
                metadata = SCRIPT_METADATA.get(script_id, SCRIPT_METADATA[3])
                lang_name = metadata["name"]
                vocab_size = metadata["vocab_size"]

            words_data, steps = self.recognizer.recognize_line(
                processed_crop, lang_name, vocab_size)
            if not words_data:
                continue

            words_list = []
            line_text_parts = []
            per_step = processed_crop.width / max(steps, 1)

            for w_chars in words_data:
                w_text = "".join(c["char"] for c in w_chars)
                w_conf = sum(c["prob"] for c in w_chars) / len(w_chars)
                t_start, t_end = w_chars[0]["t"], w_chars[-1]["t"]
                start = t_start * per_step
                end = (t_end + 1) * per_step

                if is_vertical:
                    corners = [
                        crop_x1, crop_y1 + start,
                        crop_x2, crop_y1 + start,
                        crop_x2, crop_y1 + end,
                        crop_x1, crop_y1 + end,
                    ]
                else:
                    corners = [
                        crop_x1 + start, crop_y1,
                        crop_x1 + end, crop_y1,
                        crop_x1 + end, crop_y2,
                        crop_x1 + start, crop_y2,
                    ]
                box = self._map_box_back(corners, angle, orig_w, orig_h)
                words_list.append(Word(
                    text=w_text,
                    bbox=Quad(x1=box[0], y1=box[1], x2=box[2], y2=box[3],
                              x3=box[4], y3=box[5], x4=box[6], y4=box[7]),
                    confidence=float(w_conf),
                ))
                line_text_parts.append(w_text)

            full_line_text = ("".join(line_text_parts) if lang_name == "cjk"
                              else " ".join(line_text_parts))
            if not full_line_text.strip():
                continue

            line_box = self._map_box_back(shape.quad, angle, orig_w, orig_h)
            lines_out.append(Line(
                text=full_line_text,
                bbox=Quad(x1=line_box[0], y1=line_box[1],
                          x2=line_box[2], y2=line_box[3],
                          x3=line_box[4], y3=line_box[5],
                          x4=line_box[6], y4=line_box[7]),
                style=1 if is_vertical else 0,
                words=words_list,
            ))

        return OcrResult(lines=lines_out, image_angle=float(angle))

    def recognize_file(
        self,
        path: str | Path,
        language: Optional[str] = None,
        rotation: Optional[int] = None,
        max_side: Optional[int] = None,
        score_threshold: Optional[float] = None,
        link_threshold: Optional[float] = None,
    ) -> OcrResult:
        with Image.open(path) as handle:
            return self.recognize(
                handle,
                language=language,
                rotation=rotation,
                max_side=max_side,
                score_threshold=score_threshold,
                link_threshold=link_threshold,
            )
