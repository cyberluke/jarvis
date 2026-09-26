"""Action prompts and the saved prompt library.

Prompts are first-class: every action prompt is fully editable through
config, and users can save arbitrary prompts with their own result mode and
model profile. The library persists next to the active config file so a
restart keeps prompts, shortcuts and recent choices. No external account is
required.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

#: Built-in action prompt defaults (editable via ``everywhere_prompts``).
DEFAULT_ACTION_PROMPTS: Dict[str, str] = {
    "rewrite": (
        "Rewrite the selected text. Preserve its meaning unless the "
        "instruction says otherwise. Return only the rewritten text."
    ),
    "proofread": (
        "Proofread the selected text. Return strict JSON: "
        '{"source_hash": str, "issues": [{"start_utf16": int, '
        '"end_utf16": int, "original": str, "replacement": str, '
        '"category": "spelling|grammar|punctuation|style|clarity|'
        'consistency|terminology", "rule": str, "explanation": str, '
        '"confidence": number}], "corrected_text": str}. '
        "List every issue separately."
    ),
    "alternatives": (
        "Produce N distinct alternatives for the selected text, numbered "
        "1..N, one per line, no other text."
    ),
    "explain": (
        "Explain the selected text concisely in the language of the text. "
        "Use the provided semantic context (language id, shell, remote "
        "identity) when present."
    ),
    "translate": (
        "Translate the selected text into {target_language}. "
        "Output ONLY the translated text. "
        "Do NOT include any reasoning, explanation, notes, or commentary. "
        "Do NOT include the original text. "
        "Do NOT include phrases like 'Here is the translation' or 'Translation:'. "
        "Preserve line breaks, lists, basic formatting, code spans, URLs, "
        "identifiers and numbers. "
        "If the input is already in {target_language}, return it unchanged."
    ),
}

#: Configurable proofread categories (§Proofread).
PROOFREAD_CATEGORIES = (
    "spelling",
    "grammar",
    "punctuation",
    "style",
    "clarity",
    "consistency",
    "terminology",
)

#: Result modes a saved prompt may declare (§Custom prompt library).
RESULT_MODES = ("replace", "panel", "alternatives", "structured", "copy-only")

#: Recent-language / recent-prompt window (§Translate, §Custom prompt library).
RECENT_WINDOW = 5


def get_action_prompt(cfg, action: str) -> str:
    """Editable prompt text for ``action`` (config wins over built-in)."""
    table: Dict[str, str] = dict(DEFAULT_ACTION_PROMPTS)
    override = getattr(cfg, "everywhere_prompts", None)
    if isinstance(override, dict):
        for key, value in override.items():
            if str(key) in table and isinstance(value, str) and value.strip():
                table[str(key)] = value.strip()
    return table.get(action, "")


def build_user_block(snapshot_dict: Dict[str, Any], *,
                     target_language: Optional[str] = None,
                     alternatives_count: Optional[int] = None) -> str:
    """Compose the user message: semantic context first, then the text."""
    parts: List[str] = []
    sem = snapshot_dict.get("semantic_context") or {}
    if isinstance(sem, dict) and sem:
        kv = ", ".join(f"{k}={v}" for k, v in sem.items() if v not in (None, ""))
        if kv:
            parts.append(f"[context] {kv}")
    if target_language:
        parts.append(f"[target_language] {target_language}")
    if alternatives_count:
        parts.append(f"[count] {alternatives_count}")
    parts.append("[selection]")
    parts.append(str(snapshot_dict.get("text") or ""))
    return "\n".join(parts)


# ── saved prompt library ────────────────────────────────────────────────

def _new_entry(name: str, instruction: str, *, category: str = "",
               description: str = "", result_mode: str = "panel",
               model_profile: str = "user-selected",
               shortcut: str = "") -> Dict[str, Any]:
    now = time.time()
    return {
        "id": f"p{int(now * 1000)}",
        "name": name,
        "category": category,
        "description": description,
        "instruction": instruction,
        "result_mode": result_mode if result_mode in RESULT_MODES else "panel",
        "model_profile": model_profile,
        "shortcut": shortcut,
        "created_at": now,
        "updated_at": now,
        "usage_count": 0,
        "last_used_at": None,
    }


def normalize_library(raw: Any) -> List[Dict[str, Any]]:
    """Coerce the on-disk library into well-formed entries (order kept)."""
    out: List[Dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        instruction = str(item.get("instruction") or "").strip()
        if not name or not instruction:
            continue
        mode = str(item.get("result_mode") or "panel")
        out.append({
            "id": str(item.get("id") or f"p{len(out) + 1}"),
            "name": name,
            "category": str(item.get("category") or ""),
            "description": str(item.get("description") or ""),
            "instruction": instruction,
            "result_mode": mode if mode in RESULT_MODES else "panel",
            "model_profile": str(item.get("model_profile") or "user-selected"),
            "shortcut": str(item.get("shortcut") or ""),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
            "usage_count": int(item.get("usage_count") or 0),
            "last_used_at": item.get("last_used_at"),
            "pinned": bool(item.get("pinned", False)),
        })
    return out


def fuzzy_match(query: str, text: str) -> bool:
    """Subsequence fuzzy match, case-insensitive (prompt/language search)."""
    q = (query or "").casefold().strip()
    t = (text or "").casefold()
    if not q:
        return True
    it = iter(t)
    return all(ch in it for ch in q)


def search_library(entries: List[Dict[str, Any]], query: str
                   ) -> List[Dict[str, Any]]:
    """Pinned first, then recent, then the rest; fuzzy name/category match."""
    if not query or not query.strip():
        ordered = sorted(
            entries,
            key=lambda e: (not e.get("pinned"),
                           -(e.get("last_used_at") or e.get("updated_at") or 0)),
        )
        return ordered
    hits = [e for e in entries
            if fuzzy_match(query, str(e.get("name") or ""))
            or fuzzy_match(query, str(e.get("category") or ""))]
    return sorted(
        hits,
        key=lambda e: (not e.get("pinned"),
                       -(e.get("last_used_at") or e.get("updated_at") or 0)),
    )


def touch_usage(entries: List[Dict[str, Any]], prompt_id: str
                ) -> List[Dict[str, Any]]:
    """Bump usage_count/last_used_at for one entry; keep list identity."""
    now = time.time()
    for e in entries:
        if e.get("id") == prompt_id:
            e["usage_count"] = int(e.get("usage_count") or 0) + 1
            e["last_used_at"] = now
            e["updated_at"] = now
            break
    return entries


def recent_first(entries: List[Dict[str, Any]], limit: int = RECENT_WINDOW
                 ) -> List[Dict[str, Any]]:
    """Last-used-first window (pinned entries stay at the front)."""
    used = [e for e in entries if e.get("last_used_at")]
    used.sort(key=lambda e: e.get("last_used_at") or 0, reverse=True)
    pinned = [e for e in entries if e.get("pinned")]
    seen: set = set()
    out: List[Dict[str, Any]] = []
    for e in pinned + used + entries:
        eid = e.get("id")
        if eid and eid not in seen:
            seen.add(eid)
            out.append(e)
    return out[:max(1, limit)]
