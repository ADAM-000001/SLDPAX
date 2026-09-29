"""
================================================================================
Event Log
================================================================================
The dashboard repaints the whole terminal every 2 seconds with `cls`, so any
message printed directly to stdout is destroyed almost immediately. This ring
buffer keeps recent desk events in memory so the HUD can re-render them,
giving the operator continuous visibility into fills, blocks and faults.
"""

import threading
from collections import deque
from datetime import datetime, timezone
from typing import Deque, List

LEVEL_INFO = "INFO"
LEVEL_TRADE = "TRADE"
LEVEL_BLOCK = "BLOCK"
LEVEL_ERROR = "ERR"

_SEVERITY_ORDER = {LEVEL_INFO: 0, LEVEL_BLOCK: 1, LEVEL_TRADE: 2, LEVEL_ERROR: 3}


class EventLog:
    def __init__(self, capacity: int = 200):
        self._events: Deque[dict] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._seq = 0

    def record(self, level: str, message: str) -> None:
        with self._lock:
            self._seq += 1
            self._events.append(
                {
                    "seq": self._seq,
                    "level": level,
                    "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                    "message": message,
                }
            )

    def info(self, message: str) -> None:
        self.record(LEVEL_INFO, message)

    def trade(self, message: str) -> None:
        self.record(LEVEL_TRADE, message)

    def block(self, message: str) -> None:
        self.record(LEVEL_BLOCK, message)

    def error(self, message: str) -> None:
        self.record(LEVEL_ERROR, message)

    def recent(self, count: int, min_level: str = LEVEL_INFO) -> List[dict]:
        """Return the newest `count` events at or above `min_level` severity."""
        threshold = _SEVERITY_ORDER.get(min_level, 0)
        with self._lock:
            qualifying = [e for e in self._events if _SEVERITY_ORDER.get(e["level"], 0) >= threshold]
        return qualifying[-count:]
