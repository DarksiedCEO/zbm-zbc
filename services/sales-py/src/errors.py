"""Typed errors. The API maps each class to exactly one HTTP status; ``reason`` is a code from reasons.py and the
body never carries request content."""

from __future__ import annotations


class SalesError(Exception):
    status_code = 500

    def __init__(self, reason: str, **body):
        super().__init__(reason)
        self.reason = reason[:1000]
        self.body = body


class NotFound(SalesError):
    status_code = 404


class Forbidden(SalesError):
    """403: the caller is not allowed this route, or the contact has no consent, or is suppressed."""

    status_code = 403


class FounderRefused(SalesError):
    """403: Andre's approval token is missing, wrong, or not configured."""

    status_code = 403


class Conflict(SalesError):
    """409: request_id reused with a different body, wrong state, stale version or hash."""

    status_code = 409


class Invalid(SalesError):
    """422: structurally valid JSON that breaks a domain rule."""

    status_code = 422


class Throttled(SalesError):
    """429: today's send cap for the outreach domain is used up."""

    status_code = 429


class Unavailable(SalesError):
    """503: the ledger, the local store or a port could not act; nothing took effect."""

    status_code = 503
