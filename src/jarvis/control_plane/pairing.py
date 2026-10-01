"""Pairing store for the desktop control plane.

One host, one pairing, one local secret. The browser pairs once with
the local Toastovač and then authenticates every request with the
secret; Toastovač never receives or stores v271 cookies.

Persisted as JSON next to the daemon config so a restart keeps the same
host identity and the browser does not have to re-pair.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
import uuid
from typing import Any, Dict, Optional


class PairingStore:
    """Persists ``{hostId, pairingId, secret}`` and validates secrets.

    Thread-safe. File writes are atomic (temp file + rename); on
    platforms where ``os.chmod`` applies, the file is written 0o600 so
    the secret stays user-owned.
    """

    def __init__(self, path: str) -> None:
        self._path = os.path.abspath(path)
        self._lock = threading.Lock()
        self._host_id: Optional[str] = None
        self._pairings: Dict[str, Dict[str, Any]] = {}
        self._load()

    # -- persistence ----------------------------------------------------

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                host_id = data.get("hostId")
                if isinstance(host_id, str) and host_id:
                    self._host_id = host_id
                pairings = data.get("pairings")
                if isinstance(pairings, dict):
                    self._pairings = {
                        str(k): v
                        for k, v in pairings.items()
                        if isinstance(v, dict) and isinstance(v.get("secret"), str)
                    }
        except FileNotFoundError:
            pass
        except Exception:
            # A corrupt pairing file must not brick the control plane; a
            # fresh pairing is created on demand and the file is rewritten.
            self._host_id = None
            self._pairings = {}

    def _save(self) -> None:
        data = {"hostId": self._host_id, "pairings": self._pairings}
        tmp = f"{self._path}.tmp"
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass  # Windows: no POSIX mode bits; default ACLs apply
            os.replace(tmp, self._path)
        except OSError:
            # Never raise on persistence failure: pairing still works for
            # this process lifetime, the file is best-effort durability.
            pass

    # -- public API -----------------------------------------------------

    @property
    def host_id(self) -> str:
        with self._lock:
            if self._host_id is None:
                self._host_id = str(uuid.uuid4())
            return self._host_id

    def get_or_create_pairing(self, user_id: Optional[str] = None) -> Dict[str, Any]:
        """Return the host's pairing, creating it on first use.

        Idempotent: the browser may call ``pair`` on every start; the
        secret stays stable until the store is deleted.
        """
        with self._lock:
            if self._host_id is None:
                self._host_id = str(uuid.uuid4())
            if self._pairings:
                pairing_id, pairing = next(iter(self._pairings.items()))
                # Merge a late-provided userId into the stored pairing.
                if user_id and not pairing.get("userId"):
                    pairing["userId"] = user_id
                    self._save()
                return dict(pairing, hostId=self._host_id, pairingId=pairing_id)
            pairing_id = str(uuid.uuid4())
            pairing = {
                "pairingId": pairing_id,
                "secret": secrets.token_urlsafe(32),
                "createdAt": time.time(),
                "userId": user_id or "",
            }
            self._pairings[pairing_id] = pairing
            self._save()
            return dict(pairing, hostId=self._host_id, pairingId=pairing_id)

    def validate_secret(self, secret: str) -> Optional[Dict[str, Any]]:
        """Return the pairing for a secret, or ``None`` when invalid."""
        if not isinstance(secret, str) or not secret:
            return None
        with self._lock:
            for pairing in self._pairings.values():
                if secrets.compare_digest(str(pairing.get("secret") or ""), secret):
                    return dict(pairing)
            return None

    def clear(self) -> None:
        """Delete all pairings (used by tests / re-pair flows)."""
        with self._lock:
            self._host_id = None
            self._pairings = {}
        try:
            os.remove(self._path)
        except OSError:
            pass