"""V271 ingestion API client (meetings.write / graph.write scopes).

The client implements the documented V271 contract (see
``v271_ingest.spec.md``): an idempotent meeting upsert keyed on the
meeting ID, a graph write applying the entities/links payload, and a
vector indexing handoff into V271's central pipeline. The Bearer token
identifies the owner, so no ``user_id`` is ever sent as authority.

All calls raise ``V271ApiError`` on transport/HTTP failures so the
coordinator can mark the job FAILED and retry from the stage.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..debug import debug_log

_TIMEOUT_SEC = 60.0


class V271ApiError(RuntimeError):
    """Transport or HTTP failure talking to V271 (retryable)."""


class V271ApiClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_sec: float = _TIMEOUT_SEC,
        session: Any = None,
    ) -> None:
        self._base = (base_url or "").rstrip("/")
        self._token = token or ""
        self._timeout = timeout_sec
        self._session = session  # requests.Session for tests

    # -- public stages --------------------------------------------------

    def upload_meeting(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """PUT the canonical record; idempotent by meeting ID."""
        meeting_id = str(record.get("meeting_id") or "")
        if not meeting_id:
            raise V271ApiError("record has no meeting_id")
        data = self._request(
            "PUT", f"/meetings/{meeting_id}", body=record
        )
        return self._expect_ok(data)

    def write_graph(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST the graph entities/links payload."""
        meeting_id = str(payload.get("meetingId") or "")
        if not meeting_id:
            raise V271ApiError("graph payload has no meetingId")
        data = self._request(
            "POST", f"/meetings/{meeting_id}/graph", body=payload
        )
        return self._expect_ok(data)

    def index_vectors(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST meaning-bearing content into V271's vector pipeline."""
        meeting_id = str(payload.get("meetingId") or "")
        if not meeting_id:
            raise V271ApiError("vector payload has no meetingId")
        data = self._request(
            "POST", f"/meetings/{meeting_id}/vectors", body=payload
        )
        return self._expect_ok(data)

    # -- internals ------------------------------------------------------

    def _request(self, method: str, path: str, body: Any) -> Dict[str, Any]:
        if not self._token:
            raise V271ApiError(
                "V271 API token is not configured "
                "(v271_api_token or V271_API_TOKEN)"
            )
        url = f"{self._base}{path}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        try:
            if self._session is not None:
                resp = self._session.request(
                    method, url, json=body, headers=headers,
                    timeout=self._timeout,
                )
            else:
                import requests  # noqa: PLC0415

                resp = requests.request(
                    method, url, json=body, headers=headers,
                    timeout=self._timeout,
                )
        except Exception as exc:  # noqa: BLE001
            raise V271ApiError(f"V271 request failed: {exc}") from exc
        if resp.status_code == 409:
            # Idempotency: the server already holds this meeting revision.
            # The upsert is satisfied — treat the conflict as success so
            # the job proceeds to the graph/vector stages.
            debug_log(
                f"v271 ingest: 409 for {method} {path} "
                "(server already has this revision)",
                "v271_ingest",
            )
            return {"ok": True, "conflict": True}
        if resp.status_code < 200 or resp.status_code >= 300:
            detail = ""
            try:
                detail = resp.text[:300]
            except Exception:  # noqa: BLE001
                pass
            raise V271ApiError(
                f"V271 {method} {path} -> HTTP {resp.status_code}: {detail}"
            )
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise V271ApiError(
                f"V271 {method} {path} returned non-JSON body"
            ) from exc
        return data if isinstance(data, dict) else {"ok": True, "data": data}

    @staticmethod
    def _expect_ok(data: Dict[str, Any]) -> Dict[str, Any]:
        if data.get("ok") is False:
            raise V271ApiError(
                f"V271 rejected the payload: {data.get('error') or data}"
            )
        return data


def build_graph_payload(
    record: Dict[str, Any],
    *,
    owner_user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Graph payload from the canonical record (spec: Graph mapping).

    Entities and links are emitted as the contract the client POSTs;
    V271 owns resolving stable participant identities (email/phone/v271
    user id) and creating ``MENTIONS`` links to existing person/contact
    entities. People are never merged by similar display name.
    """
    meeting_id = str(record.get("meeting_id") or "")
    segments = record.get("transcript_segments") or []
    speakers = record.get("speakers") or []
    decisions = record.get("decisions") or []
    action_items = record.get("action_items") or []

    entities = {
        "meeting": {
            "externalId": meeting_id,
            "title": record.get("title") or "",
            "startedAt": record.get("started_at"),
            "endedAt": record.get("ended_at"),
        },
        "speakers": [
            {"entityType": "meeting.speaker", "externalId": s["speakerId"],
             "label": s.get("label"), "lane": s.get("lane")}
            for s in speakers if s.get("speakerId")
        ],
        "segments": [
            {"entityType": "meeting.segment", "externalId": s["segmentId"],
             "speakerId": s.get("speakerId"), "startMs": s.get("startMs"),
             "endMs": s.get("endMs"), "text": s.get("text")}
            for s in segments if s.get("segmentId")
        ],
        "decisions": [
            {"entityType": "meeting.decision", "externalId": f"d{i}",
             "text": d.get("text"), "sourceSegmentIds": d.get("sourceSegmentIds") or []}
            for i, d in enumerate(decisions)
        ],
        "actionItems": [
            {"entityType": "meeting.action_item", "externalId": f"a{i}",
             "text": a.get("text"), "owner": a.get("owner"),
             "sourceSegmentIds": a.get("sourceSegmentIds") or []}
            for i, a in enumerate(action_items)
        ],
    }

    links: List[Dict[str, str]] = []
    for s in speakers:
        if s.get("speakerId"):
            links.append({
                "type": "HAS_SPEAKER",
                "from": f"meeting:{meeting_id}",
                "to": f"meeting.speaker:{s['speakerId']}",
            })
    for s in segments:
        if s.get("segmentId"):
            links.append({
                "type": "HAS_SEGMENT",
                "from": f"meeting:{meeting_id}",
                "to": f"meeting.segment:{s['segmentId']}",
            })
    for i in range(len(decisions)):
        links.append({
            "type": "DECIDED",
            "from": f"meeting:{meeting_id}",
            "to": f"meeting.decision:d{i}",
        })
    for i in range(len(action_items)):
        links.append({
            "type": "HAS_ACTION_ITEM",
            "from": f"meeting:{meeting_id}",
            "to": f"meeting.action_item:a{i}",
        })

    return {
        "meetingId": meeting_id,
        "entities": entities,
        "links": links,
        "participantIdentityHints": [
            {
                "participantId": p.get("participantId") or p.get("speakerId"),
                "name": p.get("name"),
                "email": p.get("email"),
                "phone": p.get("phone"),
                "v271UserId": p.get("v271UserId"),
            }
            for p in (record.get("participants") or [])
            if isinstance(p, dict)
        ],
        "calendarEventId": record.get("sourceCalendarEventId"),
        "mailThreadId": record.get("sourceMailThreadId"),
    }


def build_vector_payload(
    record: Dict[str, Any],
    *,
    owner_user_id: Optional[str] = None,
    chunk_chars: int = 1500,
) -> Dict[str, Any]:
    """Vector payload: meaning-bearing content with metadata.

    V271 runs these through its central vector pipeline; Toastovač never
    embeds. ``ownerUserId`` is indexing metadata, never authority; it is
    omitted unless explicitly configured.
    """
    meeting_id = str(record.get("meeting_id") or "")
    items: List[Dict[str, Any]] = []

    # Transcript chunks (split on whole segments to respect boundaries).
    chunk: List[Dict[str, Any]] = []
    chunk_chars_now = 0
    for seg in record.get("transcript_segments") or []:
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        item = {
            "entityType": "meeting.segment",
            "text": text,
            "segmentId": seg.get("segmentId"),
            "speakerId": seg.get("speakerId"),
            "startMs": seg.get("startMs"),
            "endMs": seg.get("endMs"),
        }
        chunk.append(item)
        chunk_chars_now += len(text)
        if chunk_chars_now >= chunk_chars:
            _flush_chunk(items, chunk)
            chunk = []
            chunk_chars_now = 0
    _flush_chunk(items, chunk)

    if record.get("summary"):
        items.append({"entityType": "meeting.summary", "text": str(record["summary"])})
    for i, d in enumerate(record.get("decisions") or []):
        item = {"entityType": "meeting.decision", "text": str(d.get("text") or "")}
        seg_ids = d.get("sourceSegmentIds") or []
        if seg_ids:
            item["segmentId"] = _segment_id_for_index(record, seg_ids[0])
        if item["text"]:
            items.append(item)
    for i, a in enumerate(record.get("action_items") or []):
        item = {"entityType": "meeting.action_item", "text": str(a.get("text") or "")}
        seg_ids = a.get("sourceSegmentIds") or []
        if seg_ids:
            item["segmentId"] = _segment_id_for_index(record, seg_ids[0])
        if item["text"]:
            items.append(item)

    payload: Dict[str, Any] = {
        "meetingId": meeting_id,
        "items": items,
    }
    if owner_user_id:
        payload["ownerUserId"] = owner_user_id
    return payload


def _flush_chunk(items: List[Dict[str, Any]], chunk: List[Dict[str, Any]]) -> None:
    if not chunk:
        return
    if len(chunk) == 1:
        items.append(chunk[0])
        return
    merged = {
        "entityType": "meeting.segment",
        "text": " ".join(c["text"] for c in chunk),
        # A chunk spans multiple segments: the ids stay identifiable.
        "segmentIds": [c.get("segmentId") for c in chunk if c.get("segmentId")],
        "speakerIds": sorted({c.get("speakerId") for c in chunk if c.get("speakerId")}),
        "startMs": chunk[0].get("startMs"),
        "endMs": chunk[-1].get("endMs"),
    }
    items.append(merged)


def _segment_id_for_index(record: Dict[str, Any], index: int) -> Optional[str]:
    segments = record.get("transcript_segments") or []
    if 0 <= index < len(segments):
        return segments[index].get("segmentId")
    return None