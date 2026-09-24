"""Typed errors. The API maps each class to exactly one HTTP status."""

from __future__ import annotations


class CreativeError(Exception):
    """Base class. `reason` is the human-readable, API-visible explanation."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class NotFound(CreativeError):
    """404 — the referenced object does not exist."""


class GuardrailViolation(CreativeError):
    """403 — the action is forbidden for this actor (self-approval, wrong
    role, wrong registry row owner, unknown actor)."""


class FounderApprovalRefused(CreativeError):
    """403 — Andre's approval token was missing, wrong, or not configured."""


class FrozenError(CreativeError):
    """409 — the object is frozen (a live or superseded rulebook version)."""


class PreconditionFailed(CreativeError):
    """409 — the step is out of order (e.g. production without an approved
    brief, Andre approval before Compliance passed)."""


class ValidationFailed(CreativeError):
    """422 — the input is structurally valid JSON but breaks a domain rule.
    `issues` lists every rule broken, not just the first."""

    def __init__(self, reason: str, issues: list[str] | None = None):
        super().__init__(reason)
        self.issues = list(issues or [])


class RegistryRowBlocked(CreativeError):
    """A Platform Rules Registry row is expired, unverified, or missing, so
    its use is blocked (fail closed)."""

    def __init__(self, row_id: str, reason: str):
        super().__init__(f"registry row {row_id!r} blocked: {reason}")
        self.row_id = row_id
