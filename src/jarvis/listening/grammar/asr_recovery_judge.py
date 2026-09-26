"""Pass 2: the ASR Recovery Judge.

Runs only when Pass 1 flags likely ASR corruption or a semantic correction.
Its job is narrower than the grammar judge's: decide whether the suspicious
spans are likely ASR substitutions for a nearby phonetic sequence, and if so
propose the smallest plausible reconstruction.

A recovery candidate is a *diagnostic signal* — it may feed constrained
re-decoding, N-best reranking, or semantic comparison — but it is never blindly
promoted to user intent. A candidate whose semantic distance exceeds the
configured threshold routes to re-decode even when it reads correctly.

Called at ``temperature=0`` through the same fast-tier backend as Pass 1.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, List, Optional

from ...debug import debug_log
from ...llm import get_llm_backend, resolve_model, Tier
from ._json_extract import extract_strict_json, recovery_response_format
from .grammar_schema import GrammarJudgeInput, GrammarJudgeResult
from .language_map import language_name
from .recovery_schema import (
    ASR_RECOVERY_PROMPT_VERSION,
    AsrRecoveryResult,
    parse_recovery_result,
)


@dataclass
class AsrRecoveryConfig:
    cfg: Any = None
    timeout_sec: float = 8.0


_SYSTEM_PROMPT = """You are an ASR recovery judge.

The upstream speech recognizer detected language {language_code} ({language_name}).

The transcript contains one or more linguistically suspicious spans reported by
the grammar validator. Your job is NOT to rewrite the sentence freely. Your job
is to determine whether the suspicious words are likely speech-recognition
substitutions for a nearby phonetic sequence.

Prefer corrections that:
1. preserve the rest of the transcript,
2. are phonetically close to the suspicious words,
3. produce natural grammar in {language_name},
4. require the fewest semantic assumptions,
5. are supported by low-confidence ASR tokens.

If a confident reconstruction cannot be made, return recoverable=false and an
empty candidates list.

Do not translate. Do not paraphrase. Do not invent entities. Keep names,
products and technical terms verbatim unless acoustic evidence strongly
supports a correction.

Return JSON only, no Markdown:
{{"language": "{language_code}", "recoverable": true/false,
  "candidates": [{{"text": "...", "confidence": 0.0-1.0,
    "phoneticSimilarity": 0.0-1.0, "semanticDistance": 0.0-1.0,
    "changedSpans": [{{"from": "...", "to": "..."}}]}}],
  "bestCandidate": null,
  "confidence": 0.0-1.0,
  "recommendation": "accept_recovery|redecode|ask_user"}}"""


class AsrRecoveryJudge:
    """Second-pass phonetic reconstruction for ASR-corrupted transcripts."""

    def __init__(self, config: Optional[AsrRecoveryConfig] = None):
        self.config = config or AsrRecoveryConfig()

    def _build_user_prompt(
        self, inp: GrammarJudgeInput, grammar: GrammarJudgeResult
    ) -> str:
        lines = [
            f"Transcript: {inp.transcript!r}",
            "",
            "Suspicious spans from the grammar validator:",
        ]
        if grammar.issues:
            for issue in grammar.issues:
                lines.append(
                    f"  - span={issue.span!r} type={issue.type} severity={issue.severity}"
                )
        else:
            lines.append("  (none reported)")

        asr = inp.asr
        if asr.tokens:
            lines.append("")
            lines.append("ASR tokens (low logprob = weak acoustic evidence):")
            for tok in asr.tokens:
                lp = f"{tok.logprob:.4f}" if tok.logprob is not None else "-"
                lines.append(f"  - text={tok.text!r} logprob={lp}")
        if asr.average_logprob is not None:
            lines.append(f"averageLogprob={asr.average_logprob:.4f}")
        return "\n".join(lines)

    def recover(
        self, inp: GrammarJudgeInput, grammar: GrammarJudgeResult
    ) -> AsrRecoveryResult:
        """Propose a phonetic reconstruction for ``inp.transcript``.

        Never raises for expected failure modes; a timeout / unreachable
        backend / unparseable answer returns an explicit ``status``.
        """
        cfg = self.config.cfg
        language_code = inp.language.code
        language_disp = language_name(language_code) or language_code

        model = resolve_model(cfg, Tier.FAST) if cfg is not None else ""
        if not model:
            debug_log("asr_recovery: no fast model resolved", "grammar")
            return AsrRecoveryResult(language=language_code, status="unavailable")

        system = _SYSTEM_PROMPT.format(
            language_code=language_code,
            language_name=language_disp,
        )
        user = self._build_user_prompt(inp, grammar)

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        extra_options = {
            "temperature": 0.0,
            "top_p": 1.0,
            "max_tokens": 700,
            "response_format": recovery_response_format(),
        }

        started = time.perf_counter()
        text: Optional[str] = None
        try:
            backend = get_llm_backend(cfg)
            resp = backend.chat(
                model,
                messages,
                timeout_sec=self.config.timeout_sec,
                extra_options=extra_options,
                thinking=False,
            )
            if isinstance(resp, dict):
                message = resp.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str) and content.strip():
                        text = content
        except Exception as exc:
            latency = (time.perf_counter() - started) * 1000.0
            debug_log(
                f"asr_recovery: backend error ({type(exc).__name__}): {exc}",
                "grammar",
            )
            return AsrRecoveryResult(
                language=language_code, status="unavailable", latency_ms=latency
            )
        latency = (time.perf_counter() - started) * 1000.0

        if text is None:
            debug_log("asr_recovery: timeout / empty response", "grammar")
            return AsrRecoveryResult(
                language=language_code, status="timeout", latency_ms=latency
            )

        data = extract_strict_json(text)
        if data is None:
            debug_log(
                f"asr_recovery: fail-closed parse rejected response (len={len(text)})",
                "grammar",
            )
            return AsrRecoveryResult(
                language=language_code, status="parse_error", latency_ms=latency
            )

        result = parse_recovery_result(data, latency_ms=latency)
        result.language = result.language or language_code
        return result
