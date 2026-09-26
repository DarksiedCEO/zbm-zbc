"""The ten V&I intelligences (spec §C). Each decides one thing; the service wires them to records and ports."""

from __future__ import annotations

from intelligences import (i01_platform_metrics, i02_view_certifier, i03_clip_fingerprint, i04_liveness,
                           i05_age_assurance, i06_engagement_anomaly, i07_duplicate_identity, i08_stolen_content,
                           i09_strike_ledger, i10_evidence_audit)

ALL = (i01_platform_metrics, i02_view_certifier, i03_clip_fingerprint, i04_liveness, i05_age_assurance,
       i06_engagement_anomaly, i07_duplicate_identity, i08_stolen_content, i09_strike_ledger, i10_evidence_audit)


def registry() -> list[dict]:
    return [{"number": m.NUMBER, "name": m.NAME, "actor": m.ACTOR} for m in ALL]
