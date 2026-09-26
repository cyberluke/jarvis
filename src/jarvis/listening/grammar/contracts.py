"""Shared enums and provenance-carrying contracts for the recovery loop.

These types extend the grammar pipeline from analysis-only to a real
audio-retaining recovery loop:

- :class:`LinguisticValidity` — richer than a bare ``valid`` bool; ``NONSENSE``
  and ``LIKELY_ASR_NOISE`` short-circuit intent handling entirely.
- :class:`TranscriptFinalStatus` — the only states that may reach intent
  handling are the ``ACCEPTED_*`` ones.
- :class:`RedecodeStrategy` — how a second Whisper decode must differ from the
  first, so a re-decode is never a useless identical replay.
- :class:`TranscriptCandidate` — one transcript hypothesis with provenance and
  the acoustic/grammar evidence used for admissibility and ranking.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class LinguisticValidity(str, Enum):
    """Language-aware validity classification beyond a bare bool."""

    VALID = "valid"
    VALID_BUT_UNUSUAL = "valid_but_unusual"
    MALFORMED = "malformed"
    NONSENSE = "nonsense"
    LIKELY_ASR_NOISE = "likely_asr_noise"

    @property
    def blocks_intent(self) -> bool:
        """True when intent judgment must not run on this transcript."""
        return self in (
            LinguisticValidity.MALFORMED,
            LinguisticValidity.NONSENSE,
            LinguisticValidity.LIKELY_ASR_NOISE,
        )


class TranscriptFinalStatus(str, Enum):
    """Terminal states for one utterance's admission decision."""

    ACCEPTED_ORIGINAL = "accepted_original"
    ACCEPTED_CORRECTED = "accepted_corrected"
    ACCEPTED_REDECODE = "accepted_redecode"

    REJECTED_INVALID = "rejected_invalid"
    REJECTED_NO_SPEECH = "rejected_no_speech"

    REDECODE_FAILED = "redecode_failed"
    JUDGE_FAILED = "judge_failed"

    NEEDS_USER_RETRY = "needs_user_retry"

    @property
    def admitted(self) -> bool:
        """Only ACCEPTED_* states may continue to intent handling."""
        return self in (
            TranscriptFinalStatus.ACCEPTED_ORIGINAL,
            TranscriptFinalStatus.ACCEPTED_CORRECTED,
            TranscriptFinalStatus.ACCEPTED_REDECODE,
        )


class RedecodeStrategy(str, Enum):
    """How a re-decode differs meaningfully from the initial decode."""

    ALTERNATE_BEAM = "alternate_beam"
    TEMPERATURE_FALLBACK = "temperature_fallback"
    RECOVERY_HINTED = "recovery_hinted"


@dataclass
class TranscriptCandidate:
    """One transcript hypothesis with full provenance and evidence.

    ``source`` names where the text came from; the acoustic and grammar fields
    are the evidence used for admissibility and ranking. The LLM is never the
    sole authority — the retained audio (via acoustic confidence) is the trust
    root for spoken content.
    """

    text: str
    source: str  # "whisper_initial" | "whisper_redecode" |
    #            # "grammar_surface_correction" | "llm_recovery_hypothesis"
    avg_logprob: Optional[float] = None
    no_speech_prob: Optional[float] = None
    grammar_valid: Optional[bool] = None
    grammar_naturalness: Optional[float] = None
    grammar_confidence: Optional[float] = None
    recovery_confidence: Optional[float] = None


def candidate_is_admissible(
    c: TranscriptCandidate,
    *,
    no_speech_reject: float = 0.5,
    grammar_naturalness_min: float = 0.4,
) -> bool:
    """Reject clearly-invalid candidates before ranking.

    Acoustic authority is preserved: a grammatically-valid sentence with a high
    no-speech probability (a Whisper boilerplate hallucination) is still
    rejected — grammar validity never rescues high-no-speech output.
    """
    if c.no_speech_prob is not None and c.no_speech_prob >= no_speech_reject:
        return False
    if c.grammar_valid is False:
        return False
    if (
        c.grammar_naturalness is not None
        and c.grammar_naturalness < grammar_naturalness_min
    ):
        return False
    return True


def rank_candidates(candidates: List[TranscriptCandidate]) -> List[TranscriptCandidate]:
    """Order admissible candidates best-first, deterministically.

    Ranking blends acoustic confidence (``avg_logprob``), grammar naturalness
    and grammar confidence. It never defers to the LLM alone. The sort key is
    total-ordered so the result is stable and reproducible.
    """

    def key(c: TranscriptCandidate) -> tuple:
        grammar_nat = c.grammar_naturalness if c.grammar_naturalness is not None else 0.5
        grammar_conf = c.grammar_confidence if c.grammar_confidence is not None else 0.0
        # Normalise avg_logprob (typically in [-1, 0]) into [0, 1] via +1 clamp.
        acoustic = 0.0
        if c.avg_logprob is not None:
            acoustic = min(1.0, max(0.0, c.avg_logprob + 1.0))
        no_speech = c.no_speech_prob if c.no_speech_prob is not None else 0.0
        score = (0.5 * acoustic) + (0.3 * grammar_nat) + (0.2 * grammar_conf)
        # Lower no_speech is better; negate so higher key wins.
        return (score, -no_speech, grammar_conf, c.text)

    return sorted(candidates, key=key, reverse=True)
