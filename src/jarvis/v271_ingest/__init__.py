"""V271 meeting graph ingestion (see ``v271_ingest.spec.md``).

Durable handoff from the Toastovač meeting scribe into the V271
Personal Graph: canonical meeting record, idempotent upload, graph
entities/links, and vector indexing handoff into V271's central
pipeline.
"""

from .coordinator import V271IngestCoordinator
from .client import V271ApiClient, V271ApiError, build_graph_payload, build_vector_payload
from .record import (
    build_meeting_record,
    default_quality,
    default_speakers,
    extract_meeting_structure,
    new_meeting_id,
)
from .store import (
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_GRAPH_WRITTEN,
    STATE_INDEXING,
    STATE_QUEUED,
    STATE_UPLOADING,
    MeetingIngestStore,
)

__all__ = [
    "MeetingIngestStore",
    "V271ApiClient",
    "V271ApiError",
    "V271IngestCoordinator",
    "build_graph_payload",
    "build_meeting_record",
    "build_vector_payload",
    "default_quality",
    "default_speakers",
    "extract_meeting_structure",
    "new_meeting_id",
    "STATE_COMPLETED",
    "STATE_FAILED",
    "STATE_GRAPH_WRITTEN",
    "STATE_INDEXING",
    "STATE_QUEUED",
    "STATE_UPLOADING",
]