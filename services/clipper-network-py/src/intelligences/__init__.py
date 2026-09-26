"""The 10 single-task, deterministic intelligences of Clipper Network (spec §C). No LLM calls."""

from __future__ import annotations

from intelligences import (i01_recruiting, i02_admission, i03_tiering, i04_enrolment, i05_kit_delivery, i06_comms,
                           i07_disputes, i08_discipline, i09_offboarding, i10_evidence_audit)

ALL = (i01_recruiting, i02_admission, i03_tiering, i04_enrolment, i05_kit_delivery, i06_comms, i07_disputes,
       i08_discipline, i09_offboarding, i10_evidence_audit)


def registry() -> list[dict]:
    return [{"number": m.NUMBER, "name": m.NAME, "actor": m.ACTOR, "llm": False} for m in ALL]
