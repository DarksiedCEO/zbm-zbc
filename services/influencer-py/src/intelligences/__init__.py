"""The single-task Influencer & Partnership Marketing intelligences (ADR 0015 decision 5). Each is deterministic (no
model call), does one job, and states what it decides and what it never does. The service orchestrates them; none of
them writes state or calls a port."""

from __future__ import annotations

from importlib import import_module

MODULES = ("i01_intake", "i02_identity", "i03_fit", "i04_suppression", "i05_templates", "i06_send_cap",
           "i07_replies", "i08_disclosure", "i09_deal_approval", "i10_dm_guard", "i11_audit_export")


def registry() -> list[dict]:
    out = []
    for name in MODULES:
        m = import_module(f"intelligences.{name}")
        out.append({"number": m.NUMBER, "name": m.NAME, "decides": m.DECIDES, "module": name})
    return out
