"""The bounded, audio-retaining recovery loop.

This runner turns a ``redecode`` recommendation into a *real* second Whisper
invocation on the same retained audio — not a silent admission of the original
transcript. It owns the full admission decision for one utterance:

    initial candidate
      -> grammar judge
      -> if corruption/semantic: recovery judge
      -> bounded re-decode loop over the retained PCM (different strategy each)
      -> re-grammar every re-decoded candidate
      -> admissibility filter + deterministic ranking
      -> one TranscriptFinalStatus

Invariants enforced here:

- The retained audio is the trust root; the LLM recovery text is only ever a
  candidate/hint, never auto-promoted.
- Acoustic authority is preserved: a high no-speech candidate is rejected even
  if grammatically valid (boilerplate hallucinations stay filtered).
- The loop is bounded (``max_redecode_attempts``); on exhaustion with no valid
  candidate the transcript is rejected/needs-retry, never admitted as valid.
- A judge failure never silently admits: it yields ``JUDGE_FAILED``.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from ...debug import debug_log
from .asr_recovery_judge import AsrRecoveryJudge
from .contracts import (
    LinguisticValidity,
    RedecodeStrategy,
    TranscriptCandidate,
    TranscriptFinalStatus,
    candidate_is_admissible,
    rank_candidates,
)
from .grammar_events import GrammarEventLog
from .grammar_judge import GrammarJudge
from .grammar_schema import GrammarJudgeInput

#: Counter for a per-process utterance id used in trace logging.
_utterance_counter = itertools.count(1)

#: Re-decode strategies cycled across attempts, each differing meaningfully.
_REDECODE_STRATEGIES = (
    RedecodeStrategy.ALTERNATE_BEAM,
    RedecodeStrategy.TEMPERATURE_FALLBACK,
    RedecodeStrategy.RECOVERY_HINTED,
)


@dataclass
class AdmissionOutcome:
    """The terminal admission decision for one utterance."""

    status: TranscriptFinalStatus
    text: str
    source: str = "original"
    utterance_id: str = ""
    reason: str = ""

    @property
    def admitted(self) -> bool:
        return self.status.admitted


def _trace(utterance_id: str, event: str, **fields: Any) -> None:
    """One structured trace line carrying the utterance id across stages."""
    import json

    payload = {"utterance_id": utterance_id, "event": event}
    payload.update(fields)
    try:
        debug_log(json.dumps(payload, ensure_ascii=False), "grammar")
    except Exception:
        pass


class RecoveryRunner:
    """Drives the bounded recovery/re-decode loop for one utterance."""

    def __init__(
        self,
        cfg: Any,
        judge: GrammarJudge,
        recovery: AsrRecoveryJudge,
        # Callable(audio, language=..., strategy=..., attempt=...) ->
        #     (segments_list, avg_logprob, no_speech_prob)
        redecode_fn: Callable[..., tuple] = None,
    ):
        self.cfg = cfg
        self.judge = judge
        self.recovery = recovery
        self._redecode_fn = redecode_fn
        self.max_redecode_attempts = int(
            getattr(cfg, "grammar_max_redecode_attempts", 2) if cfg is not None else 2
        )
        self.no_speech_reject = float(
            getattr(cfg, "whisper_no_speech_threshold", 0.5) if cfg is not None else 0.5
        )
        self.grammar_naturalness_min = float(
            getattr(cfg, "grammar_reject_threshold", 0.4) if cfg is not None else 0.4
        )

    def _grammar_input_for(
        self, base: GrammarJudgeInput, transcript: str
    ) -> GrammarJudgeInput:
        """A fresh judge input for a candidate transcript, same language/ASR context."""
        return GrammarJudgeInput(
            transcript=transcript,
            language=base.language,
            asr=base.asr,
            lexical=base.lexical,
        )

    def _judge_candidate(
        self, base: GrammarJudgeInput, candidate: TranscriptCandidate
    ) -> TranscriptCandidate:
        """Run the grammar judge on a candidate and fold the verdict into it."""
        result = self.judge.judge(self._grammar_input_for(base, candidate.text))
        candidate.grammar_valid = result.valid
        candidate.grammar_naturalness = result.naturalness_score
        candidate.grammar_confidence = result.grammar_confidence
        return candidate, result

    def run(
        self,
        base_input: GrammarJudgeInput,
        *,
        audio,
        initial_grammar,
        initial_text: str,
        utterance_id: Optional[str] = None,
    ) -> AdmissionOutcome:
        """Run the full bounded recovery loop for one utterance.

        ``audio`` is the retained PCM (numpy float32). ``initial_grammar`` is
        the Pass 1 result for ``initial_text``. Returns the terminal decision.
        """
        uid = utterance_id or f"u-{next(_utterance_counter)}"
        language = base_input.language.code

        candidates: List[TranscriptCandidate] = [
            TranscriptCandidate(
                text=initial_text,
                source="whisper_initial",
                avg_logprob=base_input.asr.average_logprob,
                no_speech_prob=base_input.asr.no_speech_probability,
                grammar_valid=initial_grammar.valid,
                grammar_naturalness=initial_grammar.naturalness_score,
                grammar_confidence=initial_grammar.grammar_confidence,
            )
        ]

        # Pass 2: recovery hypothesis (a hint, never the authority).
        recovery_result = self.recovery.recover(base_input, initial_grammar)
        GrammarEventLog.recovery_result(recovery_result)
        if recovery_result.status == "ok" and recovery_result.best_candidate:
            candidates.append(
                TranscriptCandidate(
                    text=recovery_result.best_candidate,
                    source="llm_recovery_hypothesis",
                    recovery_confidence=recovery_result.confidence,
                )
            )

        # Bounded re-decode loop over the retained audio.
        redecode_ok = self._redecode_fn is not None and audio is not None
        attempts = 0
        if redecode_ok:
            for strategy in _REDECODE_STRATEGIES:
                if attempts >= self.max_redecode_attempts:
                    break
                attempts += 1
                rows, avg_lp, no_speech = self._redecode_fn(
                    audio, language=language, strategy=strategy.value, attempt=attempts
                )
                if not rows:
                    continue
                re_text = " ".join(
                    (getattr(r, "text", "") or "").strip() for r in rows
                ).strip()
                if not re_text:
                    continue
                _trace(uid, "asr_redecode", attempt=attempts, text=re_text,
                       strategy=strategy.value)
                cand = TranscriptCandidate(
                    text=re_text,
                    source="whisper_redecode",
                    avg_logprob=avg_lp,
                    no_speech_prob=no_speech,
                )
                # Re-grammar every re-decoded candidate.
                cand, g = self._judge_candidate(base_input, cand)
                _trace(uid, "grammar_result", text=re_text,
                       validity=g.linguistic_validity, valid=g.valid,
                       naturalness=round(g.naturalness_score, 4))
                candidates.append(cand)
        else:
            _trace(uid, "redecode_unavailable", reason="no retained audio or backend")

        # Grammar-validate the initial + recovery-hypothesis candidates too so
        # every candidate carries grammar evidence before ranking.
        for cand in candidates:
            if cand.grammar_valid is None:
                cand, _ = self._judge_candidate(base_input, cand)

        # Admissibility + deterministic ranking.
        admissible = [
            c
            for c in candidates
            if candidate_is_admissible(
                c,
                no_speech_reject=self.no_speech_reject,
                grammar_naturalness_min=self.grammar_naturalness_min,
            )
        ]
        ranked = rank_candidates(admissible)

        if ranked:
            winner = ranked[0]
            if winner.source == "whisper_initial":
                status = TranscriptFinalStatus.ACCEPTED_ORIGINAL
            elif winner.source == "grammar_surface_correction":
                status = TranscriptFinalStatus.ACCEPTED_CORRECTED
            else:
                status = TranscriptFinalStatus.ACCEPTED_REDECODE
            _trace(uid, "transcript_final", status=status.value, text=winner.text,
                   source=winner.source)
            return AdmissionOutcome(
                status=status, text=winner.text, source=winner.source,
                utterance_id=uid, reason="ranked_winner",
            )

        # No admissible candidate: reject / needs-retry. Never admit an invalid.
        if not redecode_ok:
            status = TranscriptFinalStatus.REDECODE_FAILED
            reason = "redecode_unavailable_no_valid_candidate"
        else:
            status = TranscriptFinalStatus.NEEDS_USER_RETRY
            reason = "no_admissible_candidate"
        _trace(uid, "transcript_final", status=status.value, text=initial_text,
               reason=reason)
        return AdmissionOutcome(
            status=status, text=initial_text, source="original",
            utterance_id=uid, reason=reason,
        )
