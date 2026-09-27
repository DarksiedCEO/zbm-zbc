"""Typed errors. The API maps each class to exactly one HTTP status (finance-py pattern)."""

from __future__ import annotations


class DlvError(Exception):
    status_code = 500

    def __init__(self, reason: str, **body):
        super().__init__(reason)
        self.reason = reason[:1000]
        self.body = body


class NotFound(DlvError):
    status_code = 404


class Forbidden(DlvError):
    """403 — caller not authorized for this route."""

    status_code = 403


class FounderRefused(DlvError):
    """403 — Andre's approval token missing, wrong, non-ASCII or not configured."""

    status_code = 403


class Conflict(DlvError):
    """409 — request_id reused with another body, run in progress, wrong state."""

    status_code = 409


class Invalid(DlvError):
    """422 — structurally valid JSON that breaks a domain rule."""

    status_code = 422


class Unavailable(DlvError):
    """503 — the ledger, the store, the sandbox or the model is unavailable; nothing took effect."""

    status_code = 503


class Refused(DlvError):
    """503 with reason items — the run is refused before anything is created (SANDBOX_UNAVAILABLE, ...)."""

    status_code = 503

    def __init__(self, message: str, reasons: list[dict], **body):
        from zbm_delivery import reasons as R

        super().__init__(message, reasons=reasons, reason_lines=R.lines(reasons), **body)
