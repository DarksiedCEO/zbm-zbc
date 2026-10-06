"""Typed errors. The API maps each class to exactly one HTTP status; ``reason`` is a code from reasons.py and the
body never carries request content."""

from __future__ import annotations


class InfError(Exception):
    status_code = 500

    def __init__(self, reason: str, **body):
        super().__init__(reason)
        self.reason = reason[:1000]
        self.body = body


class NotFound(InfError):
    status_code = 404


class Forbidden(InfError):
    """403: the caller is not allowed this route, or the influencer is suppressed, held or blocked."""

    status_code = 403


class FounderRefused(InfError):
    """403: Andre's approval token is missing, wrong, or not configured."""

    status_code = 403


class Conflict(InfError):
    """409: request_id reused with a different body, wrong state, stale version or hash."""

    status_code = 409


class Invalid(InfError):
    """422: structurally valid JSON that breaks a domain rule."""

    status_code = 422


class Throttled(InfError):
    """429: the caller's queue is full."""

    status_code = 429


class Unavailable(InfError):
    """503: the ledger, the local store or a port could not act; nothing took effect."""

    status_code = 503
