"""Provider adapter contract.

Every adapter inspects ONE host kind and returns a ProviderResult. The
broker calls exactly one adapter — the one matching the classified
foreground host — so there is never a silent fallback chain (§Resolution
contract: steps 1–7).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ..models import ProviderResult, ResultKind


class TerminalProvider(ABC):
    """One deterministic adapter per verified foreground host kind."""

    #: canonical host_kind this adapter answers (see models.HOST_KINDS)
    host_kind: str = "unknown"

    @abstractmethod
    def resolve(
        self,
        hwnd: int,
        host_pid: int,
        window_class: str,
        executable: str,
        freshness_ns: int,
    ) -> ProviderResult:
        """Resolve the terminal context for this foreground host."""

    # Small helpers shared by adapters ------------------------------------
    @staticmethod
    def no_match(reason: str) -> ProviderResult:
        return ProviderResult(kind=ResultKind.NO_MATCH, reason=reason)

    @staticmethod
    def unavailable(reason: str) -> ProviderResult:
        return ProviderResult(kind=ResultKind.UNAVAILABLE, reason=reason)
