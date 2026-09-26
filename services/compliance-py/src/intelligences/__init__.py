"""The 11 single-task, deterministic intelligences of Compliance (38), spec §C."""

from __future__ import annotations

from intelligences import (i01_register, i02_activation_gate, i03_payout_gate, i04_publish_gate, i05_control_monitor,
                           i06_change_watcher, i07_jurisdiction, i08_sanctions, i09_disclosure, i10_accessibility,
                           i11_evidence_audit)

ALL = (i01_register, i02_activation_gate, i03_payout_gate, i04_publish_gate, i05_control_monitor, i06_change_watcher,
       i07_jurisdiction, i08_sanctions, i09_disclosure, i10_accessibility, i11_evidence_audit)


def registry() -> list[dict]:
    return [{"number": m.NUMBER, "name": m.NAME, "actor": m.ACTOR, "llm": False} for m in ALL]
