"""The closed catalogue of reason codes Search & Answer Intelligence (2) answers with. An error body carries one of
these and nothing from the request (no domain, no page text). ``R(code)`` refuses a code not listed here."""

from __future__ import annotations

CODES = frozenset({
    # callers and Andre
    "CALLER_UNKNOWN", "CALLER_NOT_ALLOWED", "ANDRE_APPROVAL_REQUIRED", "ANDRE_APPROVAL_INVALID", "ANDRE_NOT_CONFIGURED",
    # tenants
    "TENANT_NOT_FOUND", "TENANT_EXISTS", "TENANT_KIND_INVALID", "TENANT_TOKEN_UNKNOWN", "DOMAIN_INVALID",
    "DOMAIN_NOT_AUTHORIZED", "DOMAIN_LIMIT", "DOMAIN_TAKEN",
    # kill switches
    "KILLED_GLOBAL", "KILLED_TENANT", "KILLED_CAPABILITY", "KILLED_PROVIDER", "KILLED_WRITE", "SWITCH_UNKNOWN",
    # audits
    "AUDIT_NOT_FOUND", "AUDIT_RUNNING", "INVOICE_REQUIRED", "INVOICE_ID_INVALID", "INVOICE_NOT_FOR_OWN_TENANT",
    "PAGES_INVALID", "AUDIT_TOO_LARGE",
    # entity records
    "ENTITY_NOT_FOUND", "ENTITY_FIELD_UNKNOWN", "ENTITY_VALUE_INVALID", "ENTITY_VERSION_STALE",
    # prompt sets and probes
    "PROMPT_SET_NOT_FOUND", "PROMPT_SET_EXISTS", "PROMPT_SET_INVALID", "PROVIDER_UNKNOWN",
    # generic
    "REQUEST_ID_REUSED", "LEDGER_UNAVAILABLE", "STORE_UNAVAILABLE", "INTEGRITY_UNVERIFIED", "SERVICE_CLOSED",
    "JOB_UNKNOWN", "JOB_RUNNING", "INVALID", "FORBIDDEN_FIELD",
})


def R(code: str) -> str:
    if code not in CODES:
        raise AssertionError(f"reason code not in the catalogue: {code}")
    return code
