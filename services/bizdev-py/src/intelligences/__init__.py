"""The single-task New Business Development intelligences (ADR 0016 decision 5). Each is deterministic (no model call
in v1), does one job, and states what it decides and what it never does. service.py orchestrates them; none of them
writes state."""

from __future__ import annotations

from importlib import import_module

MODULES = ("i01_qualification", "i02_identity", "i03_deadline", "i04_assembly", "i05_gov_checklist",
           "i06_sensitivity", "i07_deal_threshold", "i08_templates", "i09_replies", "i10_suppression",
           "i11_commission", "i12_tax_refs", "i13_audit_export")


def registry() -> list[dict]:
    out = []
    for name in MODULES:
        m = import_module(f"intelligences.{name}")
        out.append({"number": m.NUMBER, "name": m.NAME, "decides": m.DECIDES, "module": name})
    return out
