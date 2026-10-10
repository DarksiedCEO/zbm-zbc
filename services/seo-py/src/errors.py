"""Typed errors. The API maps each class to exactly one HTTP status; ``reason`` is a code from reasons.py and an
error body never carries anything from the request (service-py's errors.py, with sales-py's 429)."""

from __future__ import annotations


class SeoError(Exception):
    status_code = 500

    def __init__(self, reason: str, **body):
        super().__init__(reason)
        self.reason = reason[:200]
        self.body = body


class NotFound(SeoError):
    status_code = 404


class Forbidden(SeoError):
    """403: the caller is not allowed this route, or the tenant or capability is killed."""

    status_code = 403


class FounderRefused(SeoError):
    """403: Andre's approval token is missing, wrong, or not configured."""

    status_code = 403


class Conflict(SeoError):
    """409: request_id reused with a different body, wrong state, stale version or hash, a gate not met."""

    status_code = 409


class Invalid(SeoError):
    """422: structurally valid JSON that breaks a domain rule."""

    status_code = 422


class Throttled(SeoError):
    """429: a per-tenant cap is used up."""

    status_code = 429


class Unavailable(SeoError):
    """503: the ledger, the local store or a port could not act; nothing took effect (``maybe``: it may still)."""

    status_code = 503
    maybe = False
