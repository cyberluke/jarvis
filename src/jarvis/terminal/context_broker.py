"""TerminalContextBroker — one deterministic provider, no fallback chain.

Resolution contract (spec §TerminalContextBroker):
  1. foreground HWND + focused host evidence,
  2. foreground PID via GetWindowThreadProcessId,
  3. classify the host executable + window class,
  4. invoke ONLY the matching provider,
  5. reject stale provider output (freshness window),
  6. re-run 1–4 immediately before insertion,
  7. require identical HWND + session id at composition and insertion.

The broker is also the single place that stamps freshness and performs
the identity revalidation used by the insertion broker.
"""

from __future__ import annotations

import time
from typing import Optional

from ..debug import debug_log
from .focus_guard import (
    classify_host,
    foreground_hwnd,
    hwnd_pid,
    now_ns,
)
from .models import ProviderResult, ResultKind, TerminalContext
from .providers.base import TerminalProvider


# Provider truth table (P1-5) — implemented across broker + providers.
# Every missing piece of evidence fails closed; NO_MATCH / AMBIGUOUS /
# UNAVAILABLE stay distinct kinds (never one merged low-confidence state).
#
#  host                     required evidence                       missing →
#  standalone pwsh/conhost  HWND+pid+creation time+exe path/version  NO_MATCH/AMBIGUOUS
#  Windows Terminal pwsh    HWND + unique live beacon/marker         AMBIGUOUS
#  WT WSL                   pane + distro + proven child shell       NO_MATCH
#  WT direct SSH            pane + host/session + proven rmt shell   NO_MATCH
#  VS Code local            HWND + child focus + UUID + shell        NO_MATCH
#  VS Code Remote-SSH       all above + kind + unique authority +
#                           shell + OS evidence                       NO_MATCH
#
# Only evidence_level == PROVEN is insertable (providers enforce this).
class TerminalContextBroker:
    def __init__(
        self,
        providers: dict[str, TerminalProvider],
        freshness_sec: float = 10.0,
    ) -> None:
        self._providers = dict(providers)
        self._freshness_ns = int(freshness_sec * 1_000_000_000)

    # ── resolution ────────────────────────────────────────────────────
    def resolve(self) -> ProviderResult:
        hwnd = foreground_hwnd()
        if not hwnd:
            return ProviderResult(kind=ResultKind.UNAVAILABLE,
                                  reason="no foreground window")
        pid = hwnd_pid(hwnd)
        host_kind, wclass, exe = classify_host(hwnd)
        provider = self._providers.get(host_kind)
        if provider is None:
            return ProviderResult(
                kind=ResultKind.NO_MATCH,
                reason=f"no provider for host_kind={host_kind}",
            )
        try:
            result = provider.resolve(hwnd, pid, wclass, exe,
                                      self._freshness_ns)
        except Exception as exc:  # pragma: no cover — defensive
            debug_log(f"provider {host_kind} error: {exc}", "terminal")
            return ProviderResult(kind=ResultKind.UNAVAILABLE,
                                  reason=f"provider_error:{exc}")

        # freshness check on MATCH/candidates
        if result.kind is ResultKind.MATCH and result.context is not None:
            age = now_ns() - int(result.context.observed_at_ns or 0)
            if age > self._freshness_ns:
                # STALE is kept as its own normalized reason (P1-5).
                return ProviderResult(kind=ResultKind.NO_MATCH,
                                      reason="stale:STALE")
        # Only PROVEN is insertable; PARTIAL stays a distinct reason.
        if result.kind is ResultKind.MATCH and result.context is not None \
                and result.context.evidence_level != "PROVEN":
            return ProviderResult(
                kind=ResultKind.NO_MATCH,
                reason=f"evidence:{result.context.evidence_level}",
            )
        return result

    # ── identity for the insertion phase ──────────────────────────────
    def snapshot_identity(self, ctx: TerminalContext) -> tuple[int, str, int]:
        """(hwnd, session_id, observed_at_ns) — compared at insertion."""
        return (int(ctx.foreground_hwnd), str(ctx.terminal_session_id),
                int(ctx.observed_at_ns or 0))

    def revalidate(self, ctx: TerminalContext) -> bool:
        """Exact HWND + session id at composition == at insertion (§step 7)."""
        fresh = self.resolve()
        if fresh.kind is not ResultKind.MATCH or fresh.context is None:
            return False
        other = fresh.context
        return (
            int(other.foreground_hwnd) == int(ctx.foreground_hwnd)
            and str(other.terminal_session_id) == str(ctx.terminal_session_id)
        )
