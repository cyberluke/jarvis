"""Everywhere AI Subtitles — live translated subtitles over system audio.

Captures the Windows render loopback (whatever is playing: Chrome, YouTube,
a video call) from the native audio engine's render-reference ring, runs it
through the existing Whisper STT, translates each utterance through the
existing LLM router, and streams the result to the Everywhere host's
subtitles overlay. Optionally speaks the translation through Piper TTS.

Design invariants:
  * local-only: no network beyond the configured local LLM endpoint;
  * reuses the existing Whisper + LLM + Piper infrastructure;
  * the audio buffer is consumed and discarded (never persisted);
  * the LLM gate is respected (subtitles never barge into a voice reply).

This module is engine-agnostic about display: it emits structured subtitle
events; the Everywhere broker forwards them to the native host.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

from ..debug import debug_log
from .asian_context import (
    POLITENESS_LANGUAGES,
    politeness_instruction,
)

# Languages the live subtitles feature advertises in the overlay dropdowns.
# ``auto`` = Whisper language detection. Each maps to a Whisper language code.
SUBTITLE_SOURCE_LANGUAGES: dict[str, str] = {
    "auto": "auto",
    "en": "en", "cs": "cs", "sk": "sk", "de": "de", "fr": "fr", "es": "es",
    "vi": "vi", "ko": "ko", "ja": "ja", "zh": "zh", "it": "it", "pt": "pt",
    "ru": "ru", "uk": "uk", "pl": "pl", "nl": "nl", "sv": "sv", "th": "th",
    "ar": "ar", "he": "he", "el": "el", "tr": "tr",
}

#: Energy threshold (RMS) under which a frame counts as silence for the
#: utterance boundary. Rendered audio is float32 [-1, 1].
_SILENCE_RMS = 0.012
#: Min voiced frames before an utterance is worth transcribing.
_MIN_VOICED = 8
#: Silence frames that close an utterance (at 10 ms/frame -> 250 ms gap).
_END_SILENCE = 25
#: Max utterance length in frames (~6 s) before a forced flush.
_MAX_UTTERANCE = 600


class SubtitlesService:
    """Owns the capture->STT->translate->(TTS) subtitles loop.

    One long-lived instance per Everywhere broker. ``start``/``stop`` are
    thread-safe and idempotent. Transcript lines are pushed to ``on_line``.
    """

    def __init__(self, cfg: Any,
                 listener: Any = None,
                 llm_chat: Optional[Callable] = None,
                 tts: Any = None,
                 on_line: Optional[Callable[[dict], None]] = None) -> None:
        self._cfg = cfg
        self._listener = listener
        self._llm_chat = llm_chat
        self._tts = tts
        self._on_line = on_line
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        # Live settings (changed by the overlay dropdowns/checkbox).
        self.source_language = "auto"      # Whisper source ("auto" = detect)
        self.target_language = "cs"        # translation target
        self.live_audio = False            # speak the translation via Piper
        self.politeness = ""               # Asian politeness/context key

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
                debug_log("subtitles: native audio engine not loaded",
                          "everywhere")
                return False
            if not (native_audio.capabilities() & 0x0002):  # CAP_LOOPBACK
                debug_log("subtitles: render loopback unavailable",
                          "everywhere")
                return False
            self._running = True
            self._thread = threading.Thread(
                target=self._loop, name="subtitles-capture", daemon=True)
            self._thread.start()
            debug_log("subtitles: capture started", "everywhere")
            return True

    def stop(self) -> None:
        with self._lock:
            self._running = False
        thread = self._thread
        if thread is not None:
            thread.join(timeout=3.0)
        self._thread = None
        debug_log("subtitles: capture stopped", "everywhere")

    def update_settings(self, *, source: Optional[str] = None,
                        target: Optional[str] = None,
                        live_audio: Optional[bool] = None,
                        politeness: Optional[str] = None) -> None:
        with self._lock:
            if source is not None:
                self.source_language = source
            if target is not None:
                self.target_language = target
            if live_audio is not None:
                self.live_audio = live_audio
            if politeness is not None:
                self.politeness = politeness
        debug_log(
            f"subtitles: settings src={self.source_language} "
            f"tgt={self.target_language} tts={int(self.live_audio)} "
            f"politeness={self.politeness or '-'}", "everywhere")

    # ── capture + VAD loop ────────────────────────────────────────────
    def _loop(self) -> None:
        from .. import native_audio
        import numpy as np

        voiced: list = []
        silence = 0
        while True:
            with self._lock:
                if not self._running:
                    break
            try:
                _rate, frames = native_audio.pop_render_ref(96)
            except Exception as exc:
                debug_log(f"subtitles: capture read error: {exc}",
                          "everywhere")
                frames = None
            if frames is None or len(frames) == 0:
                time.sleep(0.01)
                continue
            # frames are 48 kHz float32; chunk into 10 ms (480-sample) frames
            for off in range(0, len(frames) - 479, 480):
                frame = frames[off:off + 480]
                rms = float(np.sqrt(np.mean(frame * frame)) + 1e-9)
                if rms >= _SILENCE_RMS:
                    voiced.append(frame)
                    silence = 0
                else:
                    silence += 1
                    if voiced:
                        voiced.append(frame)  # keep a little tail
                if voiced and (silence >= _END_SILENCE
                               or len(voiced) >= _MAX_UTTERANCE):
                    utter = np.concatenate(voiced)
                    voiced = []
                    silence = 0
                    if len(utter) // 480 >= _MIN_VOICED:
                        self._handle_utterance(utter)
        # flush any trailing speech on stop
        if voiced:
            try:
                utter = np.concatenate(voiced)
                if len(utter) // 480 >= _MIN_VOICED:
                    self._handle_utterance(utter)
            except Exception:
                pass

    # ── per-utterance pipeline ────────────────────────────────────────
    def _handle_utterance(self, pcm_48k) -> None:
        """Transcribe + translate + (optional) TTS one utterance."""
        import numpy as np
        started = time.monotonic()
        # Resample 48k -> 16k for Whisper (every 3rd sample).
        pcm_16k = pcm_48k[::3].astype(np.float32)
        text, detected_lang = self._transcribe(pcm_16k)
        text = (text or "").strip()
        if not text:
            return
        src = detected_lang or (
            None if self.source_language == "auto" else self.source_language)
        translated = self._translate(text, src)
        line = {
            "source_text": text,
            "translated_text": translated,
            "source_language": src or "auto",
            "target_language": self.target_language,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        debug_log(
            f"subtitles: [{line['source_language']}] -> "
            f"[{line['target_language']}] {len(text)} chars, "
            f"{line['duration_ms']} ms", "everywhere")
        if self._on_line is not None:
            try:
                self._on_line(line)
            except Exception as exc:
                debug_log(f"subtitles: on_line callback failed: {exc}",
                          "everywhere")
        if self.live_audio and translated:
            self._speak(translated)

    def _transcribe(self, pcm_16k) -> tuple[str, Optional[str]]:
        """Run the shared Whisper model. Returns (text, detected_language)."""
        listener = self._listener
        model = getattr(listener, "model", None) if listener is not None else None
        if model is None:
            return "", None
        language = None if self.source_language == "auto" \
            else self.source_language
        try:
            segments, info = model.transcribe(
                pcm_16k, language=language, beam_size=1,
                vad_filter=False)
            text = "".join(getattr(s, "text", "") for s in segments)
            detected = getattr(info, "language", None)
            return text, detected
        except Exception as exc:
            debug_log(f"subtitles: transcribe failed: {exc}", "everywhere")
            return "", None

    def _translate(self, text: str, source_lang: Optional[str]) -> str:
        """Translate via the existing LLM router (or pass through when the
        source already matches the target)."""
        if source_lang and source_lang == self.target_language:
            return text  # already in the target language
        chat = self._llm_chat
        if chat is None:
            return text
        instruction = politeness_instruction(
            self.target_language, self.politeness)
        system = (
            "You are a live subtitle translator. Translate the spoken line "
            f"into {self.target_language}. Output ONLY the translation, no "
            "commentary, no quotes. Preserve tone and meaning."
            + ("\n" + instruction if instruction else "")
        )
        try:
            resp = chat(system, text)
            out = (resp or "").strip()
            return out or text
        except Exception as exc:
            debug_log(f"subtitles: translate failed: {exc}", "everywhere")
            return text

    def _speak(self, text: str) -> None:
        tts = self._tts
        if tts is None:
            return
        try:
            # Piper picks the voice from the target language via its language
            # map; the TTS engine resolves the per-language model.
            tts.speak(text)
        except Exception as exc:
            debug_log(f"subtitles: TTS failed: {exc}", "everywhere")


# ── module-level singleton + capability helpers ──────────────────────

_service: Optional[SubtitlesService] = None


def get_service() -> Optional[SubtitlesService]:
    return _service


def piper_supports(language: str) -> bool:
    """True when Piper has a voice for the language (drives the TTS badge)."""
    try:
        from ..output.tts import PIPER_VOICE_BY_LANGUAGE
        return language in PIPER_VOICE_BY_LANGUAGE
    except Exception:
        return False


def target_language_options() -> list[dict]:
    """Dropdown model for the overlay: code, English name, native name, and
    whether Piper can speak it (the live-audio badge)."""
    from ..output.tts import PIPER_VOICE_BY_LANGUAGE
    names = {
        "cs": ("Czech", "Čeština"), "en": ("English", "English"),
        "sk": ("Slovak", "Slovenčina"), "de": ("German", "Deutsch"),
        "fr": ("French", "Français"), "es": ("Spanish", "Español"),
        "vi": ("Vietnamese", "Tiếng Việt"), "ko": ("Korean", "한국어"),
        "ja": ("Japanese", "日本語"), "zh": ("Chinese", "中文"),
        "it": ("Italian", "Italiano"), "pt": ("Portuguese", "Português"),
        "ru": ("Russian", "Русский"), "uk": ("Ukrainian", "Українська"),
        "pl": ("Polish", "Polski"), "nl": ("Dutch", "Nederlands"),
        "sv": ("Swedish", "Svenska"), "th": ("Thai", "ไทย"),
        "ar": ("Arabic", "العربية"), "he": ("Hebrew", "עברית"),
        "el": ("Greek", "Ελληνικά"), "tr": ("Turkish", "Türkçe"),
    }
    out = []
    for code, (en, native) in names.items():
        out.append({
            "code": code,
            "name_en": en,
            "name_native": native,
            "tts": code in PIPER_VOICE_BY_LANGUAGE,
            "politeness": code in POLITENESS_LANGUAGES,
        })
    return out


def source_language_options() -> list[dict]:
    """Dropdown model for the source language (includes auto-detect)."""
    opts = [{"code": "auto", "name_en": "Auto-detect", "name_native": "Auto"}]
    for code in SUBTITLE_SOURCE_LANGUAGES:
        if code == "auto":
            continue
        opts.append({"code": code, "name_en": code, "name_native": code})
    return opts


def ensure_service(cfg, listener=None, llm_chat=None, tts=None,
                   on_line=None) -> SubtitlesService:
    """Create (once) the process-wide subtitles service."""
    global _service
    if _service is None:
        _service = SubtitlesService(
            cfg, listener=listener, llm_chat=llm_chat, tts=tts,
            on_line=on_line)
    else:
        # Refresh live references (listener/tts come up after the broker).
        if listener is not None:
            _service._listener = listener
        if llm_chat is not None:
            _service._llm_chat = llm_chat
        if tts is not None:
            _service._tts = tts
        if on_line is not None:
            _service._on_line = on_line
    return _service
