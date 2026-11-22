"""core-spec/2 V-4, V-5, V-6 (card C2-10). V-4: the vault is decrypted ONLY for the owning tenant's display surface
(owner caller with tenant authentication: the audited reveal, RV-1), plus the ONE non-display exception E-COMP
(OWNER DECISION 2026-11-15 02:00 PT: the tenant's own finance client reference placed into the Compliance
activation request). Never for a log, ledger, event, error body, Finance (31) / Compliance (38) view, export,
/health, /status or another network peer. Every AES-GCM decrypt in src goes through vault._Material._aead (vault.py
is the only AESGCM user), so wrapping vault.AESGCM sees every decrypt. Synthetic values; counts only."""
from __future__ import annotations

import inspect
import json
from datetime import timedelta

import pytest

import svc_reveal
import vault as vault_mod
from helpers import T0, Harness
from test_reveal import A, B, _cid, _run, _tokens

N5 = 200
CALLS: list = []


class _Tracing:
    """Delegates to the real AESGCM; records the src function names on the stack of every decrypt."""

    def __init__(self, key):
        self._a = _REAL(key)

    def encrypt(self, *a):
        return self._a.encrypt(*a)

    def decrypt(self, *a):
        CALLS.append(tuple(f.function for f in inspect.stack()[1:40]))
        return self._a.decrypt(*a)


_REAL = vault_mod.AESGCM


@pytest.fixture
def traced(monkeypatch):
    CALLS.clear()
    monkeypatch.setattr(vault_mod, "AESGCM", _Tracing)
    yield CALLS
    CALLS.clear()


@pytest.fixture
def two(tmp_path, traced):
    h = Harness(tmp_path, data_dir=str(tmp_path / "data"))
    h.tenant(A)
    h.tenant(B)
    h.svc.settings.reveal = True
    clock = [T0]
    h.svc.now = lambda: clock[0]
    h.clock = clock
    return h


def _non_owner_requests(cid: str):
    """(label, path, caller, tenant) of every surface V-4 names, plus error bodies (unknown / cross tenant)."""
    out = [("status", "/status", "dashboard", None), ("export", "/audit/export", "dashboard", None),
           ("evidence", "/audit/evidence", "dashboard", None), ("integrity", "/audit/integrity", "dashboard", None),
           ("department", "/department", "dashboard", None), ("tenants", "/tenants", "dashboard", None)]
    for caller in ("finance_31", "compliance_38"):
        for p in ("/status", "/tenants", f"/tenants/{A}", f"/tenants/{A}/citation-runs",
                  f"/tenants/{A}/citation-runs/{cid}", f"/tenants/{A}/citation-runs/{cid}/mnc-priority",
                  f"/tenants/{A}/audits", "/audit/export"):
            out.append((caller, p, caller, None))
    # C2-10d (lead ruling 10:05 PT, F10b-1): dashboard / seo_agent have no tenant authentication -> code-only view
    for caller in ("dashboard", "seo_agent"):
        for p in (f"/tenants/{A}/citation-runs/{cid}", f"/tenants/{A}/citation-runs/{cid}/mnc-priority"):
            out.append((f"{caller}-run", p, caller, None))
    out += [("error-unknown", f"/tenants/{A}/citation-runs/{_cid(999)}", "dashboard", None),
            ("error-cross", f"/tenants/{B}/citation-runs/{cid}", "dashboard", None),
            ("other-tenant", f"/tenants/{B}/citation-runs", "hub", B)]
    return out


def test_v4_decrypt_only_on_the_owner_display_surface(two, traced):
    toks = _tokens(1001, 20)
    codes = _run(two.svc, A, _cid(1), toks)
    traced.clear()
    per = {}
    for label, path, caller, tenant in _non_owner_requests(_cid(1)):
        n0 = len(traced)
        r = two.get(path, caller=caller, tenant=tenant)
        per[label] = per.get(label, 0) + (len(traced) - n0)
        assert all(t not in r.text for t in toks)
    n0 = len(traced)
    two.svc.health()
    per["health"] = len(traced) - n0
    print("V-4 decrypts per non-owner surface:", json.dumps(per, sort_keys=True))
    # F10-1 fixed (C2-10b, lead ruling 09:10 PT): compliance_38 is refused on citation-runs/{cid} and /mnc-priority
    # before any read, so every non-owner surface here (Compliance included) decrypts nothing.
    assert sum(per.values()) == 0, per
    # positive control: the owner reveal (hub caller + tenant A token) decrypts, and only via the reveal path
    n0 = len(traced)
    req = {"surface": "citation_run", "run_id": _cid(1), "span": codes[toks[0]]}
    r = two.post(f"/tenants/{A}/reveals", req, caller="hub", tenant=A)
    assert r.status_code == 200 and r.json() == {"value": toks[0]}
    owner = traced[n0:]
    assert owner and all("reveal_span" in s for s in owner)
    # the same reveal by every non-owner caller: refused, 0 decrypts
    for caller, tenant in (("dashboard", None), ("seo_agent", None), ("finance_31", None), ("compliance_38", None),
                           ("hub", B)):
        n0 = len(traced)
        r = two.post(f"/tenants/{A}/reveals", req, caller=caller, tenant=tenant)
        assert r.status_code in (403, 404) and toks[0] not in r.text and len(traced) == n0


def test_v5_planted_own_text_found_for_owner_only(two, traced):
    toks = _tokens(1501, N5)
    codes = _run(two.svc, A, _cid(1), toks)
    found = 0
    for i, t in enumerate(toks):
        two.clock[0] = T0 + timedelta(hours=i // svc_reveal.RATE_CAP, seconds=i % svc_reveal.RATE_CAP)
        r = two.post(f"/tenants/{A}/reveals", {"surface": "citation_run", "run_id": _cid(1), "span": codes[t]},
                     caller="hub", tenant=A)
        found += r.status_code == 200 and r.json() == {"value": t}
    leaked = 0
    for label, path, caller, tenant in _non_owner_requests(_cid(1)):
        if label in ("finance_31", "compliance_38", "other-tenant", "error-cross"):
            body = two.get(path, caller=caller, tenant=tenant).text
            leaked += sum(t in body for t in toks)
    for caller, tenant in (("hub", B), ("finance_31", None), ("compliance_38", None)):
        for t in toks[:50]:
            r = two.post(f"/tenants/{A}/reveals", {"surface": "citation_run", "run_id": _cid(1), "span": codes[t]},
                         caller=caller, tenant=tenant)
            leaked += t in r.text
    print(f"V-5 planted={N5} owner_found={found} other_tenant_finance_compliance_found={leaked}")
    assert found == N5 and leaked == 0


@pytest.mark.local_http
def test_v4_e_comp_is_the_single_non_display_decrypt(tmp_path, srv, traced):
    from test_e_comp import _paid
    from test_invoice_verification import FakeFinance, client_tenant, harness
    f = FakeFinance()
    h = harness(tmp_path, srv, f)
    client_tenant(h)
    traced.clear()
    _paid(h, f)
    comp = [s for s in traced if "_verify_compliance" in s]
    # E-COMP: inside the Compliance step the vault is opened ONLY by _fc_open (the tenant's own reference). The other
    # openings of this flow are the audit's own run (_execute_audit) and the 201 answer to its caller (audit_view).
    assert comp and all("_fc_open" in s for s in comp)
    assert all("_fc_open" not in s or "_verify_compliance" in s for s in traced)
    ecomp = comp
    n0 = len(traced)
    for caller in ("dashboard", "finance_31", "compliance_38"):
        h.get("/tenants/acme", caller=caller)
        h.get("/audit/export", caller=caller)
    h.svc.tenant_view("acme")
    print(f"V-4 E-COMP decrypts={len(ecomp)} after-views={len(traced) - n0}")
    assert len(traced) == n0


def test_v4_compliance_38_citation_run_read_decrypts_nothing(two, traced):
    """F10-1 (C2-10b, lead ruling 09:10 PT): V-4 never decrypts for a Compliance (38) view; no frozen spec line grants
    Compliance the run document, so both routes refuse it (403 CALLER_NOT_ALLOWED) before any read."""
    _run(two.svc, A, _cid(1), _tokens(1002, 5))
    traced.clear()
    for path in (f"/tenants/{A}/citation-runs/{_cid(1)}", f"/tenants/{A}/citation-runs/{_cid(1)}/mnc-priority"):
        r = two.get(path, caller="compliance_38")
        assert r.status_code == 403 and "CALLER_NOT_ALLOWED" in r.text, (path.rsplit("/", 1)[-1], r.status_code)
    assert traced == []
    # the run list (no vault read) keeps Compliance in its caller set (the synthetic _run record has no list fields,
    # so only "not refused by the caller check" is asserted); the other readers keep the run document
    assert two.get(f"/tenants/{A}/citation-runs", caller="compliance_38").status_code == 200   # F10b-3 fixture
    assert two.get(f"/tenants/{A}/citation-runs/{_cid(1)}", caller="dashboard").status_code == 200


def _completed(svc, tid: str, cid: str, toks: list) -> dict:
    """``_run`` recorded as a completed run (the status the MNC list needs)."""
    codes = _run(svc, tid, cid, toks)
    with svc.lock:
        svc.citation_runs[cid]["status"] = "completed"
    return codes


def test_v4_no_tenant_auth_gets_the_code_only_run_view(two, traced):
    """F10b-1 (C2-10d, lead ruling 10:05 PT): V-4 / D-02h decrypt only for the owner caller WITH tenant
    authentication. dashboard / seo_agent get the code-only view of the run and of its MNC list, 0 decrypts; the
    owner hub + tenant token keeps the full view (decrypts)."""
    toks = _tokens(1003, 5)
    _completed(two.svc, A, _cid(1), toks)
    run, mnc = f"/tenants/{A}/citation-runs/{_cid(1)}", f"/tenants/{A}/citation-runs/{_cid(1)}/mnc-priority"
    per = {}
    for caller in ("dashboard", "seo_agent"):
        traced.clear()
        r = two.get(run, caller=caller)
        assert r.status_code == 200 and "result" not in r.json() and r.json()["citation_run_id"] == _cid(1)
        m = two.get(mnc, caller=caller)
        assert m.status_code == 200 and m.json()["items"] is None and m.json()["view"] == "codes_only"
        assert all(t not in r.text + m.text for t in toks)
        per[caller] = len(traced)
    traced.clear()
    r = two.get(run, caller="hub", tenant=A)
    per["hub+A"] = len(traced)
    print("V-4 run-view decrypts per caller:", json.dumps(per, sort_keys=True))
    assert r.status_code == 200 and "result" in r.json()
    assert per["dashboard"] == 0 and per["seo_agent"] == 0 and per["hub+A"] > 0


def test_v4_mnc_409_is_decided_before_any_vault_open(two, traced):
    """F10b-2 (C2-10d): a run that is not completed answers 409 CITATION_RUN_NOT_COMPLETED without opening the vault,
    for every reader (the owner included): no error body is built after a decrypt."""
    _run(two.svc, A, _cid(1), _tokens(1004, 3))            # status "recorded": not completed
    for caller, tenant in (("hub", A), ("dashboard", None), ("seo_agent", None)):
        traced.clear()
        r = two.get(f"/tenants/{A}/citation-runs/{_cid(1)}/mnc-priority", caller=caller, tenant=tenant)
        assert r.status_code == 409 and "CITATION_RUN_NOT_COMPLETED" in r.text and traced == [], caller
