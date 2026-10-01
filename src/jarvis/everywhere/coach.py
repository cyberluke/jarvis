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

import platform
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

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
        # Audio routing chosen in the overlay: ``mic`` (default, the headset
        # mic lane), ``loopback`` (remote-party system audio) or ``both``.
        # Output is a PortAudio device index or None (system default).
        self.audio_input = "both"
        self.audio_output: Optional[int] = None
        # Rolling interview context: (speaker, text) pairs.
        self._context: list[tuple[str, str]] = []
        self._max_context = 24
        # Canonical meeting capture (v271_ingest.spec.md): one session =
        # one meeting record with lane-diarized transcript segments.
        self._meeting_id: Optional[str] = None
        self._title = ""
        self._participants: List[Dict[str, Any]] = []
        self._calendar_event_id: Optional[str] = None
        self._mail_thread_id: Optional[str] = None
        self._session_started_at = ""
        self._session_ended_at = ""
        self._t0 = 0.0  # monotonic clock at session start (segment ms base)
        self._segments: List[Dict[str, Any]] = []
        self._revision = 0  # bumped on every summarize()

    # ── lifecycle ─────────────────────────────────────────────────────
    @property
    def running(self) -> bool:
        return self._running

    def start(
        self,
        *,
        title: Optional[str] = None,
        participants: Optional[List[Dict[str, Any]]] = None,
        calendar_event_id: Optional[str] = None,
        mail_thread_id: Optional[str] = None,
    ) -> bool:
        with self._lock:
            if self._running:
                return True
            from .. import native_audio
            if not native_audio.load():
                debug_log("coach: native audio engine not loaded", "everywhere")
                return False
            # Meet/Teams do not use the Windows default speaker. Pin both
            # the candidate mic and the remote-party loopback to the
            # Plantronics USB headset (or interview_headset_match).
            try:
                from ..listening import audio_io as _aio
                pair = _aio.ensure_interview_headset(self._cfg)
                debug_log(
                    f"coach: headset mic='{pair.get('capture_name')}' "
                    f"ear='{pair.get('render_name')}'",
                    "everywhere",
                )
            except Exception as exc:
                debug_log(f"coach: interview headset required: {exc}", "everywhere")
                return False
            self._running = True
            self._context = []
            # New session identity: the meeting id is the root
            # idempotency key for V271 ingestion.
            self._meeting_id = f"mtg-{uuid.uuid4().hex[:16]}"
            self._title = str(title or "").strip() or (
                f"Meeting {time.strftime('%Y-%m-%d %H:%M')}"
            )
            self._participants = [
                p for p in (participants or []) if isinstance(p, dict)
            ]
            self._calendar_event_id = calendar_event_id or None
            self._mail_thread_id = mail_thread_id or None
            self._segments = []
            self._revision = 0
            self._t0 = time.monotonic()
            self._session_started_at = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime())
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
                        domain_hint: Optional[str] = None,
                        audio_input: Optional[str] = None,
                        audio_output: Optional[object] = None) -> None:
        with self._lock:
            if answer_language is not None:
                self.answer_language = answer_language
            if domain_hint is not None:
                self.domain_hint = domain_hint
            if audio_input is not None:
                lane = str(audio_input or "both").strip().lower()
                self.audio_input = lane if lane in ("mic", "loopback", "both") \
                    else "both"
            if audio_output is not None:
                try:
                    self.audio_output = int(audio_output) if audio_output else None
                except (TypeError, ValueError):
                    self.audio_output = None
        debug_log(f"coach: settings lang={self.answer_language} "
                  f"domain={self.domain_hint} input={self.audio_input} "
                  f"output={self.audio_output}", "everywhere")

    # ── capture loop ──────────────────────────────────────────────────
    def _loop(self) -> None:
        """Mic from the v2 local lane; others from WASAPI loopback of the headset ear."""
        from .. import native_audio
        from ..listening import audio_io as _aio
        import numpy as np

        mic_voiced: list = []
        mic_silence = 0
        mic_start = 0.0
        lb_voiced: list = []
        lb_silence = 0
        lb_start = 0.0
        loopback = _HeadsetLoopback(self._cfg)
        try:
            loopback.start()
        except Exception as exc:
            debug_log(f"coach: headset loopback required: {exc}", "everywhere")
            with self._lock:
                self._running = False
            self._emit({"type": "coach_error",
                        "error": f"interview headset loopback failed: {exc}"})
            return

        while True:
            with self._lock:
                if not self._running:
                    break
                input_lane = self.audio_input
            # Mic (me): cleaned 16 kHz from the Plantronics capture lane.
            if input_lane in ("mic", "both"):
                try:
                    _r, mic = _aio.pop_local_clean(48)
                    if mic is None:
                        _r, mic = native_audio.pop_asr(48)
                except Exception:
                    mic = None
                if mic is not None and len(mic) > 0:
                    for off in range(0, len(mic) - 159, 160):
                        frame = mic[off:off + 160]
                        rms = float(np.sqrt(np.mean(frame * frame)) + 1e-9)
                        if rms >= _SILENCE_RMS:
                            if not mic_voiced:
                                mic_start = time.monotonic()
                            mic_voiced.append(frame); mic_silence = 0
                        else:
                            mic_silence += 1
                            if mic_voiced: mic_voiced.append(frame)
                        if mic_voiced and (mic_silence >= _END_SILENCE
                                           or len(mic_voiced) >= _MAX_UTTERANCE):
                            self._on_utterance(
                                "me", np.concatenate(mic_voiced), 16000,
                                mic_start, time.monotonic())
                            mic_voiced = []; mic_silence = 0
            # Loopback (others): WASAPI loopback of the same headset earphone.
            if input_lane in ("loopback", "both"):
                try:
                    _r, lb = loopback.pop(96)
                except Exception:
                    lb = None
                if lb is not None and len(lb) > 0:
                    for off in range(0, len(lb) - 479, 480):
                        frame = lb[off:off + 480]
                        rms = float(np.sqrt(np.mean(frame * frame)) + 1e-9)
                        if rms >= _SILENCE_RMS:
                            if not lb_voiced:
                                lb_start = time.monotonic()
                            lb_voiced.append(frame); lb_silence = 0
                        else:
                            lb_silence += 1
                            if lb_voiced: lb_voiced.append(frame)
                        if lb_voiced and (lb_silence >= _END_SILENCE
                                          or len(lb_voiced) >= _MAX_UTTERANCE):
                            self._on_utterance(
                                "others", np.concatenate(lb_voiced), 48000,
                                lb_start, time.monotonic())
                            lb_voiced = []; lb_silence = 0
            if (mic is None or len(mic) == 0) and (lb is None or len(lb) == 0):
                time.sleep(0.01)
        if loopback is not None:
            try:
                loopback.stop()
            except Exception:
                pass

    # ── per-utterance pipeline ────────────────────────────────────────
    def _on_utterance(self, speaker: str, pcm, rate: int,
                      t_start: float, t_end: float) -> None:
        import numpy as np
        pcm16 = pcm[::3].astype(np.float32) if rate == 48000 else pcm
        text, confidence = self._transcribe(pcm16)
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            self._context.append((speaker, text))
            if len(self._context) > self._max_context:
                self._context = self._context[-self._max_context:]
            # Canonical transcript segment: lane-based speaker id,
            # wall-relative ms timestamps, Whisper avg_logprob confidence.
            if self._meeting_id is not None:
                self._segments.append({
                    "segmentId": f"seg-{len(self._segments):04d}",
                    "startMs": int(max(0.0, (t_start - self._t0) * 1000)),
                    "endMs": int(max(0.0, (t_end - self._t0) * 1000)),
                    "speakerId": speaker,
                    "text": text,
                    "confidence": confidence,
                })
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

    def _transcribe(self, pcm16) -> Tuple[str, Optional[float]]:
        model = getattr(self._listener, "model", None) \
            if self._listener is not None else None
        if model is None:
            return "", None
        try:
            segments, _info = model.transcribe(
                pcm16, beam_size=1, vad_filter=False)
            parts = list(segments)
            text = "".join(getattr(s, "text", "") for s in parts)
            logprobs = [getattr(s, "avg_logprob", None) for s in parts]
            logprobs = [v for v in logprobs if isinstance(v, (int, float))]
            confidence = (
                round(sum(logprobs) / len(logprobs), 4) if logprobs else None
            )
            return text, confidence
        except Exception as exc:
            debug_log(f"coach: transcribe failed: {exc}", "everywhere")
            return "", None

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
        """Finalize the session: extract structure with the local LLM,
        push the transcript into the dialogue memory (diary), and hand
        the canonical meeting record to the V271 graph ingestion
        coordinator. Returns the summary text ('' when empty)."""
        with self._lock:
            history_lines = list(self._context)
            segments = list(self._segments)
            meeting_id = self._meeting_id
            title = self._title
            started_at = self._session_started_at
            ended_at = self._session_ended_at or time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            participants = list(self._participants)
            calendar_event_id = self._calendar_event_id
            mail_thread_id = self._mail_thread_id
            revision = self._revision + 1
            self._revision = revision
        if not history_lines:
            return ""
        history = "\n".join(f"{who}: {txt}" for who, txt in history_lines)

        # Structured extraction: summary + decisions + action items +
        # topics in one LLM call (falls back to raw transcript).
        try:
            from ..v271_ingest.record import (
                build_meeting_record,
                default_quality,
                default_speakers,
                extract_meeting_structure,
                segments_to_transcript,
            )
        except Exception as exc:
            debug_log(f"coach: v271 record helpers unavailable: {exc}",
                      "v271_ingest")
            return ""

        try:
            structure = extract_meeting_structure(
                self._llm_chat,
                segments_to_transcript(segments),
                language=self.answer_language,
            )
        except Exception as exc:
            debug_log(f"coach: meeting structure extraction failed: {exc}",
                      "v271_ingest")
            return ""
        summary = structure["summary"]

        # Persist into the shared dialogue memory so the diary update carries
        # the meeting (topics, Q&A, follow-ups) alongside normal conversation.
        try:
            from ..daemon import record_coach_session
            record_coach_session(history_lines, summary)
        except Exception as exc:
            debug_log(f"coach: diary push failed: {exc}", "everywhere")

        # Durable handoff to V271 graph ingestion (v271_ingest.spec.md).
        try:
            from ..daemon import get_v271_ingest

            ingest = get_v271_ingest()
            if ingest is not None and meeting_id is not None:
                source_device = str(
                    getattr(self._cfg, "v271_source_device", "") or ""
                ).strip() or platform.node()
                record = build_meeting_record(
                    meeting_id=meeting_id,
                    title=title,
                    started_at=started_at,
                    ended_at=ended_at,
                    source_device=source_device,
                    segments=segments,
                    summary=summary,
                    decisions=structure["decisions"],
                    action_items=structure["actionItems"],
                    topics=structure["topics"],
                    participants=participants,
                    speakers=default_speakers(),
                    quality=default_quality(segments),
                    revision=revision,
                    source_calendar_event_id=calendar_event_id,
                    source_mail_thread_id=mail_thread_id,
                )
                ingest.enqueue(record)
                self._emit({
                    "type": "meeting_saved",
                    "meetingId": meeting_id,
                    "title": title,
                    "state": "QUEUED",
                    "label": "Saved locally",
                })
        except Exception as exc:
            debug_log(f"coach: v271 ingest enqueue failed: {exc}", "v271_ingest")
        return summary

    def retry_ingest(self, meeting_id: str) -> bool:
        """Re-queue a failed V271 ingestion for ``meeting_id``."""
        try:
            from ..daemon import get_v271_ingest

            ingest = get_v271_ingest()
            if ingest is None:
                return False
            return bool(ingest.retry(meeting_id))
        except Exception as exc:
            debug_log(f"coach: v271 ingest retry failed: {exc}", "v271_ingest")
            return False

    def ingest_status(self, meeting_id: Optional[str] = None) -> list:
        """Ingestion states for the meeting detail UX.

        ``meeting_id=None`` returns all tracked meetings.
        """
        try:
            from ..daemon import get_v271_ingest

            ingest = get_v271_ingest()
            if ingest is None:
                return []
            if meeting_id is not None:
                status = ingest.status(meeting_id)
                return [status] if status is not None else []
            return ingest.list_status()
        except Exception as exc:
            debug_log(f"coach: v271 ingest status failed: {exc}", "v271_ingest")
            return []


class _HeadsetLoopback:
    """WASAPI loopback of the interview headset earphone (Meet/Teams path).

    PortAudio WASAPI loopback captures whatever that endpoint is playing,
    which is the remote interviewer when Meet/Teams are pinned to the
    Plantronics headset — not the Windows default speaker.
    """

    def __init__(self, cfg: Any) -> None:
        self._cfg = cfg
        self._q: list = []
        self._lock = threading.Lock()
        self._stream = None
        self._device = None
        self._rate = 48000

    def start(self) -> None:
        import sounddevice as sd
        from ..listening.audio_io import resolve_interview_headset
        pair = resolve_interview_headset(self._cfg)
        want = str(pair["render_name"] or "").casefold()
        device = None
        for i, dev in enumerate(sd.query_devices()):
            name = str(dev.get("name") or "").casefold()
            # Loopback is opened against the WASAPI *output* endpoint.
            if want and want[:24] in name and int(dev.get("max_output_channels") or 0) > 0:
                host = ""
                try:
                    host = str(sd.query_hostapis()[int(dev.get("hostapi") or 0)].get("name") or "")
                except Exception:
                    pass
                if "wasapi" in host.casefold():
                    device = i
                    break
        extra = None
        try:
            extra = sd.WasapiSettings(loopback=True)
        except Exception as exc:
            raise RuntimeError(f"WASAPI loopback unavailable: {exc}") from exc
        if device is None:
            raise RuntimeError(
                f"no WASAPI output view of headset ear '{pair['render_name']}'"
            )
        self._device = device
        info = sd.query_devices(device)
        ch = min(2, max(1, int(info.get("max_output_channels") or 2)))
        rate = int(info.get("default_samplerate") or 48000)
        self._rate = rate

        def _cb(indata, frames, time_info, status):  # noqa: ARG001
            import numpy as np
            if indata is None or len(indata) == 0:
                return
            mono = np.mean(indata, axis=1).astype(np.float32) if indata.ndim > 1 else indata.reshape(-1)
            with self._lock:
                self._q.append(mono.copy())
                if len(self._q) > 200:
                    self._q = self._q[-80:]

        self._stream = sd.InputStream(
            device=device,
            channels=ch,
            samplerate=rate,
            dtype="float32",
            blocksize=0,
            extra_settings=extra,
            callback=_cb,
        )
        self._stream.start()
        debug_log(
            f"coach: WASAPI loopback device={device} '{pair['render_name']}'",
            "everywhere",
        )

    def pop(self, max_frames: int = 96):
        import numpy as np
        with self._lock:
            if not self._q:
                return 48000, None
            take = self._q[:max_frames]
            del self._q[:len(take)]
        pcm = np.concatenate(take)
        src = int(self._rate or 48000)
        if src != 48000 and pcm.size > 1:
            n_out = max(1, int(round(pcm.size * 48000 / src)))
            pcm = np.interp(
                np.linspace(0.0, 1.0, n_out),
                np.linspace(0.0, 1.0, pcm.size),
                pcm,
            ).astype(np.float32)
        return 48000, pcm

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None


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
