"""Language confidence gate and decision engine unit tests.

These tests are backend-free: they exercise the deterministic policy layer
(language confidence classification, auto-correct gating, decision routing)
without any LLM call. The judge passes are covered separately with a stubbed
backend in ``test_grammar_judge.py``.
"""

from jarvis.listening.grammar.decision_engine import (
    decide_transcript,
    may_auto_correct,
)
from jarvis.listening.grammar.grammar_schema import GrammarJudgeResult
from jarvis.listening.grammar.language_confidence import (
    LanguageAlternative,
    LanguageConfidence,
    WhisperLanguageDetection,
    classify_language_confidence,
)
from jarvis.listening.grammar.recovery_schema import (
    AsrRecoveryCandidate,
    AsrRecoveryResult,
)


# --- Language confidence gate -------------------------------------------------


def test_high_confidence_single_language():
    det = WhisperLanguageDetection(language="cs", probability=0.994, logprob=-0.006)
    assert classify_language_confidence(det) is LanguageConfidence.HIGH


def test_high_requires_margin_over_runner_up():
    # Strong winner but a close runner-up: margin too small -> not "high".
    det = WhisperLanguageDetection(
        language="cs",
        probability=0.92,
        logprob=-0.08,
        alternatives=[LanguageAlternative("sk", 0.80, -0.22)],
    )
    assert classify_language_confidence(det) is not LanguageConfidence.HIGH


def test_ambiguous_close_cs_sk_split():
    # The spec's example: cs 0.48 / sk 0.43 must be ambiguous.
    det = WhisperLanguageDetection(
        language="cs",
        probability=0.48,
        logprob=-0.73,
        alternatives=[LanguageAlternative("sk", 0.43, -0.84)],
    )
    assert classify_language_confidence(det) is LanguageConfidence.AMBIGUOUS


def test_medium_band():
    det = WhisperLanguageDetection(
        language="en",
        probability=0.70,
        logprob=-0.36,
        alternatives=[LanguageAlternative("de", 0.50, -0.69)],
    )
    assert classify_language_confidence(det) is LanguageConfidence.MEDIUM


# --- Auto-correct gate ----------------------------------------------------------


def _surface_grammar(confidence=0.97):
    return GrammarJudgeResult(
        language="cs",
        valid=False,
        correction_type="surface",
        corrected_text="fixed",
        meaning_changed=False,
        grammar_confidence=confidence,
        recommendation="accept_surface_correction",
    )


def test_may_auto_correct_surface_high_confidence():
    assert may_auto_correct(_surface_grammar()) is True


def test_may_auto_correct_rejects_low_confidence():
    assert may_auto_correct(_surface_grammar(confidence=0.80)) is False


def test_may_auto_correct_rejects_semantic():
    g = _surface_grammar()
    g.correction_type = "semantic"
    assert may_auto_correct(g) is False


def test_may_auto_correct_rejects_meaning_changed():
    g = _surface_grammar()
    g.meaning_changed = True
    assert may_auto_correct(g) is False


def test_may_auto_correct_rejects_failed_judge():
    g = _surface_grammar()
    g.status = "timeout"
    assert may_auto_correct(g) is False


# --- Decision engine ------------------------------------------------------------


def test_decide_accepts_valid_original():
    g = GrammarJudgeResult(
        language="cs", valid=True, recommendation="accept_original"
    )
    d = decide_transcript(g, original_text="Jaké je počasí?")
    assert d.kind == "accept"
    assert d.text == "Jaké je počasí?"
    assert d.source == "original"


def test_decide_corrects_surface():
    g = _surface_grammar()
    d = decide_transcript(g, original_text="raw")
    assert d.kind == "correct"
    assert d.text == "fixed"
    assert d.source == "grammar"


def test_decide_redecode_on_likely_asr_corruption():
    # The Czech regression: "na té počasí v Praze" is invalid + likely ASR
    # corruption -> must re-decode, never silently auto-correct the semantic
    # reconstruction.
    g = GrammarJudgeResult(
        language="cs",
        valid=False,
        likely_asr_corruption=True,
        correction_type="semantic",
        meaning_changed=True,
        recommendation="run_asr_recovery",
    )
    recovery = AsrRecoveryResult(
        language="cs",
        recoverable=True,
        candidates=[
            AsrRecoveryCandidate(
                text="Jaké je počasí v Praze?",
                confidence=0.88,
                phonetic_similarity=0.83,
                semantic_distance=0.41,
            )
        ],
        best_candidate="Jaké je počasí v Praze?",
        confidence=0.88,
        recommendation="redecode",
    )
    d = decide_transcript(g, recovery, original_text="na té počasí v Praze")
    assert d.kind == "redecode"
    # The guessed recovery text is NOT promoted to user intent.
    assert d.text == "na té počasí v Praze"


def test_decide_failed_judge_preserves_original_as_ask_user():
    g = GrammarJudgeResult(language="cs", status="unavailable")
    d = decide_transcript(g, original_text="raw text")
    assert d.kind == "ask_user"
    assert d.status == "unavailable"
    assert d.text == "raw text"


def test_decide_ambiguous_falls_to_ask_user():
    g = GrammarJudgeResult(
        language="cs", valid=False, recommendation="ask_user",
        correction_type="uncertain",
    )
    d = decide_transcript(g, original_text="raw")
    assert d.kind == "ask_user"
