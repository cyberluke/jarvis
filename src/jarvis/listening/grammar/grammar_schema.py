"""Pass 1 Grammar Judge data contracts.

These mirror the implementation spec's TypeScript interfaces. The judge returns
schema-shaped JSON; this module validates and normalises it into typed results
the decision engine can consume without re-parsing prose.

The judge is a *validator*, not a rewriter: ``correctedText`` is only ever a
minimal, meaning-preserving surface fix. Anything that changes meaning is
flagged via ``correctionType="semantic"`` / ``likelyAsrCorruption`` and routed
to the recovery pass or re-decode, never silently applied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional

#: Prompt contract version, logged with every result so a prompt change is
#: never silently deployed. Bump when the system/user template changes.
GRAMMAR_JUDGE_PROMPT_VERSION = "grammar-judge-v1"

#: Valid values for the issue ``type`` field.
_ISSUE_TYPES = {
    "grammar",
    "agreement",
    "morphology",
    "syntax",
    "word_order",
    "semantic_anomaly",
    "invalid_construction",
    "likely_asr_corruption",
    "other",
}

_ISSUE_SEVERITIES = {"low", "medium", "high"}

_CORRECTION_TYPES = {"none", "surface", "semantic", "uncertain"}

_RECOMMENDATIONS = {
    "accept_original",
    "accept_surface_correction",
    "run_asr_recovery",
    "redecode",
    "ask_user",
}

_LINGUISTIC_VALIDITIES = {
    "valid",
    "valid_but_unusual",
    "malformed",
    "nonsense",
    "likely_asr_noise",
}


@dataclass
class LanguageAlternativeEvidence:
    language: str
    probability: float
    logprob: float


@dataclass
class LanguageEvidence:
    """Language block forwarded to the judge."""

    code: str
    probability: float
    logprob: float
    alternatives: List[LanguageAlternativeEvidence] = field(default_factory=list)


@dataclass
class AsrTokenEvidence:
    text: str
    logprob: Optional[float] = None
    start_ms: Optional[int] = None
    end_ms: Optional[int] = None


@dataclass
class GrammarAsrEvidence:
    """ASR confidence block forwarded to the judge."""

    average_logprob: Optional[float] = None
    no_speech_probability: Optional[float] = None
    tokens: List[AsrTokenEvidence] = field(default_factory=list)


@dataclass
class LexicalEvidence:
    """Cheap lexical (Hunspell) signal — a feature, not a validity verdict."""

    unknown_tokens: List[str] = field(default_factory=list)
    misspellings: int = 0


@dataclass
class GrammarJudgeInput:
    transcript: str
    language: LanguageEvidence
    asr: GrammarAsrEvidence = field(default_factory=GrammarAsrEvidence)
    lexical: LexicalEvidence = field(default_factory=LexicalEvidence)


@dataclass
class GrammarIssue:
    span: str
    type: str
    severity: str
    explanation: str


@dataclass
class GrammarJudgeResult:
    """Normalised Pass 1 result.

    ``status`` carries the failure dimension separately from ``valid`` so a
    timeout/unavailable judge is never confused with a "valid transcript":
    ``"ok"`` | ``"timeout"`` | ``"unavailable"`` | ``"parse_error"``.
    """

    language: str = ""
    valid: bool = False
    naturalness_score: float = 0.0
    grammar_confidence: float = 0.0
    issues: List[GrammarIssue] = field(default_factory=list)
    correction_type: str = "uncertain"
    corrected_text: Optional[str] = None
    meaning_changed: bool = False
    likely_asr_corruption: bool = False
    #: Richer validity classification (``valid`` / ``valid_but_unusual`` /
    #: ``malformed`` / ``nonsense`` / ``likely_asr_noise``). Derived from
    #: ``valid`` when the model omits it.
    linguistic_validity: str = "valid"
    recommendation: str = "ask_user"
    status: str = "ok"
    prompt_version: str = GRAMMAR_JUDGE_PROMPT_VERSION
    latency_ms: float = 0.0


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def parse_grammar_result(data: Any, *, latency_ms: float = 0.0) -> GrammarJudgeResult:
    """Validate/normalise the judge's decoded JSON into a result.

    Unknown enum values are normalised to safe fallbacks (``other`` /
    ``uncertain`` / ``ask_user``) so a slightly off-spec answer cannot crash
    the decision engine; the numbers are clamped into [0, 1].
    """
    result = GrammarJudgeResult(latency_ms=latency_ms)
    if not isinstance(data, dict):
        result.status = "parse_error"
        return result

    result.language = str(data.get("language", "") or "")
    result.valid = _as_bool(data.get("valid"))
    result.naturalness_score = min(1.0, max(0.0, _as_float(data.get("naturalnessScore"))))
    result.grammar_confidence = min(1.0, max(0.0, _as_float(data.get("grammarConfidence"))))

    issues: List[GrammarIssue] = []
    for raw in data.get("issues") or []:
        if not isinstance(raw, dict):
            continue
        itype = str(raw.get("type", "other") or "other")
        if itype not in _ISSUE_TYPES:
            itype = "other"
        severity = str(raw.get("severity", "medium") or "medium")
        if severity not in _ISSUE_SEVERITIES:
            severity = "medium"
        issues.append(
            GrammarIssue(
                span=str(raw.get("span", "") or ""),
                type=itype,
                severity=severity,
                explanation=str(raw.get("explanation", "") or ""),
            )
        )
    result.issues = issues

    ctype = str(data.get("correctionType", "uncertain") or "uncertain")
    result.correction_type = ctype if ctype in _CORRECTION_TYPES else "uncertain"

    corrected = data.get("correctedText")
    result.corrected_text = str(corrected) if isinstance(corrected, str) and corrected.strip() else None

    result.meaning_changed = _as_bool(data.get("meaningChanged"))
    result.likely_asr_corruption = _as_bool(data.get("likelyAsrCorruption"))

    validity = str(data.get("linguisticValidity", "") or "")
    if validity in _LINGUISTIC_VALIDITIES:
        result.linguistic_validity = validity
    else:
        # Derive from the legacy bool when the model omits the richer label.
        if result.likely_asr_corruption:
            result.linguistic_validity = "likely_asr_noise"
        elif not result.valid:
            result.linguistic_validity = "malformed"
        else:
            result.linguistic_validity = "valid"

    rec = str(data.get("recommendation", "ask_user") or "ask_user")
    result.recommendation = rec if rec in _RECOMMENDATIONS else "ask_user"

    return result
