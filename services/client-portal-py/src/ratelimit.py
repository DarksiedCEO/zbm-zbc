"""In-process fixed-window rate limits (one replica; ADR 0028). Bounded memory: at most ``max_keys`` keys per limiter,
the oldest window evicted first. Keys are never raw IPs or tokens: the app passes HMACs of them."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Callable


class Limiter:
    def __init__(self, limit: int, window_s: float, max_keys: int = 20_000,
                 clock: Callable[[], float] = time.monotonic):
        self.limit, self.window, self.max_keys, self.clock = limit, window_s, max_keys, clock
        self._hits: "OrderedDict[str, tuple[float, int]]" = OrderedDict()
        self._lock = threading.Lock()

    def hit(self, key: str) -> tuple[bool, int]:
        """Count one request for ``key``. Returns (allowed, seconds until the window resets)."""
        now = self.clock()
        with self._lock:
            start, n = self._hits.get(key, (now, 0))
            if now - start >= self.window:
                start, n = now, 0
            n += 1
            self._hits[key] = (start, n)
            self._hits.move_to_end(key)
            while len(self._hits) > self.max_keys:
                self._hits.popitem(last=False)
            retry = max(1, int(self.window - (now - start)) + 1)
            return n <= self.limit, retry
