"""C2-6g3: E-COMP (OWNER DECISION 2026-11-15 02:00 PT; lead ruling 02:40 PT). The tenant's finance client reference
is stored at every sink only as its T-2 code (``c:text:<16hex>``); the raw value lives only in the owning tenant's
operator text vault and is opened for exactly one purpose: the Compliance activation request to the configured
Compliance host. Spec T-2, T-5, T-6 (D-05 forms), V-2, V-4, O-4 I. Synthetic values only; nothing is printed but
counts and seconds."""
from __future__ import annotations

import json
import os
import time

import pytest

import compliance_client as cc
import outbound
import service
import vault as vault_mod
from helpers import rid
from test_canary_all_routes import forms
from test_invoice_verification import CLIENT, FakeFinance, client_tenant, harness, paid_audit
from test_outbound_clients import _cmp, _recorder

pytestmark = pytest.mark.local_http


def _sinks(h, tmp_path, caplog) -> str:
    """Every sink this harness can write: files under tmp_path (data dir, ledger, seo_log, vault blobs: AES-GCM),
    the in-memory seo_log, the ledger object and every log record."""
    parts = [caplog.text, json.dumps(h.svc.log, default=repr), json.dumps(vars(h.ledger), default=repr)]
    for root, _dirs, files in os.walk(tmp_path):
        for fn in files:
            with open(os.path.join(root, fn), "rb") as fh:
                parts.append(fh.read().decode("latin-1"))
    return "\n".join(parts)


def _readable(blob: str, raw: str) -> int:
    return sum(1 for f in forms(raw) if f in blob)


def _paid(h, f, code=201):
    return h.ok(paid_audit(h, invoice_id=f.add()), code)


def test_the_planted_reference_is_never_readable_in_any_sink_in_any_d05_form(tmp_path, srv, caplog):
    caplog.set_level("DEBUG")
    f = FakeFinance()
    h = harness(tmp_path, srv, f)
    client_tenant(h)
    _paid(h, f)
    code = h.svc.tenants["acme"]["finance_client_id"]
    assert code.startswith("c:text:") and len(code) == 23
    blob = _sinks(h, tmp_path, caplog)
    assert forms(CLIENT) and _readable(blob, CLIENT) == 0
    assert code in blob                                   # the sinks hold the code (T-2)
    assert h.svc.tenant_view("acme")["finance_client_id"] == code


def test_compliance_receives_the_raw_value_and_nothing_else(tmp_path, srv):
    f = FakeFinance()
    h = harness(tmp_path, srv, f)
    client_tenant(h)
    h.ports.compliance.asked.clear()
    _paid(h, f)
    assert h.ports.compliance.asked == [CLIENT]


def test_the_activation_request_to_any_other_host_is_refused_and_never_sent():
    coder = vault_mod.Coder(vault_mod.ephemeral_vault_key(), "acme")
    code = "c:text:" + coder.code("text", "acme-live")
    for host in ("compliance.internal.test.evil", "evil.test", "COMPLIANCE.internal.test", None):
        seen, t = _recorder()
        with outbound.e_comp(code, coder, host), pytest.raises(cc.ComplianceCheckError) as e:
            _cmp(t).activation("acme-live")
        assert e.value.code == "COMPLIANCE_RESPONSE_INVALID" and seen == []
    seen, t = _recorder()                                  # another tenant's coder: the code differs, refused
    other = vault_mod.Coder(vault_mod.ephemeral_vault_key(), "globex")
    with outbound.e_comp(code, other, "compliance.internal.test"), pytest.raises(cc.ComplianceCheckError):
        _cmp(t).activation("acme-live")
    assert seen == []
    seen, t = _recorder()                                  # control: the configured host is reached once
    with outbound.e_comp(code, coder, "compliance.internal.test"), pytest.raises(cc.ComplianceCheckError):
        _cmp(t).activation("acme-live")
    assert len(seen) == 1 and seen[0].url.host == "compliance.internal.test"


def test_another_tenant_s_record_is_never_decryptable(tmp_path, srv):
    h = harness(tmp_path, srv, FakeFinance())
    client_tenant(h)
    h.tenant("globex", "client", ("globex.test",))
    h.ok(h.post("/tenants/globex/finance-client", {"request_id": rid(), "finance_client_id": CLIENT}, andre=True))
    ca, cg = (h.svc.tenants[t]["finance_client_id"] for t in ("acme", "globex"))
    assert ca != cg                                        # same raw value, tenant-bound codes
    h.svc._fc_vids().clear()                               # force the scan path (as after a restart)
    assert h.svc._fc_open("globex", ca) is None and h.svc._fc_open("acme", cg) is None
    assert (h.svc._fc_open("acme", ca) == CLIENT) is True and (h.svc._fc_open("globex", cg) == CLIENT) is True
    # the cached vault ids are per (tenant, code): a cached id never opens for the other tenant
    vid = h.svc._fc_vids()[("acme", ca)]
    with pytest.raises(vault_mod.VaultError):
        h.svc.optext_vault.get("globex", vid)


def test_restart_and_replay_keep_the_binding(tmp_path, srv):
    f = FakeFinance()
    h = harness(tmp_path, srv, f, data_dir=str(tmp_path / "data"))
    client_tenant(h)
    code = h.svc.tenants["acme"]["finance_client_id"]
    h2 = h.restart()
    assert h2.svc.tenants["acme"]["finance_client_id"] == code and h2.svc._fc_vids() == {}
    h2.ports.compliance.asked.clear()
    _paid(h2, f)
    assert h2.ports.compliance.asked == [CLIENT] and ("acme", code) in h2.svc._fc_vids()


def test_the_one_time_scan_after_a_restart_at_10000_tenants(tmp_path, srv):
    """Lead ruling 02:40 PT: the scan cost is measured (counts + seconds only). The own record is the OLDEST, so the
    scan (newest first) passes every other tenant's record first: the worst case."""
    h = harness(tmp_path, srv, FakeFinance())
    client_tenant(h)
    code = h.svc.tenants["acme"]["finance_client_id"]
    v = h.svc.optext_vault
    t0 = time.perf_counter()
    for i in range(10_000):
        tid = f"t{i:05d}"
        v.put(tid, {"kind": h.svc.FC_KIND, "code": "c:text:" + v.coder(tid).code("text", CLIENT), "ref": CLIENT})
    put_s = time.perf_counter() - t0
    h.svc._fc_vids().clear()
    t0 = time.perf_counter()
    got = h.svc._fc_open("acme", code)
    scan_s = time.perf_counter() - t0
    t0 = time.perf_counter()
    again = h.svc._fc_open("acme", code)
    cached_s = time.perf_counter() - t0
    print(f"E-COMP scan: records={len(getattr(v, '_index', {}))} put_s={put_s:.2f} scan_s={scan_s:.3f} "
          f"cached_s={cached_s:.5f}")
    assert (got == CLIENT) is True and (again == CLIENT) is True
    assert len(h.svc._fc_vids()) == 1 and cached_s < scan_s


def test_a_pre_change_line_is_replayed_as_its_code_and_writes_no_raw_value(tmp_path, srv, caplog):
    """OWNER DECISION 2026-11-15 ~15:40 PT "count and contain" (C2-10c3; supersedes lead ruling 02:40 PT F6g2-1): a
    line written before E-COMP holds the raw reference in tenant_finance_client_set; replay binds its T-2 code and
    seals the raw value in the tenant's vault once, so the Compliance check works and no sink gains a raw value."""
    caplog.set_level("DEBUG")
    f = FakeFinance()
    h = harness(tmp_path, srv, f)
    client_tenant(h, bind=None)
    n0 = len(getattr(h.svc.optext_vault, "_index", {}))
    h.svc._a_tenant_finance_client_set({"tenant_id": "acme", "finance_client_id": CLIENT}, None)
    code = h.svc.tenants["acme"]["finance_client_id"]
    assert h.svc._FC_T2.match(code) and h.svc.tenant_view("acme")["finance_client_id"] == code
    h.svc._a_tenant_finance_client_set({"tenant_id": "acme", "finance_client_id": CLIENT}, None)   # a 2nd replay
    assert len(getattr(h.svc.optext_vault, "_index", {})) == n0 + 1                          # sealed once
    h.ports.compliance.asked.clear()
    _paid(h, f)
    assert h.ports.compliance.asked == [CLIENT]
    assert _readable(_sinks(h, tmp_path, caplog), CLIENT) == 0


def test_a_retry_of_a_pre_change_request_is_409(tmp_path, srv):
    """Lead ruling 02:40 PT (F6g2-2): a request recorded before E-COMP hashed the RAW body; its retry (same
    request_id, same body) now hashes the coded body: 409 REQUEST_ID_REUSED (disclosed)."""
    h = harness(tmp_path, srv, FakeFinance())
    h.tenant("acme", domains=("site.test",))
    body = {"request_id": rid(), "finance_client_id": CLIENT}
    h.ok(h.post("/tenants/acme/finance-client", body, andre=True))
    rk = h.svc.rk("finance_client", "acme", body)
    prev = h.svc.requests[("andre", rk)]
    h.svc.requests[("andre", rk)] = (service.request_sha(h.svc._machine_body(body)),) + tuple(prev[1:])
    h.refused(h.post("/tenants/acme/finance-client", body, andre=True), 409, "REQUEST_ID_REUSED")
    h.svc.requests[("andre", rk)] = prev                   # control: the post-change record replays
    h.ok(h.post("/tenants/acme/finance-client", body, andre=True))
