"""Typed errors. The API maps each class to exactly one HTTP status."""

from __future__ import annotations


class FinError(Exception):
    status_code = 500

    def __init__(self, reason: str, **body):
        super().__init__(reason)
        self.reason = reason[:1000]
        self.body = body


class NotFound(FinError):
    status_code = 404


class Forbidden(FinError):
    """403 — caller not authorized for this route."""

    status_code = 403


class FounderRefused(FinError):
    """403 — Andre's approval token missing, wrong, non-ASCII or not configured."""

    status_code = 403


class Conflict(FinError):
    """409 — hash mismatch, stale proposal, request_id reused, decided proposal."""

    status_code = 409


class Invalid(FinError):
    """422 — structurally valid JSON that breaks a domain rule."""

    status_code = 422


class Unavailable(FinError):
    """503 — the ledger or the local store could not record; nothing was issued."""

    status_code = 503
