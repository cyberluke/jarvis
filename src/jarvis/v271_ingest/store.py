"""Durable local store for V271 meeting ingestion jobs.

One row per coach meeting in ``meeting_ingestions`` with
``interaction_kind = 'meeting'``. The full canonical record lives as
JSON on the row, so a failed ingestion is retryable from the failed
stage without re-recording or re-transcribing (spec: the meeting must
remain recoverable locally if graph ingestion fails).

All access goes through the shared ``Database`` connection + lock.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from ..debug import debug_log

#: Job states (spec): durable, ordered.
STATE_QUEUED = "QUEUED"
STATE_UPLOADING = "UPLOADING"
STATE_GRAPH_WRITTEN = "GRAPH_WRITTEN"
STATE_INDEXING = "INDEXING"
STATE_COMPLETED = "COMPLETED"
STATE_FAILED = "FAILED"

ALL_STATES = (
    STATE_QUEUED,
    STATE_UPLOADING,
    STATE_GRAPH_WRITTEN,
    STATE_INDEXING,
    STATE_COMPLETED,
    STATE_FAILED,
)

#: Stages the worker resumes from after a failure.
STAGE_UPLOADING = "UPLOADING"
STAGE_GRAPH_WRITTEN = "GRAPH_WRITTEN"
STAGE_INDEXING = "INDEXING"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class MeetingIngestStore:
    """SQLite-backed meeting ingestion jobs over the shared Database."""

    def __init__(self, db: Any) -> None:
        self._db = db

    # -- writes ---------------------------------------------------------

    def upsert(
        self,
        record: Dict[str, Any],
        *,
        state: str = STATE_QUEUED,
        stage: str = STAGE_UPLOADING,
    ) -> None:
        """Insert or update the job for ``record["meeting_id"]``.

        Re-finalization (same meeting id, bumped revision) updates the
        canonical payload in place — the meeting ID is the root
        idempotency key.
        """
        meeting_id = str(record.get("meeting_id") or "")
        if not meeting_id:
            raise ValueError("record requires meeting_id")
        now = _now()
        with self._db._lock:
            cur = self._db.conn.cursor()
            cur.execute(
                """
                INSERT INTO meeting_ingestions(
                  meeting_id, interaction_kind, title, started_at, ended_at,
                  source_device, canonical_json, revision, state, stage,
                  attempts, last_error, v271_graph_meeting_id,
                  source_calendar_event_id, source_mail_thread_id,
                  created_at, updated_at)
                VALUES (?, 'meeting', ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL,
                        ?, ?, ?, ?)
                ON CONFLICT(meeting_id) DO UPDATE SET
                  title = excluded.title,
                  started_at = excluded.started_at,
                  ended_at = excluded.ended_at,
                  source_device = excluded.source_device,
                  canonical_json = excluded.canonical_json,
                  revision = excluded.revision,
                  state = excluded.state,
                  stage = excluded.stage,
                  source_calendar_event_id = excluded.source_calendar_event_id,
                  source_mail_thread_id = excluded.source_mail_thread_id,
                  updated_at = excluded.updated_at
                """,
                (
                    meeting_id,
                    str(record.get("title") or ""),
                    str(record.get("started_at") or ""),
                    str(record.get("ended_at") or ""),
                    str(record.get("source_device") or ""),
                    json.dumps(record, ensure_ascii=False),
                    int(record.get("revision") or 1),
                    state,
                    stage,
                    record.get("sourceCalendarEventId"),
                    record.get("sourceMailThreadId"),
                    now,
                    now,
                ),
            )
            self._db.conn.commit()

    def set_state(
        self,
        meeting_id: str,
        state: str,
        *,
        stage: Optional[str] = None,
        last_error: Optional[str] = None,
        graph_meeting_id: Optional[str] = None,
    ) -> None:
        if state not in ALL_STATES:
            raise ValueError(f"unknown state {state!r}")
        now = _now()
        with self._db._lock:
            cur = self._db.conn.cursor()
            cur.execute(
                """
                UPDATE meeting_ingestions
                SET state = ?, stage = COALESCE(?, stage),
                    last_error = COALESCE(?, last_error),
                    v271_graph_meeting_id = COALESCE(?, v271_graph_meeting_id),
                    updated_at = ?
                WHERE meeting_id = ?
                """,
                (state, stage, last_error, graph_meeting_id, now, meeting_id),
            )
            self._db.conn.commit()

    def bump_attempt(self, meeting_id: str) -> int:
        now = _now()
        with self._db._lock:
            cur = self._db.conn.cursor()
            cur.execute(
                """
                UPDATE meeting_ingestions
                SET attempts = attempts + 1, updated_at = ?
                WHERE meeting_id = ?
                """,
                (now, meeting_id),
            )
            self._db.conn.commit()
            row = cur.execute(
                "SELECT attempts FROM meeting_ingestions WHERE meeting_id = ?",
                (meeting_id,),
            ).fetchone()
            return int(row["attempts"]) if row else 0

    def mark_completed(self, meeting_id: str, graph_meeting_id: str) -> None:
        """COMPLETED + store the V271 graph meeting id back into local
        metadata (memory compatibility requirement)."""
        self.set_state(
            meeting_id,
            STATE_COMPLETED,
            stage=None,
            last_error=None,
            graph_meeting_id=graph_meeting_id,
        )

    def retry(self, meeting_id: str) -> bool:
        """Re-queue a FAILED job, resuming from its failed stage."""
        with self._db._lock:
            cur = self._db.conn.cursor()
            row = cur.execute(
                "SELECT state, stage FROM meeting_ingestions WHERE meeting_id = ?",
                (meeting_id,),
            ).fetchone()
            if row is None or row["state"] != STATE_FAILED:
                return False
            cur.execute(
                """
                UPDATE meeting_ingestions
                SET state = ?, last_error = NULL, updated_at = ?
                WHERE meeting_id = ?
                """,
                (STATE_QUEUED, _now(), meeting_id),
            )
            self._db.conn.commit()
            return True

    def delete(self, meeting_id: str) -> bool:
        with self._db._lock:
            cur = self._db.conn.cursor()
            cur.execute(
                "DELETE FROM meeting_ingestions WHERE meeting_id = ?",
                (meeting_id,),
            )
            self._db.conn.commit()
            return cur.rowcount > 0

    # -- reads ----------------------------------------------------------

    def get(self, meeting_id: str) -> Optional[Dict[str, Any]]:
        with self._db._lock:
            row = self._db.conn.cursor().execute(
                "SELECT * FROM meeting_ingestions WHERE meeting_id = ?",
                (meeting_id,),
            ).fetchone()
        return self._row_to_dict(row) if row is not None else None

    def get_record(self, meeting_id: str) -> Optional[Dict[str, Any]]:
        """The canonical record JSON, parsed back."""
        row = self.get(meeting_id)
        if row is None:
            return None
        try:
            data = json.loads(row["canonical_json"])
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, TypeError):
            debug_log(
                f"v271 ingest: canonical record for {meeting_id} is corrupt",
                "v271_ingest",
            )
            return None

    def queued(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Jobs awaiting work (QUEUED), oldest first."""
        with self._db._lock:
            rows = self._db.conn.cursor().execute(
                """
                SELECT * FROM meeting_ingestions
                WHERE state = ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (STATE_QUEUED, int(limit)),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def list_all(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._db._lock:
            rows = self._db.conn.cursor().execute(
                """
                SELECT * FROM meeting_ingestions
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def _row_to_dict(self, row: Any) -> Dict[str, Any]:
        return {
            "meeting_id": row["meeting_id"],
            "interaction_kind": row["interaction_kind"],
            "title": row["title"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "source_device": row["source_device"],
            "canonical_json": row["canonical_json"],
            "revision": row["revision"],
            "state": row["state"],
            "stage": row["stage"],
            "attempts": row["attempts"],
            "last_error": row["last_error"],
            "v271_graph_meeting_id": row["v271_graph_meeting_id"],
            "source_calendar_event_id": row["source_calendar_event_id"],
            "source_mail_thread_id": row["source_mail_thread_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }