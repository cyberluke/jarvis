"""Whisper language-detection evidence and the language confidence gate.

The Whisper runtime already derives the spoken language from the decoder's
language-token logprobs. That result is authoritative input to the grammar
pipeline — the judge must NOT re-infer the language from the transcript text.
This module carries that evidence and classifies how confident it is so the
decision engine knows whether to run a single-language judge or fan out to the
top candidate languages.

All thresholds come from Settings (``grammar_language_*``) — none are magic
constants here; the dataclass defaults merely mirror the config defaults.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class LanguageConfidence(Enum):
    """How trustworthy the Whisper language detection is."""

    HIGH = "high"
    MEDIUM = "medium"
    AMBIGUOUS = "ambiguous"


@dataclass
class LanguageAlternative:
    """One runner-up language candidate from Whisper's language logprobs."""

    language: str
    probability: float
    logprob: float


@dataclass
class WhisperLanguageDetection:
    """The language evidence Whisper produced for one utterance.

    ``language``/``probability``/``logprob`` are the winning candidate;
    ``alternatives`` carries the runner-ups (best first) when the backend
    exposes them. For a forced-language decode the alternatives list is empty
    and ``probability`` is the forced code's confidence.
    """

    language: str
    probability: float
    logprob: float
    alternatives: List[LanguageAlternative] = field(default_factory=list)

    @property
    def top_alternative(self) -> Optional[LanguageAlternative]:
        return self.alternatives[0] if self.alternatives else None


def classify_language_confidence(
    detection: WhisperLanguageDetection,
    *,
    high_probability: float = 0.90,
    high_margin: float = 0.25,
    medium_probability: float = 0.65,
    medium_margin: float = 0.10,
) -> LanguageConfidence:
    """Classify a Whisper language detection as high/medium/ambiguous.

    The margin is the gap between the winner's probability and the best
    runner-up (1.0 when there is no alternative). A detection is "high" only
    when both the winner is strong AND it clearly beats the runner-up; a close
    ``cs 0.48 / sk 0.43`` split lands in "ambiguous" so the caller runs the
    grammar judge for both candidates instead of trusting the argmax.
    """
    top = detection.top_alternative
    margin = 1.0 if top is None else (detection.probability - top.probability)

    if detection.probability >= high_probability and margin >= high_margin:
        return LanguageConfidence.HIGH
    if detection.probability >= medium_probability and margin >= medium_margin:
        return LanguageConfidence.MEDIUM
    return LanguageConfidence.AMBIGUOUS
