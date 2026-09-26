"""OneOCR backend: the warm, self-contained OCR engine for Jarvis.

One long-lived engine instance per process (created when the
Everywhere/OCR service starts), never per request. Screen captures use
``rotation_hint=0`` and the known UI language so the four-way orientation
search and the classifier pass are skipped.

Structured diagnostics (tag ``ocr``): ``ocr.engine.ready``,
``ocr.request.started``, ``ocr.request.completed``,
``ocr.request.failed``, ``ocr.provider.unavailable``,
``ocr.assets.invalid``. Metadata only — recognized text is not logged.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from PIL import Image

from ..._vendor.oneocr import common as _common
from ..._vendor.oneocr import engine as _engine
from ...debug import debug_log
from .base import (
    OCR_MODEL_BUNDLE_INVALID,
    OCR_PROVIDER_UNAVAILABLE,
    OcrDocument,
)
from .result import document_from_result

_BACKEND_ID = "oneocr"


def _bundle_version() -> str:
    try:
        import json
        manifest = _engine.ASSET_ROOT / "manifest.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        commit = str((data.get("upstream") or {}).get("commit") or "")
        return commit[:12] if commit else str(data.get("schema", ""))
    except Exception:
        return ""


class OneOcrBackend:
    """Jarvis-facing wrapper over the vendored OneOCR engine."""

    def __init__(self, cfg: Any = None) -> None:
        provider = str(getattr(cfg, "everywhere_ocr_provider", "cpu")
                       or "cpu").strip().lower()
        started = time.monotonic()
        self._engine = _engine.OneOCR(provider=provider)
        self.provider = _engine.PROVIDER_BY_NAME.get(provider, provider)

        # Warm the pipeline: detector + classifier already exist; pay the
        # latin recognizer cost now, plus other configured languages.
        preload = ["latin"]
        for value in _extra_language_hints(cfg):
            if value not in preload:
                preload.append(value)
        loaded = self._engine.preload(preload)
        debug_log(
            f"ocr.engine.ready backend={_BACKEND_ID} provider={self.provider} "
            f"preload={','.join(loaded)} "
            f"bundle={_bundle_version()} "
            f"duration_ms={int((time.monotonic() - started) * 1000)}",
            "ocr",
        )

    # ── public API ────────────────────────────────────────────────────
    def preload(self, languages: Optional[list[str]] = None) -> list[str]:
        """Load recognizer sessions now; return the loaded script names."""
        return self._engine.preload(languages)

    def recognize(
        self,
        image: Image.Image,
        *,
        language_hint: Optional[str] = None,
        rotation_hint: Optional[int] = 0,
    ) -> OcrDocument:
        """Recognize one image; raises with a typed code on failure.

        ``rotation_hint=0`` (the default) skips the orientation search for
        already-upright screen captures; ``None`` runs the four-way search.
        ``language_hint`` is an ISO code or script name; ``cs``/``sk``/
        ``vi``/``en`` resolve to ``latin`` and skip the classifier.
        """
        language = _normalize_language(language_hint)
        width, height = image.size
        started = time.monotonic()
        debug_log(
            f"ocr.request.started backend={_BACKEND_ID} "
            f"provider={self.provider} image_width={width} "
            f"image_height={height} language_hint={language or 'auto'} "
            f"rotation_hint={'auto' if rotation_hint is None else rotation_hint}",
            "ocr",
        )
        try:
            result = self._engine.recognize(
                image,
                language=language,
                rotation=rotation_hint,
            )
        except RuntimeError as exc:
            code = str(exc) or "OCR_BACKEND_UNAVAILABLE"
            event = ("ocr.assets.invalid"
                     if code == OCR_MODEL_BUNDLE_INVALID
                     else "ocr.provider.unavailable"
                     if code == OCR_PROVIDER_UNAVAILABLE
                     else "ocr.request.failed")
            debug_log(
                f"{event} backend={_BACKEND_ID} reason={code} "
                f"image_width={width} image_height={height}",
                "ocr",
            )
            raise
        except Exception as exc:
            debug_log(
                f"ocr.request.failed backend={_BACKEND_ID} "
                f"reason={type(exc).__name__} "
                f"image_width={width} image_height={height}",
                "ocr",
            )
            raise

        document = document_from_result(result, backend=_BACKEND_ID)
        debug_log(
            f"ocr.request.completed backend={_BACKEND_ID} "
            f"provider={self.provider} image_width={width} "
            f"image_height={height} language_hint={language or 'auto'} "
            f"rotation_hint={int(document.rotation_deg)} "
            f"line_count={len(document.lines)} "
            f"word_count={document.word_count} "
            f"duration_ms={int((time.monotonic() - started) * 1000)} "
            f"model_bundle_version={_bundle_version()}",
            "ocr",
        )
        return document


def _normalize_language(language_hint: Optional[str]) -> Optional[str]:
    """ISO code -> script name (cs/sk/vi/en -> latin); None = auto."""
    if not language_hint:
        return None
    key = str(language_hint).strip().lower()
    if not key:
        return None
    # Closed sets like "cs+vi" share one script group here (latin).
    first = key.split("+", 1)[0]
    return _common.LANG_TO_SCRIPT.get(first, first)


def _extra_language_hints(cfg: Any) -> list[str]:
    """Script names implied by the configured speech/UI languages."""
    hints: list[str] = []
    for value in (
        getattr(cfg, "whisper_language", "") or "",
        getattr(cfg, "everywhere_translate_default_language", "") or "",
    ):
        for part in str(value).split("+"):
            script = _normalize_language(part)
            if script and script in _common.SCRIPT_BY_NAME \
                    and script not in hints:
                hints.append(script)
    return hints


def create_backend(cfg: Any = None) -> Optional[OneOcrBackend]:
    """Build the warm backend; None with an explicit typed reason."""
    try:
        return OneOcrBackend(cfg)
    except RuntimeError as exc:
        code = str(exc)
        if code == OCR_PROVIDER_UNAVAILABLE:
            debug_log(f"ocr.provider.unavailable backend={_BACKEND_ID} "
                      f"provider={getattr(cfg, 'everywhere_ocr_provider', 'cpu')}",
                      "ocr")
        elif code == OCR_MODEL_BUNDLE_INVALID:
            debug_log(f"ocr.assets.invalid backend={_BACKEND_ID} "
                      f"reason={OCR_MODEL_BUNDLE_INVALID}", "ocr")
        return None
