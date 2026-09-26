"""Registry mapping ISO-639 language codes to display names.

The Grammar Judge prompt binds the model to the Whisper-detected language by
*name* (e.g. "Czech"), not just by code, because the model's multilingual
grammar knowledge is keyed far more reliably by the language's name than by a
two-letter tag. This map is purely descriptive: any code missing here falls
back to the raw code string so an unusual Whisper detection still produces a
coherent prompt.

The map is intentionally open — Whisper can detect many more languages than
the vendored Hunspell set, and the judge must follow Whisper, not the
dictionary inventory.
"""

from __future__ import annotations

#: ISO-639-1 code -> English language name. Extend freely; unknown codes fall
#: back to the raw code in :func:`language_name`.
LANGUAGE_DESCRIPTORS: dict[str, str] = {
    "cs": "Czech",
    "sk": "Slovak",
    "en": "English",
    "de": "German",
    "vi": "Vietnamese",
    "pl": "Polish",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
    "pt": "Portuguese",
    "nl": "Dutch",
    "ru": "Russian",
    "uk": "Ukrainian",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Chinese",
    "sv": "Swedish",
    "da": "Danish",
    "no": "Norwegian",
    "fi": "Finnish",
    "hu": "Hungarian",
    "ro": "Romanian",
}


def language_name(code: str) -> str:
    """Return the display name for ``code``, or the code itself when unknown.

    ``code`` is normalised to lowercase. A missing/empty code yields ``""`` so
    the caller can decide how to surface an undetected language.
    """
    folded = str(code or "").strip().lower()
    if not folded:
        return ""
    return LANGUAGE_DESCRIPTORS.get(folded, folded)
