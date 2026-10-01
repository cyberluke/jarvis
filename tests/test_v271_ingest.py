"""Tests for V271 meeting graph ingestion (v271_ingest.spec.md).

Covers the canonical record builder, LLM structure extraction, the
durable job store, the V271 API client contract, graph/vector payload
shapes, the coordinator state machine (including stage-resuming retry
and idempotent re-upload), and the coach integration.
"""

from __future__ import annotations

import json
import sys
import time
import types
from types import SimpleNamespace

import pytest

from jarvis.memory.db import Database
from jarvis.v271_ingest.client import (
    V271ApiClient,
    V271ApiError,
    build_graph_payload,
    build_vector_payload,
)
from jarvis.v271_ingest.coordinator import V271IngestCoordinator
from jarvis.v271_ingest.record import (
    build_meeting_record,
    default_quality,
    default_speakers,
    extract_meeting_structure,
    new_meeting_id,
    segments_to_transcript,
)
from jarvis.v271_ingest.store import (
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_GRAPH_WRITTEN,
    STATE_INDEXING,
    STATE_QUEUED,
    STAGE_GRAPH_WRITTEN,
    STAGE_INDEXING,
    STAGE_UPLOADING,
    MeetingIngestStore,
)


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "v271.db"), None)
    yield database
    database.close()


@pytest.fixture
def store(db):
    return MeetingIngestStore(db)


def sample_segments():
    return [
        {"segmentId": "seg-0000", "startMs": 0, "endMs": 1200,
         "speakerId": "others", "text": "What is the plan?", "confidence": -0.31},
        {"segmentId": "seg-0001", "startMs": 1300, "endMs": 2600,
         "speakerId": "me", "text": "We ship next week.", "confidence": -0.22},
        {"segmentId": "seg-0002", "startMs": 2700, "endMs": 4000,
         "speakerId": "others", "text": "Approved.", "confidence": -0.4},
    ]


def sample_record(**overrides):
    segments = overrides.pop("segments", sample_segments())
    record = build_meeting_record(
        meeting_id=overrides.pop("meeting_id", "mtg-test-1"),
        title=overrides.pop("title", "Weekly sync"),
        started_at=overrides.pop("started_at", "2026-09-28T08:00:00Z"),
        ended_at=overrides.pop("ended_at", "2026-09-28T08:30:00Z"),
        source_device=overrides.pop("source_device", "PC-TEST"),
        segments=segments,
        summary=overrides.pop("summary", "Reviewed the roadmap."),
        decisions=overrides.pop("decisions", [{"text": "Ship next week",
                                               "sourceSegmentIds": [1]}]),
        action_items=overrides.pop("action_items", [{"text": "Book demo",
                                                     "owner": "me",
                                                     "sourceSegmentIds": [2]}]),
        topics=overrides.pop("topics", ["roadmap", "release"]),
        participants=overrides.pop("participants", [{"name": "Jan",
                                                     "email": "jan@x.cz"}]),
        speakers=default_speakers(),
        quality=default_quality(segments),
        revision=overrides.pop("revision", 1),
        source_calendar_event_id=overrides.pop("source_calendar_event_id", None),
        source_mail_thread_id=overrides.pop("source_mail_thread_id", None),
    )
    record.update(overrides)
    return record


class FakeResp:
    def __init__(self, data=None, status_code=200):
        self.status_code = status_code
        self._data = data if data is not None else {"ok": True}
        self.text = json.dumps(self._data)

    def json(self):
        return self._data


class FakeSession:
    """Records (method, url, body) and returns queued responses."""

    def __init__(self, *responses):
        self.responses = list(responses) or [FakeResp()]
        self.calls = []

    def request(self, method, url, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "body": json,
                           "headers": headers})
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


def make_client(session=None, token="tok-123", base="https://v271.cz/api/v1"):
    return V271ApiClient(base, token, session=session or FakeSession())


# ---------------------------------------------------------------------------
# canonical record
# ---------------------------------------------------------------------------


class TestMeetingRecord:
    def test_required_fields_present(self):
        record = sample_record()
        for key in ("meeting_id", "title", "started_at", "ended_at",
                    "source_device", "participants", "speakers",
                    "transcript_segments", "summary", "decisions",
                    "action_items", "topics", "quality", "provenance",
                    "revision"):
            assert key in record, key

    def test_segments_keep_all_fields(self):
        record = sample_record()
        segment = record["transcript_segments"][0]
        for key in ("segmentId", "startMs", "endMs", "speakerId", "text",
                    "confidence"):
            assert key in segment, key

    def test_provenance_and_revision(self):
        record = sample_record(revision=3)
        assert record["provenance"]["source"] == "toastovac-coach"
        assert record["provenance"]["appVersion"]
        assert record["revision"] == 3

    def test_calendar_and_mail_ids(self):
        record = sample_record(source_calendar_event_id="cal-7",
                               source_mail_thread_id="thr-9")
        assert record["sourceCalendarEventId"] == "cal-7"
        assert record["sourceMailThreadId"] == "thr-9"

    def test_quality_aggregation(self):
        record = sample_record()
        quality = record["quality"]
        assert quality["utteranceCount"] == 3
        assert quality["durationMs"] == 4000
        assert quality["diarization"] == "lane"
        assert quality["avgConfidence"] == pytest.approx(
            (-0.31 + -0.22 + -0.4) / 3, abs=1e-4)

    def test_new_meeting_id_unique(self):
        assert new_meeting_id() != new_meeting_id()


class TestStructureExtraction:
    def test_parses_llm_json(self):
        def llm(system, user):
            return ('{"summary": "S1", "decisions": [{"text": "D1", '
                    '"sourceSegmentIds": [0]}], "actionItems": [{"text": "A1", '
                    '"owner": "me", "sourceSegmentIds": [1]}], "topics": ["T"]}')

        structure = extract_meeting_structure(
            llm, segments_to_transcript(sample_segments()))
        assert structure["summary"] == "S1"
        assert structure["decisions"] == [{"text": "D1", "sourceSegmentIds": [0]}]
        assert structure["actionItems"] == [
            {"text": "A1", "sourceSegmentIds": [1], "owner": "me"}]
        assert structure["topics"] == ["T"]

    def test_malformed_llm_output_falls_back_honestly(self):
        def llm(system, user):
            return "I cannot produce JSON sorry"

        segments = sample_segments()
        structure = extract_meeting_structure(
            llm, segments_to_transcript(segments))
        # Never fabricated: summary = raw transcript tail, lists empty.
        assert "We ship next week" in structure["summary"]
        assert structure["decisions"] == []
        assert structure["actionItems"] == []
        assert structure["topics"] == []

    def test_llm_exception_falls_back(self):
        def llm(system, user):
            raise RuntimeError("model down")

        structure = extract_meeting_structure(llm, "0: me: hi")
        assert structure["decisions"] == []
        assert structure["actionItems"] == []

    def test_segments_to_transcript_indexes(self):
        transcript = segments_to_transcript(sample_segments())
        assert "0: others: What is the plan?" in transcript
        assert "2: others: Approved." in transcript


# ---------------------------------------------------------------------------
# durable store
# ---------------------------------------------------------------------------


class TestMeetingIngestStore:
    def test_upsert_and_get(self, store):
        store.upsert(sample_record())
        row = store.get("mtg-test-1")
        assert row["interaction_kind"] == "meeting"
        assert row["state"] == STATE_QUEUED
        assert row["stage"] == STAGE_UPLOADING
        assert row["attempts"] == 0
        assert row["v271_graph_meeting_id"] is None

    def test_upsert_updates_in_place(self, store):
        store.upsert(sample_record(revision=1, title="Old"))
        store.upsert(sample_record(revision=2, title="New"))
        rows = store.list_all()
        assert len(rows) == 1
        assert rows[0]["title"] == "New"
        assert rows[0]["revision"] == 2

    def test_canonical_record_roundtrip(self, store):
        record = sample_record()
        store.upsert(record)
        restored = store.get_record("mtg-test-1")
        assert restored["meeting_id"] == record["meeting_id"]
        assert restored["transcript_segments"] == record["transcript_segments"]

    def test_state_transitions_and_graph_id(self, store):
        store.upsert(sample_record())
        store.set_state("mtg-test-1", STATE_GRAPH_WRITTEN, stage=STAGE_GRAPH_WRITTEN)
        row = store.get("mtg-test-1")
        assert row["state"] == STATE_GRAPH_WRITTEN
        assert row["stage"] == STAGE_GRAPH_WRITTEN
        store.mark_completed("mtg-test-1", "graph-42")
        row = store.get("mtg-test-1")
        assert row["state"] == STATE_COMPLETED
        assert row["v271_graph_meeting_id"] == "graph-42"

    def test_failed_retry_resumes_stage(self, store):
        store.upsert(sample_record())
        store.set_state("mtg-test-1", STATE_FAILED, stage=STAGE_INDEXING,
                        last_error="boom")
        assert store.retry("mtg-test-1") is True
        row = store.get("mtg-test-1")
        assert row["state"] == STATE_QUEUED
        assert row["stage"] == STAGE_INDEXING  # resume from failed stage
        assert row["last_error"] is None

    def test_retry_requires_failed(self, store):
        store.upsert(sample_record())
        assert store.retry("mtg-test-1") is False

    def test_queued_oldest_first(self, store):
        store.upsert(sample_record(meeting_id="mtg-1"))
        time.sleep(0.01)
        store.upsert(sample_record(meeting_id="mtg-2"))
        queued = store.queued()
        assert [q["meeting_id"] for q in queued] == ["mtg-1", "mtg-2"]

    def test_bump_attempt(self, store):
        store.upsert(sample_record())
        assert store.bump_attempt("mtg-test-1") == 1
        assert store.bump_attempt("mtg-test-1") == 2


# ---------------------------------------------------------------------------
# V271 API client
# ---------------------------------------------------------------------------


class TestV271ApiClient:
    def test_upload_uses_put_with_bearer(self):
        session = FakeSession(FakeResp({"ok": True, "graphMeetingId": "g1"}))
        client = make_client(session)
        client.upload_meeting(sample_record())
        call = session.calls[0]
        assert call["method"] == "PUT"
        assert call["url"] == "https://v271.cz/api/v1/meetings/mtg-test-1"
        assert call["headers"]["Authorization"] == "Bearer tok-123"
        assert call["body"]["meeting_id"] == "mtg-test-1"
        # Authority never carries user_id.
        assert "user_id" not in call["body"]

    def test_graph_and_vectors_endpoints(self):
        session = FakeSession(FakeResp(), FakeResp({"ok": True, "indexed": 4}))
        client = make_client(session)
        client.write_graph(build_graph_payload(sample_record()))
        client.index_vectors(build_vector_payload(sample_record()))
        assert session.calls[0]["url"].endswith("/meetings/mtg-test-1/graph")
        assert session.calls[1]["url"].endswith("/meetings/mtg-test-1/vectors")

    def test_409_conflict_is_idempotent_success(self):
        session = FakeSession(FakeResp({"ok": False, "error": "older revision"},
                                       status_code=409))
        client = make_client(session)
        result = client.upload_meeting(sample_record())
        # The server already holds this meeting: upsert satisfied.
        assert result["ok"] is True
        assert result.get("conflict") is True

    def test_http_error_raises_retryable(self):
        session = FakeSession(FakeResp({}, status_code=500))
        client = make_client(session)
        with pytest.raises(V271ApiError):
            client.upload_meeting(sample_record())

    def test_network_error_raises(self):
        session = FakeSession(ConnectionError("refused"))
        client = make_client(session)
        with pytest.raises(V271ApiError):
            client.upload_meeting(sample_record())

    def test_missing_token_raises(self):
        client = V271ApiClient("https://v271.cz/api/v1", "", session=FakeSession())
        with pytest.raises(V271ApiError) as exc:
            client.upload_meeting(sample_record())
        assert "token" in str(exc.value).lower()

    def test_non_json_response_raises(self):
        session = FakeSession(FakeResp({}, status_code=200))
        session.responses = []  # replaced below via raw double

        class RawResp:
            status_code = 200
            text = "<html>"

            def json(self):
                raise ValueError("no json")

        client = make_client(FakeSession(RawResp()))
        with pytest.raises(V271ApiError):
            client.upload_meeting(sample_record())


class TestGraphPayload:
    def test_entities_and_links(self):
        record = sample_record()
        payload = build_graph_payload(record)
        assert payload["meetingId"] == "mtg-test-1"
        entities = payload["entities"]
        assert entities["meeting"]["externalId"] == "mtg-test-1"
        assert len(entities["speakers"]) == 2
        assert len(entities["segments"]) == 3
        assert len(entities["decisions"]) == 1
        assert len(entities["actionItems"]) == 1
        types = [link["type"] for link in payload["links"]]
        assert types.count("HAS_SPEAKER") == 2
        assert types.count("HAS_SEGMENT") == 3
        assert "DECIDED" in types
        assert "HAS_ACTION_ITEM" in types

    def test_participant_identity_hints(self):
        payload = build_graph_payload(sample_record())
        hints = payload["participantIdentityHints"]
        assert hints[0]["email"] == "jan@x.cz"
        # Only stable identities travel; no name-merging is ever requested.
        assert all("name" in h for h in hints)

    def test_calendar_and_mail_links(self):
        record = sample_record(source_calendar_event_id="cal-7",
                               source_mail_thread_id="thr-9")
        payload = build_graph_payload(record)
        assert payload["calendarEventId"] == "cal-7"
        assert payload["mailThreadId"] == "thr-9"

    def test_no_calendar_or_mail_by_default(self):
        payload = build_graph_payload(sample_record())
        assert payload["calendarEventId"] is None
        assert payload["mailThreadId"] is None


class TestVectorPayload:
    def test_content_types(self):
        payload = build_vector_payload(sample_record())
        kinds = {item["entityType"] for item in payload["items"]}
        assert "meeting.segment" in kinds
        assert "meeting.summary" in kinds
        assert "meeting.decision" in kinds
        assert "meeting.action_item" in kinds

    def test_segment_metadata(self):
        # chunk_chars=1 keeps every short segment as its own vector item
        # (default chunking merges adjacent segments into chunks).
        payload = build_vector_payload(sample_record(), chunk_chars=1)
        segment_items = [i for i in payload["items"]
                         if i["entityType"] == "meeting.segment"]
        first = segment_items[0]
        assert first["segmentId"] == "seg-0000"
        assert first["speakerId"] == "others"
        assert first["startMs"] == 0
        assert first["endMs"] == 1200

    def test_merged_chunk_keeps_segment_ids(self):
        payload = build_vector_payload(sample_record())
        merged = [i for i in payload["items"]
                  if i["entityType"] == "meeting.segment"
                  and "segmentIds" in i]
        assert merged
        assert "seg-0000" in merged[0]["segmentIds"]
        assert merged[0]["speakerIds"] == ["me", "others"]

    def test_decision_uses_source_segment_id(self):
        payload = build_vector_payload(sample_record())
        decision = next(i for i in payload["items"]
                        if i["entityType"] == "meeting.decision")
        assert decision["segmentId"] == "seg-0001"  # sourceSegmentIds[0] == 1

    def test_owner_user_id_only_when_configured(self):
        payload = build_vector_payload(sample_record())
        assert "ownerUserId" not in payload
        payload = build_vector_payload(sample_record(), owner_user_id="u-1")
        assert payload["ownerUserId"] == "u-1"

    def test_chunking_respects_segment_boundaries(self):
        record = sample_record()
        # Force chunking by a tiny budget: two chunks expected.
        payload = build_vector_payload(record, chunk_chars=30)
        chunks = [i for i in payload["items"]
                  if i["entityType"] == "meeting.segment"]
        assert len(chunks) >= 2


# ---------------------------------------------------------------------------
# coordinator (durable state machine)
# ---------------------------------------------------------------------------


class TestCoordinator:
    def test_full_flow_completes(self, store):
        session = FakeSession(
            FakeResp({"ok": True, "graphMeetingId": "graph-9"}),
            FakeResp({"ok": True, "graphMeetingId": "graph-9"}),
            FakeResp({"ok": True, "indexed": 5}),
        )
        client = make_client(session)
        events = []
        coord = V271IngestCoordinator(store, client, on_event=events.append,
                                      poll_sec=0.05)
        coord.start()
        try:
            coord.enqueue(sample_record())
            _wait_for(coord, "mtg-test-1", STATE_COMPLETED)
            status = coord.status("mtg-test-1")
            assert status["state"] == STATE_COMPLETED
            assert status["label"] == "Indexed"
            assert status["graphMeetingId"] == "graph-9"
            assert status["attempts"] == 1
            assert [c["method"] for c in session.calls] == [
                "PUT", "POST", "POST"]
            assert [e["state"] for e in events] == [
                STATE_QUEUED, "UPLOADING", STATE_GRAPH_WRITTEN,
                STATE_INDEXING, STATE_COMPLETED]
            # Memory compatibility: the graph id is stored back locally.
            row = store.get("mtg-test-1")
            assert row["v271_graph_meeting_id"] == "graph-9"
        finally:
            coord.stop()

    def test_upload_failure_fails_and_retry_resumes(self, store):
        session = FakeSession(
            FakeResp({}, status_code=500),          # upload fails
            FakeResp({"ok": True, "graphMeetingId": "graph-9"}),
            FakeResp({"ok": True}),
            FakeResp({"ok": True, "indexed": 5}),
        )
        client = make_client(session)
        coord = V271IngestCoordinator(store, client, poll_sec=0.05)
        coord.start()
        try:
            coord.enqueue(sample_record())
            _wait_for(coord, "mtg-test-1", STATE_FAILED)
            status = coord.status("mtg-test-1")
            assert status["state"] == STATE_FAILED
            assert status["label"] == "Sync failed"
            assert status["retryable"] is True
            assert status["lastError"]

            assert coord.retry("mtg-test-1") is True
            _wait_for(coord, "mtg-test-1", STATE_COMPLETED)
            status = coord.status("mtg-test-1")
            assert status["state"] == STATE_COMPLETED
            assert status["attempts"] == 2
            # Upload (2 calls), graph (1), vectors (1).
            assert len(session.calls) == 4
        finally:
            coord.stop()

    def test_retry_resumes_from_failed_stage(self, store):
        """A failure at INDEXING must not re-upload or re-write the graph."""
        session = FakeSession(
            FakeResp({"ok": True, "graphMeetingId": "graph-9"}),
            FakeResp({"ok": True}),
            FakeResp({}, status_code=503),           # vectors fail
            FakeResp({"ok": True, "indexed": 5}),    # retry: vectors only
        )
        client = make_client(session)
        coord = V271IngestCoordinator(store, client, poll_sec=0.05)
        coord.start()
        try:
            coord.enqueue(sample_record())
            _wait_for(coord, "mtg-test-1", STATE_FAILED)
            status = coord.status("mtg-test-1")
            assert status["stage"] == STAGE_INDEXING
            assert coord.retry("mtg-test-1") is True
            _wait_for(coord, "mtg-test-1", STATE_COMPLETED)
            calls = [c["url"] for c in session.calls]
            assert sum(1 for u in calls if u.endswith("/vectors")) == 2
            assert sum(1 for u in calls if u.endswith("/meetings/mtg-test-1")
                       and "/graph" not in u and "/vectors" not in u) == 1
            assert sum(1 for u in calls if u.endswith("/graph")) == 1
        finally:
            coord.stop()

    def test_idempotent_refinalization_no_duplicate(self, store):
        session = FakeSession(
            FakeResp({"ok": True, "graphMeetingId": "graph-9"}),
            FakeResp({"ok": True}),
            FakeResp({"ok": True, "indexed": 5}),
        )
        client = make_client(session)
        coord = V271IngestCoordinator(store, client, poll_sec=0.05)
        coord.start()
        try:
            coord.enqueue(sample_record())
            _wait_for(coord, "mtg-test-1", STATE_COMPLETED)
            # Re-finalization with a bumped revision: still one row.
            coord.enqueue(sample_record(revision=2))
            assert len(store.list_all()) == 1
            row = store.get("mtg-test-1")
            assert row["revision"] == 2
            assert row["state"] == STATE_COMPLETED
        finally:
            coord.stop()

    def test_disabled_no_client_keeps_queued(self, store):
        coord = V271IngestCoordinator(store, None, poll_sec=0.05)
        coord.start()
        try:
            coord.enqueue(sample_record())
            time.sleep(0.3)
            status = coord.status("mtg-test-1")
            assert status["state"] == STATE_QUEUED
            assert status["label"] == "Saved locally"
            assert coord.is_enabled() is False
        finally:
            coord.stop()

    def test_unknown_meeting_status(self, store):
        coord = V271IngestCoordinator(store, None)
        assert coord.status("ghost") is None
        assert coord.retry("ghost") is False


def _wait_for(coord, meeting_id, state, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = coord.status(meeting_id)
        if status is not None and status["state"] == state:
            return
        time.sleep(0.02)
    raise AssertionError(
        f"timed out waiting for {meeting_id} -> {state}; "
        f"last={coord.status(meeting_id)}")


# ---------------------------------------------------------------------------
# coach integration
# ---------------------------------------------------------------------------


def _stub_daemon_module(monkeypatch, ingest):
    """Seed a fake ``jarvis.daemon`` module so the coach's lazy imports
    work without loading the real daemon (heavy deps)."""
    mod = types.ModuleType("jarvis.daemon")
    mod.record_coach_session = lambda *a, **k: True
    mod.get_v271_ingest = lambda: ingest
    monkeypatch.setitem(sys.modules, "jarvis.daemon", mod)


class FakeWhisperModel:
    def __init__(self, texts):
        self._texts = list(texts)

    def transcribe(self, pcm16, beam_size=1, vad_filter=False):
        text, logprob = self._texts.pop(0) if self._texts else ("", None)
        seg = SimpleNamespace(text=text, avg_logprob=logprob)
        return iter([seg]), SimpleNamespace()


def _make_coach(llm_chat, model_texts):
    from jarvis.everywhere.coach import InterviewCoach

    cfg = SimpleNamespace(v271_source_device="PC-TEST")
    listener = SimpleNamespace(model=FakeWhisperModel(model_texts))
    return InterviewCoach(cfg, listener=listener, llm_chat=llm_chat)


class TestCoachIngestion:
    def test_utterances_become_segments(self):
        coach = _make_coach(lambda s, u: "hint",
                            [("What is the plan?", -0.3),
                             ("We ship next week.", -0.2)])
        coach._meeting_id = "mtg-coach-1"
        coach._t0 = time.monotonic()
        t0 = time.monotonic()
        coach._on_utterance("others", [0.1] * 160, 16000, t0, t0 + 1.0)
        coach._on_utterance("me", [0.1] * 160, 16000, t0 + 2.0, t0 + 3.5)
        assert len(coach._segments) == 2
        seg0, seg1 = coach._segments
        assert seg0["speakerId"] == "others"
        assert seg0["confidence"] == pytest.approx(-0.3)
        assert seg0["startMs"] == 0
        assert seg0["endMs"] == 1000
        assert seg1["speakerId"] == "me"
        assert seg1["startMs"] == 2000
        assert seg1["endMs"] == 3500

    def test_summarize_enqueues_canonical_record(self, monkeypatch):
        class FakeIngest:
            def __init__(self):
                self.records = []

            def enqueue(self, record):
                self.records.append(record)
                return True

        ingest = FakeIngest()
        _stub_daemon_module(monkeypatch, ingest)
        coach = _make_coach(
            lambda s, u: ('{"summary": "S", "decisions": [], "actionItems": '
                          '[], "topics": ["T"]}'),
            [("What is the plan?", -0.3), ("We ship next week.", -0.2)])
        coach._meeting_id = "mtg-coach-2"
        coach._title = "Sync"
        coach._t0 = time.monotonic()
        coach._session_started_at = "2026-09-28T08:00:00Z"
        t0 = time.monotonic()
        coach._on_utterance("others", [0.1] * 160, 16000, t0, t0 + 1.0)
        coach._on_utterance("me", [0.1] * 160, 16000, t0 + 2.0, t0 + 3.5)

        summary = coach.summarize()
        assert summary == "S"
        assert len(ingest.records) == 1
        record = ingest.records[0]
        assert record["meeting_id"] == "mtg-coach-2"
        assert record["title"] == "Sync"
        assert record["source_device"] == "PC-TEST"
        assert len(record["transcript_segments"]) == 2
        assert record["provenance"]["source"] == "toastovac-coach"

    def test_summarize_no_ingest_when_disabled(self, monkeypatch):
        _stub_daemon_module(monkeypatch, None)
        coach = _make_coach(lambda s, u: "plain summary text",
                            [("What is the plan?", -0.3)])
        coach._meeting_id = "mtg-coach-3"
        coach._t0 = time.monotonic()
        t0 = time.monotonic()
        coach._on_utterance("others", [0.1] * 160, 16000, t0, t0 + 1.0)
        # No crash; summary still returned from the fallback path.
        summary = coach.summarize()
        assert summary  # honest fallback (raw transcript tail)

    def test_retry_and_status_delegate(self, monkeypatch):
        class FakeIngest:
            def retry(self, meeting_id):
                return meeting_id == "mtg-1"

            def status(self, meeting_id):
                if meeting_id == "mtg-1":
                    return {"meetingId": meeting_id, "state": "COMPLETED"}
                return None

            def list_status(self):
                return [{"meetingId": "mtg-1", "state": "QUEUED"}]

        ingest = FakeIngest()
        _stub_daemon_module(monkeypatch, ingest)
        coach = _make_coach(lambda s, u: "x", [])
        assert coach.retry_ingest("mtg-1") is True
        assert coach.retry_ingest("mtg-2") is False
        statuses = coach.ingest_status()
        assert statuses[0]["meetingId"] == "mtg-1"
        assert coach.ingest_status("mtg-1")[0]["state"] == "COMPLETED"
        assert coach.ingest_status("ghost") == []


class TestConfigDefaults:
    def test_v271_defaults_present(self, tmp_path, monkeypatch):
        import os

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))
        from jarvis.config import load_settings

        settings = load_settings()
        assert settings.v271_ingest_enabled is True
        assert settings.v271_api_base_url == "https://v271.cz/api/v1"
        assert settings.v271_api_token == ""
        assert settings.v271_owner_user_id == ""
        assert settings.v271_ingest_timeout_sec == 60.0

    def test_token_from_environment(self, tmp_path, monkeypatch):
        import os

        cfg_path = tmp_path / "config.json"
        cfg_path.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("JARVIS_CONFIG_PATH", str(cfg_path))
        monkeypatch.setenv("V271_API_TOKEN", "env-tok")
        from jarvis.config import load_settings

        assert load_settings().v271_api_token == "env-tok"