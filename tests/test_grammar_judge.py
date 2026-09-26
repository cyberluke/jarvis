"""Grammar/Recovery judge tests with a stubbed LLM backend.

The backend is replaced by a stub returning canned JSON, so these tests verify
the full pass flow (prompt build -> call -> JSON parse -> typed result) and the
multilingual fixtures without a live model. The decision path is the thing
under test; the model's actual linguistic judgement is an integration concern.
"""

import json
from types import SimpleNamespace

import pytest

from jarvis.listening.grammar import grammar_judge as gj_module
from jarvis.listening.grammar import asr_recovery_judge as ar_module
from jarvis.listening.grammar.asr_recovery_judge import AsrRecoveryConfig, AsrRecoveryJudge
from jarvis.listening.grammar.grammar_judge import GrammarJudge, GrammarJudgeConfig
from jarvis.listening.grammar.grammar_schema import (
    AsrTokenEvidence,
    GrammarAsrEvidence,
    GrammarJudgeInput,
    LanguageEvidence,
)


class _StubBackend:
    """Returns a canned JSON string via the chat() shape the judges now use."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def chat(self, model, messages, **kwargs):
        system = messages[0]["content"] if messages else ""
        user = messages[1]["content"] if len(messages) > 1 else ""
        self.calls.append({"model": model, "system": system, "user": user})
        body = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return {"message": {"role": "assistant", "content": body}}


def _cfg(payload):
    backend = _StubBackend(payload)
    cfg = SimpleNamespace(fast_model="gemma4:e2b", llm_chat_model="gemma4:e2b")
    return cfg, backend


def _patch_backend(monkeypatch, module, backend):
    monkeypatch.setattr(module, "get_llm_backend", lambda cfg: backend)


def _input(transcript, code, prob=0.997, logprob=-0.003):
    return GrammarJudgeInput(
        transcript=transcript,
        language=LanguageEvidence(code=code, probability=prob, logprob=logprob),
        asr=GrammarAsrEvidence(
            average_logprob=-0.62,
            no_speech_probability=0.003,
            tokens=[AsrTokenEvidence(text=t, logprob=-0.5) for t in transcript.split()],
        ),
    )


# --- Czech regression (the reported bug) ----------------------------------------


def test_czech_regression_invalid_asr_corruption(monkeypatch):
    """'na té počasí v Praze' must be judged invalid + likely ASR corruption."""
    payload = {
        "language": "cs",
        "valid": False,
        "naturalnessScore": 0.18,
        "grammarConfidence": 0.99,
        "issues": [
            {"span": "té počasí", "type": "agreement", "severity": "high",
             "explanation": "morphologically inconsistent in Czech"},
            {"span": "na té počasí", "type": "likely_asr_corruption",
             "severity": "high", "explanation": "strongly unnatural"},
        ],
        "correctionType": "semantic",
        "correctedText": None,
        "meaningChanged": True,
        "likelyAsrCorruption": True,
        "recommendation": "run_asr_recovery",
    }
    cfg, backend = _cfg(payload)
    _patch_backend(monkeypatch, gj_module, backend)
    judge = GrammarJudge(GrammarJudgeConfig(cfg=cfg))
    result = judge.judge(_input("na té počasí v Praze", "cs"))

    assert result.status == "ok"
    assert result.valid is False
    assert result.likely_asr_corruption is True
    assert result.correction_type == "semantic"
    assert result.recommendation == "run_asr_recovery"
    assert result.meaning_changed is True
    # The judge must NOT have produced a free rewrite.
    assert result.corrected_text is None
    # The system prompt binds the judge to Czech.
    assert "Czech" in backend.calls[0]["system"]


def test_czech_valid_control(monkeypatch):
    payload = {
        "language": "cs", "valid": True, "naturalnessScore": 0.98,
        "grammarConfidence": 0.99, "issues": [], "correctionType": "none",
        "correctedText": None, "meaningChanged": False,
        "likelyAsrCorruption": False, "recommendation": "accept_original",
    }
    cfg, backend = _cfg(payload)
    _patch_backend(monkeypatch, gj_module, backend)
    judge = GrammarJudge(GrammarJudgeConfig(cfg=cfg))
    result = judge.judge(_input("Jaké je počasí v Praze?", "cs"))
    assert result.valid is True
    assert result.recommendation == "accept_original"


# --- Multilingual valid fixtures -------------------------------------------------


@pytest.mark.parametrize(
    "code,name,transcript",
    [
        ("en", "English", "What is the weather in Prague?"),
        ("de", "German", "Wie ist das Wetter in Prag?"),
        ("sk", "Slovak", "Aké je počasie v Prahe?"),
        ("vi", "Vietnamese", "Thời tiết ở Praha thế nào?"),
    ],
)
def test_valid_fixtures_per_language(monkeypatch, code, name, transcript):
    payload = {
        "language": code, "valid": True, "naturalnessScore": 0.97,
        "grammarConfidence": 0.98, "issues": [], "correctionType": "none",
        "correctedText": None, "meaningChanged": False,
        "likelyAsrCorruption": False, "recommendation": "accept_original",
    }
    cfg, backend = _cfg(payload)
    _patch_backend(monkeypatch, gj_module, backend)
    judge = GrammarJudge(GrammarJudgeConfig(cfg=cfg))
    result = judge.judge(_input(transcript, code))
    assert result.valid is True
    assert result.recommendation == "accept_original"
    # The prompt is dynamically bound to the detected language by name.
    assert name in backend.calls[0]["system"]


def test_unknown_language_falls_back_to_code(monkeypatch):
    payload = {"language": "xx", "valid": True, "correctionType": "none",
               "recommendation": "accept_original"}
    cfg, backend = _cfg(payload)
    _patch_backend(monkeypatch, gj_module, backend)
    judge = GrammarJudge(GrammarJudgeConfig(cfg=cfg))
    judge.judge(_input("some text", "xx"))
    # No descriptor for "xx" -> the raw code is used as the name.
    assert "xx" in backend.calls[0]["system"]


# --- Failure behavior --------------------------------------------------------------


def test_timeout_is_explicit_not_valid(monkeypatch):
    cfg = SimpleNamespace(fast_model="m", llm_chat_model="m", llm_provider="openai_compatible")

    class _NoneBackend:
        def chat(self, *a, **k):
            return None

    monkeypatch.setattr(gj_module, "get_llm_backend", lambda cfg: _NoneBackend())
    judge = GrammarJudge(GrammarJudgeConfig(cfg=cfg))
    result = judge.judge(_input("text", "cs"))
    assert result.status == "timeout"
    assert result.valid is False
    assert result.recommendation == "ask_user"


def test_unparseable_response_is_parse_error(monkeypatch):
    cfg = SimpleNamespace(fast_model="m", llm_chat_model="m", llm_provider="openai_compatible")

    class _BadBackend:
        def chat(self, *a, **k):
            return {"message": {"role": "assistant", "content": "this is not json at all"}}

    monkeypatch.setattr(gj_module, "get_llm_backend", lambda cfg: _BadBackend())
    judge = GrammarJudge(GrammarJudgeConfig(cfg=cfg))
    result = judge.judge(_input("text", "cs"))
    assert result.status == "parse_error"
    assert result.valid is False


# --- ASR Recovery Judge --------------------------------------------------------------


def test_recovery_judge_parses_candidates(monkeypatch):
    payload = {
        "language": "cs", "recoverable": True,
        "candidates": [
            {"text": "Jaké je počasí v Praze?", "confidence": 0.88,
             "phoneticSimilarity": 0.83, "semanticDistance": 0.41,
             "changedSpans": [{"from": "na té", "to": "jaké je"}]},
        ],
        "bestCandidate": "Jaké je počasí v Praze?",
        "confidence": 0.88, "recommendation": "redecode",
    }
    cfg, backend = _cfg(payload)
    _patch_backend(monkeypatch, ar_module, backend)
    judge = AsrRecoveryJudge(AsrRecoveryConfig(cfg=cfg))

    from jarvis.listening.grammar.grammar_schema import GrammarJudgeResult
    grammar = GrammarJudgeResult(
        language="cs", valid=False, likely_asr_corruption=True,
        correction_type="semantic",
    )
    result = judge.recover(_input("na té počasí v Praze", "cs"), grammar)
    assert result.status == "ok"
    assert result.recoverable is True
    assert result.best_candidate == "Jaké je počasí v Praze?"
    assert result.recommendation == "redecode"
    assert result.candidates[0].changed_spans[0].from_text == "na té"
