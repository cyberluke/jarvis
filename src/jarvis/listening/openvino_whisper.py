"""Typed adapter between the listener's Whisper contract and the worker.

The adapter speaks the same row shape the existing decision path reads
(``text`` / ``avg_logprob`` / ``no_speech_prob`` plus window timing), but it
validates the OpenVINO statistics strictly at this boundary: missing, NaN,
infinite, out-of-range, or unrecognised-semantics values raise a structured
backend error instead of entering the legacy permissive path.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .openvino_models import SCORE_SEMANTICS
from .openvino_runtime import (
    OV_DECODE_POLICY_UNSUPPORTED,
    OV_WHISPER_METRICS_UNAVAILABLE,
    OV_WHISPER_PAIR_UNSUPPORTED,
    OVWhisperError,
)


@dataclass(frozen=True)
class OVSegment:
    """One decoded window in the listener's row shape."""

    id: int
    text: str
    start_time: float
    end_time: float
    avg_logprob: float
    no_speech_prob: float
    sum_logprob: float
    token_count: int
    finish_reason: str
    kv_cache_exhausted: bool = False


@dataclass
class OVInfo:
    """The info object twin: the language the decode actually used plus the
    full per-window metadata vectors in native semantics."""

    language: Optional[str]
    sum_logprobs: Tuple[float, ...] = ()
    token_counts: Tuple[int, ...] = ()
    avg_logprobs: Tuple[float, ...] = ()
    no_speech_probs: Tuple[float, ...] = ()
    finish_reasons: Tuple[str, ...] = ()
    kv_cache_exhausted: Tuple[bool, ...] = ()
    score_semantics: str = ""
    device: str = ""
    pipeline_mode: str = ""
    num_beams: int = 1
    decode_wall_ms: float = 0.0
    audio_duration_ms: float = 0.0
    request_id: int = 0
    turn_id: Optional[int] = None
    incompatible_semantics: bool = False


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if math.isfinite(number):
            return number
    return None


def _validate_row(row: Dict[str, Any], semantics: str) -> OVSegment:
    """Strict per-row validation; never fills ``None`` with zero/one."""
    if semantics != SCORE_SEMANTICS:
        raise OVWhisperError(
            OV_WHISPER_METRICS_UNAVAILABLE,
            f"score semantics '{semantics or '-'}' is not '{SCORE_SEMANTICS}'",
        )
    avg_logprob = _finite(row.get("avg_logprob"))
    no_speech_prob = _finite(row.get("no_speech_prob"))
    sum_logprob = _finite(row.get("sum_logprob"))
    token_count = row.get("token_count")
    if avg_logprob is None or no_speech_prob is None or sum_logprob is None:
        raise OVWhisperError(
            OV_WHISPER_METRICS_UNAVAILABLE,
            "avg_logprob / no_speech_prob / sum_logprob must all be finite numbers",
        )
    if not (0.0 - 1e-6 <= no_speech_prob <= 1.0 + 1e-6):
        raise OVWhisperError(
            OV_WHISPER_METRICS_UNAVAILABLE,
            f"no_speech_prob {no_speech_prob} outside [0, 1]",
        )
    # A genuine log probability is non-positive (up to numerical tolerance)
    # and the average over a real hypothesis stays inside a sane band.
    if not (-20.0 <= avg_logprob <= 1e-6):
        raise OVWhisperError(
            OV_WHISPER_METRICS_UNAVAILABLE,
            f"avg_logprob {avg_logprob} outside the recognised log-probability band",
        )
    if not isinstance(token_count, int) or token_count < 1:
        raise OVWhisperError(
            OV_WHISPER_METRICS_UNAVAILABLE,
            "token_count must be a positive integer to audit avg_logprob",
        )
    return OVSegment(
        id=int(row.get("id", 0) or 0),
        text=str(row.get("text", "") or ""),
        start_time=float(row.get("start_time", 0.0) or 0.0),
        end_time=float(row.get("end_time", 0.0) or 0.0),
        avg_logprob=avg_logprob,
        no_speech_prob=min(1.0, max(0.0, no_speech_prob)),
        sum_logprob=sum_logprob,
        token_count=int(token_count),
        finish_reason=str(row.get("finish_reason", "") or ""),
        kv_cache_exhausted=bool(row.get("kv_cache_exhausted", False)),
    )


class _IncompatibleInfo(OVInfo):
    """Non-frozen info variant for the incompatible-semantics path."""


def _raw_info(response: Dict[str, Any]) -> "_IncompatibleInfo":
    """Preserve raw metadata when the semantics revision is unknown."""
    rows = response.get("rows") or []
    return _IncompatibleInfo(
        language=str(response.get("language") or "") or None,
        sum_logprobs=tuple(float(r.get("sum_logprob") or 0.0) for r in rows),
        token_counts=tuple(int(r.get("token_count") or 0) for r in rows),
        avg_logprobs=tuple(float(r.get("avg_logprob") or 0.0) for r in rows),
        no_speech_probs=tuple(float(r.get("no_speech_prob") or 0.0) for r in rows),
        finish_reasons=tuple(str(r.get("finish_reason") or "") for r in rows),
        kv_cache_exhausted=tuple(bool(r.get("kv_cache_exhausted") or False) for r in rows),
        score_semantics=str(response.get("score_semantics") or ""),
        device=str(response.get("device") or ""),
        pipeline_mode=str(response.get("pipeline") or ""),
        num_beams=int(response.get("num_beams") or 1),
        decode_wall_ms=float(response.get("latency_ms") or 0.0),
        audio_duration_ms=round(int(response.get("audio_length") or 0) / 16.0, 3),
        request_id=int(response.get("id") or 0),
        incompatible_semantics=True,
    )


def _json_dumps(event: Dict[str, Any]) -> str:
    return json.dumps(event, ensure_ascii=False, separators=(",", ":"))


class OpenVinoWhisperAdapter:
    """One persistent loaded pipeline per active worker, reused safely.

    ``transcribe`` matches the faster-whisper call shape the listener and the
    dictation engine already use: ``(rows, info)`` with attribute rows and an
    ``info.language``. The stateful pipeline is serialized under the shared
    ``transcribe_lock`` by the callers.
    """

    def __init__(self, worker: Any, score_semantics: str = "", npu: Optional[str] = None) -> None:
        self._worker = worker
        self.score_semantics = score_semantics or ""
        self.npu = npu
        self.last_latency_ms: float = 0.0

    def transcribe(
        self, audio: Any, language: Optional[str] = None, turn_id: Optional[int] = None, **kwargs: Any
    ) -> Tuple[List[OVSegment], OVInfo]:
        samples = _as_float_list(audio)
        num_beams = int(kwargs.get("num_beams", 1) or 1)
        # Beam guard before the native call: upstream 2026.4/2026.5 Whisper
        # beam search is broken ("vector too long" / AV); reject deterministically.
        if num_beams != 1:
            raise OVWhisperError(
                OV_DECODE_POLICY_UNSUPPORTED,
                f"num_beams={num_beams} rejected before native decode: greedy-only lane "
                "(upstream Whisper beam defect in the tested 2026.4/2026.5 family)",
            )
        response = self._worker.request(
            {
                "op": "transcribe",
                "audio": samples,
                "length": len(samples),
                "language": language,
                "task": "task" in kwargs and str(kwargs["task"]) or "transcribe",
                "num_beams": num_beams,
            },
            timeout=180.0,
        )
        self.last_latency_ms = float(response.get("latency_ms", 0.0) or 0.0)
        semantics = str(response.get("score_semantics", "") or "")
        raw_rows = list(response.get("rows") or [])
        if semantics != SCORE_SEMANTICS:
            # Unknown semantics revision: preserve raw metadata, mark the
            # interpretation incompatible, do not apply current formulas.
            info = _raw_info(response)
            info.turn_id = turn_id
            info.request_id = int(response.get("id") or 0)
            self._log_decode(info, raw_rows, accepted=None)
            rows = [
                OVSegment(
                    id=index + 1,
                    text=str(row.get("text", "") or ""),
                    start_time=float(row.get("start_time", 0.0) or 0.0),
                    end_time=float(row.get("end_time", 0.0) or 0.0),
                    avg_logprob=float(row.get("avg_logprob") or 0.0),
                    no_speech_prob=float(row.get("no_speech_prob") or 0.0),
                    sum_logprob=float(row.get("sum_logprob") or 0.0),
                    token_count=int(row.get("token_count") or 0),
                    finish_reason=str(row.get("finish_reason", "") or ""),
                    kv_cache_exhausted=bool(row.get("kv_cache_exhausted") or False),
                )
                for index, row in enumerate(raw_rows)
            ]
            return rows, info
        rows: List[OVSegment] = []
        for index, row in enumerate(raw_rows):
            payload = dict(row)
            payload.setdefault("id", index + 1)
            rows.append(_validate_row(payload, semantics))
        # Window cardinality must agree across the metadata vectors before
        # any per-window correlation is claimed.
        vector_lengths = {
            name: len(response.get(name) or [])
            for name in ("sum_logprobs", "token_counts", "avg_logprobs",
                         "no_speech_probs", "finish_reasons", "kv_cache_exhausted")
            if response.get(name) is not None
        }
        if vector_lengths and any(length != len(raw_rows) for length in vector_lengths.values()):
            raise OVWhisperError(
                OV_WHISPER_METRICS_UNAVAILABLE,
                f"window cardinality mismatch: rows={len(raw_rows)} vectors={vector_lengths}",
            )
        info = OVInfo(
            language=str(response.get("language") or language) if (response.get("language") or language) else None,
            sum_logprobs=tuple(float(row.get("sum_logprob") or 0.0) for row in raw_rows),
            token_counts=tuple(int(row.get("token_count") or 0) for row in raw_rows),
            avg_logprobs=tuple(float(row.get("avg_logprob") or 0.0) for row in raw_rows),
            no_speech_probs=tuple(float(row.get("no_speech_prob") or 0.0) for row in raw_rows),
            finish_reasons=tuple(str(row.get("finish_reason") or "") for row in raw_rows),
            kv_cache_exhausted=tuple(bool(row.get("kv_cache_exhausted") or False) for row in raw_rows),
            score_semantics=semantics,
            device=str(response.get("device") or ""),
            pipeline_mode=str(response.get("pipeline") or ""),
            num_beams=int(response.get("num_beams") or 1),
            decode_wall_ms=float(response.get("latency_ms") or 0.0),
            audio_duration_ms=round(int(response.get("audio_length") or 0) / 16.0, 3),
            request_id=int(response.get("id") or 0),
            turn_id=turn_id,
        )
        self._log_decode(info, raw_rows, accepted=None)
        return rows, info

    def _log_decode(self, info: OVInfo, raw_rows: list, accepted: Optional[Any]) -> None:
        """One structured whisper_decode event per decode on the existing
        debug channel; no raw audio, no second logging framework."""
        try:
            from ..debug import debug_log
        except Exception:
            return
        try:
            duration = info.audio_duration_ms or 0.0
            wall = info.decode_wall_ms or 0.0
            rtf = round(wall / duration, 4) if duration > 0 else None
            event = {
                "event": "whisper_decode",
                "request_id": info.request_id,
                "turn_id": info.turn_id,
                "device_requested": self.npu or info.device or "",
                "device_resolved": info.device,
                "pipeline_mode": info.pipeline_mode,
                "num_beams": info.num_beams,
                "audio_duration_ms": duration,
                "decode_wall_ms": wall,
                "real_time_factor": rtf,
                "text_length": sum(len(str(row.get("text", "") or "")) for row in raw_rows),
                "window_count": len(raw_rows),
                "sum_logprobs": list(info.sum_logprobs),
                "token_counts": list(info.token_counts),
                "avg_logprobs": list(info.avg_logprobs),
                "no_speech_probs": list(info.no_speech_probs),
                "finish_reasons": list(info.finish_reasons),
                "kv_cache_exhausted": list(info.kv_cache_exhausted),
                "score_semantics": info.score_semantics,
                "semantics_incompatible": bool(info.incompatible_semantics),
                "outcome": accepted,
            }
            debug_log("whisper_decode " + _json_dumps(event), "voice")
        except Exception:
            pass

    def pair_transcribe(
        self, audio: Any, codes: List[str]
    ) -> Tuple[str, Dict[str, Tuple[List[OVSegment], OVInfo]]]:
        """Two forced-language passes over the same preprocessed audio.

        Identical model, precision, decoding policy and score semantics for
        both; the decoder state is reset between the passes by the pipeline.
        Returns the winner code plus the per-code rows, ranked by the caller
        with the existing first-row policy.
        """
        if len(codes) < 2:
            raise OVWhisperError(OV_WHISPER_PAIR_UNSUPPORTED, "closed set needs two codes")
        per_code: Dict[str, Tuple[List[OVSegment], OVInfo]] = {}
        for code in codes:
            rows, info = self.transcribe(audio, language=code)
            if not rows:
                raise OVWhisperError(
                    OV_WHISPER_METRICS_UNAVAILABLE,
                    f"forced-language pass '{code}' produced no validated rows",
                )
            per_code[code] = (rows, info)
        winner = _rank_first_row(per_code)
        return winner, per_code


def _rank_first_row(per_code: Dict[str, Tuple[List[OVSegment], OVInfo]]) -> str:
    """Highest first-row ``avg_logprob``, then lower ``no_speech_prob``,
    then the code string for deterministic ties (the existing policy)."""
    def key(item: Tuple[str, Tuple[List[OVSegment], OVInfo]]):
        code, (rows, _info) = item
        return (-rows[0].avg_logprob, rows[0].no_speech_prob, code)

    return sorted(per_code.items(), key=key)[0][0]


def _as_float_list(audio: Any) -> List[float]:
    if hasattr(audio, "tolist"):
        return [float(value) for value in audio.tolist()]
    if isinstance(audio, (list, tuple)):
        return [float(value) for value in audio]
    raise OVWhisperError(OV_DECODE_POLICY_UNSUPPORTED, f"unsupported audio type: {type(audio)!r}")
