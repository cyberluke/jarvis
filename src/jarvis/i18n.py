"""Lightweight i18n for the Toastovač desktop UI.

The app speaks Czech first. Every user-facing string goes through ``tr(key)``
which resolves the current UI language (config ``ui_language``) against the
table below, falling back to English then the key itself.

Supported languages: en, cs, sk, vi.

To add a language: extend the ``_STRINGS`` table. To add a string: give it a
stable key and translate it everywhere.
"""

from __future__ import annotations

from typing import Optional

_current_language: Optional[str] = None

#: key -> {lang -> text}. English is the reference; Czech is the primary UI
#: language (the product is Czech-first).
_STRINGS: dict[str, dict[str, str]] = {
    # Toaster widget.
    "push_to_talk": {
        "en": "Push-to-Talk",
        "cs": "Stiskni a mluv",
        "sk": "Stlač a hovor",
        "vi": "Nhấn để nói",
    },
    "continuous": {
        "en": "Continuous",
        "cs": "Nepřetržitě",
        "sk": "Nepretržite",
        "vi": "Liên tục",
    },
    "push_to_talk_tooltip": {
        "en": "Voice PE push-to-talk: wake words off; the centre button opens the voice session.",
        "cs": "Voice PE stiskni-a-mluv: wake slova vypnutá; prostřední tlačítko otevře relaci.",
        "sk": "Voice PE stlač-a-hovor: wake slová vypnuté; stredné tlačidlo otvorí reláciu.",
        "vi": "Voice PE nhấn-để-nói: tắt từ đánh thức; nút giữa mở phiên.",
    },
    "continuous_tooltip": {
        "en": "Voice PE continuous: wake words on; the mic reopens after each reply during the conversation window.",
        "cs": "Voice PE nepřetržitě: wake slova zapnutá; mikrofon se po každé odpovědi znovu otevře.",
        "sk": "Voice PE nepretržite: wake slová zapnuté; mikrofón sa po každej odpovedi znovu otvorí.",
        "vi": "Voice PE liên tục: bật từ đánh thức; micro mở lại sau mỗi câu trả lời.",
    },
}


def set_language(language: str) -> None:
    """Set the UI language (en, cs, sk, vi). Defaults to Czech (cs)."""
    global _current_language
    lang = str(language or "cs").strip().lower()
    _current_language = lang if lang in ("en", "cs", "sk", "vi") else "cs"


def current_language() -> str:
    """The active UI language, resolved from config on first use."""
    global _current_language
    if _current_language is None:
        try:
            from jarvis.config import _load_json, default_config_path
            data = _load_json(default_config_path())
            set_language(data.get("ui_language") or data.get("whisper_language")
                         or "cs")
        except Exception:
            _current_language = "cs"
    return _current_language


def tr(key: str) -> str:
    """Translate a string key into the current UI language."""
    lang = current_language()
    entry = _STRINGS.get(key)
    if entry is None:
        return key
    return entry.get(lang) or entry.get("en") or key


def available_languages() -> list[tuple[str, str]]:
    """(code, native name) pairs for the settings dropdown."""
    return [("en", "English"), ("cs", "Čeština"), ("sk", "Slovenčina"),
            ("vi", "Tiếng Việt")]
