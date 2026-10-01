"""Durable V271 meeting ingestion coordinator.

One background worker thread drains QUEUED jobs through the state
machine (spec): QUEUED → UPLOADING → GRAPH_WRITTEN → INDEXING →
COMPLETED, with FAILED on any stage error. A retry re-queues the job
and the worker resumes from the failed stage, skipping work V271
already acknowledged. Every transition emits a ``meeting_status``
event (via the ``on_event`` callback) for the coach overlay UX.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional

from ..debug import debug_log
from .client import V271ApiClient, build_graph_payload, build_vector_payload
from .store import (
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_GRAPH_WRITTEN,
    STATE_INDEXING,
    STATE_QUEUED,
    STATE_UPLOADING,
    STAGE_GRAPH_WRITTEN,
    STAGE_INDEXING,
    STAGE_UPLOADING,
    MeetingIngestStore,
)

#: Status UX labels (spec): Indexed only after vector indexing completed.
LABEL_BY_STATE = {
    STATE_QUEUED: "Saved locally",
    STATE_UPLOADING: "Syncing to V271",
    STATE_GRAPH_WRITTEN: "Syncing to V271",
    STATE_INDEXING: "Syncing to V271",
    STATE_COMPLETED: "Indexed",
    STATE_FAILED: "Sync failed",
}

_WORKER_POLL_SEC = 2.0


class V271IngestCoordinator:
    """Owns the store + API client and runs the durable state machine."""

    def __init__(
        self,
        store: MeetingIngestStore,
        client: Optional[V271ApiClient] = None,
        *,
        owner_user_id: Optional[str] = None,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        poll_sec: float = _WORKER_POLL_SEC,
    ) -> None:
        self._store = store
        self._client = client  # None = disabled (no token) until configured
        self._owner_user_id = owner_user_id or None
        self._on_event = on_event
        self._poll_sec = max(0.1, poll_sec)
        self._cond = threading.Condition()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._worker, name="v271-ingest", daemon=True
        )
        self._thread.start()
        debug_log("v271 ingest coordinator started", "v271_ingest")

    def stop(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)
        self._thread = None

    def set_client(self, client: Optional[V271ApiClient]) -> None:
        """Attach (or detach) the API client after construction."""
        self._client = client

    def set_on_event(self, callback: Optional[Callable[[Dict[str, Any]], None]]) -> None:
        self._on_event = callback

    # -- public API -----------------------------------------------------

    def enqueue(self, record: Dict[str, Any]) -> bool:
        """Persist a meeting record and queue it for ingestion.

        Returns True when the job was queued (or already COMPLETED and
        just refreshed); False when ingestion is disabled (no client).
        Re-finalization bumps the revision and re-queues from the
        failed stage when the previous attempt failed.
        """
        meeting_id = str(record.get("meeting_id") or "")
        existing = self._store.get(meeting_id)
        if existing is not None and existing["state"] == STATE_COMPLETED:
            # Idempotent re-finalization: update payload, keep COMPLETED.
            self._store.upsert(record, state=STATE_COMPLETED, stage=STAGE_UPLOADING)
            self._emit(meeting_id, STATE_COMPLETED)
            return True
        if existing is not None and existing["state"] == STATE_FAILED:
            self._store.upsert(record, state=STATE_QUEUED, stage=existing["stage"])
        else:
            self._store.upsert(record, state=STATE_QUEUED, stage=STAGE_UPLOADING)
        self._emit(meeting_id, STATE_QUEUED)
        with self._cond:
            self._cond.notify_all()
        if self._client is None:
            debug_log(
                f"v271 ingest: {meeting_id} queued but ingestion is disabled "
                "(no API token configured)",
                "v271_ingest",
            )
        return True

    def retry(self, meeting_id: str) -> bool:
        """Re-queue a FAILED job, resuming from its failed stage."""
        if not self._store.retry(meeting_id):
            return False
        self._emit(meeting_id, STATE_QUEUED)
        with self._cond:
            self._cond.notify_all()
        return True

    def status(self, meeting_id: str) -> Optional[Dict[str, Any]]:
        row = self._store.get(meeting_id)
        if row is None:
            return None
        return {
            "meetingId": meeting_id,
            "title": row["title"],
            "state": row["state"],
            "label": LABEL_BY_STATE.get(row["state"], row["state"]),
            "stage": row["stage"],
            "attempts": row["attempts"],
            "lastError": row["last_error"],
            "graphMeetingId": row["v271_graph_meeting_id"],
            "revision": row["revision"],
            "retryable": row["state"] == STATE_FAILED,
        }

    def list_status(self, limit: int = 50) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for row in self._store.list_all(limit):
            status = self.status(row["meeting_id"])
            if status is not None:
                out.append(status)
        return out

    def is_enabled(self) -> bool:
        return self._client is not None

    # -- worker ---------------------------------------------------------

    def _worker(self) -> None:
        while not self._stop.is_set():
            processed = False
            if self._client is not None:
                for job in self._store.queued():
                    if self._stop.is_set():
                        break
                    self._run_job(job)
                    processed = True
            if not processed:
                with self._cond:
                    self._cond.wait(timeout=self._poll_sec)

    def _run_job(self, job: Dict[str, Any]) -> None:
        meeting_id = job["meeting_id"]
        record = self._store.get_record(meeting_id)
        if record is None:
            self._store.set_state(
                meeting_id, STATE_FAILED, last_error="canonical record corrupt"
            )
            self._emit(meeting_id, STATE_FAILED)
            return
        client = self._client
        if client is None:
            return
        stage = job.get("stage") or STAGE_UPLOADING
        self._store.bump_attempt(meeting_id)
        graph_meeting_id: Optional[str] = job.get("v271_graph_meeting_id")

        try:
            if stage == STAGE_UPLOADING:
                self._store.set_state(meeting_id, STATE_UPLOADING)
                self._emit(meeting_id, STATE_UPLOADING)
                resp = client.upload_meeting(record)
                graph_meeting_id = resp.get("graphMeetingId") or graph_meeting_id
                self._store.set_state(
                    meeting_id,
                    STATE_GRAPH_WRITTEN,
                    stage=STAGE_GRAPH_WRITTEN,
                    graph_meeting_id=graph_meeting_id,
                )
                self._emit(meeting_id, STATE_GRAPH_WRITTEN)
                stage = STAGE_GRAPH_WRITTEN

            if stage == STAGE_GRAPH_WRITTEN:
                graph_payload = build_graph_payload(
                    record, owner_user_id=self._owner_user_id
                )
                resp = client.write_graph(graph_payload)
                graph_meeting_id = (
                    resp.get("graphMeetingId") or graph_meeting_id
                )
                self._store.set_state(
                    meeting_id,
                    STATE_INDEXING,
                    stage=STAGE_INDEXING,
                    graph_meeting_id=graph_meeting_id,
                )
                self._emit(meeting_id, STATE_INDEXING)
                stage = STAGE_INDEXING

            if stage == STAGE_INDEXING:
                vector_payload = build_vector_payload(
                    record, owner_user_id=self._owner_user_id
                )
                client.index_vectors(vector_payload)
                graph_id = graph_meeting_id or meeting_id
                self._store.mark_completed(meeting_id, graph_id)
                self._emit(meeting_id, STATE_COMPLETED)
                debug_log(
                    f"v271 ingest: {meeting_id} completed "
                    f"(graphMeetingId={graph_id})",
                    "v271_ingest",
                )
        except Exception as exc:  # noqa: BLE001
            message = str(exc) or type(exc).__name__
            self._store.set_state(
                meeting_id,
                STATE_FAILED,
                stage=stage,
                last_error=message,
                graph_meeting_id=graph_meeting_id,
            )
            self._emit(meeting_id, STATE_FAILED)
            debug_log(
                f"v271 ingest: {meeting_id} failed at {stage}: {message}",
                "v271_ingest",
            )

    def _emit(self, meeting_id: str, state: str) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event({
                "type": "meeting_status",
                "meetingId": meeting_id,
                "state": state,
                "label": LABEL_BY_STATE.get(state, state),
            })
        except Exception as exc:  # noqa: BLE001
            debug_log(f"v271 ingest: status event callback failed: {exc}",
                      "v271_ingest")