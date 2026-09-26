"""Pass 1: the language-aware Grammar Judge.

Validates a FINAL Whisper transcript against the grammar, morphology, syntax,
semantic plausibility and natural word order of the *Whisper-detected*
language. It is a validator, not a rewriter: it never translates, paraphrases,
improves tone, or invents missing intent. A meaning-changing correction is
flagged (``correctionType="semantic"`` / ``likelyAsrCorruption``) and routed
onward, never silently applied.

The model is the fast tier (``resolve_model(cfg, Tier.FAST)``), called through
``get_llm_backend(cfg).direct()`` at ``temperature=0`` for determinism. The
result is schema-constrained JSON parsed into :class:`GrammarJudgeResult`.
A timeout / unreachable backend / unparseable answer yields an explicit
``status`` — never a silent "valid".
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

from ...debug import debug_log
from ...llm import get_llm_backend, resolve_model, Tier
from ._json_extract import extract_strict_json, grammar_response_format
from .grammar_schema import (
    GRAMMAR_JUDGE_PROMPT_VERSION,
    GrammarJudgeInput,
    GrammarJudgeResult,
    parse_grammar_result,
)
from .language_map import language_name


@dataclass
class GrammarJudgeConfig:
    """Runtime knobs for the judge. ``cfg`` is the Settings duck-type."""

    cfg: Any = None
    timeout_sec: float = 6.0


_SYSTEM_PROMPT = """You are a deterministic grammar and ASR transcript validation engine.

The upstream Whisper speech recognizer detected the input language as:

LANGUAGE_CODE: {language_code}
LANGUAGE_NAME: {language_name}
LANGUAGE_PROBABILITY: {probability}
LANGUAGE_LOGPROB: {logprob}

Evaluate the transcript ONLY according to the grammar, morphology, syntax,
semantic plausibility, and natural word order of the detected language
({language_name}).

Rules:
- Do not translate the transcript.
- Do not rewrite it stylistically. Do not improve tone.
- Do not invent missing intent.
- Do not normalize content unless the change is a minimal, meaning-preserving
  correction.
- Preserve names, products, places, commands, code identifiers, model names and
  technical terms verbatim (e.g. SGLang, OpenVINO, Qwen, Gemma, Jarvis, Praha).
  You may flag them as unusual but must not replace them without strong
  acoustic evidence.
- If the transcript is grammatical but unusual, accept it.
- If the transcript is ungrammatical or strongly unnatural, flag the exact span.
- If fixing the sentence would require reconstructing missing meaning, set
  correctionType="semantic" and recommendation="run_asr_recovery".
- If the transcript appears to be an ASR corruption, set likelyAsrCorruption=true.
- If only a minimal surface fix is needed (punctuation, a small agreement typo,
  a duplicated token, a filler artifact) and meaning is unchanged, set
  correctionType="surface", provide correctedText, and set
  recommendation="accept_surface_correction".

Return JSON only, no Markdown:
{{"language": "{language_code}", "valid": true/false,
  "naturalnessScore": 0.0-1.0, "grammarConfidence": 0.0-1.0,
    "issues": [{{"span": "...", "type": "grammar|agreement|morphology|syntax|word_order|semantic_anomaly|invalid_construction|likely_asr_corruption|other", "severity": "low|medium|high", "explanation": "..."}}],
  "correctionType": "none|surface|semantic|uncertain",
  "correctedText": null,
  "meaningChanged": true/false,
  "likelyAsrCorruption": true/false,
  "linguisticValidity": "valid|valid_but_unusual|malformed|nonsense|likely_asr_noise",
  "recommendation": "accept_original|accept_surface_correction|run_asr_recovery|redecode|ask_user"}}"""


class GrammarJudge:
    """Single-call grammar validation against the detected language."""

    def __init__(self, config: Optional[GrammarJudgeConfig] = None):
        self.config = config or GrammarJudgeConfig()

    def _build_user_prompt(self, inp: GrammarJudgeInput) -> str:
        lang = inp.language
        lines = [
            f"Transcript: {inp.transcript!r}",
            "",
            "Language:",
            f"  code={lang.code}",
            f"  probability={lang.probability:.4f}",
            f"  logprob={lang.logprob:.4f}",
        ]
        if lang.alternatives:
            lines.append("  alternatives:")
            for alt in lang.alternatives:
                lines.append(
                    f"    - code={alt.language} probability={alt.probability:.4f} logprob={alt.logprob:.4f}"
                )
        asr = inp.asr
        lines.append("ASR:")
        if asr.average_logprob is not None:
            lines.append(f"  averageLogprob={asr.average_logprob:.4f}")
        if asr.no_speech_probability is not None:
            lines.append(f"  noSpeechProbability={asr.no_speech_probability:.4f}")
        if asr.tokens:
            lines.append("  tokens:")
            for tok in asr.tokens:
                lp = f"{tok.logprob:.4f}" if tok.logprob is not None else "-"
                lines.append(f"    - text={tok.text!r} logprob={lp}")
        lex = inp.lexical
        if lex.unknown_tokens or lex.misspellings:
            lines.append("Lexical:")
            lines.append(f"  unknownTokens={lex.unknown_tokens}")
            lines.append(f"  misspellings={lex.misspellings}")
        return "\n".join(lines)

    def judge(self, inp: GrammarJudgeInput) -> GrammarJudgeResult:
        """Validate ``inp.transcript`` for the detected language.

        Never raises for expected failure modes: timeout, unreachable backend,
        or an unparseable answer all return a result with an explicit
        ``status`` and ``recommendation="ask_user"`` so the decision engine
        applies the configured failure policy.
        """
        cfg = self.config.cfg
        language_code = inp.language.code
        language_disp = language_name(language_code) or language_code

        model = resolve_model(cfg, Tier.FAST) if cfg is not None else ""
        if not model:
            debug_log("grammar_judge: no fast model resolved", "grammar")
            return GrammarJudgeResult(
                language=language_code,
                status="unavailable",
                recommendation="ask_user",
            )

        # Log the concrete backend/model so the subsystem provably runs on the
        # configured Gemma — no silent fallback to another provider or model.
        provider = str(getattr(cfg, "llm_provider", "") or "") if cfg is not None else ""
        debug_log(
            '{"event":"grammar_judge_backend","provider":"%s","model":"%s","tier":"FAST"}'
            % (provider, model),
            "grammar",
        )

        system = _SYSTEM_PROMPT.format(
            language_code=language_code,
            language_name=language_disp,
            probability=f"{inp.language.probability:.4f}",
            logprob=f"{inp.language.logprob:.4f}",
        )
        user = self._build_user_prompt(inp)

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        # Native constrained output (OpenAI ``response_format`` JSON schema) is
        # sent first; servers without support fall back to the fail-closed
        # parser below. ``temperature=0`` + ``top_p=1`` keep it deterministic.
        extra_options = {
            "temperature": 0.0,
            "top_p": 1.0,
            "max_tokens": 700,
            "response_format": grammar_response_format(),
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
                f"grammar_judge: backend error ({type(exc).__name__}): {exc}",
                "grammar",
            )
            return GrammarJudgeResult(
                language=language_code,
                status="unavailable",
                recommendation="ask_user",
                latency_ms=latency,
            )
        latency = (time.perf_counter() - started) * 1000.0

        if text is None:
            debug_log("grammar_judge: timeout / empty response", "grammar")
            return GrammarJudgeResult(
                language=language_code,
                status="timeout",
                recommendation="ask_user",
                latency_ms=latency,
            )

        data = extract_strict_json(text)
        if data is None:
            debug_log(
                f"grammar_judge: fail-closed parse rejected response (len={len(text)})",
                "grammar",
            )
            return GrammarJudgeResult(
                language=language_code,
                status="parse_error",
                recommendation="ask_user",
                latency_ms=latency,
            )

        result = parse_grammar_result(data, latency_ms=latency)
        result.language = result.language or language_code
        return result
