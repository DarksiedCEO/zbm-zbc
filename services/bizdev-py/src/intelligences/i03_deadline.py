"""Bid deadlines (ADR 0016 decision 11).

Decides: whether a stored deadline has passed at a given instant. The instant always comes from the service's
injected clock (``service.now()``), never from the wall clock here, and the deadline from the stored pursuit, never
from the request. A deadline is an RFC 3339 timestamp WITH an offset; it is compared in UTC. ``now >= deadline`` is
passed: a submission exactly at the deadline is late. Never: extends a deadline or submits anything."""

from __future__ import annotations

from datetime import datetime

from clock import parse_iso

NUMBER = 3
NAME = "deadline_guard"
DECIDES = "whether a bid deadline has passed"


def passed(deadline: str, now: datetime) -> bool:
    """True when ``now`` is at or after the stored ``deadline``. An unreadable stored deadline counts as passed."""
    try:
        return now >= parse_iso(deadline)
    except (TypeError, ValueError):
        return True


def seconds_left(deadline: str, now: datetime) -> int:
    try:
        return max(0, int((parse_iso(deadline) - now).total_seconds()))
    except (TypeError, ValueError):
        return 0
