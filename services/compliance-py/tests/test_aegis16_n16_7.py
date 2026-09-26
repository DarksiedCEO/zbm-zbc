"""AEGIS N16-7 (shared evidence-audit code with verification-py and clipper-network-py): a
``register_version_published`` event on the ledger alone must not brick start-up. It is honoured only when its id
and payload hash match a decision record of the local log; any other one is voidable through Andre's recorded
reconcile. Written first; it failed on integration-2026-09-24 @ 923e20c ("no reconcile can void a published
version")."""

from __future__ import annotations

import pytest

from test_aegis15 import _durable, _epoch, _reconcile, _reconcile_plan, _restart


def test_n16_7_forged_register_version_event_needs_andre_void_not_a_brick(tmp_path):
    x = _durable(tmp_path)
    v = x.get("/health").json()["register_version_in_force"]
    fake = f"cmp-ver-{_epoch(x)}-99-{'a' * 32}"
    x.ledger.record_event(fake, "compliance", "register_version_published", "intel_01_register", "register:v99",
                          {"version": 99}, "forged by hand")
    with pytest.raises(RuntimeError, match="only Andre|reconcile"):
        _restart(x, tmp_path)                                    # a normal start refuses ...
    y = _restart(x, tmp_path, COMPLIANCE_RECONCILE_MODE="1")      # ... reconcile mode starts
    plan = _reconcile_plan(y)
    assert fake in plan["voidable"]["event_ids"] and not plan["fatal"], plan
    assert _reconcile(y, plan).status_code == 200
    rec = [e for e in x.ledger.events if e["event_type"] == "reconcile"][-1]
    assert rec["actor"] == "andre"
    z = _restart(x, tmp_path)
    assert z.get("/health").json()["register_version_in_force"] == v
    assert z.run_controls()["results"]["C-11"]["result"] == "pass"


def test_n16_7_genuine_version_events_are_honoured(tmp_path):
    x = _durable(tmp_path)
    y = _restart(x, tmp_path)
    assert y.run_controls()["results"]["C-11"]["result"] == "pass"
