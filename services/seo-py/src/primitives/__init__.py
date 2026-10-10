"""The seven shared primitives (ADR 0017 decision 11): fetch, render, parse, diff, access-diff, link-diff,
change-detect — plus the robots.txt parser fetch uses. Single task each; no service state."""

from __future__ import annotations


class Killed(Exception):
    """Raised by a run's guard when a kill switch is engaged; the step's outcome becomes KILLED."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code
