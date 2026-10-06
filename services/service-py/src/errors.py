"""Typed errors. The API maps each class to exactly one HTTP status; ``reason`` is a code from reasons.py and an
error body never carries anything from the request."""

from __future__ import annotations


class SvcError(Exception):
    status_code = 500

    def __init__(self, reason: str, **body):
        super().__init__(reason)
        self.reason = reason[:200]
        self.body = body


class NotFound(SvcError):
    status_code = 404


class Forbidden(SvcError):
    """403: the caller is not allowed this route or this record."""

    status_code = 403


class FounderRefused(SvcError):
    """403: Andre's approval token is missing, wrong, or not configured."""

    status_code = 403


class Conflict(SvcError):
    """409: request_id reused with a different body, a state transition not allowed, a stale version."""

    status_code = 409


class Invalid(SvcError):
    """422: structurally valid JSON that breaks a domain rule."""

    status_code = 422


class Unavailable(SvcError):
    """503: the ledger or the local store could not act; nothing took effect (``maybe``: it may still)."""

    status_code = 503
    maybe = False
