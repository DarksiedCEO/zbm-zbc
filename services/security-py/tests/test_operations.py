"""Service identity, freezes, Legal holds, incidents and alerts, vulnerability findings, jobs, and the log's
integrity against the ledger across restarts."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

import tokens
from clock import FixedClock
from helpers import Harness, rid
from ports import ControlPush, Ports

REF = "vault:finance_31.stripe_secret"
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


def store_finance(hk):
    return hk.andre_store("finance_31", "stripe_secret", readers=["finance_31"], purposes=["stripe_api"])


def freeze(hk, kind, target, code="TEST_FREEZE"):
    body = {"request_id": rid(), "target_kind": kind, "target_id": target, "reason_code": code}
    return hk.ok(hk.post("/sec/v1/freezes", hk.approved("FREEZE", f"{kind}:{target}" if kind != "secret"
                                                        else f"secret:{target}", body)), 201)


# ------------------------------------------------------------------------------------------------ identity

def test_mint_and_verify_a_service_token(hk):
    t = hk.ok(hk.post("/sec/v1/identity/tokens", {"audience": "legal_37", "scope": ["read"]}, caller="finance_31"),
              201)
    jwks = tokens.jwks_map(hk.ok(hk.get("/sec/v1/identity/jwks", caller="legal_37"))["keys"])
    v = tokens.verify(t["token"], jwks, "legal_37", int(datetime.now(timezone.utc).timestamp()))
    assert v.subject == "finance_31" and v.scope == ("read",)
    assert t["expires_at"] - int(datetime.now(timezone.utc).timestamp()) <= tokens.MAX_TTL_S
    assert hk.ledger.of_type("credential_issued")
    with pytest.raises(tokens.TokenInvalid) as e:
        tokens.verify(t["token"], jwks, "compliance_38", int(datetime.now(timezone.utc).timestamp()))
    assert e.value.code == "TOKEN_AUDIENCE_REFUSED"


def test_token_for_oneself_refused(hk):
    assert hk.post("/sec/v1/identity/tokens", {"audience": "finance_31"}, caller="finance_31").status_code == 422


@pytest.mark.parametrize("mutate,code", [
    (lambda t: t[:-4] + ("AAAA" if not t.endswith("AAAA") else "BBBB"), "TOKEN_SIGNATURE_INVALID"),
    (lambda t: "eyJhbGciOiJub25lIiwidHlwIjoiSldUIiwia2lkIjoieCJ9." + t.split(".", 1)[1], "TOKEN_HEADER_REFUSED"),
    (lambda t: t + ".x", "TOKEN_MALFORMED"),
])
def test_token_tampering(hk, mutate, code):
    t = hk.ok(hk.post("/sec/v1/identity/tokens", {"audience": "legal_37"}, caller="finance_31"), 201)["token"]
    jwks = tokens.jwks_map(hk.svc.jwks()["keys"])
    with pytest.raises(tokens.TokenInvalid) as e:
        tokens.verify(mutate(t), jwks, "legal_37", int(datetime.now(timezone.utc).timestamp()))
    assert e.value.code == code


def test_token_expiry_and_lifetime_cap():
    k = tokens.new_private_key()
    kid = tokens.kid_for(k)
    jwks = tokens.jwks_map([tokens.public_jwk(kid, k)])
    t = tokens.mint(kid, k, "finance_31", "legal_37", (), 1000, 600, "a" * 16)
    assert tokens.verify(t, jwks, "legal_37", 1599).subject == "finance_31"
    with pytest.raises(tokens.TokenInvalid):
        tokens.verify(t, jwks, "legal_37", 1600)
    with pytest.raises(tokens.TokenInvalid):
        tokens.verify(t, jwks, "legal_37", 1000 - tokens.MAX_SKEW_S - 1)
    with pytest.raises(ValueError):
        tokens.mint(kid, k, "finance_31", "legal_37", (), 1000, tokens.MAX_TTL_S + 1, "a" * 16)
    with pytest.raises(tokens.TokenInvalid) as e:
        tokens.verify(t, jwks, "legal_37", 1100, frozenset({"finance_31"}))
    assert e.value.code == "TOKEN_SUBJECT_FROZEN"


def test_signing_key_rotation_keeps_old_key_for_overlap(tmp_path):
    clock = FixedClock(NOW)
    h = Harness(tmp_path, clock=clock)
    first = h.svc.health()["signing_key"]
    clock.advance(days=31)
    h.ok(h.post("/sec/v1/jobs/rotate-signing-key/run", {"request_id": rid()}, caller="scheduler"))
    second = h.svc.health()["signing_key"]
    assert second != first and {k["kid"] for k in h.svc.jwks()["keys"]} == {first, second}
    clock.advance(days=2)
    h.ok(h.post("/sec/v1/jobs/rotate-signing-key/run", {"request_id": rid()}, caller="scheduler"))
    assert {k["kid"] for k in h.svc.jwks()["keys"]} == {second}
    old = h.svc.signing_keys[first]
    assert h.svc.secrets[old["secret_id"]]["status"] == "destroyed"


# ------------------------------------------------------------------------------------------------ freezes

def test_freeze_a_caller_stops_everything_it_does(hk):
    store_finance(hk)
    f = freeze(hk, "caller", "finance_31")
    assert hk.use("finance_31", REF, "stripe_api").json()["detail"] == "CALLER_FROZEN"
    assert hk.post("/sec/v1/identity/tokens", {"audience": "legal_37"}, caller="finance_31").status_code == 403
    assert hk.ok(hk.get("/sec/v1/identity/denylist", caller="legal_37"))["callers"] == ["finance_31"]
    hk.ok(hk.post(f"/sec/v1/freezes/{f['freeze_id']}/lift",
                  hk.approved("LIFT_FREEZE", f["freeze_id"], {"request_id": rid()})))
    assert hk.use("finance_31", REF, "stripe_api").status_code == 200


def test_freeze_one_secret(hk):
    store_finance(hk)
    freeze(hk, "secret", REF)
    assert hk.use("finance_31", REF, "stripe_api").json()["detail"] == "SECRET_FROZEN"


def test_lockdown_stops_all_callers(hk):
    store_finance(hk)
    freeze(hk, "all", "all")
    assert hk.use("finance_31", REF, "stripe_api").json()["detail"] == "LOCKDOWN"
    assert any(i["severity"] == "sev1" and i["code"] == "FREEZE_APPLIED" for i in hk.svc.incidents.values())
    assert hk.ok(hk.get("/sec/v1/identity/denylist", caller="legal_37"))["lockdown"] is True


def test_freeze_targets_validated(hk):
    for kind, target in (("caller", "nobody"), ("all", "finance_31")):
        body = {"request_id": rid(), "target_kind": kind, "target_id": target, "reason_code": "TEST"}
        assert hk.post("/sec/v1/freezes", hk.approved("FREEZE", f"{kind}:{target}", body)).status_code == 422


def test_canary_touch_freezes_the_caller_and_raises_sev1(hk):
    hk.andre_store("finance_31", "old_stripe_key", kind="canary", value=None)
    r = hk.use("finance_31", "vault:finance_31.old_stripe_key", "stripe_api")
    assert r.status_code == 404
    assert hk.svc._frozen("caller", "finance_31")
    assert any(i["code"] == "CANARY_TOUCHED" and i["severity"] == "sev1" for i in hk.svc.incidents.values())


def test_repeated_denials_auto_freeze(hk):
    store_finance(hk)
    for _ in range(3):
        hk.use("legal_37", REF, "stripe_api")
    assert hk.svc._frozen("caller", "legal_37")
    assert not hk.svc._frozen("caller", "finance_31")


def test_auth_failure_burst_opens_an_incident(hk):
    for _ in range(20):
        hk.client.get("/sec/v1/status", headers={"Authorization": "Bearer " + "w" * 40})
    assert any(i["code"] == "AUTH_FAILURE_BURST" for i in hk.svc.incidents.values())


# ------------------------------------------------------------------------------------------------ Legal holds

def test_legal_hold_answers_honestly_when_nothing_is_connected(hk):
    body = {"request_id": rid(), "hold_id": "lg-hld-1", "systems": ["email", "drive"], "subject_refs": ["post:abc"]}
    r = hk.ok(hk.post("/sec/v1/holds", body, caller="legal_37"), 201)
    assert r["delivered"] is False and "email" in r["reason"] and "drive" in r["reason"]
    assert r["systems"] == {"drive": "not_connected", "email": "not_connected"}
    assert hk.post("/sec/v1/holds", body, caller="finance_31").status_code == 403


def test_legal_hold_with_an_adapter_is_delivered(tmp_path):
    class Drive:
        system = "drive"

        def __init__(self):
            self.held = set()

        def preserve(self, hold_id, refs):
            self.held.add(hold_id)
            return True

        def release(self, hold_id):
            self.held.discard(hold_id)
            return True
    ports = Ports.default()
    drive = ports.preservation["drive"] = Drive()
    h = Harness(tmp_path, ports=ports)
    body = {"request_id": rid(), "hold_id": "h1", "systems": ["drive"]}
    assert h.ok(h.post("/sec/v1/holds", body, caller="legal_37"), 201)["delivered"] is True
    h.ok(h.post("/sec/v1/holds/h1/release", {"request_id": rid()}, caller="legal_37"))
    assert drive.held == set()


def test_a_hold_blocks_destroying_a_clients_secrets(hk):
    hk.ok(hk.post("/sec/v1/secrets", {"request_id": rid(), "name": "a", "kind": "oauth_token", "value": "v",
                                      "client_id": "c1"}, caller="onboarding"), 201)
    hk.ok(hk.post("/sec/v1/holds", {"request_id": rid(), "hold_id": "h1", "systems": ["drive"],
                                    "subject_refs": ["client:c1"]}, caller="legal_37"), 201)
    r = hk.post("/sec/v1/secrets/vault:onboarding.a/destroy", {"request_id": rid()}, caller="onboarding")
    assert r.status_code == 409 and r.json()["detail"] == "PRESERVATION_HOLD"
    assert hk.ok(hk.post("/sec/v1/clients/c1/destroy", {"request_id": rid()}, caller="onboarding"))["held"] == 1
    hk.ok(hk.post("/sec/v1/holds/h1/release", {"request_id": rid()}, caller="legal_37"))
    hk.ok(hk.post("/sec/v1/secrets/vault:onboarding.a/destroy", {"request_id": rid()}, caller="onboarding"))


# ------------------------------------------------------------------------------------------------ incidents

class Recorder:
    def __init__(self, name, result="delivered"):
        self.name, self.result, self.sent = name, result, []

    def send(self, msg):
        self.sent.append(msg)
        if self.result == "boom":
            raise RuntimeError("provider down")
        return self.result


def test_sev1_goes_to_all_three_channels_with_codes_only(tmp_path):
    ports = Ports.default()
    sms, email, push = Recorder("sms"), Recorder("email", "boom"), Recorder("push")
    ports.channels = {"sms": sms, "email": email, "push": push}
    h = Harness(tmp_path, ports=ports)
    h.enroll()
    h.andre_store("finance_31", "canary1", kind="canary", value=None)
    h.use("finance_31", "vault:finance_31.canary1", "p")
    assert len(sms.sent) == 1 and len(push.sent) == 1
    inc = next(i for i in h.svc.incidents.values() if i["code"] == "CANARY_TOUCHED")
    detail = h.ok(h.get(f"/sec/v1/incidents/{inc['incident_id']}"))
    assert detail["alerts"][0]["channels"] == {"sms": "delivered", "email": "failed", "push": "delivered"}
    msg = sms.sent[0]
    assert set(vars(msg)) == {"alert_id", "severity", "code", "incident_id", "subject"}


def test_unwired_channels_say_so(hk):
    hk.ok(hk.post("/sec/v1/incidents", {"request_id": rid(), "severity": "sev2", "code": "MANUAL_TEST",
                                        "subject": "test"}), 201)
    inc = hk.ok(hk.get("/sec/v1/incidents", params={"status": "open"}))
    detail = hk.ok(hk.get(f"/sec/v1/incidents/{inc[-1]['incident_id']}"))
    assert detail["alerts"][0]["channels"] == {"sms": "not_wired", "push": "not_wired"}
    assert hk.ok(hk.get("/sec/v1/status"))["alert_channels"] == {"sms": False, "email": False, "push": False}


def test_incident_note_and_passkey_close(hk):
    i = hk.ok(hk.post("/sec/v1/incidents", {"request_id": rid(), "severity": "sev3", "code": "MANUAL_TEST",
                                            "subject": "x"}), 201)
    iid = i["incident_id"]
    hk.ok(hk.post(f"/sec/v1/incidents/{iid}/notes", {"request_id": rid(), "note": "looked at it"}))
    body = {"request_id": rid(), "root_cause_code": "FALSE_ALARM", "note": "done"}
    closed = hk.ok(hk.post(f"/sec/v1/incidents/{iid}/close", hk.approved("INCIDENT_CLOSE", iid, body)))
    assert closed["status"] == "closed" and [t["event"] for t in closed["timeline"]] == ["opened", "note", "closed"]
    assert hk.post(f"/sec/v1/incidents/{iid}/notes", {"request_id": rid(), "note": "x"}).status_code == 409


def test_same_detection_twice_is_one_incident(hk):
    hk.andre_store("finance_31", "c", kind="canary", value=None)
    hk.andre_store("legal_37", "c", kind="canary", value=None)
    hk.use("finance_31", "vault:finance_31.c", "p")
    hk.svc._open_incident("sev1", "CANARY_TOUCHED", "caller:finance_31", "sec22_detector")
    assert sum(i["code"] == "CANARY_TOUCHED" for i in hk.svc.incidents.values()) == 1


# ------------------------------------------------------------------------------------------------ findings

def scan(hk, source, findings, when=None):
    when = when or datetime.now(timezone.utc)
    return hk.post("/sec/v1/scans", {"request_id": rid(), "source": source, "tool": "pip-audit@2.9.0",
                                     "scanned_at": when.isoformat(), "findings": findings}, caller="scheduler")


F1 = {"advisory_id": "GHSA-aaaa-bbbb-cccc", "package": "h11", "installed_version": "0.14.0",
      "fixed_versions": ["0.16.0"], "severity": "critical"}
F2 = {**F1, "advisory_id": "PYSEC-2026-1", "package": "jinja2", "severity": "low"}


def test_scan_opens_and_fixes_findings(hk):
    r = hk.ok(scan(hk, "python:finance-py", [F1, F2]), 201)
    assert r["opened"] == 2
    assert any(i["code"] == "CRITICAL_VULNERABILITY" for i in hk.svc.incidents.values())
    r = hk.ok(scan(hk, "python:finance-py", [F2]), 201)
    assert r["fixed"] == 1 and r["opened"] == 0
    st = {f["package"]: f["status"] for f in hk.ok(hk.get("/sec/v1/findings"))}
    assert st == {"h11": "fixed", "jinja2": "open"}


def test_sla_by_severity(hk):
    hk.ok(scan(hk, "python:legal-py", [F1, F2]), 201)
    due = {f["severity"]: f["due_by"] for f in hk.svc.findings.values()}
    today = datetime.now(timezone.utc).date()
    assert due["critical"] == (today + timedelta(days=7)).isoformat()
    assert due["low"] == (today + timedelta(days=180)).isoformat()


def test_accept_risk_is_bounded_and_needs_passkey(hk):
    hk.ok(scan(hk, "python:legal-py", [F2]), 201)
    fid = next(iter(hk.svc.findings))
    today = datetime.now(timezone.utc).date()
    too_long = {"request_id": rid(), "until": (today + timedelta(days=91)).isoformat(), "reason_code": "NO_FIX_YET"}
    assert hk.post(f"/sec/v1/findings/{fid}/accept", hk.approved("RISK_ACCEPT", fid, too_long)).status_code == 422
    ok = {"request_id": rid(), "until": (today + timedelta(days=30)).isoformat(), "reason_code": "NO_FIX_YET"}
    assert hk.ok(hk.post(f"/sec/v1/findings/{fid}/accept", hk.approved("RISK_ACCEPT", fid, ok)))["status"] == \
        "accepted"


def test_scans_only_from_scheduler_or_dashboard(hk):
    r = hk.post("/sec/v1/scans", {"request_id": rid(), "source": "python:x", "tool": "pip-audit@1",
                                  "scanned_at": datetime.now(timezone.utc).isoformat(), "findings": []},
                caller="finance_31")
    assert r.status_code == 403


def test_future_scan_refused(hk):
    assert scan(hk, "python:x", [], datetime.now(timezone.utc) + timedelta(hours=1)).status_code == 422


def test_overdue_findings_job(tmp_path):
    clock = FixedClock(NOW)
    h = Harness(tmp_path, clock=clock)
    h.ok(scan(h, "python:x", [F1], NOW), 201)
    clock.advance(days=8)
    r = h.ok(h.post("/sec/v1/jobs/findings-due/run", {"request_id": rid()}, caller="scheduler"))
    assert len(r["overdue"]) == 1
    assert any(i["code"] == "FINDING_OVERDUE" for i in h.svc.incidents.values())


# ------------------------------------------------------------------------------------------------ compliance C-14

class FakeCompliance:
    def __init__(self):
        self.pushed = []

    def push_control_result(self, control_id, request_id, result, tested_at, evidence):
        self.pushed.append((control_id, result, evidence))
        return ControlPush("delivered", 200)


def test_c14_report_fails_until_everything_holds(tmp_path):
    clock = FixedClock(NOW)
    ports = Ports.default()
    comp = ports.compliance = FakeCompliance()
    h = Harness(tmp_path, clock=clock, ports=ports)
    r = h.ok(h.post("/sec/v1/jobs/compliance-report/run", {"request_id": rid()}, caller="scheduler"))
    assert r["result"] == "fail" and "no scan ingested" in r["reasons"] and "no passkey enrolled" in r["reasons"]
    h.enroll()
    h.ok(scan(h, "python:x", [F2], NOW), 201)
    r = h.ok(h.post("/sec/v1/jobs/compliance-report/run", {"request_id": rid()}, caller="scheduler"))
    assert r["result"] == "pass" and r["delivery"] == "delivered"
    assert comp.pushed[-1][0] == "C-14" and comp.pushed[-1][1] == "pass"
    clock.advance(days=9)
    r = h.ok(h.post("/sec/v1/jobs/compliance-report/run", {"request_id": rid()}, caller="scheduler"))
    assert r["result"] == "fail" and any("stale" in x for x in r["reasons"])


def test_compliance_client_reads_the_answer(tmp_path):
    import httpx
    from compliance_client import HttpCompliance

    def handler(req):
        body = json.loads(req.content)
        assert req.headers["x-compliance-caller-token"] == "c" * 40 and body["result"] == "pass"
        return httpx.Response(200, json={"control": {"control_id": "C-14"}, "ledger_event_ids": []})
    c = HttpCompliance("http://compliance.internal", "s" * 40, "c" * 40, transport=httpx.MockTransport(handler))
    assert c.push_control_result("C-14", "r1", "pass", "2026-10-06T00:00:00Z", []).status == "delivered"
    bad = HttpCompliance("http://compliance.internal", "s" * 40, "c" * 40,
                         transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"control": {}})))
    assert bad.push_control_result("C-14", "r1", "pass", "x", []).status == "unavailable"
    refused = HttpCompliance("http://compliance.internal", "s" * 40, "c" * 40,
                             transport=httpx.MockTransport(lambda r: httpx.Response(403, json={})))
    assert refused.push_control_result("C-14", "r1", "pass", "x", []).status == "refused"


# ------------------------------------------------------------------------------------------------ integrity

def test_state_survives_restart(tmp_path):
    d = str(tmp_path / "data")
    h = Harness(tmp_path, data_dir=d)
    h.enroll()
    store_finance(h)
    freeze(h, "caller", "legal_37")
    h2 = h.restart()
    assert h2.ok(h2.use("finance_31", REF, "stripe_api"))["value"] == "sk_value_123"
    assert h2.svc._frozen("caller", "legal_37")
    assert h2.ok(h2.get("/health"))["integrity_ok"] is True


def test_edited_log_refuses_start(tmp_path):
    from store import StoreCorrupt
    d = str(tmp_path / "data")
    h = Harness(tmp_path, data_dir=d)
    h.enroll()
    store_finance(h)
    path = os.path.join(d, "security_log.jsonl")
    raw = open(path, "rb").read().replace(b'"finance_31"', b'"legal_37"', 1)
    open(path, "wb").write(raw)
    with pytest.raises(StoreCorrupt):
        h.restart()


def test_truncated_log_is_caught_against_the_ledger(tmp_path):
    d = str(tmp_path / "data")
    h = Harness(tmp_path, data_dir=d)
    h.enroll()
    store_finance(h)
    path = os.path.join(d, "security_log.jsonl")
    lines = open(path, "rb").read().splitlines(keepends=True)
    open(path, "wb").write(b"".join(lines[:-1]))
    h2 = h.restart()
    assert h2.ok(h2.get("/health"))["integrity_ok"] is False
    r = h2.use("finance_31", REF, "stripe_api")
    assert r.status_code == 503 and r.json()["detail"] == "INTEGRITY_UNVERIFIED"


def test_replaced_log_is_caught(tmp_path):
    d = str(tmp_path / "data")
    h = Harness(tmp_path, data_dir=d)
    h.enroll()
    os.unlink(os.path.join(d, "security_log.jsonl"))
    h2 = h.restart()
    assert h2.ok(h2.get("/health"))["integrity_ok"] is False


def test_crash_between_anchor_and_append_is_completed_at_start(tmp_path):
    d = str(tmp_path / "data")
    h = Harness(tmp_path, data_dir=d)
    h.enroll()
    h.svc.log.fail_next_append = True
    body = {"request_id": rid(), "name": "k", "kind": "api_key", "value": "v"}
    assert h.post("/sec/v1/secrets", body, caller="onboarding").status_code == 503
    assert os.path.exists(os.path.join(d, "pending.line"))
    h2 = h.restart()
    assert h2.ok(h2.get("/health"))["integrity_ok"] is True
    assert "vault:onboarding.k" in h2.svc.by_ref
    assert not os.path.exists(os.path.join(d, "pending.line"))


def test_pending_line_without_anchor_is_discarded(tmp_path):
    d = str(tmp_path / "data")
    h = Harness(tmp_path, data_dir=d)
    h.enroll()
    h.ledger.fail = True
    body = {"request_id": rid(), "name": "k", "kind": "api_key", "value": "v"}
    assert h.post("/sec/v1/secrets", body, caller="onboarding").status_code == 503
    h.ledger.fail = False
    h2 = h.restart()
    assert h2.ok(h2.get("/health"))["integrity_ok"] is True and "vault:onboarding.k" not in h2.svc.by_ref


def test_unreadable_ledger_at_start_means_nothing_works_until_it_is(tmp_path):
    d = str(tmp_path / "data")
    h = Harness(tmp_path, data_dir=d)
    h.enroll()
    store_finance(h)
    h.ledger.fail_reads = True
    h2 = h.restart()
    assert h2.use("finance_31", REF, "stripe_api").status_code == 503
    h.ledger.fail_reads = False
    h2.svc._last_integrity_try = 0
    assert h2.use("finance_31", REF, "stripe_api").status_code == 200


def test_second_process_on_same_data_dir_refused(tmp_path):
    from store import DataDirLock, StoreCorrupt
    d = str(tmp_path / "data")
    a = DataDirLock(d)
    with pytest.raises(StoreCorrupt):
        DataDirLock(d)
    a.release()
    DataDirLock(d).release()


def test_orphan_sealed_files_removed_at_start(tmp_path):
    d = str(tmp_path / "data")
    h = Harness(tmp_path, data_dir=d)
    h.svc.sealed.put("sc-sec-" + "0" * 40, 1, b"{}")
    h2 = h.restart()
    assert "sc-sec-" + "0" * 40 + ".v1" not in h2.svc.sealed.names()


def test_audit_export_has_no_secrets_and_names_approver(hk):
    store_finance(hk)
    ev = hk.ok(hk.get("/sec/v1/audit/events"))["events"]
    stored = next(e for e in ev if e["kind"] == "secret_stored" and e["data"]["owner"] == "finance_31")
    assert stored["approved_with"] == hk.keys[0].credential_id and "approval" not in stored["data"]


def test_integrity_job_alerts_on_failure(tmp_path):
    d = str(tmp_path / "data")
    h = Harness(tmp_path, data_dir=d)
    h.enroll()
    h.ledger.fail_reads = True
    r = h.ok(h.post("/sec/v1/jobs/integrity/run", {"request_id": rid()}, caller="scheduler"))
    assert r["integrity"]["ok"] is False


def test_rotation_due_job(tmp_path):
    clock = FixedClock(NOW)
    h = Harness(tmp_path, clock=clock)
    h.enroll()
    h.andre_store("finance_31", "k", rotate_by="2026-10-10")
    clock.advance(days=5)
    r = h.ok(h.post("/sec/v1/jobs/rotation-due/run", {"request_id": rid()}, caller="scheduler"))
    assert r["overdue"] == ["vault:finance_31.k"]


# ------------------------------------------------------------------------------------------------ config

def test_production_refuses_unsafe_settings(tmp_path):
    import config
    from helpers import base_env
    env = base_env(tmp_path)
    prod = {**env, "SEC_NON_PRODUCTION": "0", "SEC_DATA_DIR": str(tmp_path / "d")}
    with pytest.raises(RuntimeError, match="local_file"):
        config.load(prod)
    with pytest.raises(RuntimeError, match="SEC_DATA_DIR"):
        config.load({**{k: v for k, v in env.items() if k != "SEC_LOCAL_MASTER_KEY_FILE"},
                     "SEC_NON_PRODUCTION": "0", "SEC_KMS": "none"})
    for kms in ("aws", "gcp"):
        with pytest.raises(RuntimeError, match="not built"):
            config.load({**env, "SEC_KMS": kms})
    for name in ("SEC_ALERT_SMS", "SEC_ALERT_EMAIL", "SEC_ALERT_PUSH"):
        with pytest.raises(RuntimeError, match="refuses to start"):
            config.load({**env, name: "twilio"})
    with pytest.raises(RuntimeError, match="http"):
        config.load({**{k: v for k, v in env.items() if k != "SEC_LOCAL_MASTER_KEY_FILE"},
                     "SEC_NON_PRODUCTION": "0", "SEC_KMS": "none", "SEC_DATA_DIR": str(tmp_path / "d"),
                     "SEC_WEBAUTHN_ORIGINS": "http://localhost:3000", "SEC_WEBAUTHN_RP_ID": "localhost"})
    with pytest.raises(RuntimeError, match="pair"):
        config.load({**env, "SEC_WEBAUTHN_ORIGINS": ""})
    with pytest.raises(RuntimeError):
        config.load({**env, "SEC_SERVICE_TOKEN": "short"})


def test_master_key_file_rules(tmp_path):
    import base64

    import config
    from helpers import base_env, secret_file
    env = base_env(tmp_path)
    p = secret_file(tmp_path, "bad.key", base64.b64encode(b"k" * 16))
    with pytest.raises(RuntimeError, match="32"):
        config.load({**env, "SEC_LOCAL_MASTER_KEY_FILE": p})
    os.chmod(env["SEC_LOCAL_MASTER_KEY_FILE"], 0o644)
    with pytest.raises(RuntimeError, match="chmod"):
        config.load(env)
    link = str(tmp_path / "link.key")
    os.symlink(env["SEC_LOCAL_MASTER_KEY_FILE"], link)
    with pytest.raises(RuntimeError, match="symlink"):
        config.load({**env, "SEC_LOCAL_MASTER_KEY_FILE": link})
