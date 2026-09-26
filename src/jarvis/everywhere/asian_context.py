"""Asian-language politeness / formality context for translation.

Korean, Japanese, Vietnamese and Chinese encode social register in the words
themselves (honorifics, pronoun choice by age/gender/kinship). A flat
translation loses that. This module supplies the per-language context the
subtitle translator injects into its prompt so the output uses the right
register.

Each entry carries the native term and its English gloss, exactly as the
overlay dropdown shows them.
"""

from __future__ import annotations

from typing import Optional

#: Languages that have a politeness/register dropdown in the overlay.
POLITENESS_LANGUAGES = ("ko", "ja", "vi", "zh")

#: Per-language context choices. ``key`` is the stable value sent over the
#: pipe; ``native``/``en`` render in the dropdown; ``instruction`` is the
#: English steering line added to the translator prompt.
POLITENESS_TABLE: dict[str, list[dict]] = {
    "ko": [
        {"key": "formal",   "native": "존댓말", "en": "Formal (jondaemal)",
         "instruction": "Use formal polite Korean (jondaemal, 합니다/세요 style)."},
        {"key": "informal", "native": "반말", "en": "Informal (banmal)",
         "instruction": "Use informal Korean (banmal) between close equals."},
        {"key": "older_sister", "native": "누나/언니", "en": "To older sister (nuna/eonni)",
         "instruction": "Address as to an older sister (use 누나 if the speaker is male, 언니 if female)."},
        {"key": "older_brother", "native": "형/오빠", "en": "To older brother (hyeong/oppa)",
         "instruction": "Address as to an older brother (use 형 if the speaker is male, 오빠 if female)."},
        {"key": "younger", "native": "동생", "en": "To younger sibling (dongsaeng)",
         "instruction": "Address a younger person casually and warmly (동생 register)."},
        {"key": "elder", "native": "어르신", "en": "To an elder (eoreusin)",
         "instruction": "Use the highest honorific register for an elder (어르신)."},
    ],
    "ja": [
        {"key": "polite",   "native": "丁寧語", "en": "Polite (teineigo)",
         "instruction": "Use polite Japanese (teineigo, です/ます style)."},
        {"key": "honorific", "native": "尊敬語", "en": "Honorific (sonkeigo)",
         "instruction": "Use honorific Japanese (sonkeigo) to elevate the listener."},
        {"key": "casual",   "native": "普通形", "en": "Casual (plain form)",
         "instruction": "Use casual plain-form Japanese between close people."},
        {"key": "to_female", "native": "女性へ", "en": "To a woman",
         "instruction": "Soften the register as when addressing a woman."},
        {"key": "to_male", "native": "男性へ", "en": "To a man",
         "instruction": "Use the register appropriate when addressing a man."},
    ],
    "vi": [
        {"key": "neutral",  "native": "bạn", "en": "Neutral (bạn)",
         "instruction": "Use the neutral pronoun 'bạn'."},
        {"key": "to_male_older", "native": "anh", "en": "To older man (anh)",
         "instruction": "Address an older man as 'anh'."},
        {"key": "to_female_older", "native": "chị", "en": "To older woman (chị)",
         "instruction": "Address an older woman as 'chị'."},
        {"key": "to_younger", "native": "em", "en": "To younger person (em)",
         "instruction": "Address a younger person as 'em'."},
        {"key": "to_elder_male", "native": "ông", "en": "To an elderly man (ông)",
         "instruction": "Address an elderly man respectfully as 'ông'."},
        {"key": "to_elder_female", "native": "bà", "en": "To an elderly woman (bà)",
         "instruction": "Address an elderly woman respectfully as 'bà'."},
    ],
    "zh": [
        {"key": "formal",   "native": "您", "en": "Formal (nín)",
         "instruction": "Use formal Chinese address (您, nín)."},
        {"key": "neutral",  "native": "你", "en": "Neutral (nǐ)",
         "instruction": "Use neutral Chinese address (你, nǐ)."},
        {"key": "to_female", "native": "妳", "en": "To a woman (nǐ, female)",
         "instruction": "Address a woman (use 妳 where the feminine form fits)."},
        {"key": "to_elder", "native": "您老人家", "en": "To an elder",
         "instruction": "Use the respectful elder register (您老人家)."},
    ],
}


def politeness_options(language: str) -> list[dict]:
    """The dropdown entries for a language (empty when it has no register
    table). The first entry is the default."""
    table = POLITENESS_TABLE.get(language, [])
    return [{"key": e["key"], "native": e["native"], "en": e["en"]}
            for e in table]


def politeness_instruction(language: str, key: Optional[str]) -> str:
    """The English steering line injected into the translator prompt, or ""
    when no register applies."""
    if not key:
        return ""
    for entry in POLITENESS_TABLE.get(language, []):
        if entry["key"] == key:
            return "Register: " + entry["instruction"]
    return ""
