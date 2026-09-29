"""
================================================================================
Risk State Persistence
================================================================================
The previous implementation held every risk-critical value in RAM. A restart
mid-session silently reset `day_start_equity` to *current* equity, which erased
the day's realised losses and disabled the daily loss circuit breaker. On a
funded account that is a direct compliance violation.

This module provides crash-safe, atomic JSON persistence of:
  * the trading day and its opening equity anchor
  * the circuit breaker flag
  * per-employee trade / win / loss counters and cooldowns
  * the set of already-reconciled deal tickets (prevents double counting)
  * the position -> employee attribution map (survives netting merges)
  * open position PnL accumulation (correct partial-close accounting)
"""

import json
import logging
import os
import tempfile
from typing import Any, Dict, Optional

logger = logging.getLogger("SLDPAX_State")

SCHEMA_VERSION = 2


class RiskStateStore:
    """Atomic key/value persistence for the risk governor."""

    def __init__(self, path: str):
        self.path = path
        self._dirty = False

    # -- internals ----------------------------------------------------------
    def _atomic_write(self, data: Dict[str, Any]) -> None:
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, exist_ok=True)
        handle, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self.path)
        except Exception:
            # Never leave a half-written state file behind.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # -- public API ---------------------------------------------------------
    def load(self) -> Optional[Dict[str, Any]]:
        """Return the persisted state, or None when absent/corrupt."""
        if not os.path.exists(self.path):
            return None
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            logger.error(
                "State file %s is unreadable (%s). Backing it up and starting a fresh day. "
                "VERIFY THE ACCOUNT MANUALLY before trading.",
                self.path,
                exc,
            )
            try:
                os.replace(self.path, self.path + ".corrupt")
            except OSError:
                pass
            return None

        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
            logger.warning(
                "State file %s has schema %r, expected %d. Starting a fresh day.",
                self.path,
                data.get("schema_version") if isinstance(data, dict) else None,
                SCHEMA_VERSION,
            )
            return None
        return data

    def save(self, data: Dict[str, Any]) -> None:
        """Persist state atomically. Never raises into the trading loop."""
        try:
            data["schema_version"] = SCHEMA_VERSION
            self._atomic_write(data)
            self._dirty = False
        except OSError as exc:
            logger.error("Failed to persist risk state to %s: %s", self.path, exc)

    def clear(self) -> None:
        try:
            if os.path.exists(self.path):
                os.unlink(self.path)
        except OSError as exc:
            logger.error("Failed to remove state file %s: %s", self.path, exc)
