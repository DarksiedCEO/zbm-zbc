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


def _new_version(x):
    """A journaled write: Andre approves clipper_agreement 1.1 (document_version_approved is a ruling type)."""
    return x.approve_doc("clipper_agreement", "clipper agreement text, revised", version="1.1")


def test_ch1_a_kill_before_the_anchor_recovers_by_itself_on_start(tmp_path, monkeypatch):
    x = _durable(tmp_path)
    real = x.svc.recorder.record

    def record(event_id, et, *a, **k):
        out = real(event_id, et, *a, **k)
        if et in i10.RULING_TYPES:
            raise Crash(et)                          # killed after a ruling, before the anchor
        return out
    monkeypatch.setattr(x.svc.recorder, "record", record)
    x.client = type(x.client)(x.app, raise_server_exceptions=False)
    try:
        _new_version(x)
    except BaseException:  # noqa: BLE001 - the helper asserts on the 500; the kill is what matters
        pass
    monkeypatch.undo()
    y = _restart(x, tmp_path)
    rec = x.ledger.of_type(i10.RECONCILE_TYPE)
    assert len(rec) == 1 and "Automatic crash recovery" in rec[0]["summary"]
    assert y.ok(y.get("/legal/v1/integrity"))["status"] == "green"
    h = y.ok(y.client.get("/health"))
    assert h["status"] == "ok" and h["write_ok"] is True
    assert _request(y).status_code == 201
    z = _restart(y, tmp_path)
    assert z.ok(z.get("/legal/v1/integrity"))["status"] == "green"


@pytest.mark.parametrize("where", ["after_anchor", "before_append", "failed_append"])
def test_f2_an_anchored_interrupted_write_is_held_for_andre_and_reconcile_mode_is_deployable(tmp_path, monkeypatch,
                                                                                            where):
    """AEGIS wave A F-2 / F-1: after the anchor a crash and a restored mid-write snapshot look the same, so start-up
    holds (code interrupted_write_anchored); reconcile mode answers /livez 200 and /health 503 "maintenance" (AEGIS round 2 N-3)."""
    x = _durable(tmp_path)
    if where == "after_anchor":
        real = x.svc.recorder.record

        def record(event_id, et, *a, **k):
            out = real(event_id, et, *a, **k)
            if et == i10.ANCHOR_TYPE:
                raise Crash(et)
            return out
        monkeypatch.setattr(x.svc.recorder, "record", record)
        _crashes(lambda: _request(x))
    elif where == "before_append":
        def append(*a, **k):
            raise Crash("append")
        monkeypatch.setattr(x.svc.log, "append_prepared", append)
        _crashes(lambda: _request(x))
    else:
        x.svc.log.fail_next_append = True
        assert _request(x).status_code == 503
        assert x.svc.settle_pending() is False                   # the running watch never voids it either
        r = x.client.get("/health")
        assert r.status_code == 503 and "divergence:interrupted_write_anchored" in r.json()["degraded_reasons"]
    monkeypatch.undo()
    with pytest.raises(SH.StartupHeld) as held:
        _restart(x, tmp_path)
    assert held.value.code == "interrupted_write_anchored"
    assert x.ledger.of_type(i10.RECONCILE_TYPE) == []
    y = _restart(x, tmp_path, LEGAL_RECONCILE_MODE="1")
    assert y.client.get("/livez").status_code == 200
    h = y.ok(y.client.get("/health"), 503)                             # AEGIS round 2 N-3
    assert h["reconcile_required"] is True and h["status"] == "maintenance"


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
    real = x.ledger.record_event
    seen = []

    def record_event(event_id, department, event_type, *a, **k):
        if event_type in i10.RULING_TYPES:
            seen.append(event_id)
        elif event_type == i10.ANCHOR_TYPE and seen:
            from ledger import LedgerNotRecorded
            raise LedgerNotRecorded("simulated outage at the anchor (test)")   # the ruling landed, the anchor not
        return real(event_id, department, event_type, *a, **k)
    x.ledger.record_event = record_event
    try:
        _new_version(x)
    except AssertionError:
        pass
    x.ledger.record_event = real
    assert seen and len(x.svc.intent)
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
        aliased = {r.get("alias_of") for c, r in x.svc.register.items() if c.startswith("FIN-")}
        plain = next(c for c in sorted(x.svc.register) if not c.startswith("FIN-") and c not in aliased
                     and not x.svc.register[c].get("alias_of"))
        r = _as(x, "compliance_38", "POST", f"/legal/v1/register/{plain}/invalidate",
                {"request_id": rid(), "source_ref": "https://example.gov/x", "detected_change_sha256": "a" * 64})
    assert r.status_code == 200, r.text
    assert any('"register_row_invalidated"' in rec.getMessage() for rec in caplog.records)


# --- RTA-5: rotation window and Andre's token as a SHA-256 ---------------------------------------------------------

def test_rta5_next_service_token_and_hashed_andre_token():
    import hashlib

    from helpers import ANDRE_TOKEN, SERVICE_TOKEN
    nxt = "next-legal-service-token-openssl-rand-hex-32-aa"
    h = Harness(env={"LEGAL_SERVICE_TOKEN_NEXT": nxt, "LEGAL_ROTATION_ENDS_AT": _ends(), "LEGAL_ANDRE_APPROVAL_TOKEN": "",
                     "LEGAL_ANDRE_APPROVAL_TOKEN_SHA256": hashlib.sha256(ANDRE_TOKEN.encode()).hexdigest()})
    for bearer in (SERVICE_TOKEN, nxt):
        r = h.client.get("/legal/v1/reconcile", headers={"Authorization": f"Bearer {bearer}", "X-Andre-Approval-Token": ANDRE_TOKEN})
        assert r.status_code not in (401, 403), r.text
    r = h.client.get("/legal/v1/reconcile", headers={"Authorization": f"Bearer {nxt}", "X-Andre-Approval-Token": "wrong-" + "0" * 40})
    assert r.status_code == 403
    assert h.settings.andre_token is None and ANDRE_TOKEN not in repr(h.settings)


def test_f8_compliance_cannot_invalidate_a_fin_row_through_an_alias(tmp_path):
    """AEGIS wave A F-8: FIN-CQ-07 is an alias of CQ-01; invalidating it answered 200 {"canonical":"CQ-01"}.
    AEGIS round 2 N-7: the fix also refused Compliance on the shared rows it owns (CQ-01, CQ-04, CQ-07: CQ-01 is the
    AB 5 clipper-classification row). Only FIN-owned rows are refused: a FIN-* target, or a FIN-* canonical row."""
    x = _durable(tmp_path)
    fin_aliases = {c: r["alias_of"] for c, r in x.svc.register.items() if c.startswith("FIN-") and r.get("alias_of")}
    assert fin_aliases, "the seed has FIN aliases"
    fin_owned = sorted(c for c, r in x.svc.register.items() if c.startswith("FIN-") and not r.get("alias_of"))
    assert fin_owned, "the seed has FIN-owned rows"

    def inv(target):
        return _as(x, "compliance_38", "POST", f"/legal/v1/register/{target}/invalidate",
                   {"request_id": rid(), "source_ref": "https://example.gov/x", "detected_change_sha256": "a" * 64})
    for alias, canon in sorted(fin_aliases.items())[:3]:
        assert not canon.startswith("FIN-")
        r = inv(alias)
        assert r.status_code == 403, (alias, r.status_code, r.text)
        r = inv(canon)                                       # the shared row Compliance owns: allowed (N-7)
        assert r.status_code == 200 and r.json()["canonical"] == canon, (canon, r.status_code, r.text)
        assert x.svc.register[canon]["status"] == "unverified"
    for target in fin_owned[:3]:
        r = inv(target)
        assert r.status_code == 403, (target, r.status_code, r.text)
    for alias, canon in sorted(fin_aliases.items())[:1]:     # a FIN alias in another case is still a FIN target
        assert inv(alias.lower()).status_code in (403, 404, 422)          # never 200


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
        config_mod.load({**env, "LEGAL_SERVICE_TOKEN_NEXT": other, "LEGAL_ROTATION_ENDS_AT": _ends()})
    with pytest.raises(RuntimeError, match="NEXT"):
        config_mod.load({**env, "LEGAL_CALLER_TOKENS_NEXT": _json.dumps({"onboarding": env["LEGAL_SERVICE_TOKEN"]}),
                         "LEGAL_ROTATION_ENDS_AT": _ends()})
    with pytest.raises(RuntimeError, match="distinct"):
        config_mod.load({**env, "LEGAL_ANDRE_APPROVAL_TOKEN": "",
                         "LEGAL_ANDRE_APPROVAL_TOKEN_SHA256": hashlib.sha256(other.encode()).hexdigest()})
    with pytest.raises(RuntimeError, match="ROTATION_ENDS_AT"):
        config_mod.load({**env, "LEGAL_SERVICE_TOKEN_NEXT": "next-token-" + "a" * 40})


def test_f12_the_holding_app_never_answers_200_on_health():
    """Mutant M18 (holding app answers 200) survived this suite."""
    import asyncio
    import json as _json
    hold = SH.HoldingApp("legal-py")

    async def call(path):
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(m):
            sent.append(m)
        await hold({"type": "http", "path": path, "method": "GET", "headers": []}, receive, send)
        return sent[0]["status"], _json.loads(sent[1]["body"])
    assert asyncio.run(call("/health"))[0] == 503
    assert asyncio.run(call("/legal/v1/anything"))[0] == 503
    assert asyncio.run(call("/livez"))[0] == 200


def test_f15b_a_lowercase_word_in_an_id_position_never_reaches_the_access_log(tmp_path):
    """AEGIS rounds 2-3 F-15b: `/legal/v1/acceptances/aegisprobelowercasesecretword` was logged verbatim (a plain
    lowercase word passed as a route word). Once the app's routes are registered, only the words of a route template
    are kept; any other segment is `:id`."""
    import access_redact
    x = _durable(tmp_path)
    access_redact.register_routes(x.client.app)
    try:
        got = access_redact.redact_target("/legal/v1/acceptances/aegisprobelowercasesecretword?x=1")
        assert got == "/legal/v1/acceptances/:id?<redacted>", got
        assert access_redact.redact_target("/legal/v1/rules") == "/legal/v1/rules"
        assert access_redact.redact_target("/health") == "/health"
        assert access_redact.redact_target("/legal/v1/documents/engagement_letter/versions/3/decision") == \
            "/legal/v1/documents/:id/versions/:id/decision"
    finally:
        access_redact.register_routes(None)                    # back to the plain-word rule for other tests
