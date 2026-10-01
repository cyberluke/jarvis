# V271 meeting graph ingestion spec

## Purpose

Durable handoff from the Toastovač AI Meeting Scribe (the
`InterviewCoach` dual-lane meeting coach) into the V271 Personal Graph
and vector memory.

Toastovač owns meeting capture, transcription and (lane-based)
diarization. V271 owns canonical cross-application graph storage,
embeddings and unified retrieval. Toastovač therefore never embeds
meeting content itself; it hands V271 the canonical meeting record,
the graph payload and the vector payloads, and V271 owns the rest.

## Canonical meeting record

The coach emits one canonical record per session (`record.py`):

```text
meeting_id          root idempotency key (stable per session)
title
started_at          ISO-8601 UTC
ended_at            ISO-8601 UTC
source_device
participants[]      {participantId?, name?, email?, phone?, v271UserId?, speakerId?}
speakers[]          {speakerId, label, lane}
transcript_segments[] {segmentId, startMs, endMs, speakerId, text, confidence}
summary
decisions[]         {decisionId?, text, sourceSegmentIds[]}
action_items[]      {actionItemId?, text, owner?, sourceSegmentIds[]}
topics[]            [str]
quality             {utteranceCount, avgConfidence, durationMs, diarization}
provenance          {source: "toastovac-coach", appVersion, sourceDevice, createdAt}
revision            int, bumped on every re-finalization
sourceCalendarEventId?   only when the meeting was launched from a Vape Calendar event
sourceMailThreadId?      only when explicitly associated with a mail thread/invite
```

Transcript segments carry per-utterance `startMs`/`endMs` (relative to
session start), the lane-based `speakerId` (`me` / `others`), and the
Whisper `avg_logprob` as `confidence`. Decisions and action items are
extracted by the local LLM at summarize time and keep `sourceSegmentIds`
where the model can attribute them.

## Authentication

The client authenticates with the signed-in V271 identity: a Bearer
token (`v271_api_token`, or the `V271_API_TOKEN` environment variable)
issued with at least the `meetings.write` and `graph.write` scopes. The
token identifies the owner, so the client NEVER sends a `user_id` as
authority. Vector payloads may carry `ownerUserId` as *indexing
metadata* only when `v271_owner_user_id` is explicitly configured;
otherwise V271 stamps it from the token.

## V271 API contract

V271 implements these endpoints (base URL configurable,
`v271_api_base_url`):

### Upload (idempotent upsert)

```
PUT {base}/meetings/{meetingId}
Authorization: Bearer <token>
Content-Type: application/json

{ canonical meeting record ... }
```

The meeting ID is the root idempotency key: repeated uploads update the
existing meeting and its changed children without duplicates. The
request carries `revision`; V271 keeps per-meeting revision metadata and
an upsert with an older revision is a no-op (or a 409 conflict, which
the client treats as success since the server already has the record).
Response:

```json
{"ok": true, "meetingId": "...", "revision": 2, "graphMeetingId": "..."}
```

### Graph write

```
POST {base}/meetings/{meetingId}/graph
```

Body (the graph mapping contract):

```json
{
  "meetingId": "...",
  "entities": {
    "meeting":     {"externalId": "...", "title": "...", "startedAt": "...", "endedAt": "..."},
    "speakers":    [{"entityType": "meeting.speaker", "externalId": "...", "label": "...", "lane": "me|others"}],
    "segments":    [{"entityType": "meeting.segment", "externalId": "...", "speakerId": "...", "startMs": 0, "endMs": 0, "text": "..."}],
    "decisions":   [{"entityType": "meeting.decision", "externalId": "...", "text": "...", "sourceSegmentIds": []}],
    "actionItems": [{"entityType": "meeting.action_item", "externalId": "...", "text": "...", "owner": "...", "sourceSegmentIds": []}]
  },
  "links": [
    {"type": "HAS_SPEAKER", "from": "meeting:<id>", "to": "meeting.speaker:<id>"},
    {"type": "HAS_SEGMENT", "from": "meeting:<id>", "to": "meeting.segment:<id>"},
    {"type": "DECIDED", "from": "meeting:<id>", "to": "meeting.decision:<id>"},
    {"type": "HAS_ACTION_ITEM", "from": "meeting:<id>", "to": "meeting.action_item:<id>"},
    {"type": "MENTIONS", "from": "meeting:<id>", "to": "person:<id>"}
  ],
  "participantIdentityHints": [{"participantId": "...", "email": "...", "phone": "...", "v271UserId": "..."}],
  "calendarEventId": "...",
  "mailThreadId": "..."
}
```

V271 creates the `meeting` entity plus children and applies the links.
`MENTIONS` links are created only when a participant matches an existing
V271 person/contact entity via a **stable identity** (email, phone,
v271 user id). People are never merged by similar display name.
`calendarEventId` makes V271 create `calendar.event -> HAS_MEETING ->
meeting`; `mailThreadId` links the graph entities to the mail thread.
Neither relationship is ever inferred from timestamps.

### Vector indexing

```
POST {base}/meetings/{meetingId}/vectors
```

Body:

```json
{
  "meetingId": "...",
  "items": [
    {"entityType": "meeting.segment", "text": "...", "segmentId": "...", "speakerId": "...", "startMs": 0, "endMs": 0},
    {"entityType": "meeting.summary", "text": "..."},
    {"entityType": "meeting.decision", "text": "...", "segmentId": "..."},
    {"entityType": "meeting.action_item", "text": "...", "segmentId": "..."}
  ],
  "ownerUserId": "..."
}
```

V271 runs these through its central vector pipeline (embeddings are
V271's, never a Toastovač embedding service) with metadata
`owner_user_id`, `meeting_id`, `entity_type`, `segment_id` where
applicable, `speaker_id` where applicable, and start/end timestamps.

## Durable ingestion job

`MeetingIngestStore` persists one row per meeting locally
(`meeting_ingestions` table, `interaction_kind = 'meeting'`). The full
canonical record is stored as JSON so the meeting stays recoverable and
re-uploadable locally even if graph ingestion fails — retry never
requires re-recording or re-transcribing.

States:

```text
QUEUED → UPLOADING → GRAPH_WRITTEN → INDEXING → COMPLETED
                                   ↘ FAILED
```

On failure the row keeps the failed `stage` (`UPLOADING`,
`GRAPH_WRITTEN` or `INDEXING`). A retry (`coordinator.retry(meeting_id)`)
re-queues the job and the worker resumes from that stage, skipping work
already acknowledged by V271. Attempts and `last_error` are recorded.

The coordinator runs one background worker thread (daemon-owned). Every
transition emits `{"type": "meeting_status", "meetingId", "state",
"label"}` through its `on_event` callback, which the Everywhere broker
forwards to the coach overlay.

## Status UX

| State | Label |
|-------|-------|
| QUEUED | Saved locally |
| UPLOADING / GRAPH_WRITTEN / INDEXING | Syncing to V271 |
| COMPLETED | Indexed |
| FAILED | Sync failed (retryable) |

`Indexed` is only ever shown after V271 confirmed vector indexing
(`COMPLETED`), never after graph write alone.

## Memory compatibility

The local meeting record lives in `meeting_ingestions` with
`interaction_kind = 'meeting'`. After successful ingestion the V271
graph meeting ID from the upload/graph response is stored back into the
row (`v271_graph_meeting_id`). The coach additionally pushes the
transcript + summary into the dialogue memory as before, so the normal
diary pipeline is unchanged.

## Non-goals

No Toastovač embedding service. No person merging by display name. No
inferred calendar/mail relationships. No v271 DB binding beyond the
documented API contract. No automatic ingestion of arbitrary
conversations — only coach meetings.