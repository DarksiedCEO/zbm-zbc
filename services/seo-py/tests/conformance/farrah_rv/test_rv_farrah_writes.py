"""Reviewer: CB-6 kills mid-run, CB-7/AC-13 crash + restart, CB-8, AC-10 vault, W-FA-2/D-FA-01 amounts never outside
the vault, AC-1/AC-2 planted values in every field Farrah reads. Machine words only in output."""

from __future__ import annotations

import json
import os
import random
import unicodedata
from urllib.parse import unquote

from rv_service import install_case, make_harness, post_run, put_map, put_plan, rid
from test_rv_farrah_edges import UNK, arow, case, crmdoc, doc

M = "2026-09"


def H(tmp_path, **kw):
    h = make_harness(tmp_path, **kw)
    h.tenant("acme")
    h.tenant("globex")
    assert put_plan(h, "acme").status_code == 200
    assert put_plan(h, "globex").status_code == 200
    return h


def plain_doc(did="ds-00000000000000e1"):
    return doc([arow("20260905", "10.00") for _ in range(35)] + [arow("20260906", "1.00", **UNK)], did=did)


SWITCHES = ("global", "write", "tenant:acme", "capability:audit", "capability:attribution")


def test_rv_kill_mid_run_each_switch(tmp_path):
    import agents.farrah as fm
    out = {}
    for sw in SWITCHES:
        for where in ("compute", "prepare"):
            h = H(tmp_path / sw.replace(":", "_") / where)
            install_case(h, case(plain_doc()))
            target = (fm, "compute") if where == "compute" else (type(h.svc), "_attribution_prepare")
            real = getattr(*target)
            own = target[1] in vars(target[0])      # ported (round 2): put the class back exactly as it was

            def hook(*a, _real=real, _sw=sw, _h=h, **k):
                r = _h.switch(_sw, True, andre=True)
                assert r.status_code == 200, r.text[:200]
                return _real(*a, **k)
            setattr(*target, hook)
            try:
                r = post_run(h, "acme", M)
            finally:
                if own or not isinstance(target[0], type):
                    setattr(*target, real)
                else:
                    delattr(*target)
            v = r.json()
            rec = h.ledger.of_type("attribution_run_recorded")
            out[(sw, where)] = (r.status_code, v.get("status"), v.get("outcome"), len(rec))
    print("RV-KILL", out)
    for k, v in out.items():
        assert v[1] == "interrupted" and v[2] == "KILLED" and v[3] == 0, k


def test_rv_switch_engaged_before_request(tmp_path):
    out = {}
    for sw in SWITCHES:
        h = H(tmp_path / sw.replace(":", "_"))
        install_case(h, case(plain_doc()))
        h.switch(sw, True, andre=True)
        r = post_run(h, "acme", M)
        out[sw] = (r.status_code, r.json().get("detail"), h.store.reads)
    print("RV-KILL-BEFORE", out)
    assert all(v[0] in (403, 409, 423, 503) and v[2] == 0 for v in out.values())


def test_rv_vault_unavailable_before_any_read(tmp_path):
    h = make_harness(tmp_path, data_dir=str(tmp_path / "d"), SEO_VAULT_KEY_FILE=None, SEO_VAULT_KEY_ID=None)
    h.tenant("acme")
    r0 = put_plan(h, "acme")
    install_case_ok = True
    try:
        install_case(h, case(plain_doc()))
    except Exception:  # noqa: BLE001 - the fixture record needs the vault to hash: put the doc in alone
        install_case_ok = False
        t = h.store.reset("acme")
        t["datasets"].append(plain_doc())
    r = post_run(h, "acme", M)
    print("RV-VAULT", r0.status_code, install_case_ok, r.status_code, r.json().get("detail"), h.store.reads)
    assert r.status_code == 503 and r.json().get("detail") == "VAULT_UNAVAILABLE" and h.store.reads == 0


def test_rv_replay_and_in_progress_and_supersede(tmp_path):
    h = H(tmp_path)
    install_case(h, case(plain_doc()))
    req = rid()
    a = post_run(h, "acme", M, request_id=req).json()
    b = post_run(h, "acme", M, request_id=req).json()
    assert a["run_id"] == b["run_id"] and len(h.svc.attribution_runs) == 1
    c = post_run(h, "acme", M).json()                      # unchanged inputs: the current run
    assert c["run_id"] == a["run_id"]
    install_case(h, case(plain_doc("ds-00000000000000e2")))
    d = post_run(h, "acme", M).json()                      # basis dataset changed: a new run that supersedes
    old = h.get(f"/tenants/acme/attribution-runs/{a['run_id']}").json()
    print("RV-SUPERSEDE", d["run_id"] != a["run_id"], d.get("supersedes") == a["run_id"],
          old.get("superseded_by") == d["run_id"], old.get("status"))
    assert d["supersedes"] == a["run_id"] and old["superseded_by"] == d["run_id"] and old["status"] == "completed"
    # in progress: a second request while one runs
    import agents.farrah as fm
    real = fm.compute
    seen = {}

    def compute(*a2, **k):
        seen["second"] = post_run(h, "acme", M).status_code
        return real(*a2, **k)
    install_case(h, case(plain_doc("ds-00000000000000e3")))
    fm.compute = compute
    try:
        post_run(h, "acme", M)
    finally:
        fm.compute = real
    print("RV-INPROGRESS", seen)
    assert seen["second"] == 409


def test_rv_crash_restart_no_half_record(tmp_path):
    h = make_harness(tmp_path, data_dir=str(tmp_path / "d"))
    h.tenant("acme")
    put_plan(h, "acme")
    install_case(h, case(plain_doc()))
    svc_cls = type(h.svc)
    real = svc_cls._execute_attribution
    svc_cls._execute_attribution = lambda self, *a, **k: {"died": True}      # the process dies after requested
    try:
        post_run(h, "acme", M)
    finally:
        if "_execute_attribution" in vars(svc_cls):     # ported (round 2): never leave the mixin's function behind
            del svc_cls._execute_attribution
        assert svc_cls._execute_attribution is real
    store = h.store
    h2 = h.restart()
    h2.store = store
    h2.svc.osei_source = store
    j = h2.post("/jobs/interrupted-audits/run", {"request_id": rid()}, caller="scheduler")
    runs = list(h2.svc.attribution_runs.values())
    print("RV-CRASH", j.status_code, [(r["status"], r.get("interrupted_reason")) for r in runs])
    assert runs and runs[0]["status"] == "interrupted" and runs[0]["interrupted_reason"] == "PROCESS_LOST"
    r = post_run(h2, "acme", M)
    assert r.status_code == 201 and r.json()["status"] == "completed"
    assert len(h2.ledger.of_type("attribution_run_recorded")) == 1


def _forms(blob: str) -> list:
    return [blob, unicodedata.normalize("NFKC", blob), unquote(blob), unquote(unquote(blob))]


def _surfaces(h, tmp_path, fids, logs_text):
    """Everything readable OUTSIDE the owner display: ledger, log lines, data dir, compliance views, list/other
    tenant views, tenant view, department, audit export."""
    out = {"ledger": json.dumps(h.ledger.entries(), ensure_ascii=False, default=str), "pylog": logs_text}
    dd = h.settings.data_dir
    if dd:
        parts = []
        for root, _d, files in os.walk(dd):
            for f in files:
                try:
                    parts.append(open(os.path.join(root, f), "rb").read().decode("utf-8", "replace"))
                except OSError:
                    pass
        out["datadir"] = "\n".join(parts)
    hd = h.headers("compliance_38")
    out["compliance"] = h.client.get("/seo/v1/tenants/acme/attribution-runs", headers=hd).text + "".join(
        h.client.get(f"/seo/v1/tenants/acme/attribution-runs/{f}", headers=hd).text for f in fids)
    out["globex"] = h.client.get("/seo/v1/tenants/globex/attribution-runs", headers=h.headers("dashboard")).text + \
        "".join(h.client.get(f"/seo/v1/tenants/globex/attribution-runs/{f}", headers=h.headers("hub", tenant="globex")).text
                for f in fids)
    out["tenant_view"] = h.client.get("/seo/v1/tenants/acme", headers=h.headers("dashboard")).text
    out["department"] = h.client.get("/seo/v1/department", headers=h.headers("dashboard")).text
    out["export"] = h.client.get("/seo/v1/audit/export", params={"limit": 1000},
                                 headers=h.headers("compliance_38")).text
    return out


AMOUNTS = ("4718293.61", "8812734.07", "-6619027.43", "5590178.33")


def test_rv_amounts_never_outside_vault(tmp_path, caplog):
    """W-FA-2 / D-FA-01 / Q-FA-5: planted distinctive amounts (and every amount the run derives) appear in no log,
    ledger, data-dir plaintext, compliance answer, export or other-tenant view."""
    import logging
    caplog.set_level(logging.DEBUG)
    h = H(tmp_path, data_dir=str(tmp_path / "d"))
    rows = [arow("20260905", AMOUNTS[0]) for _ in range(12)] + [arow("20260906", AMOUNTS[1], "chatgpt.com",
                                                                     "referral", "Referral", "/a")] + \
        [arow("20260907", AMOUNTS[2], **UNK), arow("20260908", AMOUNTS[3], "newsletter", "email", "Email", "/n")]
    c = case(doc(rows), recon="3301927.58", other=crmdoc("ds-00000000000000b2"))
    install_case(h, c)
    v = post_run(h, "acme", M).json()
    owner = h.get(f"/tenants/acme/attribution-runs/{v['run_id']}").json()
    assert owner["status"] == "completed"
    derived = set(AMOUNTS) | {"3301927.58"}
    res = owner["result"]
    for ch in res["channels"].values():
        derived |= {x for x in (ch["figure"], ch["lower"], ch["upper"]) if x}
    derived |= {x for x in res["totals"].values() if x}
    derived |= {x for ch in owner["display"].values() for x in ch.values() if x}
    probes = set()
    for a in derived:
        a = a.lstrip("-")
        probes.add(a)
        probes.add(a.replace(".", "")[:9])          # a digit run of the amount
    surf = _surfaces(h, tmp_path, [v["run_id"]], caplog.text)
    hits = {k: sorted(p for p in probes if len(p) >= 7 and any(p in f for f in _forms(t))) for k, t in surf.items()}
    print("RV-AMOUNT-HITS", {k: len(x) for k, x in hits.items()})
    assert not any(hits.values())


def _plant_cases():
    from secret_corpus import corpus
    from conformance.gen.personal import cases as pcases
    n1 = int(os.environ.get("RV_AC1_N", "960"))
    n2 = int(os.environ.get("RV_AC2_N", "400"))
    out = []
    for cid, carrier, secret, kind in corpus()[:n1]:
        out.append(("AC1:" + cid.split(":")[0], carrier, secret))
    for cid, carrier, value in pcases(n2):
        out.append(("AC2:" + cid.split(":")[0], carrier, value))
    return out


def test_rv_planted_values_w_e_zero(tmp_path, caplog):
    """AC-1 / AC-2 through the real service path: each value planted into every text field Farrah reads (CRM utm
    values, lead_source, landing_path, deal_ref; analytics source / medium / landing page; the channel map)."""
    import logging
    caplog.set_level(logging.DEBUG)
    h = H(tmp_path, data_dir=str(tmp_path / "d"))
    rng = random.Random(3)
    plants = _plant_cases()
    fids, errors = [], 0
    for i, (cid, carrier, value) in enumerate(plants):
        carrier = carrier[:190]
        basis = "crm" if i % 2 else "analytics"
        put_plan(h, "acme", "managed_2", basis)
        mr = put_map(h, "acme", {"organic_search": [carrier], "ai_referral": [carrier[:50] + "x"],
                                 "other_known": []})
        if mr.status_code != 200:
            errors += 1
            if value in mr.text:
                raise AssertionError(("error echo", cid))
        if basis == "crm":
            rows = [{"deal_ref": carrier, "created_date": None, "closed_date": "2026-09-05", "stage": "won",
                     "amount": "10.00", "currency": "USD", "lead_source": carrier, "utm_source": carrier,
                     "utm_medium": carrier, "utm_campaign": carrier, "landing_path": carrier},
                    {"deal_ref": "d2", "created_date": None, "closed_date": "2026-09-05", "stage": "won",
                     "amount": "10.00", "currency": "USD", "lead_source": carrier, "utm_source": None,
                     "utm_medium": None, "utm_campaign": None, "landing_path": None}]
            d = doc([], did="ds-%016x" % rng.getrandbits(64))
            d["source"], d["rows"] = "crm", rows
            c = case(d)
            c.basis = "crm"
        else:
            rows = [arow("20260905", "10.00", carrier, carrier, carrier, carrier), arow("20260905", "5.00")]
            c = case(doc(rows, did="ds-%016x" % rng.getrandbits(64)))
        install_case(h, c)
        r = post_run(h, "acme", M)
        if r.status_code != 201:
            assert value not in r.text, cid
            continue
        fids.append(r.json()["run_id"])
    surf = _surfaces(h, tmp_path, fids[-50:], caplog.text)
    pre = {k: _forms(t) for k, t in surf.items()}
    leaks = {}
    for cid, carrier, value in plants:
        for k, forms in pre.items():
            if len(value) >= 6 and any(value in f for f in forms):
                leaks.setdefault(k, set()).add(cid)
    print("RV-WE", len(plants), "runs", len(fids), "map_refused", errors, {k: sorted(v)[:10] for k, v in leaks.items()})
    assert not leaks
