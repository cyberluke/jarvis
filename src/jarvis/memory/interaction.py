"""Short-lived social/interaction memory (doc §20–§22).

Separate from DialogueMemory (turns), Diary (episodic) and the knowledge
graph (durable facts): this layer keeps the *rhythm* of recent social
interaction — topic beats, humor fingerprints, asked questions, open
threads, mode history — each with a bounded TTL and hard cap so it stays
cheap and local. Failing here must never break the assistant: every read
is fail-soft and the engine simply omits the block when empty.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class TopicBeat:
    text: str
    at: float


@dataclass
class HumorBeat:
    category: str
    at: float


@dataclass
class ConversationThread:
    summary: str
    at: float


@dataclass
class ModeTransition:
    mode: str
    at: float


@dataclass
class InteractionMemory:
    """Bounded, in-process social memory with per-section TTLs (§20)."""

    topics: List[TopicBeat] = field(default_factory=list)
    humor: List[HumorBeat] = field(default_factory=list)
    questions: List[TopicBeat] = field(default_factory=list)
    threads: List[ConversationThread] = field(default_factory=list)
    mode_history: List[ModeTransition] = field(default_factory=list)

    topics_ttl: float = 24 * 3600.0
    humor_ttl: float = 72 * 3600.0
    questions_ttl: float = 24 * 3600.0
    threads_ttl: float = 3 * 24 * 3600.0
    modes_ttl: float = 24 * 3600.0
    max_items: int = 24

    # ── recording ──────────────────────────────────────────────────────
    def record_topic(self, text: str, *, now: Optional[float] = None) -> None:
        text = (text or "").strip()
        if not text:
            return
        self._push(self.topics, TopicBeat(text, now or time.time()),
                   self.topics_ttl)

    def record_humor(self, category: str, *, now: Optional[float] = None) -> None:
        category = (category or "").strip().lower()
        if not category:
            return
        # de-dupe near-identical categories within the window
        for beat in self.humor:
            if beat.category == category:
                beat.at = now or time.time()
                return
        self._push(self.humor, HumorBeat(category, now or time.time()),
                   self.humor_ttl)

    def record_question(self, text: str, *, now: Optional[float] = None) -> None:
        text = (text or "").strip()
        if not text:
            return
        self._push(self.questions, TopicBeat(text, now or time.time()),
                   self.questions_ttl)

    def record_thread(self, summary: str, *, now: Optional[float] = None) -> None:
        summary = (summary or "").strip()
        if not summary:
            return
        self._push(self.threads, ConversationThread(summary, now or time.time()),
                   self.threads_ttl)

    def close_thread(self, summary: str) -> None:
        key = (summary or "").strip().lower()
        self.threads = [t for t in self.threads if t.summary.lower() != key]

    def record_mode(self, mode: str, *, now: Optional[float] = None) -> None:
        mode = (mode or "").strip().lower()
        if not mode:
            return
        self._push(self.mode_history, ModeTransition(mode, now or time.time()),
                   self.modes_ttl)

    # ── reads (fail-soft) ──────────────────────────────────────────────
    def recent_humor_categories(self, *, now: Optional[float] = None,
                                limit: int = 6) -> List[str]:
        now = now or time.time()
        return [b.category for b in self._alive(self.humor, self.humor_ttl, now)][:limit]

    def recent_questions(self, *, now: Optional[float] = None,
                         limit: int = 5) -> List[str]:
        now = now or time.time()
        return [q.text for q in self._alive(self.questions, self.questions_ttl, now)][-limit:]

    def open_thread_summaries(self, *, now: Optional[float] = None,
                              limit: int = 3) -> List[str]:
        now = now or time.time()
        return [t.summary for t in self._alive(self.threads, self.threads_ttl, now)][:limit]

    def prompt_block(self, *, now: Optional[float] = None) -> str:
        """Compact injection block (§21, §22, §40). Empty string = no block."""
        now = now or time.time()
        lines: List[str] = []
        humor = self.recent_humor_categories(now=now)
        if humor:
            lines.append(
                "Recent comedy beats to avoid repeating: "
                + "; ".join(humor)
            )
        questions = self.recent_questions(now=now)
        if questions:
            lines.append(
                "Questions already asked this session: "
                + "; ".join(questions)
            )
        threads = self.open_thread_summaries(now=now)
        if threads:
            lines.append(
                "Open conversational threads: " + "; ".join(threads)
            )
        if not lines:
            return ""
        return "[Interaction]\n" + "\n".join(lines)

    # ── helpers ────────────────────────────────────────────────────────
    def _push(self, seq: list, item, ttl: float) -> None:
        seq[:] = [x for x in seq if item.at - x.at <= ttl]
        seq.append(item)
        if len(seq) > self.max_items:
            del seq[: len(seq) - self.max_items]

    @staticmethod
    def _alive(seq: list, ttl: float, now: float) -> list:
        return [x for x in reversed(seq) if now - x.at <= ttl]


def classify_humor_category(text: str) -> Optional[str]:
    """Tiny deterministic fingerprint for the anti-repetition ledger (§21).

    Keeps the doc's category vocabulary; first match wins.
    """
    t = (text or "").lower()
    if "toast" in t or "k\xef\xbb\xbf" in t:
        return "toast_offer"
    if "breakfast" in t or "sn\xeddan" in t:
        return "breakfast_statistics"
    if "two slice" in t or "dvou kraj\xed" in t or "capacity" in t:
        return "two_slice_capacity"
    if "cpu" in t or "heat" in t or "hork\xfd" in t:
        return "cpu_heat_toaster"
    if "bread" in t or "chl\xedb" in t:
        return "bread_philosophy"
    return None
