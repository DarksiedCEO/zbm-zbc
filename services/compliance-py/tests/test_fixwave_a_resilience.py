"""Fix wave A (red team Oct 30-31 2026, redteam-chaos.md CH-1/CH-2/CH-5/CH-6/CH-11; ADR 0025) for Compliance. Every
test here fails on feat/compliance-gate-deploy 5c2538b. c03_kill_legal_compliance.py killed Compliance inside an
activation ruling (3 refusals in 11 kills); the kill is reproduced in-process by ``Crash`` (a BaseException: nothing
in the service runs after it). CH-11 is c03b.out: the reconcile voided ``cmp-rul-…`` and the onboarding retry (same
request_id) recorded and returned that same id as the live ruling."""

from __future__ import annotations

import pytest

import crash_recovery as CR
import startup_hold as SH
from helpers import ANDRE_TOKEN, Harness, client_facts, rid
from intelligences import i11_evidence_audit as i11

RECONCILE = "/compliance/v1/reconcile"


class Crash(BaseException):
    pass


def _ready(h):
    h.approve_seed()
    h.run_controls()
    h.approve(*[h.memo_supersede(c) for c in ("CQ-01", "CQ-03", "CQ-11")])
    h.run_controls()
    return h


def _durable(tmp_path):
    return _ready(Harness(data_dir=str(tmp_path / "d")))


def _restart(x, tmp_path, **env):
    return Harness(data_dir=str(tmp_path / "d"), ledger=x.ledger, clock=x.clock, ports=x.ports, env=env or None)


def _crashes(call):
    try:
        r = call()
    except BaseException as exc:  # noqa: BLE001 - Crash, possibly wrapped in an ExceptionGroup by the ASGI stack
        if isinstance(exc, Crash) or (isinstance(exc, BaseExceptionGroup) and exc.subgroup(Crash) is not None):
            return
        raise
    assert r.status_code == 500, (r.status_code, r.text)


def _kill_after(x, monkeypatch, event_type):
    real = x.svc.recorder.record

    def record(event_id, et, *a, **k):
        out = real(event_id, et, *a, **k)
        if et == event_type:
            raise Crash(et)
        return out
    monkeypatch.setattr(x.svc.recorder, "record", record)


@pytest.mark.parametrize("where", ["after_ruling"])
def test_ch1_a_kill_inside_a_ruling_recovers_by_itself_on_start(tmp_path, monkeypatch, where):
    x = _durable(tmp_path)
    _kill_after(x, monkeypatch, "activation_ruling" if where == "after_ruling" else i11.ANCHOR_TYPE)
    x.client = type(x.client)(x.app, raise_server_exceptions=False)
    _crashes(lambda: x.rule("client-z", "client", client_facts(), request_id="onb-req-1"))
    monkeypatch.undo()
    y = _restart(x, tmp_path)                                   # used to refuse: "1 ruling(s) on the ledger ..."
    rec = x.ledger.of_type(i11.RECONCILE_TYPE) if hasattr(x.ledger, "of_type") else \
        [e for e in x.ledger.events if e["event_type"] == i11.RECONCILE_TYPE]
    assert len(rec) == 1 and "Automatic crash recovery" in rec[0]["summary"]
    h = y.get("/health")
    assert h.status_code == 200 and h.json()["write_ok"] is True, h.text
    # CH-11: the onboarding retry (same request_id) gets a ruling id that was NOT voided
    voided = set(y.svc.voided_ids)
    r = y.rule("client-z", "client", client_facts(), request_id="onb-req-1")
    assert r.status_code == 200, r.text
    assert r.json()["ruling_id"] not in voided
    z = _restart(y, tmp_path)
    assert z.get("/health").status_code == 200


def test_ch11_a_voided_ruling_id_never_comes_back_live_after_andres_reconcile(tmp_path):
    """c03b: the manual reconcile path too (the residue is not announced, e.g. a journal lost with the disk)."""
    x = _durable(tmp_path)
    x.svc.log.fail_next_append = True
    assert x.rule("client-z", "client", client_facts(), request_id="onb-req-2").status_code == 503
    (tmp_path / "d" / "compliance_log.jsonl.intent").unlink()
    with pytest.raises(SH.StartupHeld, match="only Andre"):
        _restart(x, tmp_path)
    y = _restart(x, tmp_path, COMPLIANCE_RECONCILE_MODE="1")
    plan = y.client.get(RECONCILE, headers=y.headers(andre=ANDRE_TOKEN)).json()
    voided = [e for e in plan["voidable"]["event_ids"] if e.startswith("cmp-rul-")]
    assert voided, plan
    r = y.post(RECONCILE, {"request_id": rid("rec"), "head_sha256": plan["head_sha256"],
                           "void_lines": plan["voidable"]["lines"], "void_event_ids": plan["voidable"]["event_ids"]},
               andre=ANDRE_TOKEN)
    assert r.status_code == 200, r.text
    z = _restart(x, tmp_path)
    r = z.rule("client-z", "client", client_facts(), request_id="onb-req-2")
    assert r.status_code == 200, r.text
    assert r.json()["ruling_id"] not in voided, "a voided ruling id must not come back as the live ruling"
    assert z.get(f"/compliance/v1/rulings/{r.json()['ruling_id']}").status_code == 200


def test_ch2_a_blip_before_the_anchor_is_degraded_then_settled_while_running(tmp_path):
    x = _durable(tmp_path)
    x.ledger.fail_on_type = i11.ANCHOR_TYPE                      # the ruling lands, its anchor does not
    assert x.rule("client-z", "client", client_facts()).status_code == 503
    x.ledger.fail_on_type = None
    h = x.client.get("/health")
    assert h.status_code == 503 and any(r.startswith("pending_unreconciled_writes")
                                        for r in h.json()["degraded_reasons"]), h.text
    w = CR.LedgerWatch("compliance-py", x.svc.settle_pending, x.svc.probe_ledger, lambda: len(x.svc.intent))
    assert w.step() is True
    assert x.client.get("/health").status_code == 200
    y = _restart(x, tmp_path)
    assert y.client.get("/health").status_code == 200


@pytest.mark.parametrize("where", ["after_anchor", "failed_append"])
def test_f2_an_anchored_interrupted_ruling_is_held_and_reconcile_mode_is_deployable(tmp_path, monkeypatch, where):
    """AEGIS wave A F-2 / F-1."""
    x = _durable(tmp_path)
    if where == "after_anchor":
        _kill_after(x, monkeypatch, i11.ANCHOR_TYPE)
        x.client = type(x.client)(x.app, raise_server_exceptions=False)
        _crashes(lambda: x.rule("client-z", "client", client_facts(), request_id="onb-req-9"))
        monkeypatch.undo()
    else:
        x.svc.log.fail_next_append = True
        assert x.rule("client-z", "client", client_facts()).status_code == 503
        assert x.svc.settle_pending() is False
    with pytest.raises(SH.StartupHeld) as held:
        _restart(x, tmp_path)
    assert held.value.code == "interrupted_write_anchored"
    y = _restart(x, tmp_path, COMPLIANCE_RECONCILE_MODE="1")
    assert y.client.get("/livez").status_code == 200
    h = y.client.get("/health")
    assert h.status_code == 200 and h.json()["reconcile_required"] is True, h.text


def test_ch1_an_unannounced_ruling_still_refuses(tmp_path):
    x = _durable(tmp_path)
    x.ledger.record_event("cmp-rul-forged0000000000000000000", "compliance", "activation_ruling", "intel_02", "c",
                          {"forged": 1}, "forged ruling")
    with pytest.raises(SH.StartupHeld, match="refusing to start"):
        _restart(x, tmp_path)


def test_ch5_and_ch6_ledger_unreachable(tmp_path):
    x = _durable(tmp_path)
    x.ledger.fail_all = True
    assert x.svc.probe_ledger() is False
    r = x.client.get("/health")
    assert r.status_code == 503 and "ledger_unreachable" in r.json()["degraded_reasons"]
    x.svc.close()
    with pytest.raises(SH.StartupTransient, match="cannot be verified against the evidence ledger"):
        _restart(x, tmp_path)
    x.ledger.fail_all = False
    y = _restart(x, tmp_path)
    assert y.client.get("/health").status_code == 200


# --- RTA-2 (redteam-access.md, lateral_probe.out sections B and D) -------------------------------------------------

@pytest.mark.parametrize("name", ["seo_02", "revenue_recovery"])
def test_rta2_crawler_and_shopify_callers_reach_only_activation_and_health(name):
    from helpers import CALLERS
    x = _ready(Harness())
    x.activate_client("client-1")
    hd = x.headers()
    hd["X-Compliance-Caller-Token"] = CALLERS[name]
    for method, path, body in [
        ("GET", "/compliance/v1/audit/export", None), ("GET", "/compliance/v1/audit/evidence", None),
        ("GET", "/compliance/v1/holds", None), ("GET", "/compliance/v1/inbox", None),
        ("GET", "/compliance/v1/register", None), ("GET", "/compliance/v1/register/versions", None),
        ("GET", "/compliance/v1/register/HR-01", None), ("GET", "/compliance/v1/controls", None),
        ("GET", "/compliance/v1/controls/C-11", None), ("GET", "/compliance/v1/trust-center", None),
        ("GET", "/compliance/v1/rulings/cmp-rul-x", None), ("GET", "/intelligences", None),
        ("GET", "/compliance/v1/readiness", None),
        ("POST", "/compliance/v1/jurisdictions/resolve", {"request_id": rid(), "subject_id": "s"}),
        ("GET", "/compliance/v1/clients/client-1/activation/../../audit/export", None),
        ("GET", "/compliance/v1/%61udit/export", None),
    ]:
        r = x.client.request(method, path, json=body, headers=hd)
        assert r.status_code == 403, (name, path, r.status_code, r.text[:200])
    r = x.client.get("/compliance/v1/clients/client-1/activation", headers=hd)
    assert r.status_code == 200 and r.json()["decision"] == "ALLOW", r.text
    assert x.client.get("/health", headers=hd).status_code == 200


# --- RTA-5: rotation window and Andre's token as a SHA-256 ---------------------------------------------------------

def test_rta5_next_service_token_and_hashed_andre_token():
    import hashlib

    from helpers import ANDRE_TOKEN, SERVICE_TOKEN
    nxt = "next-compliance-service-token-openssl-rand-hex-32-aa"
    h = Harness(env={"COMPLIANCE_SERVICE_TOKEN_NEXT": nxt, "COMPLIANCE_ROTATION_ENDS_AT": _ends(), "COMPLIANCE_ANDRE_APPROVAL_TOKEN": "",
                     "COMPLIANCE_ANDRE_APPROVAL_TOKEN_SHA256": hashlib.sha256(ANDRE_TOKEN.encode()).hexdigest()})
    for bearer in (SERVICE_TOKEN, nxt):
        r = h.client.get("/compliance/v1/reconcile", headers={"Authorization": f"Bearer {bearer}", "X-Andre-Approval-Token": ANDRE_TOKEN})
        assert r.status_code not in (401, 403), r.text
    r = h.client.get("/compliance/v1/reconcile", headers={"Authorization": f"Bearer {nxt}", "X-Andre-Approval-Token": "wrong-" + "0" * 40})
    assert r.status_code == 403
    assert h.settings.andre_token is None and ANDRE_TOKEN not in repr(h.settings)


# --- AEGIS wave A F-5: the daily off-ledger anchor store ---------------------------------------------------------

class _AnchorLedger:
    """GET /ledger/anchor[?at=N] over a list of entry hashes."""

    def __init__(self, hashes):
        self.hashes = list(hashes)

    def anchor(self, at=None):
        n = len(self.hashes) if at is None else at
        if n > len(self.hashes):
            raise RuntimeError("404")
        h = self.hashes[n - 1] if n else "0" * 64
        return {"anchor": f"{n}:{h}", "entries": n, "head_hash": h, "at": "2026-10-31T00:00:00Z", "signature": None}


def test_f5_the_anchor_store_catches_a_rollback_a_rewrite_and_its_own_tampering(tmp_path):
    import ledger_anchor as LA
    store = LA.AnchorStore(str(tmp_path / LA.FILE), "k" * 40)
    led = _AnchorLedger(["a1" * 32, "a2" * 32, "a3" * 32])
    assert LA.check(store, led, "t1") [0] is None and len(store.records()) == 1
    assert LA.check(store, led, "t2") == (None, None)                    # nothing new
    led.hashes.append("a4" * 32)
    problem, rec = LA.check(store, led, "t3")
    assert problem is None and rec["entries"] == 4 and rec["prev"] == store.records()[0]["mac"]
    rolled = _AnchorLedger(led.hashes[:3])
    assert "rolled back" in LA.check(store, rolled, "t4")[0]
    rewritten = _AnchorLedger(led.hashes[:2] + ["ff" * 32, "ee" * 32, "dd" * 32])
    assert "rewritten" in LA.check(store, rewritten, "t5")[0]
    raw = (tmp_path / LA.FILE).read_text().replace('"entries":4', '"entries":5')
    (tmp_path / LA.FILE).write_text(raw)
    assert "does not verify" in LA.check(store, led, "t6")[0]
    assert "does not verify" in LA.AnchorStore(str(tmp_path / LA.FILE), "other-key" * 5).verify()


def test_f5_a_mismatch_turns_compliance_health_red_and_is_scheduled_daily(tmp_path):
    import ledger_anchor as LA
    import scheduler
    x = _durable(tmp_path)
    x.svc.anchor_store = LA.AnchorStore(str(tmp_path / "d" / LA.FILE), "k" * 40)
    led = _AnchorLedger(["b1" * 32, "b2" * 32])
    x.svc.recorder.client.anchor = led.anchor
    s = scheduler.Scheduler(x.svc, interval_s=21600, clock=x.clock, out=lambda line: None)
    assert s.due_anchor() is True
    s.tick()
    assert s.last_anchor is not None and s.due_anchor() is False
    assert len(x.svc.anchor_store.records()) == 1
    led.hashes = led.hashes[:1]                                              # the ledger was rolled back
    assert "rolled back" in x.svc.ledger_anchor_check()
    r = x.client.get("/health")
    assert r.status_code == 503 and "ledger_anchor_mismatch" in r.json()["degraded_reasons"]


def _ends(days=1):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def test_f12_rotation_and_hash_guards_hold_here_too():
    """AEGIS wave A F-12: mutants M9 (NEXT == current accepted) and M10 (Andre's hash == another token's) survived this
    suite; finance-py alone killed them."""
    import hashlib
    import json as _json

    import config as config_mod
    from helpers import CALLERS, base_env
    env = base_env()
    other = next(iter(CALLERS.values()))
    with pytest.raises(RuntimeError, match="NEXT"):
        config_mod.load({**env, "COMPLIANCE_SERVICE_TOKEN_NEXT": other, "COMPLIANCE_ROTATION_ENDS_AT": _ends()})
    with pytest.raises(RuntimeError, match="NEXT"):
        config_mod.load({**env, "COMPLIANCE_CALLER_TOKENS_NEXT": _json.dumps({"onboarding": env["COMPLIANCE_SERVICE_TOKEN"]}),
                         "COMPLIANCE_ROTATION_ENDS_AT": _ends()})
    with pytest.raises(RuntimeError, match="distinct"):
        config_mod.load({**env, "COMPLIANCE_ANDRE_APPROVAL_TOKEN": "",
                         "COMPLIANCE_ANDRE_APPROVAL_TOKEN_SHA256": hashlib.sha256(other.encode()).hexdigest()})
    with pytest.raises(RuntimeError, match="ROTATION_ENDS_AT"):
        config_mod.load({**env, "COMPLIANCE_SERVICE_TOKEN_NEXT": "next-token-" + "a" * 40})


def test_f12_the_holding_app_never_answers_200_on_health():
    """Mutant M18 (holding app answers 200) survived this suite."""
    import asyncio
    import json as _json
    hold = SH.HoldingApp("compliance-py")

    async def call(path):
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(m):
            sent.append(m)
        await hold({"type": "http", "path": path, "method": "GET", "headers": []}, receive, send)
        return sent[0]["status"], _json.loads(sent[1]["body"])
    assert asyncio.run(call("/health"))[0] == 503
    assert asyncio.run(call("/compliance/v1/anything"))[0] == 503
    assert asyncio.run(call("/livez"))[0] == 200
