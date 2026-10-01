"""Canonical meeting record for V271 graph ingestion.

Builds the spec's canonical meeting record from the coach session
(segments with timestamps/confidence, speakers, participants) and
extracts the structured summary (summary, decisions, action items,
topics) with the local LLM. The record is transport-neutral: the V271
client serialises it as-is.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from .._version import VERSION

SOURCE = "toastovac-coach"

#: Segment fields the coach records per utterance.
SEGMENT_KEYS = ("segmentId", "startMs", "endMs", "speakerId", "text", "confidence")

_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}")


def new_meeting_id() -> str:
    """Fresh meeting id (root idempotency key)."""
    return f"mtg-{uuid.uuid4().hex[:16]}"


def build_meeting_record(
    *,
    meeting_id: str,
    title: str,
    started_at: str,
    ended_at: str,
    source_device: str,
    segments: List[Dict[str, Any]],
    summary: str,
    decisions: Optional[List[Dict[str, Any]]] = None,
    action_items: Optional[List[Dict[str, Any]]] = None,
    topics: Optional[List[str]] = None,
    participants: Optional[List[Dict[str, Any]]] = None,
    speakers: Optional[List[Dict[str, Any]]] = None,
    quality: Optional[Dict[str, Any]] = None,
    revision: int = 1,
    source_calendar_event_id: Optional[str] = None,
    source_mail_thread_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble the canonical record; every field the spec requires is
    always present (empty lists where nothing was captured)."""
    return {
        "meeting_id": meeting_id,
        "title": title,
        "started_at": started_at,
        "ended_at": ended_at,
        "source_device": source_device,
        "participants": list(participants or []),
        "speakers": list(speakers or []),
        "transcript_segments": [
            {key: segment.get(key) for key in SEGMENT_KEYS}
            for segment in (segments or [])
        ],
        "summary": summary,
        "decisions": list(decisions or []),
        "action_items": list(action_items or []),
        "topics": list(topics or []),
        "quality": dict(quality or {}),
        "provenance": {
            "source": SOURCE,
            "appVersion": VERSION,
            "sourceDevice": source_device,
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        "revision": int(revision),
        "sourceCalendarEventId": source_calendar_event_id,
        "sourceMailThreadId": source_mail_thread_id,
    }


def default_quality(segments: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate quality from the segment list."""
    confidences = [
        float(s.get("confidence"))
        for s in segments
        if isinstance(s.get("confidence"), (int, float))
    ]
    duration_ms = 0
    for s in segments:
        end = s.get("endMs") or 0
        if end > duration_ms:
            duration_ms = int(end)
    return {
        "utteranceCount": len(segments),
        "avgConfidence": round(sum(confidences) / len(confidences), 4)
        if confidences else None,
        "durationMs": duration_ms,
        "diarization": "lane",
    }


def default_speakers() -> List[Dict[str, Any]]:
    """Lane-based speakers: the coach diarizes by capture lane."""
    return [
        {"speakerId": "me", "label": "me", "lane": "me"},
        {"speakerId": "others", "label": "others", "lane": "others"},
    ]


_EXTRACT_SYSTEM = (
    "You structure a meeting transcript for a personal knowledge graph. "
    "Return ONLY a JSON object with exactly these keys:\n"
    '{"summary": string, "decisions": [{"text": string, "sourceSegmentIds": [int]}], '
    '"actionItems": [{"text": string, "owner": string|null, "sourceSegmentIds": [int]}], '
    '"topics": [string]}\n'
    "Decisions are explicit choices or conclusions the participants reached. "
    "Action items are tasks someone agreed to do. sourceSegmentIds must be "
    "indexes (0-based) into the transcript segment list that support the "
    "entry; use [] when unsure. Do not invent facts."
)


def extract_meeting_structure(
    llm_chat: Callable[[str, str], str],
    transcript: str,
    *,
    language: str = "en",
) -> Dict[str, Any]:
    """One LLM call returning ``{summary, decisions, actionItems, topics}``.

    Falls back gracefully: on any LLM/parse failure the summary is the
    full transcript (never fabricated structure) and the lists are empty,
    so ingestion can still proceed with the raw transcript.
    """
    system = _EXTRACT_SYSTEM
    if language and language.lower().startswith("cs"):
        system = (
            "Strukturuješ přepis schůzky pro osobní znalostní graf. Vrať POUZE "
            "JSON objekt s přesně těmito klíči:\n"
            '{"summary": string, "decisions": [{"text": string, "sourceSegmentIds": [int]}], '
            '"actionItems": [{"text": string, "owner": string|null, "sourceSegmentIds": [int]}], '
            '"topics": [string]}\n'
            "Rozhodnutí jsou explicitní volby či závěry. Úkoly jsou věci, které "
            "někdo slíbil udělat. sourceSegmentIds jsou 0-založené indexy do "
            "seznamu segmentů přepisu; jinak []. Nevymýšlej si fakta."
        )
    user = (
        "Transcript segments (index: speaker: text):\n"
        f"{transcript}\n\nJSON:"
    )
    try:
        raw = (llm_chat(system, user) or "").strip()
    except Exception:
        raw = ""
    parsed = _parse_extraction_json(raw)
    if parsed is None:
        # Honest fallback: raw transcript as the summary, no structure.
        return {
            "summary": _plain_summary_fallback(transcript),
            "decisions": [],
            "actionItems": [],
            "topics": [],
        }
    return {
        "summary": _clean_str(parsed.get("summary")) or _plain_summary_fallback(transcript),
        "decisions": _clean_entries(parsed.get("decisions"), "decisions"),
        "actionItems": _clean_entries(parsed.get("actionItems"), "actionItems"),
        "topics": [
            _clean_str(t) for t in (parsed.get("topics") or []) if _clean_str(t)
        ],
    }


def _parse_extraction_json(raw: str) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    match = _JSON_BLOCK_RE.search(raw)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _clean_str(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()


def _clean_entries(value: Any, kind: str) -> List[Dict[str, Any]]:
    """Normalise the LLM's decision/action-item list shape."""
    out: List[Dict[str, Any]] = []
    if not isinstance(value, list):
        return out
    for raw in value:
        if not isinstance(raw, dict):
            continue
        text = _clean_str(raw.get("text"))
        if not text:
            continue
        segment_ids = raw.get("sourceSegmentIds") or []
        if not isinstance(segment_ids, list):
            segment_ids = []
        entry: Dict[str, Any] = {
            "text": text,
            "sourceSegmentIds": [
                int(i) for i in segment_ids
                if isinstance(i, (int, float)) and not isinstance(i, bool)
            ],
        }
        if kind == "actionItems":
            entry["owner"] = _clean_str(raw.get("owner")) or None
        out.append(entry)
    return out


def _plain_summary_fallback(transcript: str) -> str:
    lines = [ln for ln in transcript.splitlines() if ln.strip()]
    return "\n".join(lines[-24:]) if lines else ""


def segments_to_transcript(segments: List[Dict[str, Any]]) -> str:
    """Render segments as ``index: speaker: text`` lines for the LLM."""
    return "\n".join(
        f"{i}: {s.get('speakerId')}: {s.get('text')}"
        for i, s in enumerate(segments)
        if s.get("text")
    )