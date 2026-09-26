"""Typed errors. The API maps each class to exactly one HTTP status (the compliance-py pattern)."""

from __future__ import annotations


class CNError(Exception):
    status_code = 500

    def __init__(self, reason: str, **body):
        super().__init__(reason)
        self.reason = reason[:1000]
        self.body = body


class NotFound(CNError):
    status_code = 404


class Forbidden(CNError):
    """403 — caller not authorized for this route."""

    status_code = 403


class FounderRefused(CNError):
    """403 — Andre's approval token (or a delegate token) missing, wrong, non-ASCII or not configured."""

    status_code = 403


class Conflict(CNError):
    """409 — request_id reused with another body, stale proposal, state that forbids the action."""

    status_code = 409


class Invalid(CNError):
    """422 — structurally valid JSON that breaks a domain rule."""

    status_code = 422


class Unavailable(CNError):
    """503 — the ledger or the local store could not record; nothing was issued."""

    status_code = 503
