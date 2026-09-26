"""Terminal Command Composer coordinator (K7 wiring).

One synchronous pipeline per request:

    utterance ─▶ intent route ─▶ broker.resolve ─▶ compose ─▶ policy
             ─▶ (confirmation) ─▶ insert ─▶ short Czech status

Deterministic intent routing (never a general-agent fallback): the
utterance must mention a terminal-ish verb set after removing one
leading/trailing wake token. Ambiguity/staleness → one short Czech
explanation, no insertion (§Non-negotiable 10).

Face/IPC states: TERMINAL_CONTEXT / TERMINAL_COMPOSING /
TERMINAL_CONFIRMATION / COMMAND_READY / TERMINAL_ERROR.
"""

from __future__ import annotations

import time
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, Optional

from .command_policy import needs_confirmation

from ..debug import debug_log
from .command_composer import compose
from .command_policy import proposal_hash
from .context_broker import TerminalContextBroker
from .insertion import insert_command, insert_via_vscode
from .models import CommandProposal, ProviderResult, ResultKind, TerminalContext
from .session_memory import TerminalSessionMemory, make_key

_WAKE_FOLDED = ("toustovac", "toastovac", "toaster")

_INTENT_VERBS = {
    "priprav", "prepin", "prepis", "pridel", "prevod", "nijak",
    "najdi", "zjisti", "ukaz", "restartuj", "spust", "vypis",
    "prepare", "find", "show", "list", "rewrite",
}


@dataclass
class _Pending:
    proposal: CommandProposal
    ctx: TerminalContext
    key: str
    expires: float


class TerminalComposer:
    def __init__(
        self,
        broker: TerminalContextBroker,
        memory: TerminalSessionMemory,
        *,
        paste_gestures: Optional[Dict[str, str]] = None,
        restore_clipboard: bool = True,
        confirmation_ttl_sec: float = 60.0,
    ) -> None:
        self.broker = broker
        self.memory = memory
        self._gestures = dict(paste_gestures or {})
        self._restore = bool(restore_clipboard)
        self._ttl = float(confirmation_ttl_sec)
        self._pending: Dict[str, _Pending] = {}

    # ── intent route ──────────────────────────────────────────────────
    @staticmethod
    def _fold(text: str) -> str:
        dec = unicodedata.normalize("NFKD", str(text or "").casefold())
        return " ".join("".join(
            ch for ch in dec if not unicodedata.combining(ch)
        ).split())

    @classmethod
    def _strip_wake(cls, folded: str) -> str:
        parts = folded.split()
        if parts and parts[0].strip(".,;") in _WAKE_FOLDED:
            parts = parts[1:]
        elif len(parts) > 1 and parts[-1].strip(".,;") in _WAKE_FOLDED:
            parts = parts[:-1]
        return " ".join(parts)

    @classmethod
    def is_terminal_intent(cls, text: str) -> bool:
        folded = cls._strip_wake(cls._fold(text))
        if not folded:
            return False
        first = folded.split()[0].strip(".,;")
        return first in _INTENT_VERBS

    # ── main pass ─────────────────────────────────────────────────────
    def handle(
        self,
        chat_callable,
        text: str,
        *,
        face=None,
        tts=None,
    ) -> str:
        """Return one short Czech line; never logs bodies (§Observability)."""
        query = self._strip_wake(self._fold(text))
        now = time.monotonic()
        for k, p in list(self._pending.items()):
            if p.expires < now:
                self.memory.expire(p.key)
                self._pending.pop(k, None)

        # confirmation continuation?
        pend = self._pending.get("last")
        if pend and pend.expires >= now:
            folded = self._fold(text)
            if folded.split(" ", 1)[0].startswith(("ano", "pokracuj", "yes")):
                res = self._insert(pend)
                self._pending.pop("last", None)
                if res.ok:
                    self._face(face, "COMMAND_READY")
                    return "Příkaz je připravený. Čeká na Enter."
                self._face(face, "TERMINAL_ERROR")
                return f"Vložení selhalo: {res.reason}."
            self._pending.pop("last", None)  # modified → invalid (§bind)

        resolved = self.broker.resolve()
        self._face(face, "TERMINAL_CONTEXT")
        if resolved.kind is ResultKind.AMBIGUOUS:
            debug_log("terminal.context.ambiguous", "terminal")
            return "Nemohu jednoznačně určit aktivní terminál. Nic jsem nevložil."
        if resolved.kind is ResultKind.UNAVAILABLE:
            debug_log("terminal.context.stale", "terminal")
            return "Terminal je nedostupný. Nic jsem nevložil."
        if resolved.kind is not ResultKind.MATCH or resolved.context is None:
            return "Shell se nepodařilo bezpečně určit. Nic jsem nevložil."

        ctx = resolved.context
        mkey = make_key(ctx.provider, ctx.terminal_session_id, None,
                        ctx.remote_kind, ctx.remote_authority)
        # Drain the execution record the bridge observed for THIS live
        # session before composing (M0.4 kind semantics in the store).
        self._drain_execution(ctx, mkey)
        block = self.memory.memory_block(mkey)

        self._face(face, "TERMINAL_COMPOSING")
        debug_log("terminal.compose.started", "terminal")
        proposal, reason = compose(
            chat_callable, ctx, query, memory_block=block,
        )
        if proposal is None:
            debug_log("terminal.compose.completed empty", "terminal")
            if reason and reason.startswith(("Chyb", "Kter", "Jak", "Na ktery")):
                return reason  # NEED_INPUT short Czech question
            self._face(face, "TERMINAL_ERROR")
            return reason or "Model neodpověděl. Nic jsem nevložil."

        if needs_confirmation(proposal.risk):
            self._pending["last"] = _Pending(
                proposal, ctx, mkey, time.monotonic() + self._ttl,
            )
            debug_log("terminal.confirmation.requested", "terminal")
            self._face(face, "TERMINAL_CONFIRMATION")
            return "Destruktivní příkaz vyžaduje potvrzení. Řekni „ano“."

        res = self._insert(_Pending(proposal, ctx, mkey, 0.0))
        if res.ok:
            self._face(face, "COMMAND_READY")
            return "Příkaz je připravený. Čeká na Enter."
        self._face(face, "TERMINAL_ERROR")
        return f"Vložení selhalo: {res.reason}."

    # ── helpers ───────────────────────────────────────────────────────
    def _drain_execution(self, ctx, mkey: str) -> None:
        try:
            from ..daemon import get_terminal_bridge
            bridge = get_terminal_bridge()
            if bridge is None:
                return
            rec = bridge.execution_for(ctx.terminal_session_id)
            if not rec:
                return
            from .models import TerminalExecutionRecord
            er = TerminalExecutionRecord(
                terminal_session_id=str(rec.get("session_id") or ""),
                proposed_command=None,
                executed_command=str(rec.get("command") or ""),
                exit_code=rec.get("exit_code"),
                cwd_before=None,
                cwd_after=rec.get("cwd") or None,
                output_tail=str(rec.get("output_tail") or "")[:2000],
                started_at_ns=int(rec.get("timestamp_ns") or 0),
                ended_at_ns=int(rec.get("timestamp_ns") or 0),
                command_confidence="shell_reported",
                execution_kind=str(rec.get("execution_kind") or "unknown"),
                success=rec.get("success")
                if isinstance(rec.get("success"), bool) else None,
            )
            self.memory.record_execution(mkey, er, _extract_entities(er))
        except Exception as exc:  # pragma: no cover — defensive
            debug_log(f"execution drain skipped: {exc}", "terminal")

    def _insert(self, pend: _Pending):
        ctx = pend.ctx
        if ctx.provider in ("vscode", "vscode_insiders"):
            from .bridge import named_pipe  # may be None if not started
            bridge = None
            try:
                from ..daemon import get_terminal_bridge
                bridge = get_terminal_bridge()
            except Exception:
                bridge = None
            return insert_via_vscode(bridge, ctx, pend.proposal)
        gesture = self._gestures.get(
            "windows_terminal" if ctx.host_kind == "windows_terminal"
            else "conhost",
            "ctrl_shift_v" if ctx.host_kind == "windows_terminal"
            else "ctrl_v",
        )
        return insert_command(
            self.broker, ctx, pend.proposal,
            paste_gesture=gesture, restore_clipboard=self._restore,
        )

    @staticmethod
    def _face(face, name: str) -> None:
        if face is None:
            return
        try:
            face.update_presence_mode("terminal", name)
        except Exception:
            pass


def _extract_entities(er) -> list:
    """Typed entities from the sanitized tail (deterministic, §K6)."""
    import re
    out: list = []
    tail = er.output_tail or ""
    m = re.search(r"\b([0-9a-f]{12})\b", tail)
    if m:
        out.append(("container_id", m.group(1), 0.95))
    m2 = re.search(r"\b(\d{1,5})\b.*LISTEN", tail, re.I)
    if m2:
        out.append(("port", m2.group(1), 0.9))
    return out
