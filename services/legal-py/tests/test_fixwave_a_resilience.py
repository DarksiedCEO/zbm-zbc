"""Fix wave A (red team Oct 30-31 2026, redteam-chaos.md CH-1/CH-2/CH-5/CH-6; ADR 0025) for Legal. Every test here
fails on feat/compliance-gate-deploy 5c2538b. c03_kill_legal_compliance.py killed Legal inside a contract write
(3 refusals in 9 kills, e.g. "local log truncated: the ledger anchors line 8 but the local log has 7 lines"); the
kill is reproduced in-process by ``Crash`` (a BaseException: nothing in the service runs after it)."""

from __future__ import annotations

import pytest

import crash_recovery as CR
import startup_hold as SH
from helpers import Harness, rid
from intelligences import i10_evidence_audit as i10


class Crash(BaseException):
    pass


def _durable(tmp_path):
    x = Harness(data_dir=str(tmp_path / "d"))
    x.approve_rules()
    x.engage()
    x.approve_doc("clipper_agreement", "clipper agreement text")
    return x


def _restart(x, tmp_path, **env):
    return Harness(data_dir=str(tmp_path / "d"), ledger=x.ledger, clock=x.clock, ports=x.ports, env=env or None)


def _request(x):
    return x.post("/legal/v1/requests", {"request_id": rid(), "channel": "email", "requester_ref": "r",
                                         "kind": "question"}, caller="hub")


def _crashes(call):
    try:
        r = call()
    except BaseException as exc:  # noqa: BLE001 - Crash, possibly wrapped in an ExceptionGroup by the ASGI stack
        if isinstance(exc, Crash) or (isinstance(exc, BaseExceptionGroup) and exc.subgroup(Crash) is not None):
            return
        raise
    assert r.status_code == 500, (r.status_code, r.text)


@pytest.mark.parametrize("where", ["after_anchor", "before_append"])
def test_ch1_a_kill_inside_a_write_recovers_by_itself_on_start(tmp_path, monkeypatch, where):
    x = _durable(tmp_path)
    if where == "after_anchor":
        real = x.svc.recorder.record

        def record(event_id, et, *a, **k):
            out = real(event_id, et, *a, **k)
            if et == i10.ANCHOR_TYPE:
                raise Crash(et)
            return out
        monkeypatch.setattr(x.svc.recorder, "record", record)
    else:
        def append(*a, **k):
            raise Crash("append")
        monkeypatch.setattr(x.svc.log, "append_prepared", append)
    _crashes(lambda: _request(x))
    monkeypatch.undo()
    y = _restart(x, tmp_path)                       # used to refuse: "local log truncated: the ledger anchors line ..."
    rec = x.ledger.of_type(i10.RECONCILE_TYPE)
    assert len(rec) == 1 and "Automatic crash recovery" in rec[0]["summary"]
    assert y.ok(y.get("/legal/v1/integrity"))["status"] == "green"
    h = y.ok(y.client.get("/health"))
    assert h["status"] == "ok" and h["write_ok"] is True
    assert _request(y).status_code == 201
    z = _restart(y, tmp_path)
    assert z.ok(z.get("/legal/v1/integrity"))["status"] == "green"


def test_ch1_a_failed_local_append_is_residue_too(tmp_path):
    """A 503 local-disk failure after the anchor was the "only Andre reconciles" case: it was never acknowledged, so
    the next start rolls it back itself."""
    x = _durable(tmp_path)
    x.svc.log.fail_next_append = True
    assert _request(x).status_code == 503
    assert x.client.get("/health").status_code == 503
    assert x.svc.settle_pending() is True
    assert x.ok(x.client.get("/health"))["status"] == "ok"
    assert x.ok(x.get("/legal/v1/integrity"))["status"] == "green"
    y = _restart(x, tmp_path)
    assert y.ok(y.get("/legal/v1/integrity"))["status"] == "green"


def test_ch1_an_unannounced_stray_anchor_still_refuses(tmp_path):
    x = _durable(tmp_path)
    n, ep = len(x.svc.log), x.svc.log.epoch
    x.ledger.record_event(i10.anchor_id(ep, n + 1, "ab" * 32), "legal", i10.ANCHOR_TYPE, i10.ACTOR, i10.LOG_SUBJECT,
                          {"forged": 1}, "forged anchor")
    with pytest.raises(SH.StartupHeld, match="truncated"):
        _restart(x, tmp_path)
    assert x.ledger.of_type(i10.RECONCILE_TYPE) == []


def test_ch6_health_is_503_while_the_ledger_cannot_be_reached(tmp_path):
    x = _durable(tmp_path)
    x.ledger.fail_all = True
    assert x.svc.probe_ledger() is False
    r = x.client.get("/health")
    assert r.status_code == 503 and "ledger_unreachable" in r.json()["degraded_reasons"]
    x.ledger.fail_all = False
    assert x.svc.probe_ledger() is True
    assert x.client.get("/health").status_code == 200


def test_ch5_ledger_down_at_start_is_transient(tmp_path):
    x = _durable(tmp_path)
    x.svc.close()
    x.ledger.fail_all = True
    with pytest.raises(SH.StartupTransient, match="cannot be verified against the evidence ledger"):
        _restart(x, tmp_path)
    x.ledger.fail_all = False
    y = _restart(x, tmp_path)
    assert y.ok(y.client.get("/health"))["status"] == "ok"


def test_ch2_watch_step_settles_and_alerts(tmp_path, caplog):
    x = _durable(tmp_path)
    x.svc.log.fail_next_append = True
    assert _request(x).status_code == 503
    w = CR.LedgerWatch("legal-py", x.svc.settle_pending, x.svc.probe_ledger, lambda: len(x.svc.intent))
    with caplog.at_level("WARNING", logger="zbm.alert"):
        assert w.step() is True
    assert any('"crash_recovered"' in r.getMessage() for r in caplog.records)


# --- RTA-3 (redteam-access.md, lateral_probe.out): per-caller route allowlists -------------------------------------

def _as(x, name, method, path, body=None):
    from helpers import CALLERS
    hd = x.headers()
    hd["X-LEGAL-Caller-Token"] = CALLERS[name]
    return x.client.request(method, path, json=body, headers=hd)


@pytest.mark.parametrize("name", ["finance_31", "compliance_38"])
def test_rta3_finance_and_compliance_reach_only_their_routes(tmp_path, name):
    x = _durable(tmp_path)
    for method, path, body in [
        ("GET", "/legal/v1/register", None), ("GET", "/legal/v1/obligations", None),
        ("GET", "/legal/v1/filings", None), ("GET", "/legal/v1/matters", None),
        ("GET", "/legal/v1/takedowns", None), ("GET", "/legal/v1/rules", None),
        ("GET", "/legal/v1/audit/export", None), ("GET", "/legal/v1/documents/clipper_agreement", None),
        ("POST", "/legal/v1/requests", {"request_id": rid(), "channel": "email", "requester_ref": "r",
                                        "kind": "demand_letter", "subject_refs": ["clipper:cn-clp-X"]}),
        ("GET", "/legal/v1/%72egister", None),
    ]:
        r = _as(x, name, method, path, body)
        assert r.status_code == 403, (name, method, path, r.status_code, r.text)
    # what their deployed clients call still answers (not 403)
    assert _as(x, name, "GET", "/legal/v1/documents/clipper_agreement/current").status_code == 200
    assert _as(x, name, "GET", "/legal/v1/acceptances/acc-none").status_code in (404, 422)
    assert _as(x, name, "GET", "/health").status_code == 200


def test_rta3_compliance_cannot_invalidate_a_finance_determination(tmp_path, caplog):
    x = _durable(tmp_path)
    r = _as(x, "compliance_38", "POST", "/legal/v1/register/FIN-CQ-11/invalidate",
            {"request_id": rid(), "source_ref": "https://example.gov/x", "detected_change_sha256": "a" * 64})
    assert r.status_code in (403, 404), r.text
    assert r.status_code == 403 or "no such" in r.text
    # a non-Finance row is still tightened (the designed "tighten only" path), with an ALERT line for Andre
    with caplog.at_level("WARNING", logger="zbm.alert"):
        r = _as(x, "compliance_38", "POST", "/legal/v1/register/CQ-01/invalidate",
                {"request_id": rid(), "source_ref": "https://example.gov/x", "detected_change_sha256": "a" * 64})
    assert r.status_code == 200, r.text
    assert any('"register_row_invalidated"' in rec.getMessage() for rec in caplog.records)


# --- RTA-5: rotation window and Andre's token as a SHA-256 ---------------------------------------------------------

def test_rta5_next_service_token_and_hashed_andre_token():
    import hashlib

    from helpers import ANDRE_TOKEN, SERVICE_TOKEN
    nxt = "next-legal-service-token-openssl-rand-hex-32-aa"
    h = Harness(env={"LEGAL_SERVICE_TOKEN_NEXT": nxt, "LEGAL_ANDRE_APPROVAL_TOKEN": "",
                     "LEGAL_ANDRE_APPROVAL_TOKEN_SHA256": hashlib.sha256(ANDRE_TOKEN.encode()).hexdigest()})
    for bearer in (SERVICE_TOKEN, nxt):
        r = h.client.get("/legal/v1/reconcile", headers={"Authorization": f"Bearer {bearer}", "X-Andre-Approval-Token": ANDRE_TOKEN})
        assert r.status_code not in (401, 403), r.text
    r = h.client.get("/legal/v1/reconcile", headers={"Authorization": f"Bearer {nxt}", "X-Andre-Approval-Token": "wrong-" + "0" * 40})
    assert r.status_code == 403
    assert h.settings.andre_token is None and ANDRE_TOKEN not in repr(h.settings)
