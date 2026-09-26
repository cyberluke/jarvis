"""Language-aware ASR grammar validation and recovery.

This package owns the post-ASR validation pipeline that runs *after* Whisper
produces a transcript and *before* the text is admitted downstream (transcript
buffer, intent judge, LLM). It replaces the old behaviour where Hunspell's
word-level validity was implicitly treated as transcript validity.

Pipeline (per the implementation spec):

    audio -> Whisper/OpenVINO ASR
          -> language detection from Whisper language logprobs
          -> token/segment confidence metadata
          -> cheap lexical checks (optional Hunspell evidence)
          -> GrammarJudge (Pass 1) for the detected language
          -> AsrRecoveryJudge (Pass 2) for the detected language
          -> decision engine: accept / surface-correct / re-decode / ask user

Design invariants:

- Czech is NOT hardcoded. The judge is bound dynamically to the Whisper-detected
  language via :mod:`language_map`.
- The Grammar Judge is not a rewriting agent: it validates and minimally
  corrects; it never paraphrases, translates, or invents intent.
- Semantic corrections are never silently auto-accepted; they route to
  re-decode or ask-user through one deterministic decision engine.
- A judge failure/timeout is explicit and observable; it is never silently
  reported as "valid".
"""

from .language_map import language_name, LANGUAGE_DESCRIPTORS
from .language_confidence import (
    LanguageConfidence,
    WhisperLanguageDetection,
    classify_language_confidence,
)
from .grammar_schema import (
    GrammarIssue,
    GrammarJudgeInput,
    GrammarJudgeResult,
    GrammarAsrEvidence,
    LanguageEvidence,
)
from .recovery_schema import (
    AsrRecoveryCandidate,
    AsrRecoveryResult,
    ChangedSpan,
)
from .grammar_judge import GrammarJudge, GrammarJudgeConfig
from .asr_recovery_judge import AsrRecoveryJudge
from .decision_engine import (
    TranscriptDecision,
    decide_transcript,
    may_auto_correct,
)
from .grammar_events import GrammarMetrics, GrammarEventLog, GrammarCache
from .contracts import (
    LinguisticValidity,
    RedecodeStrategy,
    TranscriptCandidate,
    TranscriptFinalStatus,
    candidate_is_admissible,
    rank_candidates,
)
from .recovery_runner import AdmissionOutcome, RecoveryRunner
from .pipeline import GrammarPipeline

__all__ = [
    "language_name",
    "LANGUAGE_DESCRIPTORS",
    "LanguageConfidence",
    "WhisperLanguageDetection",
    "classify_language_confidence",
    "GrammarIssue",
    "GrammarJudgeInput",
    "GrammarJudgeResult",
    "GrammarAsrEvidence",
    "LanguageEvidence",
    "AsrRecoveryCandidate",
    "AsrRecoveryResult",
    "ChangedSpan",
    "GrammarJudge",
    "GrammarJudgeConfig",
    "AsrRecoveryJudge",
    "TranscriptDecision",
    "decide_transcript",
    "may_auto_correct",
    "GrammarMetrics",
    "GrammarEventLog",
    "GrammarCache",
    "LinguisticValidity",
    "RedecodeStrategy",
    "TranscriptCandidate",
    "TranscriptFinalStatus",
    "candidate_is_admissible",
    "rank_candidates",
    "AdmissionOutcome",
    "RecoveryRunner",
    "GrammarPipeline",
]
