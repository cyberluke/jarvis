"""TerminalSessionMemory (K6 + L1/L4 remediation).

Key (L1): provider | terminal_session_id | process_created_ns |
          remote_kind | remote_authority
Two Remote-SSH hosts therefore cannot collide: distinct authorities
produce distinct keys even when remoteName ('ssh-remote') matches.
Legacy pre-L1 keys (4-part) are never reinterpreted: a version marker
in each bucket invalidates them on read.

Rules (§TerminalSessionMemory, L4):
  * promotion ONLY from a real execution record with exit_code == 0
    and a sanitized output tail; proposed != executed is stored as two
    hashes (plaintext bodies stay out of logs);
  * missing/None exit code is 'unknown', never success;
  * closing a terminal (or a stale creation identity) expires the lease;
  * ambiguous pronoun resolution requires exactly one ranked candidate;
  * in-memory TTL-bound by default.
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional, Tuple

from ..debug import debug_log
from .command_policy import proposal_hash
from .models import EntityRecord, TerminalExecutionRecord

_KEY_VERSION = 2  # bump invalidates older buckets on read


def make_key(
    provider: str,
    session_id: str,
    process_created_ns: Optional[int],
    remote_kind: Optional[str],
    remote_authority: Optional[str],
) -> str:
    return "|".join(
        str(x) for x in (
            provider or "unknown",
            session_id or "-",
            process_created_ns or 0,
            remote_kind or "-",
            remote_authority or "-",
        )
    )


def key_hash(key: str) -> str:
    import hashlib
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


class TerminalSessionMemory:
    def __init__(self, ttl_sec: float = 3600.0) -> None:
        self._ttl = float(ttl_sec)
        #: key → {version, entities, stamp}
        self._store: Dict[str, Dict[str, object]] = {}

    # ── writes ────────────────────────────────────────────────────────
    def record_execution(
        self,
        key: str,
        record: TerminalExecutionRecord,
        extracted: List[Tuple[str, str, float]],
    ) -> None:
        """Promote entities from one real execution (M0.4 kind semantics).

        native: exit_code == 0 is authoritative.
        powershell: captured success is authoritative (exit may be None).
        unknown: never promote.
        """
        bucket = self._store.setdefault(
            key, {"version": _KEY_VERSION, "entities": {}, "stamp": 0.0}
        )
        bucket["stamp"] = time.monotonic()
        kind = (record.execution_kind or "unknown").lower()
        if kind == "native":
            if record.exit_code != 0:
                return
        elif kind == "powershell":
            if record.success is not True:
                return
        else:
            return  # unknown kind → no promotion
        if not extracted:
            return
        if not record.output_tail and not extracted:
            pass
        ents: Dict[str, List[EntityRecord]] = bucket["entities"]  # type: ignore
        cmd_id = proposal_hash(record.executed_command)[:8]
        for ek, ev, conf in extracted:
            ents.setdefault(ek, []).append(
                EntityRecord(
                    key=ek, value=ev, source=record.command_confidence,
                    command_id=cmd_id, confidence=conf,
                    observed_at_ns=record.ended_at_ns or record.started_at_ns,
                )
            )
            ents[ek] = sorted(
                ents[ek], key=lambda r: -r.observed_at_ns
            )[:3]
        debug_log(
            f"terminal.memory.entity_promoted n={len(extracted)} "
            f"key={key_hash(key)}",
            "terminal",
        )

    def expire(self, key: str) -> None:
        self._store.pop(key, None)

    # ── reads ─────────────────────────────────────────────────────────
    def _alive(self, key: str) -> Optional[Dict[str, object]]:
        bucket = self._store.get(key)
        if not bucket:
            return None
        if int(bucket.get("version", 0)) != _KEY_VERSION:
            self._store.pop(key, None)  # legacy bucket: invalidate (§L1)
            return None
        if time.monotonic() - float(bucket.get("stamp", 0.0)) > self._ttl:
            self._store.pop(key, None)
            return None
        return bucket

    def resolve(self, key: str, entity_key: str) -> Optional[EntityRecord]:
        """Unique candidate or None; ambiguity fails closed (§pronouns)."""
        bucket = self._alive(key)
        if not bucket:
            return None
        cands: List[EntityRecord] = bucket["entities"].get(entity_key, [])  # type: ignore
        if not cands:
            return None
        values = {c.value for c in cands}
        if len(values) > 1:
            return None  # ambiguous → composer must ask
        return cands[0]

    def memory_block(self, key: str) -> str:
        """Compact VERIFIED_TERMINAL_MEMORY injection for one session."""
        bucket = self._alive(key)
        if not bucket:
            return ""
        ents: Dict[str, List[EntityRecord]] = bucket["entities"]  # type: ignore
        lines: List[str] = []
        for ek, cands in ents.items():
            values = {c.value for c in cands}
            if len(values) == 1:
                lines.append(f"{ek}={next(iter(values))}")
            else:
                lines.append(f"{ek}=?({len(values)})")
        return "\n".join(lines)
