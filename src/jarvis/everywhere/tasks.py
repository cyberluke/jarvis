"""Asynchronous Everywhere task manager: queue, worker pool, retry, cache.

The broker stays a thin pipe front-end; all LLM work lives here. Actions are
submitted and return a ``task_id`` immediately; a bounded worker pool runs the
two-tier LLM router concurrently (configurable), retries transient failures
with backoff, reuses cached results (same action + text hash + model +
target/prompt within TTL) and keeps a bounded history the queue overlay reads.

Threading contract:
* ``submit`` / ``cancel`` / ``snapshot`` / ``get`` are thread-safe.
* State transitions happen on the worker that owns the task; every transition
  emits one event through the ``on_event`` callback (the broker broadcasts it
  to subscribed hosts). ``done_event`` lets synchronous callers (the voice
  route) wait for a task they own.
* ``shutdown`` cancels everything and joins the pool within a bounded wait.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..debug import debug_log
from . import proofread as proofread_lib
from . import prompts as prompt_lib
from .model_profiles import resolve_model_for_action
from .snapshots import SelectionSnapshot

#: Task lifecycle states (host queue overlay mirrors this set).
TASK_STATES = ("queued", "running", "streaming", "completed", "failed",
               "cancelled")

#: Transient failures worth a retry (everything else is final).
_RETRYABLE_FAILURES = ("MODEL_UNAVAILABLE", "PROVIDER_UNAVAILABLE",
                       "PIPE_DISCONNECTED")

#: Deterministic actions whose result may be reused verbatim within TTL.
CACHEABLE_ACTIONS = ("translate", "rewrite", "explain", "prompt")

#: Result text shipped inside a ``completed`` event; larger results stay on
#: the broker and are fetched on demand via ``task_result`` (the pipe frame
#: cap is 256 KiB, and escaped CJK can blow past that quickly).
_EVENT_RESULT_CHARS = 30_000

#: Hard cap on a stored result (protects the bounded history from one giant
#: proofread payload evicting everything else).
_MAX_RESULT_CHARS = 240_000


class _Cancelled(Exception):
    """Raised inside the streaming callback when the task is cancelled."""


@dataclass
class EverywhereTask:
    """One queued/running/finished action (the unit the queue UI renders)."""

    task_id: str
    request_id: str
    action: str
    snapshot: SelectionSnapshot
    profile: str
    model: str
    target_language: str = ""
    prompt_id: str = ""
    state: str = "queued"
    result: str = ""
    failure: str = ""
    cache_hit: bool = False
    retry_count: int = 0
    queued_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    first_token_at: Optional[float] = None
    finished_at: Optional[float] = None
    #: Synchronous waiters (voice route) block on this.
    done_event: threading.Event = field(
        default_factory=threading.Event, repr=False)
    #: Cancellation flag; the streaming callback polls it.
    cancel: threading.Event = field(
        default_factory=threading.Event, repr=False)

    @property
    def result_mode(self) -> str:
        from .actions import ACTION_RESULT_MODES
        return ACTION_RESULT_MODES.get(self.action, "panel")

    @property
    def snapshot_id(self) -> str:
        return self.snapshot.snapshot_id

    @property
    def duration_ms(self) -> Optional[int]:
        if self.started_at is None or self.finished_at is None:
            return None
        return int((self.finished_at - self.started_at) * 1000)

    @property
    def first_token_ms(self) -> Optional[int]:
        if self.started_at is None or self.first_token_at is None:
            return None
        return int((self.first_token_at - self.started_at) * 1000)


class ResultCache:
    """Bounded LRU + TTL result cache (memory only, session-scoped).

    Reusing a translation/rewrite of the *same* selection+model+language is
    the single biggest win for a multitasking user: the queue drains in
    milliseconds instead of re-running the model.
    """

    def __init__(self, cfg) -> None:
        self._enabled = bool(getattr(cfg, "everywhere_cache_enabled", True))
        self._max_entries = max(
            1, int(getattr(cfg, "everywhere_cache_max_entries", 200) or 200))
        self._ttl_sec = float(
            getattr(cfg, "everywhere_cache_ttl_sec", 86400.0) or 86400.0)
        self._lock = threading.Lock()
        self._entries: "OrderedDict[str, tuple[str, float]]" = OrderedDict()

    @staticmethod
    def key(action: str, text_sha: str, model: str, target_language: str,
            prompt_id: str) -> str:
        return f"{action}|{text_sha}|{model}|{target_language}|{prompt_id}"

    def get(self, key: str) -> Optional[str]:
        if not self._enabled:
            return None
        now = time.time()
        with self._lock:
            hit = self._entries.get(key)
            if hit is None:
                return None
            result, stored_at = hit
            if now - stored_at > self._ttl_sec:
                self._entries.pop(key, None)
                return None
            self._entries.move_to_end(key)
            return result

    def put(self, key: str, result: str) -> None:
        if not self._enabled or not key or not result:
            return
        now = time.time()
        with self._lock:
            self._entries[key] = (result, now)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


class TaskManager:
    """Owns the task registry, the worker pool and the cache.

    ``on_event(task_dict)`` is invoked for every state transition with a
    wire-ready payload (without ``protocol``/``kind`` — the broker wraps it).
    """

    def __init__(self, cfg, on_event: Optional[Callable[[Dict[str, Any]], None]]
                 = None) -> None:
        self._cfg = cfg
        self._on_event = on_event
        self._lock = threading.Lock()
        self._tasks: Dict[str, EverywhereTask] = {}
        self._by_request: Dict[str, str] = {}
        self._order: List[str] = []          # newest first
        self._stop = threading.Event()
        self._history_max = max(
            5, int(getattr(cfg, "everywhere_history_max", 50) or 50))
        workers = max(1, int(
            getattr(cfg, "everywhere_max_concurrent_tasks", 3) or 3))
        self._pool = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="everywhere-task")
        self._cache = ResultCache(cfg)
        debug_log(
            f"everywhere task manager: workers={workers} "
            f"retries={int(getattr(cfg, 'everywhere_max_retries', 2) or 0)} "
            f"cache={'on' if self._cache._enabled else 'off'} "
            f"history={self._history_max}", "everywhere")

    # ── submit / cancel / query ────────────────────────────────────────
    def submit(self, action: str, snapshot: SelectionSnapshot, *,
               request_id: str = "", profile: str = "", model: str = "",
               target_language: str = "", prompt_id: str = "") -> EverywhereTask:
        """Queue one action; returns immediately with the task handle."""
        task = EverywhereTask(
            task_id=f"t{secrets.token_hex(6)}",
            request_id=request_id or f"t{secrets.token_hex(4)}",
            action=action,
            snapshot=snapshot,
            profile=profile or "reasoning",
            model=model or resolve_model_for_action(self._cfg, action),
            target_language=target_language,
            prompt_id=prompt_id,
        )
        with self._lock:
            self._tasks[task.task_id] = task
            self._by_request[task.request_id] = task.task_id
            self._order.insert(0, task.task_id)
            self._trim_history_locked()
        self._emit(task, "queued")
        self._pool.submit(self._run, task)
        return task

    def cancel(self, task_id_or_request: str) -> bool:
        task = self.get(task_id_or_request)
        if task is None or task.state in ("completed", "failed", "cancelled"):
            return False
        task.cancel.set()
        # The running worker observes the flag in the streaming callback and
        # finalises the task; a queued task never started is finalised here.
        if task.state == "queued":
            task.state = "cancelled"
            task.failure = "MODEL_CANCELLED"
            task.finished_at = time.time()
            task.done_event.set()
            self._emit(task, "cancelled")
        return True

    def get(self, task_id_or_request: str) -> Optional[EverywhereTask]:
        with self._lock:
            tid = self._tasks.get(task_id_or_request)
            if tid is None:
                tid = self._by_request.get(task_id_or_request)
            if tid is None:
                return None
            return self._tasks.get(tid)

    def snapshot(self, limit: int = 200) -> List[Dict[str, Any]]:
        """Wire-ready task list, newest first (for subscribe/list replies)."""
        with self._lock:
            out = []
            for tid in self._order[:limit]:
                task = self._tasks.get(tid)
                if task is not None:
                    out.append(self._summary_locked(task))
            return out

    # ── worker ─────────────────────────────────────────────────────────
    def _run(self, task: EverywhereTask) -> None:
        snap = task.snapshot
        cache_key = ""
        if task.action in CACHEABLE_ACTIONS and not task.cancel.is_set():
            cache_key = ResultCache.key(
                task.action, snap.text_sha, task.model, task.target_language,
                task.prompt_id)
            cached = self._cache.get(cache_key)
            if cached is not None:
                task.result = cached
                task.state = "completed"
                task.cache_hit = True
                task.started_at = time.time()
                task.finished_at = time.time()
                task.done_event.set()
                debug_log(
                    f"everywhere.task.cache_hit task={task.task_id} "
                    f"action={task.action} len={len(cached)}", "everywhere")
                self._emit(task, "completed")
                return
        max_retries = max(0, int(
            getattr(self._cfg, "everywhere_max_retries", 2) or 0))
        backoff = 1.0
        while not self._stop.is_set() and not task.cancel.is_set():
            task.started_at = time.time()
            task.state = "running"
            self._emit(task, "running")
            ok, outcome = self._execute_llm(task, snap)
            if ok:
                task.result = outcome or ""
                task.state = "completed"
                task.finished_at = time.time()
                task.done_event.set()
                if cache_key:
                    self._cache.put(cache_key, task.result)
                debug_log(
                    f"everywhere.task.completed task={task.task_id} "
                    f"action={task.action} len={len(task.result)} "
                    f"first_token_ms={task.first_token_ms}", "everywhere")
                self._emit(task, "completed")
                return
            failure = outcome or "MODEL_UNAVAILABLE"
            if task.cancel.is_set():
                task.state = "cancelled"
                task.failure = "MODEL_CANCELLED"
                task.finished_at = time.time()
                task.done_event.set()
                self._emit(task, "cancelled")
                return
            if failure in _RETRYABLE_FAILURES and task.retry_count < max_retries:
                task.retry_count += 1
                task.state = "queued"
                task.finished_at = None
                debug_log(
                    f"everywhere.task.retry task={task.task_id} "
                    f"action={task.action} attempt={task.retry_count} "
                    f"failure={failure} backoff={backoff:.0f}s", "everywhere")
                self._emit(task, "retrying")
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 10.0)
                continue
            task.state = "failed"
            task.failure = failure
            task.finished_at = time.time()
            task.done_event.set()
            debug_log(
                f"everywhere.task.failed task={task.task_id} "
                f"action={task.action} failure={failure}", "everywhere")
            self._emit(task, "failed")
            return
        if task.state in ("queued", "running", "streaming"):
            task.state = "cancelled"
            task.failure = "MODEL_CANCELLED"
            task.finished_at = time.time()
            task.done_event.set()
            self._emit(task, "cancelled")

    def _execute_llm(self, task: EverywhereTask,
                     snap: SelectionSnapshot) -> tuple[bool, str]:
        """Run one action through the canonical two-tier LLM router.

        Returns ``(True, result)`` on success or ``(False, failure_code)``.
        """
        from ..llm import get_llm_backend

        cfg = self._cfg
        backend = get_llm_backend(cfg)
        model = task.model
        if not model or backend is None:
            return False, "MODEL_UNAVAILABLE"
        fast = task.profile in ("fast-edit", "structured-edit")
        timeout = float(getattr(
            cfg, "llm_digest_timeout_sec" if fast else "llm_chat_timeout_sec",
            12.0 if fast else 180.0))
        system = prompt_lib.get_action_prompt(cfg, task.action)
        sem = dict(snap.semantic_context or {})
        if snap.source_kind == "vscode-editor" and snap.provider_token.get(
                "languageId"):
            sem.setdefault("language_id",
                           str(snap.provider_token["languageId"]))
        from .actions import TERMINAL_SOURCE_KINDS
        if snap.source_kind in TERMINAL_SOURCE_KINDS:
            for key in ("shell", "remote_kind", "remote_authority", "cwd"):
                if snap.provider_token.get(key):
                    sem.setdefault(key, str(snap.provider_token[key]))
        snap_dict = snap.to_dict()
        snap_dict["semantic_context"] = sem
        target_language = None
        count = None
        if task.action == "translate":
            target_language = str(
                task.target_language
                or getattr(cfg, "everywhere_translate_default_language", "cs")
                or "cs")
            system = system.replace("{target_language}", target_language)
        if task.action == "alternatives":
            from .actions import alternatives_count
            count = alternatives_count(cfg)
        user = prompt_lib.build_user_block(
            snap_dict, target_language=target_language,
            alternatives_count=count)
        if task.action == "prompt" and task.prompt_id:
            entry = self._lookup_prompt(task.prompt_id)
            if entry is None:
                return False, "PROVIDER_UNAVAILABLE"
            system = str(entry.get("instruction") or system)
            model = model or resolve_model_for_action(cfg, "prompt")
            prompt_lib.touch_usage(
                list(getattr(cfg, "everywhere_prompt_library", []) or []),
                task.prompt_id)
        buffer: list = []

        def _on_token(chunk: str) -> None:
            if task.cancel.is_set():
                raise _Cancelled()
            if task.first_token_at is None:
                task.first_token_at = time.time()
                task.state = "streaming"
                self._emit(task, "first_token")
            buffer.append(chunk)

        wants_thinking = task.profile not in (
            "fast-edit", "structured-edit", "creative-edit")
        try:
            full = backend.streaming(
                model, system, user, on_token=_on_token,
                timeout_sec=timeout, thinking=wants_thinking)
            if task.cancel.is_set():
                raise _Cancelled()
            result = full if full is not None else "".join(buffer)
            if not result:
                return False, "MODEL_UNAVAILABLE"
            if task.action == "proofread":
                parsed, reason = proofread_lib.parse_proofread(
                    result, source_hash=snap.text_sha)
                if parsed is None:
                    return False, ("MODEL_UNAVAILABLE"
                                   if reason == "empty"
                                   else "PROVIDER_UNAVAILABLE")
                result = json.dumps(parsed, ensure_ascii=False)
            if task.action == "translate" and not result.strip():
                debug_log(
                    f"everywhere.task.empty_translation task={task.task_id}",
                    "everywhere")
                return False, "MODEL_UNAVAILABLE"
            if len(result) > _MAX_RESULT_CHARS:
                result = result[:_MAX_RESULT_CHARS]
            return True, result
        except _Cancelled:
            return False, "MODEL_CANCELLED"
        except Exception as exc:  # noqa: BLE001 — reported, not swallowed
            debug_log(
                f"everywhere.task.error task={task.task_id} "
                f"error={type(exc).__name__}: {exc}", "everywhere")
            return False, "MODEL_UNAVAILABLE"

    def _lookup_prompt(self, prompt_id: str) -> Optional[Dict[str, Any]]:
        for entry in getattr(self._cfg, "everywhere_prompt_library", []) or []:
            if isinstance(entry, dict) and entry.get("id") == prompt_id:
                return entry
        return None

    # ── events / summaries ─────────────────────────────────────────────
    def _emit(self, task: EverywhereTask, event: str) -> None:
        with self._lock:
            payload = self._summary_locked(task)
        payload["event"] = event
        if event == "completed":
            payload["result_length"] = len(task.result or "")
            payload["preview"] = (task.result or "")[:_EVENT_RESULT_CHARS]
            payload["truncated"] = len(task.result or "") > _EVENT_RESULT_CHARS
            if len(task.result or "") <= _EVENT_RESULT_CHARS:
                payload["result"] = task.result or ""
            else:
                payload["result"] = ""
        elif event == "failed":
            payload["failure"] = task.failure
        if self._on_event is not None:
            try:
                self._on_event(payload)
            except Exception:  # noqa: BLE001 — events must never kill workers
                pass

    def _summary_locked(self, task: EverywhereTask) -> Dict[str, Any]:
        snap = task.snapshot
        return {
            "task_id": task.task_id,
            "request_id": task.request_id,
            "action": task.action,
            "snapshot_id": task.snapshot_id,
            "state": task.state,
            "model": task.model,
            "profile": task.profile,
            "target_language": task.target_language,
            "prompt_id": task.prompt_id,
            "cache_hit": task.cache_hit,
            "retry_count": task.retry_count,
            "queued_at": task.queued_at,
            "started_at": task.started_at,
            "first_token_at": task.first_token_at,
            "finished_at": task.finished_at,
            "duration_ms": task.duration_ms,
            "first_token_ms": task.first_token_ms,
            "failure": task.failure,
            "result_length": len(task.result or ""),
            "preview": (task.result or "")[:120],
            "source_kind": snap.source_kind,
            "process_name": snap.process_name,
            "position": self._position_locked(task),
        }

    def _position_locked(self, task: EverywhereTask) -> int:
        active = ("queued", "running", "streaming")
        return sum(
            1 for tid in self._order
            if (t := self._tasks.get(tid)) is not None
            and t.state in active and t.queued_at <= task.queued_at)

    def _trim_history_locked(self) -> None:
        """Drop finished tasks beyond the history window (never live ones)."""
        finished = ("completed", "failed", "cancelled")
        while len(self._order) > self._history_max:
            tid = self._order[-1]
            task = self._tasks.get(tid)
            if task is None:
                self._order.pop()
                continue
            if task.state not in finished:
                break  # keep live tasks; only finished ones are evicted
            self._order.pop()
            self._tasks.pop(tid, None)
            self._by_request.pop(task.request_id, None)

    # ── lifecycle ──────────────────────────────────────────────────────
    def shutdown(self, timeout_sec: float = 3.0) -> None:
        self._stop.set()
        with self._lock:
            tasks = [t for t in self._tasks.values()
                     if t.state in ("queued", "running", "streaming")]
        for task in tasks:
            task.cancel.set()
            if task.state == "queued":
                task.state = "cancelled"
                task.finished_at = time.time()
                task.done_event.set()
        self._pool.shutdown(wait=False)
        deadline = time.monotonic() + timeout_sec
        for task in tasks:
            task.done_event.wait(max(0.0, deadline - time.monotonic()))
        self._cache.clear()
        debug_log(
            f"everywhere task manager stopped "
            f"(tasks={len(self._tasks)})", "everywhere")