"""
BoundedExpiringMap — the in-memory dedupe stores in api.py, with a time
window and a hard cap (fix wave 1: the audit flagged unbounded growth).

  - An entry older than `ttl` (on the caller-supplied clock) is evicted.
  - At most `max_entries` live entries. When the map is full and nothing is
    old enough to evict, `put` of a NEW key raises StateFull: the caller must
    fail closed (not dial / not write back) rather than forget an entry it
    relies on for idempotency.
  - An entry is never evicted early. If the clock moves backwards, entries
    stamped "in the future" simply stay until they are ttl old.

Not thread-safe by itself: every caller in api.py already holds its lock.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Generic, Hashable, TypeVar

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


class StateFull(Exception):
    """The map is at its hard cap and nothing in it has expired."""


class BoundedExpiringMap(Generic[K, V]):
    def __init__(self, *, ttl: timedelta, max_entries: int):
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 1:
            raise ValueError("max_entries must be a positive integer")
        if ttl <= timedelta(0):
            raise ValueError("ttl must be positive")
        self.ttl = ttl
        self.max_entries = max_entries
        self._entries: OrderedDict[K, tuple[datetime, V]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    def evict_expired(self, now: datetime) -> None:
        cutoff = now - self.ttl
        # Insertion order is stamp order unless the clock went backwards; in
        # that case this stops early (keeps entries longer), never evicts early.
        while self._entries:
            key, (stamped, _) = next(iter(self._entries.items()))
            if stamped > cutoff:
                break
            del self._entries[key]

    def get(self, key: K, now: datetime) -> V | None:
        self.evict_expired(now)
        hit = self._entries.get(key)
        return None if hit is None else hit[1]

    def contains(self, key: K, now: datetime) -> bool:
        self.evict_expired(now)
        return key in self._entries

    def room(self, now: datetime) -> int:
        """How many NEW keys can be added right now."""
        self.evict_expired(now)
        return self.max_entries - len(self._entries)

    def put(self, key: K, value: V, now: datetime) -> None:
        self.evict_expired(now)
        if key not in self._entries and len(self._entries) >= self.max_entries:
            raise StateFull(f"in-memory state is full ({self.max_entries} entries inside the {self.ttl} window)")
        self._entries.pop(key, None)
        self._entries[key] = (now, value)
