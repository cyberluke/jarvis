"""Pass 2 ASR Recovery Judge data contracts.

The recovery pass runs only when Pass 1 flags likely ASR corruption or a
semantic correction. It proposes the smallest *phonetically-plausible*
reconstruction, but a candidate is a diagnostic signal, never blindly trusted:
a high semantic distance routes to re-decode even when the candidate reads
correctly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional

#: Prompt contract version, logged with every recovery result.
ASR_RECOVERY_PROMPT_VERSION = "asr-recovery-v1"

_RECOVERY_RECOMMENDATIONS = {"accept_recovery", "redecode", "ask_user"}


@dataclass
class ChangedSpan:
    from_text: str
    to_text: str


@dataclass
class AsrRecoveryCandidate:
    text: str
    confidence: float
    phonetic_similarity: float
    semantic_distance: float
    changed_spans: List[ChangedSpan] = field(default_factory=list)


@dataclass
class AsrRecoveryResult:
    """Normalised Pass 2 result.

    ``status`` mirrors the Grammar Judge: ``"ok"`` | ``"timeout"`` |
    ``"unavailable"`` | ``"parse_error"``.
    """

    language: str = ""
    recoverable: bool = False
    candidates: List[AsrRecoveryCandidate] = field(default_factory=list)
    best_candidate: Optional[str] = None
    confidence: float = 0.0
    recommendation: str = "ask_user"
    status: str = "ok"
    prompt_version: str = ASR_RECOVERY_PROMPT_VERSION
    latency_ms: float = 0.0


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def parse_recovery_result(data: Any, *, latency_ms: float = 0.0) -> AsrRecoveryResult:
    """Validate/normalise the recovery judge's decoded JSON into a result."""
    result = AsrRecoveryResult(latency_ms=latency_ms)
    if not isinstance(data, dict):
        result.status = "parse_error"
        return result

    result.language = str(data.get("language", "") or "")
    result.recoverable = _as_bool(data.get("recoverable"))

    candidates: List[AsrRecoveryCandidate] = []
    for raw in data.get("candidates") or []:
        if not isinstance(raw, dict):
            continue
        spans: List[ChangedSpan] = []
        for sp in raw.get("changedSpans") or []:
            if not isinstance(sp, dict):
                continue
            spans.append(
                ChangedSpan(
                    from_text=str(sp.get("from", "") or ""),
                    to_text=str(sp.get("to", "") or ""),
                )
            )
        candidates.append(
            AsrRecoveryCandidate(
                text=str(raw.get("text", "") or ""),
                confidence=_clamp01(_as_float(raw.get("confidence"))),
                phonetic_similarity=_clamp01(_as_float(raw.get("phoneticSimilarity"))),
                semantic_distance=_clamp01(_as_float(raw.get("semanticDistance"))),
                changed_spans=spans,
            )
        )
    result.candidates = candidates

    best = data.get("bestCandidate")
    result.best_candidate = str(best) if isinstance(best, str) and best.strip() else None
    result.confidence = _clamp01(_as_float(data.get("confidence")))

    rec = str(data.get("recommendation", "ask_user") or "ask_user")
    result.recommendation = rec if rec in _RECOVERY_RECOMMENDATIONS else "ask_user"

    return result
