"""Reviewer's driver: puts generated cases through the REAL service path (HTTP API -> svc_attribution -> farrah ->
vault -> owner view) with the reviewer's own fixture store of Osei's datasets (GF-2 interface: ``view(tid)``)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
from types import SimpleNamespace

from svc_vaulted import canonical

CLOCK = datetime(2029, 1, 10, 18, 0, tzinfo=timezone.utc)


class Store:
    def __init__(self):
        self.t = {}
        self.reads = 0
        self.raise_on_view = None

    def reset(self, tid):
        self.t[tid] = {"datasets": [], "records": [], "reconciliation": None, "freshness": None}
        return self.t[tid]

    def view(self, tid):
        self.reads += 1
        if self.raise_on_view:
            raise self.raise_on_view
        x = self.t.get(tid) or {"datasets": [], "records": [], "reconciliation": None, "freshness": None}
        return SimpleNamespace(datasets=list(x["datasets"]), records=list(x["records"]),
                               reconciliation=x["reconciliation"], freshness=x["freshness"])


def record(svc, doc, tid=None, kind=None, state="VALID", mac=None):
    tid = tid or doc["tenant_id"]
    if mac is None:
        mac = svc.vault.field_hash(doc["tenant_id"], "dataset", canonical(doc))[1]
    return {"kind": kind or {"analytics": "source_read_recorded", "crm": "crm_import_recorded"}[doc["source"]],
            "tenant_id": tid, "dataset_id": doc["dataset_id"], "read_id": doc["read_id"], "validation_state": state,
            "dataset_hmac": mac, "at": "2026-10-05T12:00:00Z"}


def freshness_doc(tid, basis, state):
    return {"contract": "osei.panel_freshness/1", "tenant_id": tid, "produced_at": "2026-10-05T12:00:00Z",
            "panels": [{"panel": "revenue", "state": state, "freshness_timestamp": "2026-10-05T12:00:00Z",
                        "inputs": [{"producer": basis, "contract": "osei.dataset/1", "run_id": "read-1",
                                    "produced_at": "2026-10-05T12:00:00Z", "tier": "first_party", "state": state}]}]}


def rid():
    return str(uuid.uuid4())


def make_harness(tmp, data_dir=None, **env):
    from helpers import Harness
    from clock import FixedClock
    h = Harness(tmp, data_dir=data_dir, clock=FixedClock(CLOCK), **env)
    h.store = Store()
    h.svc.osei_source = h.store
    return h


def put_plan(h, tid, plan="managed_2", basis="analytics", caller="dashboard", andre=True):
    return h.client.put(f"/seo/v1/tenants/{tid}/plan", json={"request_id": rid(), "plan": plan,
                                                             "revenue_basis": basis},
                        headers=h.headers(caller, andre=andre))


def put_map(h, tid, cmap, caller="dashboard", andre=True):
    body = {"request_id": rid(), **{c: list(cmap.get(c, [])) for c in ("organic_search", "ai_referral",
                                                                       "other_known")}}
    return h.client.put(f"/seo/v1/tenants/{tid}/channel-map", json=body, headers=h.headers(caller, andre=andre))


def install_case(h, case, tid="acme"):
    from gen_rv_farrah import month_range
    t = h.store.reset(tid)
    doc = case.basis_doc
    t["datasets"].append(doc)
    t["records"].append(record(h.svc, doc))
    if case.state is not None:
        t["freshness"] = freshness_doc(tid, case.basis, case.state)
    if case.other_doc is not None:
        t["datasets"].append(case.other_doc)
        t["records"].append(record(h.svc, case.other_doc))
        first, last = month_range(case.month)
        t["reconciliation"] = {"contract": "osei.reconciliation/1", "tenant_id": tid,
                               "produced_at": "2026-10-05T12:00:00Z", "items": [{
                                   "rule": "RC-4", "datasets": [doc["dataset_id"], case.other_doc["dataset_id"]],
                                   "window": {"start": first.isoformat(), "end": last.isoformat(),
                                              "timezone": doc["window"]["timezone"]},
                                   "values": {"crm_won_revenue": "0", "analytics_revenue": "0",
                                              "difference": case.recon_d or "0"},
                                   "result": "COMPARED", "label": "measured",
                                   "evidence_ref": {"run_id": "rec-1", "contract": "osei.reconciliation/1",
                                                    "item_key": "RC-4"}}]}


def post_run(h, tid, month, caller="dashboard", tenant=None, request_id=None):
    return h.post(f"/tenants/{tid}/attribution-runs", {"request_id": request_id or rid(), "month": month},
                  caller=caller, tenant=tenant)


def frac(s):
    return None if s is None else Fraction(Decimal(s))


def compare(result: dict, expected: dict) -> list:
    """Mismatch descriptions (machine words only, never values)."""
    bad = []
    for c, e in expected.items():
        got = result["channels"][c]
        if got["label"] != e["label"]:
            bad.append(f"{c}:label:{got['label']}!={e['label']}")
        for k in ("figure", "lower", "upper"):
            if frac(got[k]) != e[k]:
                bad.append(f"{c}:{k}")
    return bad
