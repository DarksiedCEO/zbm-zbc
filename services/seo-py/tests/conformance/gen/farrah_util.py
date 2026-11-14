"""Shared steps of Farrah's conformance tests (plan, channel map, runs) over the real API."""

from __future__ import annotations

import uuid

from conformance.gen.attribution import OseiStore


def rid() -> str:
    return str(uuid.uuid4())


def setup(h, tid: str = "acme", plan: str = "managed_2", basis: str = "analytics") -> None:
    h.store = OseiStore()
    h.svc.osei_source = h.store
    if tid not in h.svc.tenants:
        h.tenant(tid)
    if plan is not None:
        set_plan(h, tid, plan, basis)


def set_plan(h, tid: str, plan: str, basis: str = "analytics", andre: bool = True, caller: str = "dashboard"):
    r = h.client.put(f"/seo/v1/tenants/{tid}/plan", json={"request_id": rid(), "plan": plan, "revenue_basis": basis},
                     headers=h.headers(caller, andre=andre))
    return r


def set_map(h, tid: str, mapping: dict, andre: bool = True, caller: str = "dashboard"):
    body = {"request_id": rid(), **{c: list(mapping.get(c, [])) for c in ("organic_search", "ai_referral",
                                                                           "other_known")}}
    return h.client.put(f"/seo/v1/tenants/{tid}/channel-map", json=body, headers=h.headers(caller, andre=andre))


def run(h, tid: str = "acme", month: str = "2026-09", caller: str = "dashboard", request_id=None, tenant=None):
    return h.post(f"/tenants/{tid}/attribution-runs", {"request_id": request_id or rid(), "month": month},
                  caller=caller, tenant=tenant)


def ok_run(h, **kw) -> dict:
    r = run(h, **kw)
    assert r.status_code == 201, (r.status_code, r.text[:300])
    return r.json()


def simple(h, tid: str = "acme", n_org: int = 40, n_ai: int = 12, n_unk: int = 1, rev: str = "100.00",
           unk_rev: str = "10.00", basis: str = "analytics", state: str = "FRESH", **kw) -> dict:
    """A plain VALID, recorded basis dataset for 2026-09 (organic, AI and a little unknown revenue) and a FRESH
    basis input on Osei's revenue panel."""
    import random
    from datetime import date
    from conformance.gen.attribution import arow, crow, dataset, freshness
    rng = random.Random(7)
    rows = []
    for i in range(n_org):
        d = date(2026, 9, 1 + i % 30)
        rows.append(arow(d, rev, landing=f"/o/{i}") if basis == "analytics" else
                    crow(d, rev, utm_medium="organic", utm_source="google", ref=f"o{i}"))
    for i in range(n_ai):
        d = date(2026, 9, 1 + i % 30)
        rows.append(arow(d, rev, "chatgpt.com", "referral", "Referral", f"/a/{i}") if basis == "analytics" else
                    crow(d, rev, utm_medium="referral", utm_source="chatgpt.com", ref=f"a{i}"))
    for i in range(n_unk):
        d = date(2026, 9, 2)
        rows.append(arow(d, unk_rev, "(not set)", "(not set)", "Unassigned", "(not set)") if basis == "analytics"
                    else crow(d, unk_rev, lead_source="Walk in", ref=f"u{i}"))
    doc = dataset(tid, basis, rows, rng=rng, **kw)
    h.store.add(h.svc, doc)
    t = h.store.tenant(tid)
    t["freshness"] = freshness(tid, {basis: state})
    return doc
