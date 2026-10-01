#!/usr/bin/env python3
"""Toastovač media-job wrapper around IntelVideoEngine.

Preserves source.mp4. Writes enhanced/<profile>.mp4 + sidecar JSON.
AUTO pass-through for already-4K. Enhancement failure → original, explicit.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from jarvis.everywhere import intel_video

ENGINE_VERSION = "0.3.0"

# Measured enhancement throughput (media-seconds per wall-second) from real
# runs — NOT an LLM, NOT a guess:
#   720p→1440p Anime4K: 105.0 s media processed in 317.3 s wall
#   1080p→2160p Anime4K: 3.93 s media processed in 33.7 s wall
MEASURED_MEDIA_PER_WALL = {
    "720p_to_1440p": 105.0 / 317.3,
    "1080p_to_2160p": 3.93 / 33.7,
}


def _interactive_budget_sec() -> float:
    """Configurable interactive enhancement budget (P5.3), default 10 s."""
    try:
        from jarvis.config import load_settings
        cfg = load_settings()
        return max(1.0, float(getattr(cfg, "interactive_enhancement_budget_sec", 10.0) or 10.0))
    except Exception:
        return 10.0


def _estimate_processing_sec(meta: Dict[str, Any], preset: str) -> float:
    """Predict enhancement wall time from source size + measured throughput.

    0.0 means no enhancement is predicted (pass-through or 4K source).
    """
    w = int(meta.get("width") or 0)
    h = int(meta.get("height") or 0)
    dur = float(meta.get("duration") or 0)
    if dur <= 0 or w <= 0 or h <= 0:
        return 0.0
    if w >= 3800 and h >= 2100:
        return 0.0
    if preset not in ("auto", "fast", "balanced"):
        return 0.0
    if w >= 1920 and h >= 1080:
        rate = MEASURED_MEDIA_PER_WALL["1080p_to_2160p"]
    else:
        rate = MEASURED_MEDIA_PER_WALL["720p_to_1440p"]
    return dur / max(rate, 0.001)


def _ffprobe(path: Path) -> Dict[str, Any]:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,codec_name,color_transfer,duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        data = json.loads(r.stdout or "{}")
        s0 = (data.get("streams") or [{}])[0]
        return {
            "width": int(s0.get("width") or 0),
            "height": int(s0.get("height") or 0),
            "codec": s0.get("codec_name") or "",
            "color_transfer": s0.get("color_transfer") or "",
            "duration": float(s0.get("duration") or 0),
        }
    except Exception as e:
        return {"width": 0, "height": 0, "error": str(e)}


def _profile_id(meta: Dict[str, Any], preset: str, feature: str,
                engine: Dict[str, Any]) -> str:
    blob = json.dumps({
        "engine": ENGINE_VERSION,
        "preset": preset,
        "feature": feature,
        "w": meta.get("width"),
        "h": meta.get("height"),
        "tool": engine.get("tool"),
        "device": engine.get("selectedDevice") or engine.get("device"),
        "decision": engine.get("decision"),
        # Schema v2: audio-preserving derivatives must never collide with the
        # earlier silent SR derivatives in the cache (P3.1).
        "audio": "PRESERVE",
    }, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _audio_summary(eng: Dict[str, Any]) -> Dict[str, Any]:
    """Extract audio-preservation facts from the engine result."""
    a = eng.get("audio") or {}
    return {
        "audioPresent": bool(a.get("present")),
        "audioMode": a.get("mode") or "NONE",
        "audioCodec": a.get("codec") or "",
        "audioVideoDeltaSec": a.get("audioVideoDeltaSec") or 0.0,
        "containerDurationSec": a.get("containerDurationSec") or 0.0,
        "sourceDurationSec": a.get("sourceDurationSec") or 0.0,
        "sourceDurationDeltaSec": a.get("sourceDurationDeltaSec") or 0.0,
    }


def _cache_lookup(out_dir: Path, meta: Dict[str, Any], preset: str,
                  feature: str = "auto") -> Optional[Dict[str, Any]]:
    """Return the cached ENHANCE record without invoking the engine.

    The profile hash is deterministic for a given source size + preset +
    engine version + the engine's selected tool/device/decision, so an
    existing sidecar can be verified by recomputing its hash from the
    values the engine would have chosen. This avoids re-running the
    expensive SR job on every cast of the same source (P1.1).
    """
    if not out_dir.is_dir():
        return None
    for side in out_dir.glob("*.json"):
        try:
            rec = json.loads(side.read_text(encoding="utf-8"))
        except Exception:
            continue
        if rec.get("decision") != "ENHANCE" or rec.get("playbackSource") != "ENHANCED":
            continue
        sm = rec.get("source") or {}
        if (int(sm.get("width") or 0) != int(meta.get("width") or 0)
                or int(sm.get("height") or 0) != int(meta.get("height") or 0)):
            continue
        if rec.get("preset") != preset:
            continue
        pid = _profile_id(meta, preset, feature, {
            "tool": rec.get("tool"),
            "selectedDevice": rec.get("device"),
            "decision": "ENHANCE",
        })
        mp4 = out_dir / f"{pid}.mp4"
        if pid != side.stem or not mp4.is_file() or mp4.stat().st_size < 1000:
            continue
        hit = dict(rec)
        hit.update({
            "cacheHit": True,
            "enhancementStatus": "CACHED",
            "playbackSource": "ENHANCED",
            "playbackPath": str(mp4),
            "originalPath": None,  # filled by caller
            "processingSec": 0.0,
            "nvidiaUsed": False,
        })
        return hit
    return None


def decide_and_maybe_enhance(source: Path, preset: str = "auto",
                             intent: str = "PLAY_NOW",
                             schedule_background: bool = False) -> Dict[str, Any]:
    """Return playback path + diagnostics. Never overwrites source.

    intent (P5.1):
      PLAY_NOW         - cached derivative if present; else play ORIGINAL
                         immediately when the predicted enhancement cost
                         exceeds the interactive budget (and optionally
                         schedule the derivative for later).
      PREPARE_CINEMATIC- wait for the enhancement (quality over latency).
      BACKGROUND_CACHE - build the derivative without playback intent.
    """
    source = Path(source)
    intent = str(intent or "PLAY_NOW").upper()
    if intent not in ("PLAY_NOW", "PREPARE_CINEMATIC", "BACKGROUND_CACHE"):
        intent = "PLAY_NOW"
    if not source.is_file():
        return {
            "ok": False,
            "playbackPath": None,
            "playbackSource": "MISSING",
            "enhancementStatus": "SKIPPED",
            "error": f"missing {source}",
            "intent": intent,
        }
    meta = _ffprobe(source)
    t0 = time.perf_counter()
    try:
        probe = intel_video.probe()
    except Exception as e:
        probe = {"status": "error", "error": str(e)}

    # Pre-decide 4K pass-through without invoking enhance (still record).
    already_4k = int(meta.get("width") or 0) >= 3800 and int(meta.get("height") or 0) >= 2100
    if already_4k and preset in ("auto", "fast", "balanced"):
        rec = {
            "ok": True,
            "decision": "PASS_THROUGH",
            "reason": f"source already {meta.get('width')}x{meta.get('height')}",
            "superResolution": False,
            "enhancementStatus": "SKIPPED",
            "playbackSource": "ORIGINAL",
            "playbackPath": str(source),
            "originalPath": str(source),
            "preset": preset,
            "intent": intent,
            "estimatedProcessingSec": 0.0,
            "actualProcessingSec": 0.0,
            "tool": None,
            "device": None,
            "cacheHit": False,
            "processingSec": 0.0,
            "source": meta,
            "nvidiaUsed": False,
            "engineProbe": {"npuVerdict": probe.get("npuVerdict"), "engine": probe.get("engine")},
        }
        _write_sidecar(source.parent / "enhanced" / "passthrough.json", rec)
        return rec

    out_dir = source.parent / "enhanced"
    out_dir.mkdir(parents=True, exist_ok=True)

    hit = _cache_lookup(out_dir, meta, preset)
    if hit:
        hit["originalPath"] = str(source)
        hit["intent"] = intent
        hit["estimatedProcessingSec"] = round(_estimate_processing_sec(meta, preset), 1)
        hit["actualProcessingSec"] = round(float(hit.get("processingSec") or 0.0), 1)
        _write_sidecar(out_dir / f"{Path(hit['playbackPath']).stem}.json", hit)
        return hit

    # P5.1/P5.3: PLAY_NOW must never wait minutes for an uncached enhancement.
    estimate = round(_estimate_processing_sec(meta, preset), 1)
    budget = _interactive_budget_sec()
    if intent == "PLAY_NOW" and estimate > budget and preset in ("auto", "fast", "balanced"):
        rec = {
            "ok": True,
            "decision": "PASS_THROUGH_FOR_LATENCY",
            "reason": (f"predicted processing cost {estimate}s exceeds "
                       f"interactive budget {budget}s"),
            "superResolution": False,
            "enhancementStatus": "SKIPPED_FOR_LATENCY",
            "playbackSource": "ORIGINAL",
            "playbackPath": str(source),
            "originalPath": str(source),
            "preset": preset,
            "intent": intent,
            "estimatedProcessingSec": estimate,
            "actualProcessingSec": 0.0,
            "interactiveEnhancementBudgetSec": budget,
            "cacheHit": False,
            "processingSec": time.perf_counter() - t0,
            "source": meta,
            "nvidiaUsed": False,
        }
        _write_sidecar(out_dir / "last_latency_fallback.json", rec)
        if schedule_background and preset in ("auto", "fast", "balanced"):
            import threading

            def _bg():
                try:
                    decide_and_maybe_enhance(source, preset=preset,
                                             intent="BACKGROUND_CACHE")
                except Exception as e:
                    print("BACKGROUND_ENHANCE_FAIL", e, flush=True)

            threading.Thread(target=_bg, daemon=True).start()
            rec["backgroundCacheScheduled"] = True
        return rec

    tmp = out_dir / f"_tmp_{preset}_{os.getpid()}_{uuid.uuid4().hex[:6]}.mp4"
    try:
        eng = intel_video.enhance(str(source), str(tmp), preset=preset, feature="auto")
    except Exception as e:
        rec = {
            "ok": True,
            "decision": "PASS_THROUGH",
            "reason": f"enhancement exception: {e}",
            "enhancementStatus": "FAILED",
            "playbackSource": "ORIGINAL",
            "playbackPath": str(source),
            "originalPath": str(source),
            "fallbackReason": str(e),
            "preset": preset,
            "intent": intent,
            "estimatedProcessingSec": estimate,
            "actualProcessingSec": round(time.perf_counter() - t0, 1),
            "processingSec": time.perf_counter() - t0,
            "source": meta,
            "nvidiaUsed": False,
        }
        _write_sidecar(out_dir / "last_failed.json", rec)
        return rec

    decision = str(eng.get("decision") or "")
    if decision == "PASS_THROUGH" or eng.get("superResolution") is False and not eng.get("features"):
        rec = {
            "ok": True,
            "decision": "PASS_THROUGH",
            "reason": eng.get("reason") or "engine pass-through",
            "superResolution": False,
            "enhancementStatus": "SKIPPED",
            "playbackSource": "ORIGINAL",
            "playbackPath": str(source),
            "originalPath": str(source),
            "preset": preset,
            "intent": intent,
            "estimatedProcessingSec": estimate,
            "actualProcessingSec": round(time.perf_counter() - t0, 1),
            "tool": eng.get("tool"),
            "device": eng.get("selectedDevice"),
            "cacheHit": False,
            "processingSec": time.perf_counter() - t0,
            "source": meta,
            "nvidiaUsed": bool(eng.get("nvidiaUsed")),
            "engine": {k: eng.get(k) for k in
                       ("decision", "reason", "status", "backend", "tool")},
        }
        if tmp.is_file():
            try:
                tmp.unlink()
            except Exception:
                pass
        _write_sidecar(out_dir / "passthrough.json", rec)
        return rec

    if eng.get("status") != "ok" or not tmp.is_file() or tmp.stat().st_size < 1000:
        rec = {
            "ok": True,
            "decision": "PASS_THROUGH",
            "reason": eng.get("reason") or eng.get("error") or "enhance failed",
            "enhancementStatus": "FAILED",
            "playbackSource": "ORIGINAL",
            "playbackPath": str(source),
            "originalPath": str(source),
            "fallbackReason": str(eng.get("run") or eng.get("error") or eng.get("status")),
            "preset": preset,
            "intent": intent,
            "estimatedProcessingSec": estimate,
            "actualProcessingSec": round(time.perf_counter() - t0, 1),
            "processingSec": time.perf_counter() - t0,
            "source": meta,
            "nvidiaUsed": bool(eng.get("nvidiaUsed")),
            "engine": eng,
        }
        if tmp.is_file():
            try:
                tmp.unlink()
            except Exception:
                pass
        _write_sidecar(out_dir / "last_failed.json", rec)
        return rec

    pid = _profile_id(meta, preset, "auto", eng)
    final = out_dir / f"{pid}.mp4"
    actual = round(time.perf_counter() - t0, 1)
    if final.is_file() and final.stat().st_size > 1000:
        try:
            tmp.unlink()
        except Exception:
            pass
        rec = {
            "ok": True,
            "decision": "ENHANCE",
            "reason": eng.get("reason"),
            "enhancementStatus": "CACHED",
            "playbackSource": "ENHANCED",
            "playbackPath": str(final),
            "originalPath": str(source),
            "cacheHit": True,
            "preset": preset,
            "intent": intent,
            "estimatedProcessingSec": estimate,
            "actualProcessingSec": actual,
            "tool": eng.get("tool"),
            "device": eng.get("selectedDevice"),
            "processingSec": 0.0,
            "source": meta,
            "nvidiaUsed": False,
            **_audio_summary(eng),
        }
        _write_sidecar(out_dir / f"{pid}.json", rec)
        return rec

    shutil.move(str(tmp), str(final))
    rec = {
        "ok": True,
        "decision": "ENHANCE",
        "reason": eng.get("reason"),
        "superResolution": bool(eng.get("superResolution")),
        "enhancementStatus": "OK",
        "playbackSource": "ENHANCED",
        "playbackPath": str(final),
        "originalPath": str(source),
        "cacheHit": False,
        "preset": preset,
        "intent": intent,
        "estimatedProcessingSec": estimate,
        "actualProcessingSec": actual,
        "tool": eng.get("tool"),
        "device": eng.get("selectedDevice"),
        "processingSec": time.perf_counter() - t0,
        "source": meta,
        "outputMeta": eng.get("outputMeta"),
        "nvidiaUsed": bool(eng.get("nvidiaUsed")),
        "engine": {k: eng.get(k) for k in
                   ("decision", "reason", "status", "backend", "tool", "durationSec")},
        **_audio_summary(eng),
    }
    _write_sidecar(out_dir / f"{pid}.json", rec)
    return rec


def _write_sidecar(path: Path, rec: Dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def playback_mp4_for_video(video_id: str, preset: str = "auto") -> Dict[str, Any]:
    root = Path(
        __import__("os").environ.get("LOCALAPPDATA") or ""
    ) / "VIVERRA" / "Toastovac" / "videos" / video_id
    src = root / "source.mp4"
    return decide_and_maybe_enhance(src, preset=preset)
