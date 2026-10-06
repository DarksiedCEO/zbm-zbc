"""Record-first durability (ADR 0016 decision 25), service-py's / security-py's design as fixed in their AEGIS rounds:
restart keeps state, a forged pending line is inert, a truncated or replaced log stops all writes, a lost ledger
answer is rolled forward, nothing takes effect without the ledger, appends are exact-size, blank lines refuse start."""

from __future__ import annotations

import json
import os

import pytest

import store as store_mod
from helpers import FakeLedger, Harness, rid, wired_ports
from ledger import LedgerRecordError


def _log(d):
    return os.path.join(d, "bizdev_log.jsonl")


def _act(h, i=0):
    return h.post("/partners", {"request_id": rid(), "partner_key": f"partner-{i:04d}", "kind": "referral",
                                "brands": ["zbm"], "name": f"Partner {i}", "domain": f"p{i}.test"})


class Lossy(FakeLedger):
    """Anchors are recorded but the answer is lost (``after``) or never land (default)."""
    lose = 0
    after = False

    def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary):
        if self.lose and event_type == "log_anchor":
            self.lose -= 1
            if self.after:
                super().record_event(event_id, department, event_type, actor, subject_id, payload, summary)
            raise LedgerRecordError("lost")
        super().record_event(event_id, department, event_type, actor, subject_id, payload, summary)


def test_restart_keeps_everything(hd):
    p = hd.pursuit()
    hd.bid(p["pursuit_id"])
    r = hd.ready_response(p["pursuit_id"])
    hd.ok(hd.submit(r), 201)
    pt = hd.partner()
    hd.rate(pt["partner_id"])
    c = hd.contact()
    t = hd.template()
    hd.ok(hd.queue(c, t), 201)
    h2 = hd.restart()
    assert h2.svc.integrity["ok"] is True
    assert h2.ok(h2.get(f"/pursuits/{p['pursuit_id']}"))["stage"] == "responding"
    assert h2.ok(h2.get("/submissions?status=queued"))
    assert h2.ok(h2.get(f"/partners/{pt['partner_id']}"))["rate_approved"]["rate_pct"] == "10.00"
    assert h2.ok(h2.get("/outreach/messages?status=queued"))
    h2.refused(h2.submit(r), 409, "RESPONSE_ALREADY_QUEUED")


def test_idempotent_replay_survives_restart(hd):
    body = {"request_id": "same-one", "partner_key": "west", "kind": "referral", "brands": ["zbm"], "name": "West",
            "domain": "west.test"}
    first = hd.ok(hd.post("/partners", body), 201)
    h2 = hd.restart()
    assert h2.ok(h2.post("/partners", body), 201)["partner_id"] == first["partner_id"]
    h2.refused(h2.post("/partners", {**body, "name": "Other"}), 409, "REQUEST_ID_REUSED")


def test_request_key_is_actor_op_target_request_id(h):
    p = h.partner()
    body = {"request_id": "rk-1", "version": 1, "rate_pct": "10.00"}
    h.ok(h.post(f"/partners/{p['partner_id']}/rate", body))
    h.ok(h.post(f"/partners/{p['partner_id']}/rate", body))                  # replay: same answer
    assert ("bizdev_agent", f"rate_propose|{p['partner_id']}|rk-1") in h.svc.requests
    assert ("dashboard", f"rate_propose|{p['partner_id']}|rk-1") not in h.svc.requests


def test_every_line_is_anchored_and_typed_events_come_first(h):
    p = h.partner()
    h.rate(p["partner_id"])
    types = [e["event_type"] for e in h.ledger.events]
    assert types.count("log_anchor") == len(h.svc.log)
    i = types.index("rate_approved")
    assert types[i + 1] == "log_anchor"


def test_no_contact_details_amounts_or_text_in_ledger_payloads(tmp_path):
    h = Harness(tmp_path, ports=wired_ports())
    d = h.won_deal(value="8123.45", rate="11.00")
    h.ok(h.money_event(d["deal_id"], "payment", "777.77"))
    h.ok(h.job("payout-request"))
    c = h.contact(email="person@client.test")
    h.ok(h.queue(c, h.template()), 201)
    h.ok(h.job("send-queue"))
    blob = json.dumps([e["_payload"] for e in h.ledger.events]) + json.dumps(h.ledger.entries())
    for needle in ("person@client.test", "8123.45", "777.77", "11.00", "West Coast Agency", "vault:tax", "fin:payee",
                   "Pat"):
        assert needle not in blob, needle


def test_audit_export_minimises(hd):
    c = hd.contact(email="person@client.test")
    out = hd.ok(hd.get("/audit/export", caller="compliance_38"))
    blob = json.dumps(out)
    assert "person@client.test" not in blob and "Pat Lee" not in blob and c["email_hash"] in blob


def test_ledger_unreadable_at_start_nothing_takes_effect(tmp_path):
    led = FakeLedger()
    led.fail_reads = True
    h = Harness(tmp_path, ledger=led)
    assert h.svc.integrity["ok"] is False
    h.refused(_act(h), 503, "INTEGRITY_UNVERIFIED")
    assert h.client.get("/health").json() == {"status": "degraded"}


def test_ledger_down_nothing_applied(h):
    h.ledger.fail = True
    h.refused(_act(h), 503, "LEDGER_UNAVAILABLE")
    assert h.svc.partners == {}
    h.ledger.fail = False
    h.ok(_act(h), 201)


def test_typed_event_failure_writes_no_line(h):
    p = h.partner()
    h.rate(p["partner_id"], approve=False)
    lines = len(h.svc.log)
    h.ledger.fail_types = {"rate_approved"}
    pr = h.ok(h.get(f"/partners/{p['partner_id']}"))["rate_proposed"]
    h.refused(h.post(f"/partners/{p['partner_id']}/rate/approve", {"request_id": rid(), "version": 1,
                                                                    "binding_sha256": pr["binding_sha256"]},
                     andre=True), 503, "LEDGER_UNAVAILABLE")
    assert len(h.svc.log) == lines and h.svc.partners[p["partner_id"]]["rate_approved"] is None


def test_truncated_log_is_detected_against_the_ledger(hd):
    for i in range(3):
        hd.ok(_act(hd, i), 201)
    path = _log(hd.settings.data_dir)
    lines = open(path, "rb").read().splitlines(keepends=True)
    open(path, "wb").write(b"".join(lines[:-1]))
    h2 = hd.restart()
    assert h2.svc.integrity["ok"] is False and "beyond the local log" in h2.svc.integrity["problem"]
    h2.refused(_act(h2, 9), 503, "INTEGRITY_UNVERIFIED")


def test_a_replaced_log_is_detected(hd):
    hd.ok(_act(hd), 201)
    os.unlink(_log(hd.settings.data_dir))
    h2 = hd.restart()
    assert h2.svc.integrity["ok"] is False and "another bizdev log" in h2.svc.integrity["problem"]


def test_an_edited_log_line_refuses_start(hd):
    hd.ok(_act(hd), 201)
    path = _log(hd.settings.data_dir)
    raw = open(path, "rb").read().replace(b'"referral"', b'"white_label"', 1)
    open(path, "wb").write(raw)
    with pytest.raises(store_mod.StoreCorrupt):
        hd.restart()


def test_a_blank_line_refuses_start(hd):
    hd.ok(_act(hd), 201)
    with open(_log(hd.settings.data_dir), "ab") as fh:
        fh.write(b"\n")
    with pytest.raises(store_mod.StoreCorrupt, match="empty line"):
        hd.restart()


def test_a_blank_line_is_never_appended(h):
    rec, line = h.svc.log.prepare("job_ran", "2026-10-06T18:00:00Z", {"job": "x", "actor": "scheduler"})
    assert line and b"\n" not in line


def test_append_is_exact_size(hd):
    hd.ok(_act(hd), 201)
    with open(_log(hd.settings.data_dir), "ab") as fh:
        fh.write(b'{"junk":1}\n')                         # the file no longer matches memory
    hd.refused(_act(hd, 1), 503)
    assert hd.svc.integrity["ok"] is False


def test_second_process_on_the_same_data_dir_refuses(hd):
    with pytest.raises(store_mod.StoreCorrupt, match="another bizdev-py process"):
        store_mod.DataDirLock(hd.settings.data_dir)


def test_pii_key_change_refuses_start(hd):
    hd.ok(_act(hd), 201)
    other = hd.tmp / "other.key"
    fd = os.open(str(other), os.O_WRONLY | os.O_CREAT, 0o600)
    os.write(fd, ("ab" * 16 + "cd" * 16).encode() + b"0123456789abcdef" * 2)
    os.close(fd)
    with pytest.raises(store_mod.StoreCorrupt, match="different key"):
        hd.restart(NBD_PII_HASH_KEY_FILE=str(other))


# --------------------------------------------------------------------------------------------------- pending line

def _forge_pending(d, actor="andre"):
    """An attacker with write access to NBD_DATA_DIR only: a valid, chained line approving a deal."""
    lines = [ln for ln in open(_log(d), "rb").read().split(b"\n") if ln]
    rl = store_mod.RecordLog(None)
    rl._lines = lines
    _, line = rl.prepare("partner_registered", json.loads(lines[-1])["at"],
                         {"partner_id": "nb-ptn-" + "f" * 40, "partner_key": "forged", "kind": "referral",
                          "brands": ["zbm"], "name": "Forged", "domain": "forged.test", "notes": None, "flags": [],
                          "actor": actor})
    with open(os.path.join(d, "pending.line"), "wb") as fh:
        fh.write(line)


def test_a_forged_pending_line_is_never_anchored_or_applied(hd):
    d = hd.settings.data_dir
    hd.ok(_act(hd), 201)
    _forge_pending(d)
    anchors = len(hd.ledger.of_type("log_anchor"))
    h2 = hd.restart()
    assert h2.svc.integrity["ok"] is True
    assert "nb-ptn-" + "f" * 40 not in h2.svc.partners
    assert len(h2.ledger.of_type("log_anchor")) == anchors
    assert os.path.exists(os.path.join(d, "pending.discarded"))
    h2.ok(_act(h2, 5), 201)
    h2.svc._last_integrity_try = 0
    assert h2.svc.verify_integrity(force=True, always=True)["ok"] is True
    assert not os.path.exists(os.path.join(d, "pending.discarded"))


def test_a_forged_line_replacing_our_own_pending_line_is_ignored(tmp_path):
    d = str(tmp_path / "d")
    led = Lossy()
    h = Harness(tmp_path, data_dir=d, ledger=led)
    h.ok(_act(h, 0), 201)
    led.lose = 2
    h.refused(_act(h, 1), 503)
    _forge_pending(d)
    h.svc._last_integrity_try = 0
    assert h.svc.verify_integrity(force=True, always=True)["ok"] is True
    assert "nb-ptn-" + "f" * 40 not in h.svc.partners and len(h.svc.partners) == 2      # our own line rolled forward


def test_a_lost_ledger_answer_stops_writes_and_is_rolled_forward(tmp_path):
    led = Lossy()
    led.after = True
    h = Harness(tmp_path, data_dir=str(tmp_path / "d"), ledger=led)
    led.lose = 2
    body = {"request_id": "once", "partner_key": "lost-one", "kind": "referral", "brands": ["zbm"], "name": "L",
            "domain": "l.test"}
    h.refused(h.post("/partners", body), 503, "LEDGER_UNAVAILABLE")
    assert h.svc.integrity["ok"] is False
    h.svc._last_integrity_try = 0
    assert h.svc.verify_integrity(force=True, always=True)["ok"] is True
    again = h.ok(h.post("/partners", body), 201)
    assert len(h.svc.partners) == 1 and again["partner_id"] in h.svc.partners


def test_an_anchor_that_lands_after_restart_is_appended_then(tmp_path):
    class Deferred(FakeLedger):
        hold = False
        held: list = []

        def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary):
            a = (event_id, department, event_type, actor, subject_id, payload, summary)
            if self.hold and event_type == "log_anchor":
                self.held.append(a)
                raise LedgerRecordError("in flight")
            super().record_event(*a)
    d = str(tmp_path / "d")
    led = Deferred()
    h = Harness(tmp_path, data_dir=d, ledger=led)
    led.hold = True
    h.refused(_act(h), 503)
    led.hold = False
    h2 = h.restart()
    assert not h2.svc.partners and h2.svc.integrity["ok"] is True
    FakeLedger.record_event(led, *led.held[0])
    h2.svc._last_integrity_try = 0
    assert h2.svc.verify_integrity(force=True, always=True)["ok"] is True
    assert len(h2.svc.partners) == 1


def test_a_failed_append_is_rolled_forward_never_written_twice(hd):
    hd.svc.log.fail_next_append = True
    hd.refused(_act(hd), 503)
    assert hd.svc.integrity["ok"] is False
    hd.svc._last_integrity_try = 0
    assert hd.svc.verify_integrity(force=True, always=True)["ok"] is True
    assert len(hd.svc.partners) == 1 and hd.svc.log.verify()
    assert hd.restart().svc.integrity["ok"] is True


def test_the_integrity_job_and_route(hd):
    hd.ok(_act(hd), 201)
    r = hd.ok(hd.job("integrity"))
    assert r["integrity"]["ok"] is True and r["ledger_valid"] is True
    a = hd.ok(hd.get("/audit/integrity", caller="compliance_38"))
    assert a["integrity"]["ok"] and a["log_length"] == len(hd.svc.log)


def test_restart_with_wired_ports_does_not_resend(tmp_path):
    ports = wired_ports()
    h = Harness(tmp_path, data_dir=str(tmp_path / "d"), ports=ports)
    h.ok(h.queue(h.contact(), h.template()), 201)
    p = h.pursuit()
    h.bid(p["pursuit_id"])
    h.ok(h.submit(h.ready_response(p["pursuit_id"])), 201)
    h.ok(h.job("send-queue"))
    h.ok(h.job("submission-queue"))
    h2 = h.restart()
    h2.ok(h2.job("send-queue"))
    h2.ok(h2.job("submission-queue"))
    assert len(ports.email.sent) == 1 and len(ports.submission.calls) == 1
