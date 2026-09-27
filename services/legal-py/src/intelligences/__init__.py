"""The ten single-task Legal intelligences (spec §C). Each module states what it decides and what it never does."""

from __future__ import annotations

from importlib import import_module

MODULES = ("i01_documents", "i02_playbooks", "i03_acceptance", "i04_obligations", "i05_memo_intake", "i06_matters",
           "i07_takedowns", "i08_filings", "i09_music_policy", "i10_evidence_audit")


def registry() -> list[dict]:
    out = []
    for name in MODULES:
        m = import_module(f"intelligences.{name}")
        out.append({"number": m.NUMBER, "name": m.NAME, "actor": m.ACTOR, "module": name})
    return out
