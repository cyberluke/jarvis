#!/usr/bin/env python3
"""Translate a Korean Whisper transcript to Czech subtitles via Toastovač's LLM path.

P0 product policy (no-silent-fallback):
- The translation provider is ALWAYS the configured provider
  (``cfg.llm_provider`` / ``cfg.llm_base_url`` / ``cfg.llm_chat_model``).
- There is deliberately no second provider in this module. If the configured
  provider is unavailable the stage fails explicitly
  (``translationStatus = PROVIDER_UNAVAILABLE``) — no inferred fallback.
- Target-language validation is deterministic (no LLM validator).
- Repair re-uses the SAME configured provider/model, bounded attempts.
- Cues past media end are clamped/dropped once the media duration is known.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

VIDEO_ROOT = Path(r"C:\Users\lukes.COREI9\AppData\Local\VIVERRA\Toastovac\videos")

MAX_CUE_REPAIR_ATTEMPTS = 2
BATCH_SIZE = 10
MEDIA_END_TOLERANCE_SEC = 0.5

_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF\u0500-\u052F]")
_HANGUL_RE = re.compile(
    r"[\uAC00-\uD7AF\u1100-\u11FF\u3130-\u318F\uA960-\uA97F\uD7B0-\uD7FF]")
_CONTROL_RE = re.compile(r"[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]")
_NUMBERED_RE = re.compile(r"^\s*(\d+)\s*[.)]\s*(.*)$")
_LETTER_RE = re.compile(r"[A-Za-zÀ-ž]")


class TranslationProviderError(RuntimeError):
    """The configured translation provider could not complete the request."""


def load_cfg():
    from jarvis.config import load_settings
    return load_settings()


def provider_identity(cfg) -> dict:
    """Configured provider identity (no secrets)."""
    kind = str(getattr(cfg, "llm_provider", "") or "ollama").strip().lower()
    base = str(getattr(cfg, "llm_base_url", "") or "").strip().rstrip("/")
    model = str(getattr(cfg, "llm_chat_model", "") or "").strip()
    label = "UNKNOWN"
    try:
        host = base.split("//")[-1].split("/")[0] if base else ""
    except Exception:
        host = ""
    if kind == "openai_compatible":
        if host.startswith("192.168.1."):
            label = "LM_STUDIO"
        elif "11434" in host:
            label = "OLLAMA"
        else:
            label = f"OPENAI_COMPATIBLE:{host}"
    elif kind == "ollama":
        label = "OLLAMA"
    return {
        "providerKind": kind,
        "baseUrl": base,
        "model": model,
        "label": label,
    }


def _chat(cfg, messages, timeout_sec: float = 180.0) -> str:
    """Call the CONFIGURED provider only. Never switches providers.

    Bounded retries on the SAME provider absorb transient HTTP errors
    (model/slot reloads); a persistent failure raises
    TranslationProviderError so the stage fails explicitly.

    ``chat_template_kwargs.enable_thinking:false`` keeps the configured
    reasoning model (gemma4-26b) from spending the token budget on long
    chain-of-thought; the answer still comes back in ``content``.
    """
    from jarvis.llm.ollama import extract_text_from_response
    from jarvis.reply.engine import chat_with_messages
    extra = {
        "max_tokens": 4096,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    last = None
    for attempt in range(3):
        try:
            resp = chat_with_messages(cfg, messages, timeout_sec=timeout_sec,
                                      extra_options=extra)
            text = extract_text_from_response(resp) if isinstance(resp, dict) else None
            if text:
                return text
            last = TranslationProviderError("configured provider returned empty content")
        except Exception as e:
            last = TranslationProviderError(f"configured provider call failed: {e}")
        if attempt < 2:
            time.sleep(1.5 * (attempt + 1))
    raise last


def validate_czech_cue(cue: dict, idx: int, prev_text: str = "") -> tuple[list, bool]:
    """Deterministic Czech subtitle validator (no LLM). Returns (issues, ok)."""
    issues: list = []
    text = str(cue.get("text") or "").strip()
    try:
        start = float(cue.get("start") or 0.0)
        end = float(cue.get("end") or 0.0)
    except (TypeError, ValueError):
        start, end = -1.0, -1.0
    if not text:
        issues.append("empty_text")
    if _CONTROL_RE.search(text):
        issues.append("invalid_unicode")
    if _CYRILLIC_RE.search(text):
        issues.append("cyrillic")
    if _HANGUL_RE.search(text):
        issues.append("korean_residue")
    if prev_text and text == prev_text:
        issues.append("duplicate_consecutive")
    if text and not _LETTER_RE.search(text) and not re.search(r"[0-9]", text):
        issues.append("no_letters_or_digits")
    if start < 0:
        issues.append("start_negative")
    if end <= start:
        issues.append("end_not_after_start")
    return issues, len(issues) == 0


def _parse_numbered(text: str, n: int) -> list:
    """Parse '1. ...' lines into an n-length list; missing entries are ''."""
    out: list = [""] * n
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        m = _NUMBERED_RE.match(ln)
        if not m:
            continue
        k = int(m.group(1))
        if 1 <= k <= n:
            out[k - 1] = m.group(2).strip()
    return out


def _translate_batch(cfg, batch: list, target: str, request_index: int) -> list:
    system = (
        "You are a professional subtitle translator. Translate each numbered "
        f"line into {target}. Requirements: concise, natural Czech, preserve "
        "meaning, keep the same line count, no numbering inside the text, no "
        "explanations, no English, no Korean script. Output ONLY numbered "
        "lines matching the input, one per line, exactly like: '1. text'."
    )
    user = "\n".join(f"{k + 1}. {c['text']}" for k, c in enumerate(batch))
    text = _chat(cfg, [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ], timeout_sec=180.0)
    lines = _parse_numbered(text, len(batch))
    for k, cue in enumerate(batch):
        cue["text"] = lines[k] if lines[k] else str(cue.get("text") or "").strip()
    return batch


def _repair_cue(cfg, cue: dict, target: str, attempt: int) -> str:
    system = (
        "You are a Czech subtitle repairer. A subtitle line is invalid for "
        f"a {target}-only subtitle track. Fix ONLY the following line: remove "
        "any non-Czech script (e.g. Cyrillic, Korean), keep the meaning, keep "
        "the timestamp-independent text concise and natural. Output ONLY the "
        "corrected line text, with no numbering and no explanation."
    )
    user = f"Invalid Czech line: {cue['text']}"
    text = _chat(cfg, [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ], timeout_sec=120.0)
    cleaned = text.strip()
    # strip a leading quoted or numbered wrapper if the model added one
    m = _NUMBERED_RE.match(cleaned)
    if m:
        cleaned = m.group(2).strip()
    cleaned = cleaned.strip('"')
    return cleaned


def translate_pipeline(
    cfg,
    cues: list,
    target: str = "Czech",
    source_language: str = "ko",
    media_duration_sec: float | None = None,
) -> tuple[list, dict]:
    """Translate -> validate -> repair (same provider) -> media-end clamp/drop.

    Returns (segments, quality_report). Raises TranslationProviderError when
    the configured provider is unavailable.
    """
    ident = provider_identity(cfg)
    report = {
        "translationStatus": "OK",
        "configuredProvider": ident["label"],
        "actualProvider": ident["label"],
        "providerKind": ident["providerKind"],
        "model": ident["model"],
        "endpoint": ident["baseUrl"],
        "requestCount": 0,
        "sourceLanguage": source_language,
        "targetLanguage": "cs" if target.lower() == "czech" else target.lower(),
        "cueCount": len(cues),
        "repairCount": 0,
        "invalidCueCount": 0,
        "clampedCueCount": 0,
        "droppedCueCount": 0,
        "cuePastMediaEndCount": 0,
        "cyrillicCueCount": 0,
        "koreanResidueCount": 0,
        "mediaDurationSec": media_duration_sec,
    }

    # 1) translate in batches through the configured provider
    segments = [dict(c) for c in cues]
    for i in range(0, len(segments), BATCH_SIZE):
        batch = segments[i:i + BATCH_SIZE]
        t_b = time.perf_counter()
        _translate_batch(cfg, batch, target, report["requestCount"])
        report["requestCount"] += 1
        print(f"BATCH {i // BATCH_SIZE} done in {time.perf_counter() - t_b:.1f}s "
              f"({len(batch)} cues)", flush=True)

    # 2) deterministic validation
    invalid_idx: list[int] = []
    prev_text = ""
    for i, seg in enumerate(segments):
        issues, ok = validate_czech_cue(seg, i, prev_text)
        if issues:
            if "cyrillic" in issues:
                report["cyrillicCueCount"] += 1
            if "korean_residue" in issues:
                report["koreanResidueCount"] += 1
            invalid_idx.append(i)
        if ok:
            prev_text = str(seg.get("text") or "").strip()
        else:
            prev_text = ""

    # 3) repair with the SAME configured provider/model
    for i in invalid_idx:
        seg = segments[i]
        for attempt in range(1, MAX_CUE_REPAIR_ATTEMPTS + 1):
            try:
                fixed = _repair_cue(cfg, seg, target, attempt)
            except TranslationProviderError:
                raise  # provider went away mid-run: fail explicitly, no fallback
            seg["text"] = fixed
            report["requestCount"] += 1
            report["repairCount"] += 1
            issues, ok = validate_czech_cue(seg, i)
            if ok:
                break
        else:
            report["invalidCueCount"] += 1

    # 4) media-end handling once the media duration is known
    if media_duration_sec is not None and media_duration_sec > 0:
        segments = sanitize_media_end(segments, media_duration_sec, report)
        report["cueCount"] = len(segments)

    if report["invalidCueCount"]:
        report["translationStatus"] = "PARTIAL_INVALID"
    return segments, report


def translate_cues(cfg, cues, target: str = "Czech", media_duration_sec: float | None = None) -> list:
    """Back-compat wrapper: returns only the segments."""
    segments, _report = translate_pipeline(cfg, cues, target=target,
                                           media_duration_sec=media_duration_sec)
    return segments


def sanitize_media_end(segments: list, media_duration_sec: float, report: dict | None = None) -> list:
    """Deterministic media-end hygiene: drop cues starting past the end,
    clamp ends to the media duration, drop cues that collapse."""
    if report is None:
        report = {}
    kept: list = []
    for seg in segments:
        start = float(seg.get("start") or 0.0)
        end = float(seg.get("end") or 0.0)
        if start >= media_duration_sec:
            report["cuePastMediaEndCount"] = report.get("cuePastMediaEndCount", 0) + 1
            report["droppedCueCount"] = report.get("droppedCueCount", 0) + 1
            continue
        if end > media_duration_sec + MEDIA_END_TOLERANCE_SEC:
            end = media_duration_sec
            report["clampedCueCount"] = report.get("clampedCueCount", 0) + 1
        elif end > media_duration_sec:
            end = media_duration_sec
        if end <= start:
            report["droppedCueCount"] = report.get("droppedCueCount", 0) + 1
            continue
        seg["end"] = end
        kept.append(seg)
    return kept


def parse_srt(path: Path) -> list:
    """Parse a plain SRT into [{start, end, text}]; malformed blocks skipped."""
    try:
        raw = path.read_text(encoding="utf-8-sig", errors="replace")
    except Exception:
        return []
    out: list = []
    blocks = re.split(r"\n\s*\n", raw.strip())
    for block in blocks:
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        m = re.match(r"(\d{1,2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2}),(\d{3})", lines[1])
        if not m:
            continue

        def _ts(g):
            return (int(g[0]) * 3600 + int(g[1]) * 60 + int(g[2])) + int(g[3]) / 1000.0

        out.append({"start": _ts(m.groups()[:4]), "end": _ts(m.groups()[4:]),
                    "text": " ".join(lines[2:])})
    return out


def apply_media_end_to_srt(srt_path: Path, media_duration_sec: float) -> dict:
    """Hygiene pass for a cached SRT: drop/clamp cues past media end, rewrite."""
    stats = {"cueCount": 0, "clampedCueCount": 0, "droppedCueCount": 0,
             "cuePastMediaEndCount": 0}
    segs = parse_srt(srt_path)
    if not segs:
        return stats
    kept = sanitize_media_end(segs, media_duration_sec, stats)
    stats["cueCount"] = len(kept)
    write_srt(kept, srt_path)
    return stats


def write_srt(segments, path: Path):
    def ts(sec):
        ms = int(round((sec - int(sec)) * 1000))
        s = int(sec) % 60
        m = (int(sec) // 60) % 60
        h = int(sec) // 3600
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    lines = []
    for i, seg in enumerate(segments, 1):
        lines.append(f"{i}")
        lines.append(f"{ts(seg['start'])} --> {ts(seg['end'])}")
        lines.append(seg["text"])
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_quality_report(report: dict, path: Path) -> Path:
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def media_duration_of(path: Path) -> float:
    """Deterministic ffprobe duration of the media that will actually play."""
    import subprocess
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30)
        data = json.loads(out.stdout or "{}")
        return float(data.get("format", {}).get("duration") or 0.0)
    except Exception:
        return 0.0


def main(video_id: str):
    vd = VIDEO_ROOT / video_id
    tr = json.loads((vd / "ko_transcript.json").read_text(encoding="utf-8"))
    cues = tr["segments"]
    cfg = load_cfg()
    # effective playback media: the enhanced derivative when present
    eff = vd / "enhanced"
    media = None
    if eff.is_dir():
        cands = sorted(eff.glob("*.mp4"))
        if cands:
            media = cands[0]
    if media is None:
        media = vd / "source.mp4"
    media_dur = media_duration_of(media)
    t0 = time.perf_counter()
    cz, report = translate_pipeline(cfg, cues, "Czech", media_duration_sec=media_dur)
    print("TRANSLATE_DONE", f"{time.perf_counter() - t0:.1f}s", len(cz), "cues",
          "status", report["translationStatus"], flush=True)

    write_srt(cz, vd / "cs_subtitles.srt")
    (vd / "cs_transcript.json").write_text(
        json.dumps({"language": "cs", "segments": cz,
                    "quality": report}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    write_quality_report(report, vd / "subtitle_quality.json")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    for seg in cz:
        print(f"  {seg['start']:6.2f}-{seg['end']:6.2f}  {seg['text']}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1])