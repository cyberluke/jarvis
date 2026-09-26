"""End-to-end recovery-loop tests for the reported Czech bug (real re-decode).

These drive ``GrammarPipeline.run_recovery`` with stubbed judges and a stubbed
listener re-decode callable, proving the intended architecture: the corrupted
initial transcript is NOT admitted; a real second decode on the retained audio
produces a valid candidate that wins; an unrecoverable clip yields a non-admit
final status instead of silently accepting the original.
"""

import json
from types import SimpleNamespace

from jarvis.listening.grammar.contracts import TranscriptFinalStatus
from jarvis.listening.grammar.pipeline import GrammarPipeline


class _ScriptedJudge:
    """Grammar judge returning per-text verdicts without an LLM."""

    def __init__(self, verdicts):
        self.verdicts = verdicts

    def judge(self, inp):
        return self.verdicts[inp.transcript]


class _ScriptedRecovery:
    """Recovery judge returning a fixed hypothesis."""

    def __init__(self, best=None, confidence=0.88):
        self.best = best
        self.confidence = confidence

    def recover(self, inp, grammar):
        return SimpleNamespace(
            language=inp.language.code,
            recoverable=self.best is not None,
            candidates=[],
            status="ok",
            best_candidate=self.best,
            confidence=self.confidence,
            recommendation="redecode",
            prompt_version="asr-recovery-v1",
            latency_ms=0.0,
        )


def _grammar(valid, validity, recommendation, naturalness=0.9, correction="none"):
    return SimpleNamespace(
        language="cs",
        valid=valid,
        naturalness_score=naturalness,
        grammar_confidence=0.99 if valid else 0.95,
        issues=[],
        correction_type=correction,
        corrected_text=None,
        meaning_changed=not valid,
        likely_asr_corruption=not valid,
        linguistic_validity=validity,
        recommendation=recommendation,
        status="ok",
    )


def _cfg():
    return SimpleNamespace(
        grammar_judge_enabled=True,
        grammar_max_redecode_attempts=2,
        whisper_no_speech_threshold=0.5,
        grammar_reject_threshold=0.4,
        fast_model="gemma4-26b",
        llm_chat_model="gemma4-26b",
        llm_provider="openai_compatible",
        grammar_judge_timeout_sec=6.0,
        asr_recovery_timeout_sec=8.0,
        grammar_judge_cache_size=0,
        grammar_language_high_probability=0.90,
        grammar_language_high_margin=0.25,
        grammar_language_medium_probability=0.65,
        grammar_language_medium_margin=0.10,
        grammar_auto_correction_threshold=0.95,
    )


def _pipeline(judge, recovery):
    pipe = GrammarPipeline(_cfg())
    pipe.judge = judge
    pipe.recovery = recovery
    return pipe


def test_czech_bug_real_redecode_wins():
    """'na té počasí v Praze' -> real re-decode -> valid candidate accepted."""
    bad = "na té počasí v Praze"
    good = "Jaké je počasí v Praze?"
    verdicts = {
        bad: _grammar(False, "likely_asr_noise", "run_asr_recovery",
                      naturalness=0.18, correction="semantic"),
        good: _grammar(True, "valid", "accept_original", naturalness=0.98),
    }
    judge = _ScriptedJudge(verdicts)
    recovery = _ScriptedRecovery(best=good)
    pipe = _pipeline(judge, recovery)

    redecode_calls = []

    def fake_redecode(audio, *, language, strategy, attempt):
        redecode_calls.append({"language": language, "strategy": strategy,
                               "attempt": attempt})
        # One row whose text is the recovered Czech sentence.
        return [SimpleNamespace(text=good)], -0.05, 0.01

    inp = pipe.build_input(
        transcript=bad, language_code="cs",
        language_probability=0.997, language_logprob=-0.003,
        average_logprob=-0.62, no_speech_probability=0.003,
    )
    outcome = pipe.run_recovery(
        inp, audio=b"\x00\x01", initial_grammar=verdicts[bad],
        initial_text=bad, redecode_fn=fake_redecode,
    )

    # A real second Whisper invocation happened on the retained audio.
    assert len(redecode_calls) >= 1
    assert redecode_calls[0]["language"] == "cs"
    # The valid re-decode candidate wins; the invalid original is not admitted.
    assert outcome.status == TranscriptFinalStatus.ACCEPTED_REDECODE
    assert outcome.text == good
    assert outcome.admitted is True


def test_nonsense_rejected_when_redecode_cannot_fix():
    """'Daxitousty nebo Daxitousty.' stays nonsense -> not admitted."""
    nonsense = "Daxitousty nebo Daxitousty."
    verdicts = {
        nonsense: _grammar(False, "nonsense", "redecode",
                           naturalness=0.05, correction="uncertain"),
    }
    judge = _ScriptedJudge(verdicts)
    recovery = _ScriptedRecovery(best=None)
    pipe = _pipeline(judge, recovery)

    def fake_redecode(audio, *, language, strategy, attempt):
        # Re-decode returns the same nonsense (audio really is noise).
        return [SimpleNamespace(text=nonsense)], -0.5, 0.4

    inp = pipe.build_input(
        transcript=nonsense, language_code="cs",
        language_probability=0.9, language_logprob=-0.1,
        average_logprob=-0.5, no_speech_probability=0.4,
    )
    outcome = pipe.run_recovery(
        inp, audio=b"\x00\x01", initial_grammar=verdicts[nonsense],
        initial_text=nonsense, redecode_fn=fake_redecode,
    )

    # No admissible candidate -> not admitted to intent handling.
    assert outcome.admitted is False
    assert outcome.status in (
        TranscriptFinalStatus.NEEDS_USER_RETRY,
        TranscriptFinalStatus.REJECTED_INVALID,
    )


def test_high_no_speech_valid_text_stays_rejected():
    """A grammatically-valid sentence with high no_speech is still rejected
    (acoustic authority is preserved over linguistic validity)."""
    boilerplate = "Cảm ơn các bạn đã theo dõi và hẹn gặp lại."
    # Grammar judge would call it valid Vietnamese...
    verdicts = {
        boilerplate: _grammar(True, "valid", "accept_original", naturalness=0.95),
    }
    judge = _ScriptedJudge(verdicts)
    recovery = _ScriptedRecovery(best=None)
    pipe = _pipeline(judge, recovery)

    def fake_redecode(audio, *, language, strategy, attempt):
        # Re-decode still returns the boilerplate with high no_speech.
        return [SimpleNamespace(text=boilerplate)], -0.2, 0.81

    inp = pipe.build_input(
        transcript=boilerplate, language_code="vi",
        language_probability=0.9, language_logprob=-0.1,
        average_logprob=-0.2, no_speech_probability=0.81,
    )
    # Force the recovery path even though the grammar verdict says "valid" by
    # marking the initial grammar as likely corruption.
    initial = _grammar(False, "likely_asr_noise", "run_asr_recovery",
                       naturalness=0.2, correction="semantic")
    outcome = pipe.run_recovery(
        inp, audio=b"\x00\x01", initial_grammar=initial,
        initial_text=boilerplate, redecode_fn=fake_redecode,
    )

    # Every candidate carries no_speech_prob >= the reject threshold, so none
    # is admissible despite being "valid Vietnamese".
    assert outcome.admitted is False


def test_redecode_unavailable_is_redecode_failed_not_accept():
    """When no re-decode callable/audio exists, do not silently accept."""
    bad = "na té počasí v Praze"
    verdicts = {bad: _grammar(False, "likely_asr_noise", "run_asr_recovery",
                              naturalness=0.18, correction="semantic")}
    judge = _ScriptedJudge(verdicts)
    recovery = _ScriptedRecovery(best=None)
    pipe = _pipeline(judge, recovery)

    inp = pipe.build_input(
        transcript=bad, language_code="cs",
        language_probability=0.997, language_logprob=-0.003,
        average_logprob=-0.62, no_speech_probability=0.003,
    )
    # redecode_fn=None and no audio -> re-decode unavailable.
    outcome = pipe.run_recovery(
        inp, audio=None, initial_grammar=verdicts[bad],
        initial_text=bad, redecode_fn=None,
    )
    assert outcome.admitted is False
    assert outcome.status == TranscriptFinalStatus.REDECODE_FAILED
