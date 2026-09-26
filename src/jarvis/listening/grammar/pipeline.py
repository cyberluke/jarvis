"""Top-level grammar validation pipeline.

This facade is the single entry point the listener calls for each FINAL
transcript. It wires together: the Whisper language evidence -> confidence
gate -> Pass 1 grammar judge -> (conditional) Pass 2 recovery judge -> the
deterministic decision engine, with caching, metrics and structured events.

It deliberately does NOT perform the actual audio re-decode: re-decoding is an
ASR-engine operation owned by the listener. The decision engine returns
``kind="redecode"`` and the listener decides whether to act on it (a real
re-decode of the same clip) using the detected language preserved.
"""

from __future__ import annotations

from typing import Any, List, Optional

from ...debug import debug_log
from ...llm import resolve_model, Tier
from .asr_recovery_judge import AsrRecoveryConfig, AsrRecoveryJudge
from .decision_engine import TranscriptDecision, decide_transcript
from .grammar_events import GrammarCache, GrammarEventLog, GrammarMetrics
from .grammar_judge import GrammarJudge, GrammarJudgeConfig
from .grammar_schema import (
    GRAMMAR_JUDGE_PROMPT_VERSION,
    AsrTokenEvidence,
    GrammarAsrEvidence,
    GrammarJudgeInput,
    LanguageAlternativeEvidence,
    LanguageEvidence,
    LexicalEvidence,
)
from .language_confidence import (
    LanguageConfidence,
    WhisperLanguageDetection,
    LanguageAlternative,
    classify_language_confidence,
)
from .recovery_runner import AdmissionOutcome, RecoveryRunner


def _cfg_get(cfg: Any, name: str, default: Any) -> Any:
    return getattr(cfg, name, default) if cfg is not None else default


class GrammarPipeline:
    """Runs the language-aware validation passes for one FINAL transcript."""

    def __init__(self, cfg: Any = None):
        self.cfg = cfg
        self.enabled = bool(_cfg_get(cfg, "grammar_judge_enabled", True))
        if not self.enabled:
            # Explicit, observable kill-switch: no validation claim is made.
            debug_log(
                '{"event":"grammar_judge_disabled"}',
                "grammar",
            )
        self.judge = GrammarJudge(
            GrammarJudgeConfig(
                cfg=cfg,
                timeout_sec=float(_cfg_get(cfg, "grammar_judge_timeout_sec", 6.0)),
            )
        )
        self.recovery = AsrRecoveryJudge(
            AsrRecoveryConfig(
                cfg=cfg,
                timeout_sec=float(_cfg_get(cfg, "asr_recovery_timeout_sec", 8.0)),
            )
        )
        self.metrics = GrammarMetrics()
        self.cache = GrammarCache(int(_cfg_get(cfg, "grammar_judge_cache_size", 256)))
        # Thresholds
        self.high_prob = float(_cfg_get(cfg, "grammar_language_high_probability", 0.90))
        self.high_margin = float(_cfg_get(cfg, "grammar_language_high_margin", 0.25))
        self.med_prob = float(_cfg_get(cfg, "grammar_language_medium_probability", 0.65))
        self.med_margin = float(_cfg_get(cfg, "grammar_language_medium_margin", 0.10))
        self.auto_threshold = float(_cfg_get(cfg, "grammar_auto_correction_threshold", 0.95))

    def build_input(
        self,
        *,
        transcript: str,
        language_code: str,
        language_probability: float = 1.0,
        language_logprob: float = 0.0,
        language_alternatives: Optional[List[dict]] = None,
        average_logprob: Optional[float] = None,
        no_speech_probability: Optional[float] = None,
        tokens: Optional[List[AsrTokenEvidence]] = None,
        unknown_tokens: Optional[List[str]] = None,
        misspellings: int = 0,
    ) -> GrammarJudgeInput:
        alts = [
            LanguageAlternativeEvidence(
                language=str(a.get("language", "") or ""),
                probability=float(a.get("probability", 0.0) or 0.0),
                logprob=float(a.get("logprob", 0.0) or 0.0),
            )
            for a in (language_alternatives or [])
            if isinstance(a, dict)
        ]
        return GrammarJudgeInput(
            transcript=transcript,
            language=LanguageEvidence(
                code=language_code,
                probability=language_probability,
                logprob=language_logprob,
                alternatives=alts,
            ),
            asr=GrammarAsrEvidence(
                average_logprob=average_logprob,
                no_speech_probability=no_speech_probability,
                tokens=tokens or [],
            ),
            lexical=LexicalEvidence(
                unknown_tokens=unknown_tokens or [],
                misspellings=misspellings,
            ),
        )

    def language_confidence(self, inp: GrammarJudgeInput) -> LanguageConfidence:
        detection = WhisperLanguageDetection(
            language=inp.language.code,
            probability=inp.language.probability,
            logprob=inp.language.logprob,
            alternatives=[
                LanguageAlternative(a.language, a.probability, a.logprob)
                for a in inp.language.alternatives
            ],
        )
        return classify_language_confidence(
            detection,
            high_probability=self.high_prob,
            high_margin=self.high_margin,
            medium_probability=self.med_prob,
            medium_margin=self.med_margin,
        )

    def validate(self, inp: GrammarJudgeInput) -> TranscriptDecision:
        """Run the full pipeline for one FINAL transcript.

        Returns the decision. When the subsystem is disabled the original
        transcript passes straight through as ``accept`` (a transparent
        bypass, matching the spellcheck bypass convention).
        """
        if not self.enabled:
            return TranscriptDecision(
                kind="accept", text=inp.transcript, source="original", reason="disabled"
            )

        confidence = self.language_confidence(inp)
        if confidence is LanguageConfidence.AMBIGUOUS:
            self.metrics.record_language_ambiguous()
            debug_log(
                f"grammar: language ambiguous (code={inp.language.code} "
                f"p={inp.language.probability:.3f})",
                "grammar",
            )

        # Cache identical deterministic evaluations.
        model = resolve_model(self.cfg, Tier.FAST) if self.cfg is not None else ""
        confidence_summary = (
            f"p={inp.language.probability:.3f};lp={inp.language.logprob:.3f};"
            f"alp={inp.asr.average_logprob};nsp={inp.asr.no_speech_probability}"
        )
        cache_key = GrammarCache.make_key(
            model=model,
            prompt_version=GRAMMAR_JUDGE_PROMPT_VERSION,
            language=inp.language.code,
            transcript=inp.transcript,
            confidence_summary=confidence_summary,
        )
        cached = self.cache.get(cache_key)
        if cached is not None:
            debug_log("grammar: cache hit", "grammar")
            return cached

        # Pass 1.
        grammar = self.judge.judge(inp)
        self.metrics.record_grammar(grammar)
        GrammarEventLog.grammar_result(
            grammar,
            language_probability=inp.language.probability,
            transcript=inp.transcript,
        )

        # Pass 2 only when Pass 1 flags corruption / semantic change.
        recovery = None
        needs_recovery = (
            grammar.status == "ok"
            and (grammar.likely_asr_corruption or grammar.correction_type == "semantic")
        )
        if needs_recovery:
            recovery = self.recovery.recover(inp, grammar)
            GrammarEventLog.recovery_result(recovery)

        decision = decide_transcript(
            grammar,
            recovery,
            original_text=inp.transcript,
            auto_correction_threshold=self.auto_threshold,
        )
        self.metrics.record_decision(decision)
        GrammarEventLog.decision(decision, language=inp.language.code)

        self.cache.put(cache_key, decision)
        return decision

    def make_runner(self, redecode_fn) -> RecoveryRunner:
        """Build a recovery runner bound to the listener's re-decode callable.

        ``redecode_fn`` is the listener's real re-decode API (same retained
        PCM, forced detected language, a meaningfully different strategy). The
        runner owns the bounded loop, per-candidate re-grammar, admissibility
        and deterministic ranking; the LLM recovery output is only a candidate.
        """
        return RecoveryRunner(
            self.cfg, self.judge, self.recovery, redecode_fn=redecode_fn
        )

    def run_recovery(
        self,
        inp: GrammarJudgeInput,
        *,
        audio,
        initial_grammar,
        initial_text: str,
        redecode_fn,
        utterance_id: str = "",
    ) -> AdmissionOutcome:
        """Run the bounded audio-retaining recovery loop for one utterance."""
        runner = self.make_runner(redecode_fn)
        return runner.run(
            inp,
            audio=audio,
            initial_grammar=initial_grammar,
            initial_text=initial_text,
            utterance_id=utterance_id,
        )
