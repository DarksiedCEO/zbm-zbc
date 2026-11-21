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


class DeadLinks:
    """AEGIS r3 N3-M1: links Legal answered 404 for (used, expired, revoked, documents changed), remembered for
    ``ttl_s`` so a flood on a dead link costs at most one Legal call per window. Bounded (oldest dropped first); keys
    are HMACs of tag-valid tokens only (a token Legal did not mint never gets here). A restart forgets everything."""

    def __init__(self, ttl_s: float = 900, max_keys: int = 20_000, clock: Callable[[], float] = time.monotonic):
        self.ttl, self.max_keys, self.clock = ttl_s, max_keys, clock
        self._until: "OrderedDict[str, float]" = OrderedDict()
        self._lock = threading.Lock()

    def add(self, key: str) -> None:
        with self._lock:
            self._until[key] = self.clock() + self.ttl
            self._until.move_to_end(key)
            while len(self._until) > self.max_keys:
                self._until.popitem(last=False)

    def __contains__(self, key: str) -> bool:
        with self._lock:
            until = self._until.get(key)
            if until is None:
                return False
            if self.clock() >= until:
                del self._until[key]
                return False
            return True


class Once:
    """At most one ``True`` per key per ``window_s`` (bounded): keeps a flood from flooding the log."""

    def __init__(self, window_s: float = 60, max_keys: int = 1_000, clock: Callable[[], float] = time.monotonic):
        self.window, self.max_keys, self.clock = window_s, max_keys, clock
        self._last: "OrderedDict[str, float]" = OrderedDict()
        self._lock = threading.Lock()

    def __call__(self, key: str) -> bool:
        now = self.clock()
        with self._lock:
            last = self._last.get(key)
            if last is not None and now - last < self.window:
                return False
            self._last[key] = now
            self._last.move_to_end(key)
            while len(self._last) > self.max_keys:
                self._last.popitem(last=False)
            return True
