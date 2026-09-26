"""Interview / Meeting Coach — dual-lane live assistant.

Two audio lanes feed one coach:
  * "me"     -> the local microphone (cleaned ASR ring, 16 kHz);
  * "others" -> the Windows render loopback (the remote meeting: HR, the
                technical interviewer) at 48 kHz, resampled to 16 kHz.

Each utterance is transcribed by the shared Whisper model. When the "others"
lane produces a question, the coach answers it with full interview context
(earlier topics, your stated experience) so the hint lands on what you
actually said before — e.g. after a Python discussion, "difference between a
list and a tuple" gets "list is mutable, tuple is immutable, tuple is
hashable/usable as a dict key…" tuned to the interview, in English or Czech.

The coach NEVER auto-speaks; it only writes hint lines to the overlay. The
user reads and answers in their own words. At the end of a session the coach
summarizes the meeting into the diary/memory pipeline.

Language: the coach answers in the interview's language (en or cs), chosen by
the overlay's "answer language" dropdown; the transcript stays verbatim.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from ..debug import debug_log

# The interviewer-question heuristic: a sentence ending in "?" or matching an
# interrogative opener counts as a question worth a hint.
_QUESTION_OPENERS = (
    "what", "why", "how", "when", "where", "which", "who", "can you",
    "could you", "would you", "do you", "are you", "have you", "is it",
    "tell me", "explain", "describe", "difference", "co ", "jak", "proč",
    "kdy", "kde", "který", "která", "které", "můžeš", "můžete", "jste",
    "umíš", "umíte", "vysvětli", "popiš", "rozdíl",
)

# Speech energy VAD constants (render loopback is float32 [-1,1]).
_SILENCE_RMS = 0.012
_MIN_VOICED = 8
_END_SILENCE = 25
_MAX_UTTERANCE = 600

_COACH_SYSTEM_EN = """You are a live interview coach whispering answer hints
to a senior AI/ML developer mid-interview. The transcript so far is the
interview context. When the interviewer (the "others" line) asks a question,
produce a SHORT spoken-style answer the candidate can adapt — 1-3 sentences,
technically precise, first person ("In my experience…"). Match the interview
language. For Python/ML/HR questions, answer at senior depth: correct, with a
concrete angle, no fluff. Output ONLY the suggested answer."""

_COACH_SYSTEM_CS = """Jsi živý pohovorový kouč, který šeptá nápovědu seniornímu
AI/ML vývojáři uprostřed pohovoru. Dosavadní přepis je kontext pohovoru. Když
se tazatel (řádek "others") zeptá na otázku, vytvoř KRÁTKOU odpověď v první
osobě ("Podle mojí zkušenosti…"), technicky přesnou, 1-3 věty, v jazyce
pohovoru. U otázek na Python/ML/HR odpovídej na seniorské úrovni. Vypiš POUZE
navrženou odpověď."""


def _is_question(text: str) -> bool:
    t = text.strip().lower()
    if t.endswith("?"):
        return True
    return any(t.startswith(o) or f" {o}" in t for o in _QUESTION_OPENERS)


class InterviewCoach:
    """Owns the dual-lane capture -> STT -> hint loop for one session."""

    def __init__(self, cfg: Any, listener: Any,
                 llm_chat: Callable[[str, str], str],
                 on_event: Optional[Callable[[dict], None]] = None) -> None:
        self._cfg = cfg
        self._listener = listener
        self._llm_chat = llm_chat
        self._on_event = on_event
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        # Live settings from the overlay.
        self.answer_language = "en"       # "en" | "cs" (or others later)
        self.domain_hint = "AI/ML developer interview (Python, ML, HR)"
        # Rolling interview context: (speaker, text) pairs.
        self._context: list[tuple[str, str]] = []
        self._max_context = 24

    # ── lifecycle ─────────────────────────────────────────────────────
    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> bool:
        with self._lock:
            if self._running:
                return True
            from .. import native_audio
            if not native_audio.load():
                debug_log("coach: native audio engine not loaded", "everywhere")
                return False
            self._running = True
            self._context = []
            self._thread = threading.Thread(
                target=self._loop, name="coach-capture", daemon=True)
            self._thread.start()
            debug_log("coach: session started", "everywhere")
            return True

    def stop(self) -> None:
        with self._lock:
            self._running = False
        thread = self._thread
        if thread is not None:
            thread.join(timeout=3.0)
        self._thread = None
        debug_log("coach: session stopped", "everywhere")

    def update_settings(self, *, answer_language: Optional[str] = None,
                        domain_hint: Optional[str] = None) -> None:
        with self._lock:
            if answer_language is not None:
                self.answer_language = answer_language
            if domain_hint is not None:
                self.domain_hint = domain_hint
        debug_log(f"coach: settings lang={self.answer_language} "
                  f"domain={self.domain_hint}", "everywhere")

    # ── capture loop ──────────────────────────────────────────────────
    def _loop(self) -> None:
        """Drain both rings: mic (me) at 16 kHz and loopback (others) at 48 kHz."""
        from .. import native_audio
        import numpy as np

        mic_voiced: list = []
        mic_silence = 0
        lb_voiced: list = []
        lb_silence = 0

        while True:
            with self._lock:
                if not self._running:
                    break
            # Mic (me): 16 kHz frames of 160 samples.
            try:
                _r, mic = native_audio.pop_asr(48)
            except Exception:
                mic = None
            if mic is not None and len(mic) > 0:
                for off in range(0, len(mic) - 159, 160):
                    frame = mic[off:off + 160]
                    rms = float(np.sqrt(np.mean(frame * frame)) + 1e-9)
                    if rms >= _SILENCE_RMS:
                        mic_voiced.append(frame); mic_silence = 0
                    else:
                        mic_silence += 1
                        if mic_voiced: mic_voiced.append(frame)
                    if mic_voiced and (mic_silence >= _END_SILENCE
                                       or len(mic_voiced) >= _MAX_UTTERANCE):
                        self._on_utterance("me", np.concatenate(mic_voiced), 16000)
                        mic_voiced = []; mic_silence = 0
            # Loopback (others): 48 kHz frames of 480 samples.
            try:
                _r, lb = native_audio.pop_render_ref(96)
            except Exception:
                lb = None
            if lb is not None and len(lb) > 0:
                for off in range(0, len(lb) - 479, 480):
                    frame = lb[off:off + 480]
                    rms = float(np.sqrt(np.mean(frame * frame)) + 1e-9)
                    if rms >= _SILENCE_RMS:
                        lb_voiced.append(frame); lb_silence = 0
                    else:
                        lb_silence += 1
                        if lb_voiced: lb_voiced.append(frame)
                    if lb_voiced and (lb_silence >= _END_SILENCE
                                      or len(lb_voiced) >= _MAX_UTTERANCE):
                        self._on_utterance("others", np.concatenate(lb_voiced), 48000)
                        lb_voiced = []; lb_silence = 0
            if (mic is None or len(mic) == 0) and (lb is None or len(lb) == 0):
                time.sleep(0.01)

    # ── per-utterance pipeline ────────────────────────────────────────
    def _on_utterance(self, speaker: str, pcm, rate: int) -> None:
        import numpy as np
        pcm16 = pcm[::3].astype(np.float32) if rate == 48000 else pcm
        text = self._transcribe(pcm16)
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            self._context.append((speaker, text))
            if len(self._context) > self._max_context:
                self._context = self._context[-self._max_context:]
        debug_log(f"coach: [{speaker}] {text[:60]}", "everywhere")
        self._emit({"type": "transcript", "speaker": speaker, "text": text})
        # Only the "others" lane produces a hint. Interview mode: hints fire on
        # questions. Chit-chat mode: any substantive statement gets a
        # thinking-analysis hint.
        if speaker == "others":
            if self.domain_hint == "__chitchat__":
                if len(text.split()) >= 3:
                    hint = self._analyze_thinking(text)
                    if hint:
                        self._emit({"type": "hint", "question": text,
                                    "answer": hint})
            elif _is_question(text):
                hint = self._answer_hint(text)
                if hint:
                    self._emit({"type": "hint", "question": text,
                                "answer": hint})

    def _transcribe(self, pcm16) -> str:
        model = getattr(self._listener, "model", None) \
            if self._listener is not None else None
        if model is None:
            return ""
        try:
            segments, _info = model.transcribe(
                pcm16, beam_size=1, vad_filter=False)
            return "".join(getattr(s, "text", "") for s in segments)
        except Exception as exc:
            debug_log(f"coach: transcribe failed: {exc}", "everywhere")
            return ""

    def _analyze_thinking(self, statement: str) -> str:
        """Chit-chat mode: analyse the other person's reasoning/thinking and
        suggest a sharp conversational angle. Runs on statements, not just
        questions."""
        with self._lock:
            history = "\n".join(f"{who}: {txt}" for who, txt in self._context)
        lang = self.answer_language
        system = (
            "You are a sharp conversational thinking coach. Read what the other "
            "person said and the conversation so far, then give a SHORT insight "
            "that helps the user respond well: the assumption behind the "
            "statement, a clever angle, a good follow-up question, or a witty "
            "observation. 1-2 sentences. Be perceptive, a little playful, never "
            "mean. Output ONLY the insight.")
        if lang == "cs":
            system = (
                "Jsi bystrý konverzační kouč. Přečti, co druhá strana řekla, a "
                "celou dosavadní konverzaci, a navrhni KRÁTKÝ postřeh: předpoklad "
                "za výrokem, chytrý úhel, dobrou doplňující otázku nebo vtipnou "
                "poznámku. 1-2 věty. Vnímavě, lehce hravě, nikdy zlomyslně. "
                "Vypiš POUZE postřeh.")
        system += f"\nAnswer in {lang}."
        user = (f"Conversation so far:\n{history}\n\n"
                f"They just said: {statement}\n\nYour insight:")
        try:
            return (self._llm_chat(system, user) or "").strip()
        except Exception as exc:
            debug_log(f"coach: chit-chat insight failed: {exc}", "everywhere")
            return ""

    def _answer_hint(self, question: str) -> str:
        """Answer the interviewer's question with the full interview context."""
        with self._lock:
            history = "\n".join(f"{who}: {txt}" for who, txt in self._context)
        system = (_COACH_SYSTEM_CS if self.answer_language == "cs"
                  else _COACH_SYSTEM_EN)
        system += (f"\nInterview domain: {self.domain_hint}. "
                   f"Answer in {self.answer_language}.")
        user = (f"Interview so far:\n{history}\n\n"
                f"Interviewer just asked: {question}\n\nSuggested answer:")
        try:
            return (self._llm_chat(system, user) or "").strip()
        except Exception as exc:
            debug_log(f"coach: hint LLM failed: {exc}", "everywhere")
            return ""

    def _emit(self, event: dict) -> None:
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception as exc:
                debug_log(f"coach: on_event callback failed: {exc}",
                          "everywhere")

    # ── post-meeting summary ──────────────────────────────────────────
    def summarize(self) -> str:
        """Summarize the session, push it into the dialogue memory for the
        diary, and return the summary text."""
        with self._lock:
            history_lines = list(self._context)
            history = "\n".join(f"{who}: {txt}" for who, txt in history_lines)
        if not history.strip():
            return ""
        system = ("You summarize a technical interview/meeting for a personal "
                  "knowledge diary. List topics covered, Q&A highlights, and "
                  "follow-ups to review. Be concise.")
        try:
            summary = (self._llm_chat(system, history) or "").strip()
        except Exception as exc:
            debug_log(f"coach: summary LLM failed: {exc}", "everywhere")
            return ""
        # Persist into the shared dialogue memory so the diary update carries
        # the meeting (topics, Q&A, follow-ups) alongside normal conversation.
        try:
            from ..daemon import record_coach_session
            record_coach_session(history_lines, summary)
        except Exception as exc:
            debug_log(f"coach: diary push failed: {exc}", "everywhere")
        return summary


# ── module-level singleton ────────────────────────────────────────────

_coach: Optional[InterviewCoach] = None


def get_coach() -> Optional[InterviewCoach]:
    return _coach


def ensure_coach(cfg, listener=None, llm_chat=None, on_event=None) -> InterviewCoach:
    global _coach
    if _coach is None:
        _coach = InterviewCoach(cfg, listener=listener, llm_chat=llm_chat,
                                on_event=on_event)
    else:
        if listener is not None:
            _coach._listener = listener
        if llm_chat is not None:
            _coach._llm_chat = llm_chat
        if on_event is not None:
            _coach._on_event = on_event
    return _coach
