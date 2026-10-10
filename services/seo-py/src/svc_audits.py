"""The audit product (ADR 0017 decision 13). Stage A: the run bookkeeping only; stage E adds the audit itself."""

from __future__ import annotations


class AuditsMixin:
    def mark_interrupted_audits(self) -> dict:
        """No audit exists before stage E."""
        return {"interrupted": 0}
