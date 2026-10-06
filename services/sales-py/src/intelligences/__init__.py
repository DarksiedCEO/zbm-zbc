"""The single-task Sales intelligences (ADR 0013 decision 5). Each is deterministic (no model call in v1), does one
job, and states what it decides and what it never does. service.py orchestrates them; none of them writes state."""

from __future__ import annotations

from importlib import import_module

MODULES = ("i01_intake", "i02_identity", "i03_scoring", "i04_routing", "i05_suppression", "i06_consent",
           "i07_quiet_hours", "i08_templates", "i09_send_cap", "i10_replies", "i11_pricing", "i12_audit_export")


def registry() -> list[dict]:
    out = []
    for name in MODULES:
        m = import_module(f"intelligences.{name}")
        out.append({"number": m.NUMBER, "name": m.NAME, "decides": m.DECIDES, "module": name})
    return out
