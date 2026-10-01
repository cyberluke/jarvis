#!/usr/bin/env python3
"""Interview Coach TV scene — SceneGraph state → 4K PQ raster → hevc_qsv.

UI updates ONLY from InterviewBroker events. Painter consumes state.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parents[1] / "src"))

from hdr_scene import LiveScene, W, H, blit_mask, draw_text_mask  # noqa: E402
from jarvis.everywhere.interview_coach import (  # noqa: E402
    ANSWERS, QUESTIONS, InterviewBroker, InterviewSession, evaluate_answer,
    COACH_HINT, CANDIDATE_TRANSCRIPT_FINAL, CANDIDATE_TRANSCRIPT_PARTIAL,
    INTERVIEW_ENDED, INTERVIEW_STARTED, QUESTION,
)

ART = ROOT.parents[1] / "android" / "toastovac-tv" / "docs" / "autonomous"
RESULTS = ART / "results"
LOGS = ART / "logs"
RESULTS.mkdir(parents=True, exist_ok=True)
LOGS.mkdir(parents=True, exist_ok=True)

# Shared scene state consumed by the rasterizer (event-driven).
TV = {
    "mode": "PYTHON INTERVIEW",
    "question": "",
    "candidate": "",
    "phase": "idle",
    "coach": "",
    "progress": "0:00",
    "turn": "0",
}


def interview_cmd() -> list:
    FPS = 15
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-f", "rawvideo", "-pix_fmt", "rgb48le", "-s", f"{W}x{H}", "-r", str(FPS),
        "-i", "pipe:0",
        "-vf", "format=p010le",
        "-c:v", "hevc_qsv", "-profile:v", "main10", "-preset", "veryfast",
        "-bf", "0", "-g", "15", "-forced_idr", "1", "-async_depth", "2",
        "-aud", "1", "-adaptive_i", "0", "-scenario", "livestreaming",
        "-b:v", "12M", "-maxrate", "18M", "-bufsize", "24M",
        "-color_primaries", "bt2020", "-color_trc", "smpte2084",
        "-colorspace", "bt2020nc", "-color_range", "tv",
        "-f", "hevc",
        "-bsf:v", "hevc_metadata=colour_primaries=9:transfer_characteristics=16:matrix_coefficients=9",
        "pipe:1",
    ]


class InterviewScene(LiveScene):
    """Near-black PQ canvas + restrained Spark + interview typography."""

    def render(self, t: float, hud=None):
        img = super().render(t, None)
        mode, rgb = draw_text_mask(TV["mode"], 36, (0, 140, 130))
        blit_mask(img, 80, 80, mode, rgb)
        q = TV["question"] or "…"
        # wrap roughly
        q_lines = _wrap(q, 42)
        y = 520
        for line in q_lines[:3]:
            m, rgb = draw_text_mask(line, 64, (180, 180, 200))
            blit_mask(img, 160, y, m, rgb)
            y += 90
        phase = TV["phase"]
        m, rgb = draw_text_mask(phase, 32, (130, 70, 200))
        blit_mask(img, 160, y + 20, m, rgb)
        coach = TV["coach"]
        if coach:
            m, rgb = draw_text_mask(coach[:90], 40, (0, 160, 140))
            blit_mask(img, 160, 1680, m, rgb)
        meta = f"turn {TV['turn']}   {TV['progress']}"
        m, rgb = draw_text_mask(meta, 28, (90, 90, 110))
        blit_mask(img, W - m.shape[1] - 80, 80, m, rgb)
        return img


def _wrap(text: str, n: int):
    words = (text or "").split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if len(trial) > n and cur:
            lines.append(cur)
            cur = w
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines or [""]


class InterviewTvSession(InterviewSession):
    """Same broker / Whisper-final boundary; TV state from events only."""

    def __init__(self, topic: str = "Python", duration_sec: float = 75.0) -> None:
        super().__init__(topic=topic, duration_sec=duration_sec)
        self.frames_painted = 0
        self.coach_shown_at = 0.0
        self._hint_hold = 4.0  # min coach display interval (P2.6: >= 2.5 s)
        self._question_shown_at = 0.0
        self._prev_question = ""
        self._question_visible: list = []   # (text, sec)
        self._coach_visible: list = []      # (note, sec)
        self._current_coach = ""
        self._transition_kinds: list = []   # every event kind in order
        self._rendered_on_box = False

    def _on_event(self, ev: dict) -> None:
        super()._on_event(ev)
        kind = ev.get("kind")
        text = ev.get("text") or ""
        now = time.monotonic()
        self._transition_kinds.append(kind)
        if kind == INTERVIEW_STARTED:
            TV["phase"] = "ready"
            TV["question"] = text
            TV["coach"] = ""
        elif kind == QUESTION:
            if self._question_shown_at and self._prev_question:
                self._question_visible.append(
                    (self._prev_question, now - self._question_shown_at))
            TV["question"] = text
            TV["phase"] = "question"
            TV["turn"] = str(len(self.broker.turns))
            self._prev_question = text
            self._question_shown_at = now
            # previous coach hint already held; clear only after hold and
            # record how long it was visible
            if now - self.coach_shown_at > self._hint_hold and TV["coach"]:
                if self._current_coach and self.coach_shown_at:
                    self._coach_visible.append(
                        (self._current_coach, now - self.coach_shown_at))
                TV["coach"] = ""
                self._current_coach = ""
        elif kind == CANDIDATE_TRANSCRIPT_PARTIAL:
            TV["phase"] = "candidate speaking"
            TV["candidate"] = text
        elif kind == CANDIDATE_TRANSCRIPT_FINAL:
            TV["phase"] = "answered"
            TV["candidate"] = text
        elif kind == COACH_HINT:
            TV["coach"] = text
            self._current_coach = text
            TV["phase"] = "coach"
            self.coach_shown_at = now
        elif kind == INTERVIEW_ENDED:
            TV["phase"] = "ended"
            if self._question_shown_at and self._prev_question:
                self._question_visible.append(
                    (self._prev_question, now - self._question_shown_at))
            if TV["coach"] and self.coach_shown_at and self._current_coach:
                self._coach_visible.append(
                    (self._current_coach, now - self.coach_shown_at))
        TV["progress"] = self.tv_state.get("progress", "0:00")

    def note_rendered_on_box(self) -> None:
        """Called by the host when decoder/HWC evidence confirms rendering."""
        self._rendered_on_box = True

    def start_on_host(self) -> bool:
        # slightly slower coach hold so the TV note is readable
        ok = super().start_on_host()
        self._write_tv_report()
        return ok

    def _write_tv_report(self) -> None:
        notes = [t.coach_note for t in self.broker.turns if t.coach_note]
        long_notes = [n for n in notes if len(n.split()) > 30]
        kinds = self._transition_kinds
        dup = 0
        for i in range(1, len(kinds)):
            if kinds[i] == kinds[i - 1]:
                dup += 1
        # stale question = two consecutive QUESTION events with identical text
        stale = 0
        qs = [e.get("text") for e in self.broker.events if e.get("kind") == QUESTION]
        for i in range(1, len(qs)):
            if qs[i] == qs[i - 1]:
                stale += 1
        q_vis = [round(d, 2) for _q, d in self._question_visible]
        c_vis = [round(d, 2) for _c, d in self._coach_visible]
        listening_events = sum(
            1 for k in kinds if k == CANDIDATE_TRANSCRIPT_PARTIAL)
        # the coach hint follows the candidate's final transcript (EVALUATION
        # sits between them by protocol)
        coach_after_answer = any(
            kinds[i] == COACH_HINT and CANDIDATE_TRANSCRIPT_FINAL in kinds[:i]
            for i in range(len(kinds)))
        elapsed = (self.broker.events[-1]["timestamp"] if self.broker.events else 0.0)
        report = {
            "scenario": "interview_coach_tv",
            "status": "pass" if (
                elapsed >= 75.0 and len(self.broker.turns) >= 4
                and len(long_notes) == 0 and dup == 0 and stale == 0
                and listening_events >= 1 and coach_after_answer
                and (not c_vis or min(c_vis) >= 2.5)
                and self._rendered_on_box
            ) else "partial",
            "duration_sec": round(elapsed, 2),
            "turns": len(self.broker.turns),
            "input_path": "InterviewBroker.inject_final",
            "rendered_on_box": self._rendered_on_box,
            "tv_state_last": dict(TV),
            "tv_notes": notes,
            "checks": {
                "wall_clock_75s": elapsed >= 75.0,
                "four_turns": len(self.broker.turns) >= 4,
                "notes_short": len(long_notes) == 0,
                "questions_changed": len(set(qs)) >= 2,
                "listening_state_changed": listening_events >= 1,
                "coach_after_answer": coach_after_answer,
                "coach_min_hold_2_5s": (not c_vis) or min(c_vis) >= 2.5,
                "no_duplicate_transitions": dup == 0,
                "no_stale_question": stale == 0,
                "rendered_through_toastovac": self._rendered_on_box,
            },
            "measure": {
                "question_visible_sec": q_vis,
                "coach_visible_sec": c_vis,
                "transition_count": len(kinds),
                "duplicate_transition_count": dup,
                "stale_question_count": stale,
            },
            "issues": ([] if len(long_notes) == 0 else ["TV note too long"])
                     + ([] if dup == 0 else ["duplicate transitions"])
                     + ([] if stale == 0 else ["stale question"])
                     + ([] if self._rendered_on_box else ["not rendered on box"]),
        }
        (RESULTS / "interview_coach_tv_result.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        (RESULTS / "interview_coach_tv.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def render_loop_into(proc, stop: threading.Event, fps: int = 15):
    scene = InterviewScene()
    t0 = time.perf_counter()
    n = 0
    while not stop.is_set():
        target = t0 + n / fps
        now = time.perf_counter()
        if now < target:
            time.sleep(target - now)
        try:
            blob = scene.render(n / fps).tobytes()
            proc.stdin.write(blob)
        except (BrokenPipeError, ValueError):
            break
        n += 1
    try:
        proc.stdin.close()
    except Exception:
        pass
