"""Typed errors. The API maps each class to exactly one HTTP status; ``reason`` is a code from reasons.py."""

from __future__ import annotations


class SecError(Exception):
    status_code = 500

    def __init__(self, reason: str, **body):
        super().__init__(reason)
        self.reason = reason[:1000]
        self.body = body


class NotFound(SecError):
    status_code = 404


class Forbidden(SecError):
    """403: the caller is not allowed this route, or this secret, or is frozen."""

    status_code = 403


class ApprovalRefused(SecError):
    """403: Andre's passkey approval is missing, invalid, expired, reused or bound to another action."""

    status_code = 403


class Conflict(SecError):
    """409: request_id reused with different content, wrong state, stale version."""

    status_code = 409


class Invalid(SecError):
    """422: structurally valid JSON that breaks a domain rule."""

    status_code = 422


class Throttled(SecError):
    """429: the caller is over its release rate."""

    status_code = 429


class Unavailable(SecError):
    """503: the ledger, the local store or the key service could not act; nothing took effect."""

    status_code = 503
