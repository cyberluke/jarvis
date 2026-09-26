"""OpenVINO IR Whisper model catalog, cache layout, and download lifecycle.

Single source of truth shared by the setup wizard, the Settings window, the
listener's OpenVINO adapter, and the isolated speech worker. Backend identity,
model family, precision, repository revision, and cache path all agree here.

The catalog entries are the official OpenVINO IR exports (NNCF INT8
weight-compressed and FP16 artifacts). Repository presence is not proof of
compatibility with a given NPU/driver/runtime; the worker reports the actual
loaded identities at startup.

INT8 here means the selected INT8 weight-compressed artifact; an FP16 artifact
likewise names the stored weights, not the precision of every runtime
operation. Artifact precision and device/compiler execution details stay
separate concepts in every status line.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Dict, Optional, Tuple

#: Backend identity string stored in ``whisper_backend``.
BACKEND_ID = "openvino"

#: Score-semantics identifier carried through the worker handshake. It names
#: the genuine decoder statistics contract (avg/no-speech log probabilities).
SCORE_SEMANTICS = "ov-genai-whisper-logprob-v1"

#: Worker stdio protocol version.
PROTOCOL_VERSION = 1

#: Immutable Hub revisions (resolved 2026-09-24 from the repository API).
#: ``model -> precision -> (repository id, revision, total_bytes)``.
CATALOG: Dict[str, Dict[str, Tuple[str, str, int]]] = {
    "tiny": {
        "int8": ("OpenVINO/whisper-tiny-int8-ov", "a850762d97243dee30f46ca309720541af619ab0", 48798585),
        "fp16": ("OpenVINO/whisper-tiny-fp16-ov", "44662d68573bd732e50fc295d1c1ec47e77f67df", 84345023),
    },
    "base": {
        "int8": ("OpenVINO/whisper-base-int8-ov", "0606293f0511136ada21755a265492f623a934b8", 84713802),
        "fp16": ("OpenVINO/whisper-base-fp16-ov", "84fbe975a79a8c996fd32c036558f29e2db6670f", 154240654),
    },
    "small": {
        "int8": ("OpenVINO/whisper-small-int8-ov", "5b831719e093f86e1970be663e524fe001489f9b", 256761816),
        "fp16": ("OpenVINO/whisper-small-fp16-ov", "2410d022171ca8a97343182f88eec8807a324db9", 493214244),
    },
    "medium": {
        "int8": ("OpenVINO/whisper-medium-int8-ov", "8d43cce846729381f56bd45a1c70925cee2222ff", 784027062),
        "fp16": ("OpenVINO/whisper-medium-fp16-ov", "eddc8397f55af66a5c4e411893e1b21caf187f0b", 1538858732),
    },
    "large-v3": {
        "int8": ("OpenVINO/whisper-large-v3-int8-ov", "e197310d1fe6d5b40d0ce84c2614a547de21cb62", 1568411870),
        "fp16": ("OpenVINO/whisper-large-v3-fp16-ov", "220761e60602a5ca694c409d5f424563b75d6820", 3099051222),
    },
    "large-v3-turbo": {
        "int8": ("OpenVINO/whisper-large-v3-turbo-int8-ov", "b568445dd5dc8c695bde596f8acbb4694fd6ba64", 828096445),
        "fp16": ("OpenVINO/whisper-large-v3-turbo-fp16-ov", "0250c28d68c7c10d6b5cb39707e876c0c66ab6f8", 1627657898),
    },
}

#: Every multilingual family is valid for ``cs``, ``vi`` and ``cs+vi``. The
#: ``.en`` / distilled options belong to the other backends only.
SUPPORTED_FAMILIES = tuple(CATALOG.keys())

#: Files the GenAI Whisper pipeline needs from a repository (actual layout of
#: the OpenVINO IR exports: encoder + stateless decoder IR pairs plus the
#: tokenizer/detokenizer IR pair and the JSON assets). No CTranslate2
#: ``model.bin`` and no decoder-with-past naming is assumed.
REQUIRED_ASSETS: Tuple[str, ...] = (
    "config.json",
    "generation_config.json",
    "preprocessor_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.json",
    "merges.txt",
    "added_tokens.json",
    "special_tokens_map.json",
    "normalizer.json",
    "openvino_encoder_model.xml",
    "openvino_encoder_model.bin",
    "openvino_decoder_model.xml",
    "openvino_decoder_model.bin",
    "openvino_tokenizer.xml",
    "openvino_tokenizer.bin",
    "openvino_detokenizer.xml",
    "openvino_detokenizer.bin",
)

#: Multilingual fallback for the no-speech token id when the model's own
#: ``generation_config.json`` carries no explicit ``no_speech_token_id``.
DEFAULT_NO_SPEECH_TOKEN_ID = 1


def catalog_entry(model: str, precision: str) -> Optional[Tuple[str, str, int]]:
    """Return ``(repo_id, revision, total_bytes)`` for one exact selection."""
    family = CATALOG.get(str(model or "").strip())
    if family is None:
        return None
    return family.get(str(precision or "").strip().lower())


def cache_subdir(model: str, precision: str, revision: str) -> str:
    """Backend/model/precision/revision separated cache folder name."""
    return f"openvino/{model}-{precision}-{revision[:12]}"


def resolve_model_dir(cache_root: str, model: str, precision: str) -> str:
    """Snapshot directory holding the complete IR manifest, or ``""``.

    The IR manifest is the presence of every required asset with a non-zero
    size; a partial folder is reported as incomplete, never as installed.
    """
    entry = catalog_entry(model, precision)
    if not cache_root or entry is None:
        return ""
    repo_id, revision, _total = entry
    root = os.path.join(str(cache_root), cache_subdir(model, precision, revision))
    if _validate_folder(root):
        return root
    return ""


def _validate_folder(folder: str) -> bool:
    """True when every required asset exists with non-zero size."""
    try:
        if not os.path.isdir(folder):
            return False
        for name in REQUIRED_ASSETS:
            path = os.path.join(folder, name)
            if not (os.path.isfile(path) and os.path.getsize(path) > 0):
                return False
        return True
    except OSError:
        return False


def no_speech_token_id(model_dir: str) -> Optional[int]:
    """No-speech token id from the model's own generation config.

    Resolved from ``generation_config.json`` (``no_speech_token_id``) first,
    then from the ``suppress_tokens``-adjacent ``<|notimestamps|>``-style
    special-token table, and finally the documented multilingual default.
    """
    try:
        with open(os.path.join(model_dir, "generation_config.json"), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    value = data.get("no_speech_token_id")
    if isinstance(value, int):
        return value
    special = data.get("suppressed_tokens") or data.get("lang_to_id") or {}
    if isinstance(special, dict) and "<|nospeech|>" in special and isinstance(special["<|nospeech|>"], int):
        return int(special["<|nospeech|>"])
    return DEFAULT_NO_SPEECH_TOKEN_ID


def download_model(
    model: str,
    precision: str,
    cache_root: str,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """Download the selected artifact only, with per-asset resume.

    Returns ``{"ok": bool, "dir": str, "error": str, "code": str}``. INT8 and
    FP16 selections use separate folders, so one never overwrites the other.
    Each asset is fetched individually: a cancelled or failed run leaves the
    already-fetched files in place and the next call resumes from the first
    missing asset. The folder is reported installed only after the full
    required-asset validation passes.
    """
    entry = catalog_entry(model, precision)
    if entry is None:
        return {"ok": False, "dir": "", "error": f"unknown model/precision: {model}/{precision}", "code": "OV_MODEL_INCOMPLETE"}
    repo_id, revision, _total = entry
    folder = os.path.join(str(cache_root), cache_subdir(model, precision, revision))
    if _validate_folder(folder):
        return {"ok": True, "dir": folder, "error": "", "code": ""}
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "dir": folder, "error": f"disk error: {exc}", "code": "OV_MODEL_INCOMPLETE"}

    try:
        from huggingface_hub import hf_hub_download
    except ImportError as exc:
        return {"ok": False, "dir": folder, "error": f"huggingface_hub unavailable: {exc}", "code": "OV_MODEL_INCOMPLETE"}

    done = 0
    total = len(REQUIRED_ASSETS)
    for name in REQUIRED_ASSETS:
        if cancel_cb is not None and cancel_cb():
            return {"ok": False, "dir": folder, "error": "cancelled", "code": "OV_MODEL_INCOMPLETE"}
        try:
            path = os.path.join(folder, name)
            if not (os.path.isfile(path) and os.path.getsize(path) > 0):
                hf_hub_download(
                    repo_id=repo_id,
                    filename=name,
                    revision=revision,
                    local_dir=folder,
                )
        except Exception as exc:  # network/disk failures are actionable below
            reason = str(exc)
            code = "OV_MODEL_INCOMPLETE"
            if "429" in reason or "rate limit" in reason.lower():
                code = "OV_MODEL_INCOMPLETE"
            return {"ok": False, "dir": folder, "error": f"{name}: {reason}", "code": code}
        done += 1
        if progress_cb is not None:
            try:
                progress_cb(done, total)
            except Exception:
                pass

    if not _validate_folder(folder):
        return {"ok": False, "dir": folder, "error": "required assets incomplete", "code": "OV_MODEL_INCOMPLETE"}
    return {"ok": True, "dir": folder, "error": "", "code": ""}


def compile_cache_id(model: str, precision: str, revision: str, core_version: str,
                     genai_version: str, device: str, driver: str = "") -> str:
    """Compilation-cache identity string.

    Model revision, artifact precision, runtime/companion versions, device
    identity, driver/compiler identity and the compile settings all take part
    in invalidation, so a stale NPU blob cannot be reused after any of them
    changes.
    """
    parts = [
        f"model={model}",
        f"precision={precision}",
        f"revision={revision[:12] if revision else '-'}",
        f"core={core_version or '-'}",
        f"genai={genai_version or '-'}",
        f"device={device or '-'}",
        f"driver={driver or '-'}",
    ]
    return "|".join(parts)
