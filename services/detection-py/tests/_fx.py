"""Shared test constants for the Revenue Recovery fix wave (Oct 6 2026):
every detect call names its tenant, and the renewal agent its as-of instant."""

from datetime import datetime, timezone

from fixtures_loader import FIXTURE_CLIENT_ID

TENANT = FIXTURE_CLIENT_ID
# After every fixture renewal date (2026-05/06) — a fixed instant, never the clock.
AS_OF = datetime(2026, 10, 1, tzinfo=timezone.utc)
AS_OF_WIRE = "2026-10-01T00:00:00Z"


def make_finding(**overrides):
    """A valid Finding for tests that need one by hand: finding_id is derived
    (E-3), and evidence_class follows recoverable_value (E-4)."""
    from zbm_schema import (
        CauseCertainty,
        EntityType,
        EvidenceClass,
        LeakCategory,
        new_finding,
    )

    fields = {
        "client_id": TENANT, "agent_id": "test-agent", "leak_category": LeakCategory.DISCOUNT_MISUSE,
        "entity_type": EntityType.ORDER, "entity_id": "ord_x", "customer_id": "cust_x",
        "cause_certainty": CauseCertainty.NAMED, "cause_description": "test finding",
        "recoverable_value": None, "methodology_id": "test_method", "methodology": "test methodology",
    }
    fields.update(overrides)
    fields.setdefault("evidence_class",
                      EvidenceClass.UNKNOWN if fields["recoverable_value"] is None else EvidenceClass.OBSERVED)
    return new_finding(**fields)
