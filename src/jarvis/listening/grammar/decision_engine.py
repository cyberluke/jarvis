"""The single deterministic decision engine for transcript admission.

One function owns the final action for every FINAL Whisper transcript, so the
decision logic is never duplicated across call sites. It consumes the Pass 1
grammar result and (when present) the Pass 2 recovery result and emits exactly
one decision: accept original, accept a surface correction, re-decode, or ask
the user.

Policy highlights (from the spec):

- Only surface-level, meaning-preserving, high-confidence corrections are
  auto-accepted.
- A judge that failed/timed out is never treated as "valid": the original
  transcript is preserved and the configured failure action applies.
- Likely ASR corruption prefers re-decode over semantic guessing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .grammar_schema import GrammarJudgeResult
from .recovery_schema import AsrRecoveryResult


@dataclass
class TranscriptDecision:
    """The single outcome of the validation pipeline.

    ``kind``: ``"accept"`` | ``"correct"`` | ``"redecode"`` | ``"ask_user"``.
    ``text`` is the admitted text for accept/correct, else the original.
    ``source``: ``"original"`` | ``"grammar"``. ``reason`` is a short machine
    string for observability. ``status`` mirrors the judge status so a failed
    judge is visible downstream.
    """

    kind: str
    text: str
    source: str = "original"
    reason: str = ""
    status: str = "ok"


def may_auto_correct(
    grammar: GrammarJudgeResult,
    recovery: Optional[AsrRecoveryResult] = None,
    *,
    auto_correction_threshold: float = 0.95,
) -> bool:
    """True only when a correction is a safe, meaning-preserving surface fix."""
    if grammar.status != "ok":
        return False
    if grammar.correction_type != "surface":
        return False
    if grammar.meaning_changed:
        return False
    if not grammar.corrected_text:
        return False
    if grammar.grammar_confidence < auto_correction_threshold:
        return False
    if recovery is not None and recovery.confidence < auto_correction_threshold:
        return False
    return True


def decide_transcript(
    grammar: GrammarJudgeResult,
    recovery: Optional[AsrRecoveryResult] = None,
    *,
    original_text: str = "",
    auto_correction_threshold: float = 0.95,
) -> TranscriptDecision:
    """Decide the final action for one FINAL transcript.

    The caller passes the original Whisper text via ``original_text`` so an
    accept/correct decision can name the exact admitted string. A failed judge
    (``status != "ok"``) preserves the original and reports the failure; the
    caller's configured fallback decides whether to keep going, re-decode, or
    ask the user — here we surface it as ``ask_user`` with the failing status.
    """
    original = original_text or ""

    # A failed/unavailable judge is never "valid": preserve original, surface it.
    if grammar.status != "ok":
        return TranscriptDecision(
            kind="ask_user",
            text=original,
            source="original",
            reason=f"grammar_{grammar.status}",
            status=grammar.status,
        )

    # Valid transcript: accept as-is.
    if grammar.valid and grammar.recommendation == "accept_original":
        return TranscriptDecision(
            kind="accept",
            text=grammar.corrected_text or original,
            source="original",
            reason="valid",
            status="ok",
        )

    # Safe surface correction: auto-accept.
    if may_auto_correct(
        grammar, recovery, auto_correction_threshold=auto_correction_threshold
    ):
        return TranscriptDecision(
            kind="correct",
            text=grammar.corrected_text or original,
            source="grammar",
            reason="surface_correction",
            status="ok",
        )

    # Likely ASR corruption or an explicit re-decode recommendation wins over
    # any semantic guess.
    if grammar.likely_asr_corruption or grammar.recommendation == "redecode":
        return TranscriptDecision(
            kind="redecode",
            text=original,
            source="original",
            reason="grammar_or_asr_corruption",
            status="ok",
        )

    if recovery is not None and recovery.status == "ok":
        if recovery.recommendation == "redecode":
            return TranscriptDecision(
                kind="redecode",
                text=original,
                source="original",
                reason="recovery_recommends_redecode",
                status="ok",
            )
        # A recovery candidate with acceptable confidence but semantic change:
        # still prefer re-decode over promoting a guessed reconstruction.
        if grammar.correction_type == "semantic" or grammar.meaning_changed:
            return TranscriptDecision(
                kind="redecode",
                text=original,
                source="original",
                reason="semantic_correction_needs_redecode",
                status="ok",
            )

    # Anything unresolved: ask the user rather than guessing.
    return TranscriptDecision(
        kind="ask_user",
        text=original,
        source="original",
        reason="ambiguous_transcript",
        status="ok",
    )
