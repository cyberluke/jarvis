"""Structured observability for the grammar pipeline: events, metrics, cache.

Every grammar/recovery decision produces a structured log line through
:func:`debug_log` (category ``"grammar"``), so a transcript is never silently
mutated. A thread-safe in-process counter set backs the spec's metric names,
and a bounded LRU caches identical deterministic evaluations.

Raw transcript text is intentionally kept OUT of any counter label; it appears
only in the structured event log lines, never as a metric dimension.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from typing import Any, Dict, Optional

from ...debug import debug_log
from .decision_engine import TranscriptDecision
from .grammar_schema import GrammarJudgeResult
from .recovery_schema import AsrRecoveryResult


class GrammarEventLog:
    """Emits the spec's structured observability events."""

    @staticmethod
    def _emit(payload: Dict[str, Any]) -> None:
        try:
            debug_log(json.dumps(payload, ensure_ascii=False), "grammar")
        except Exception:
            pass

    @classmethod
    def grammar_result(
        cls,
        result: GrammarJudgeResult,
        *,
        language_probability: float = 0.0,
        transcript: str = "",
    ) -> None:
        cls._emit(
            {
                "event": "grammar_judge_result",
                "language": result.language,
                "language_probability": round(language_probability, 4),
                "transcript": transcript,
                "grammar_valid": result.valid,
                "naturalness_score": round(result.naturalness_score, 4),
                "grammar_confidence": round(result.grammar_confidence, 4),
                "likely_asr_corruption": result.likely_asr_corruption,
                "correction_type": result.correction_type,
                "recommendation": result.recommendation,
                "status": result.status,
                "prompt_version": result.prompt_version,
                "latency_ms": round(result.latency_ms, 1),
            }
        )

    @classmethod
    def recovery_result(cls, result: AsrRecoveryResult) -> None:
        cls._emit(
            {
                "event": "asr_recovery_result",
                "language": result.language,
                "recoverable": result.recoverable,
                "candidate": result.best_candidate,
                "confidence": round(result.confidence, 4),
                "recommendation": result.recommendation,
                "status": result.status,
                "prompt_version": result.prompt_version,
                "latency_ms": round(result.latency_ms, 1),
            }
        )

    @classmethod
    def decision(cls, decision: TranscriptDecision, *, language: str = "") -> None:
        cls._emit(
            {
                "event": "transcript_decision",
                "language": language,
                "kind": decision.kind,
                "source": decision.source,
                "reason": decision.reason,
                "status": decision.status,
                "text": decision.text,
            }
        )

    @classmethod
    def redecode(
        cls, *, reason: str, language: str, original: str, replacement: str = ""
    ) -> None:
        cls._emit(
            {
                "event": "asr_redecode",
                "reason": reason,
                "language": language,
                "original": original,
                "replacement": replacement,
            }
        )


class GrammarMetrics:
    """Thread-safe in-process counters for the spec's metric names.

    These are process-local counters (the project has no external metrics
    sink). They are read for tests and diagnostics; no raw transcript text is
    ever used as a label.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Dict[str, int] = {}

    def _inc(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + amount

    # Counter helpers matching the spec's names.
    def record_grammar(self, result: GrammarJudgeResult) -> None:
        self._inc("grammar_judge_total")
        if result.status != "ok":
            self._inc(f"grammar_judge_status_{result.status}")
            return
        if result.valid:
            self._inc("grammar_judge_valid_total")
        else:
            self._inc("grammar_judge_invalid_total")
        if result.correction_type == "surface":
            self._inc("grammar_surface_correction_total")
        elif result.correction_type == "semantic":
            self._inc("grammar_semantic_recovery_total")

    def record_decision(self, decision: TranscriptDecision) -> None:
        kind = decision.kind
        if kind == "redecode":
            self._inc("grammar_redecode_total")
        elif kind == "ask_user":
            self._inc("grammar_ask_user_total")
        self._inc(f"grammar_decision_{kind}")

    def record_language_ambiguous(self) -> None:
        self._inc("grammar_language_ambiguous_total")

    def snapshot(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._counters)


class GrammarCache:
    """Bounded LRU for identical deterministic grammar evaluations.

    The key hashes model + prompt version + language + transcript + a
    confidence summary, so reconnects / duplicate events / retry loops reuse
    the cached verdict instead of a redundant LLM call. Growth is bounded by
    ``maxsize``; the oldest entry is evicted on overflow.
    """

    def __init__(self, maxsize: int = 256) -> None:
        self.maxsize = max(0, int(maxsize))
        self._lock = threading.Lock()
        self._store: "OrderedDict[str, Any]" = OrderedDict()

    @staticmethod
    def make_key(
        *,
        model: str,
        prompt_version: str,
        language: str,
        transcript: str,
        confidence_summary: str = "",
    ) -> str:
        h = hashlib.sha256()
        for part in (model, prompt_version, language, transcript, confidence_summary):
            h.update(part.encode("utf-8", "replace"))
            h.update(b"\x00")
        return h.hexdigest()

    def get(self, key: str) -> Optional[Any]:
        if self.maxsize <= 0:
            return None
        with self._lock:
            if key not in self._store:
                return None
            self._store.move_to_end(key)
            return self._store[key]

    def put(self, key: str, value: Any) -> None:
        if self.maxsize <= 0:
            return
        with self._lock:
            self._store[key] = value
            self._store.move_to_end(key)
            while len(self._store) > self.maxsize:
                self._store.popitem(last=False)

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)
