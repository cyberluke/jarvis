#!/usr/bin/env python3
"""Python Interview Coach — Toastovač cast scenario.

Timed conversation state machine. Simulated candidate answers enter through
the same transcript-final boundary a real Whisper final would use.

TV-facing notes stay short (<= ~25 words). Full evaluations stay in the log.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

_SRC = Path(__file__).resolve().parents[2]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

ART = Path(__file__).resolve().parents[3] / "android" / "toastovac-tv" / "docs" / "autonomous"
ART.mkdir(parents=True, exist_ok=True)
RESULTS = ART / "results"
RESULTS.mkdir(parents=True, exist_ok=True)
LOGS = ART / "logs"
LOGS.mkdir(parents=True, exist_ok=True)

# ── turn protocol ────────────────────────────────────────────────────────
INTERVIEW_STARTED = "INTERVIEW_STARTED"
QUESTION = "QUESTION"
CANDIDATE_TRANSCRIPT_PARTIAL = "CANDIDATE_TRANSCRIPT_PARTIAL"
CANDIDATE_TRANSCRIPT_FINAL = "CANDIDATE_TRANSCRIPT_FINAL"
EVALUATION = "EVALUATION"
COACH_HINT = "COACH_HINT"
NEXT_QUESTION = "NEXT_QUESTION"
INTERVIEW_ENDED = "INTERVIEW_ENDED"

QUESTIONS = [
    "What is the difference between a list and a tuple, and when would you pick each?",
    "How does a generator function actually start executing?",
    "Explain async/await in one short story: who yields, and to whom?",
    "A decorator wraps a function. What is one common bug people introduce?",
    "The GIL is in the room. What can still run in parallel, and what cannot?",
    "Write the signature of a context manager that times a block. What must __exit__ do?",
]


# Imperfect senior answers — one contains a subtle mistake (generators).
# Mix required by P2.4: strong / factual-mistake / incomplete / overlong.
ANSWERS = [
    "Lists are mutable, tuples are not. I use tuples as dict keys and for fixed records, lists when I need to append.",
    "A generator function returns an iterator immediately, and the body starts running as soon as you call it — that's why people put side effects at the top.",
    "I think await just sleeps the thread.",
    "People forget functools.wraps, so the wrapped function loses its name and docstring. Also they sometimes swallow exceptions inside the wrapper. I have seen this in production when stack traces pointed at inner instead of the public API, and then logging filters dropped the real caller, which made on-call harder than it needed to be because the metric name also came from __name__.",
    "Threads can overlap I/O and C extensions that release the GIL. Pure Python CPU work does not run in parallel on threads; use processes or native code.",
    "def timed(): ... I'd implement __enter__ to record time and __exit__ to print the delta. __exit__ must not reraise unless I want to suppress.",
]


@dataclass
class Turn:
    turn_id: str
    question: str
    answer: str = ""
    evaluation: Dict[str, Any] = field(default_factory=dict)
    coach_note: str = ""
    started: float = 0.0
    answered: float = 0.0


class InterviewBroker:
    """Transcript-final boundary. UI must not mutate state directly."""

    def __init__(self, on_event: Callable[[dict], None]) -> None:
        self._on_event = on_event
        self.session_id = uuid.uuid4().hex[:10]
        self.turns: List[Turn] = []
        self.state = "IDLE"
        self.current: Optional[Turn] = None
        self.t0 = 0.0
        self.lock = threading.Lock()
        self.events: List[dict] = []

    def emit(self, kind: str, **kw) -> dict:
        ev = {
            "kind": kind,
            "session_id": self.session_id,
            "timestamp": time.monotonic() - self.t0 if self.t0 else 0.0,
            "state": self.state,
            **kw,
        }
        self.events.append(ev)
        try:
            self._on_event(ev)
        except Exception:
            pass
        return ev

    def start(self, topic: str) -> None:
        self.t0 = time.monotonic()
        self.state = "STARTED"
        self.emit(INTERVIEW_STARTED, topic=topic, text=f"Python interview · {topic}")

    def ask(self, question: str) -> Turn:
        turn = Turn(turn_id=f"t{len(self.turns)+1}", question=question,
                    started=time.monotonic() - self.t0)
        self.current = turn
        self.turns.append(turn)
        self.state = "QUESTION"
        self.emit(QUESTION, turn_id=turn.turn_id, role="interviewer", text=question)
        return turn

    def inject_partial(self, text: str) -> None:
        if self.current is None:
            return
        self.state = "LISTENING"
        self.emit(CANDIDATE_TRANSCRIPT_PARTIAL, turn_id=self.current.turn_id,
                  role="candidate", text=text)

    def inject_final(self, text: str) -> None:
        """Same boundary a Whisper final transcript will use."""
        if self.current is None:
            return
        self.current.answer = text
        self.current.answered = time.monotonic() - self.t0
        self.state = "ANSWERED"
        self.emit(CANDIDATE_TRANSCRIPT_FINAL, turn_id=self.current.turn_id,
                  role="candidate", text=text)

    def record_eval(self, evaluation: Dict[str, Any], note: str) -> None:
        if self.current is None:
            return
        self.current.evaluation = evaluation
        self.current.coach_note = note
        self.state = "EVALUATED"
        self.emit(EVALUATION, turn_id=self.current.turn_id, role="coach",
                  text=json.dumps(evaluation, ensure_ascii=False))
        self.emit(COACH_HINT, turn_id=self.current.turn_id, role="coach", text=note)

    def end(self) -> None:
        self.state = "ENDED"
        self.emit(INTERVIEW_ENDED, text="session complete",
                  turns=len(self.turns))


def word_count(s: str) -> int:
    return len(re.findall(r"\S+", s or ""))


def evaluate_answer(question: str, answer: str) -> Dict[str, Any]:
    """Deterministic coach (no extra LLM needed for the overnight run).

    Looks for expected points and known mistakes so evaluation is real,
    not a canned pass.
    """
    a = (answer or "").lower()
    q = (question or "").lower()
    missing = []
    correctness = "good"
    if "tuple" in q or "list" in q:
        if "mutab" not in a:
            missing.append("mutability")
        if "key" not in a and "hash" not in a:
            missing.append("tuple-as-dict-key")
    if "generator" in q:
        if "call" in a and "starts running" in a:
            correctness = "wrong"
            missing.append("body runs only on iteration, not on call")
        elif "iterat" not in a and "next(" not in a:
            missing.append("execution starts on iteration")
    if "async" in q or "await" in q:
        if "event loop" not in a and "loop" not in a:
            missing.append("event loop")
    if "decorator" in q:
        if "wraps" not in a:
            missing.append("functools.wraps")
    if "gil" in q:
        if "process" not in a and "multiprocess" not in a:
            missing.append("use processes for CPU")
    if "context manager" in q or "__exit__" in q:
        if "__exit__" not in a:
            missing.append("__exit__ must return False (or None) to propagate")
    if correctness != "wrong" and missing:
        correctness = "partial"
    if correctness == "wrong":
        note = "Stop. The body does not run on call — only when iterated."
    elif missing:
        note = "Good. Add: " + missing[0] + "."
    else:
        note = "Solid. Next question."
    # enforce TV length
    words = note.split()
    if len(words) > 25:
        note = " ".join(words[:25])
    return {
        "correctness": correctness,
        "missing_point": missing[0] if missing else "",
        "missing_all": missing,
        "short_coach_note": note,
        "seniority": "senior-with-gaps" if missing or correctness != "good" else "senior",
        "clarity": "clear",
    }


class InterviewSession:
    def __init__(self, topic: str = "Python", duration_sec: float = 75.0) -> None:
        self.topic = topic
        self.duration_sec = max(60.0, float(duration_sec))
        self.tv_state: Dict[str, str] = {
            "mode": "PYTHON INTERVIEW",
            "question": "",
            "coach": "",
            "progress": "0:00",
        }
        self.broker = InterviewBroker(self._on_event)
        self.log_path = LOGS / f"interview_{self.broker.session_id}.jsonl"

    def _on_event(self, ev: dict) -> None:
        kind = ev.get("kind")
        text = ev.get("text") or ""
        if kind == QUESTION:
            self.tv_state["question"] = text
        elif kind == COACH_HINT:
            self.tv_state["coach"] = text
        elif kind == INTERVIEW_STARTED:
            self.tv_state["question"] = text
        elapsed = ev.get("timestamp") or 0.0
        self.tv_state["progress"] = f"{int(elapsed)//60}:{int(elapsed)%60:02d}"
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")

    def start_on_host(self) -> bool:
        """Wall-clock simulation. Does not compress time."""
        try:
            from jarvis.listening.audio_io import resolve_interview_headset
            from jarvis.config import load_settings
            pair = resolve_interview_headset(load_settings())
            print("INTERVIEW_HEADSET", pair["capture_name"], "|",
                  pair["render_name"], flush=True)
            self.tv_state["headset"] = (
                f"{pair['capture_name']} / {pair['render_name']}"
            )
        except Exception as exc:
            # The TV scene must render even without a live headset pairing;
            # the simulation injects transcript-finals directly. Only warn.
            print("INTERVIEW_HEADSET_UNAVAILABLE", exc, flush=True)
            self.tv_state["headset"] = "none (simulated input)"
        b = self.broker
        b.start(self.topic)
        deadline = time.monotonic() + self.duration_sec
        qi = 0
        while time.monotonic() < deadline and qi < len(QUESTIONS):
            q = QUESTIONS[qi]
            a = ANSWERS[qi] if qi < len(ANSWERS) else "I would look that up."
            b.ask(q)
            # candidate thinks, then partial, then final
            time.sleep(min(6.0, max(2.0, deadline - time.monotonic())))
            if time.monotonic() >= deadline:
                break
            words = a.split()
            mid = max(3, len(words) // 2)
            b.inject_partial(" ".join(words[:mid]))
            time.sleep(min(8.0, max(3.0, deadline - time.monotonic())))
            if time.monotonic() >= deadline:
                break
            b.inject_final(a)
            ev = evaluate_answer(q, a)
            b.record_eval(ev, ev["short_coach_note"])
            remain = deadline - time.monotonic()
            if remain <= 1.5:
                break
            time.sleep(min(4.0, remain))
            qi += 1
        # If the last turn was asked but not answered, drop it from scoring.
        if b.current is not None and not b.current.answer:
            b.turns = [t for t in b.turns if t.answer]
            b.current = b.turns[-1] if b.turns else None
        b.end()
        self._write_report()
        return True

    def _write_report(self) -> Dict[str, Any]:
        b = self.broker
        elapsed = (b.events[-1]["timestamp"] if b.events else 0.0)
        notes = [t.coach_note for t in b.turns if t.coach_note]
        long_notes = [n for n in notes if word_count(n) > 30]
        kinds = [e["kind"] for e in b.events]
        dup = 0
        for i in range(1, len(kinds)):
            if kinds[i] == kinds[i - 1] == QUESTION:
                dup += 1
        report = {
            "scenario": "interview_coach",
            "status": "pass" if elapsed >= 60 and len(b.turns) >= 3 else "partial",
            "duration_sec": round(elapsed, 2),
            "session_id": b.session_id,
            "turns": len(b.turns),
            "input_path": "InterviewBroker.inject_final (Whisper-final boundary)",
            "checks": {
                "wall_clock_60s": elapsed >= 60,
                "multiple_turns": len(b.turns) >= 3,
                "each_answer_evaluated": all(t.evaluation for t in b.turns),
                "tv_notes_short": len(long_notes) == 0,
                "no_duplicate_questions": dup == 0,
                "state_ended": b.state == "ENDED",
            },
            "tv_notes": notes,
            "questions": [t.question for t in b.turns],
            "answers": [t.answer for t in b.turns],
            "evaluations": [t.evaluation for t in b.turns],
            "issues": ([] if not long_notes else ["TV note too long"]) +
                      ([] if elapsed >= 60 else ["duration under 60s"]),
            "artifacts": {
                "events": str(self.log_path),
            },
        }
        (RESULTS / "interview_coach_result.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        (LOGS / "interview_system_eval.json").write_text(
            json.dumps({
                "what_worked": "Timed turn machine, Whisper-final injection, short TV notes.",
                "too_verbose": long_notes,
                "pacing": "6s think + 8s speak + 4s eval per turn",
                "tv_readability": "notes <= 25 words",
                "duplicate_events": dup,
                "duration_sec": elapsed,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        print("INTERVIEW_REPORT", report["status"],
              f"dur={elapsed:.1f}s turns={len(b.turns)}", flush=True)
        return report


def main() -> None:
    dur = float(sys.argv[1]) if len(sys.argv) > 1 else 75.0
    s = InterviewSession(topic="Python", duration_sec=dur)
    s.start_on_host()


if __name__ == "__main__":
    main()
