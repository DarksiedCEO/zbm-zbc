"""Record-first durability (ADR 0014 decisions 22-25), security-py's design as fixed in its AEGIS rounds 1-5: restart
keeps state, a forged pending line is inert, a truncated or replaced log stops all writes, a lost ledger answer is
rolled forward, nothing takes effect without the ledger, bodies never reach the ledger."""

from __future__ import annotations

import json
import os

import pytest

import store as store_mod
from helpers import FakeLedger, Harness, RecordingSender, rid
from ledger import LedgerRecordError
from ports import Ports


def _log(d):
    return os.path.join(d, "service_log.jsonl")


def test_restart_keeps_everything(hd):
    hd.article()
    cid = hd.contact(phone="+13105551234", timezone="America/Los_Angeles")
    hd.ok(hd.consent(cid), 201)
    r = hd.ok(hd.chat("what are your hours"), 201)
    esc = hd.ok(hd.chat("refund please", ref="client:other"), 201)
    h2 = hd.restart()
    assert h2.svc.integrity["ok"] is True
    assert h2.ok(h2.get(f"/svc/v1/contacts/{cid}/consents", caller="hub"))[0]["status"] == "active"
    t = h2.ok(h2.get(f"/svc/v1/tickets/{r['ticket_id']}"))
    assert [m["text"] for m in t["messages"]] == ["what are your hours",
                                                  "We are open Monday to Friday, 9am to 6pm Pacific."]
    assert h2.ok(h2.get(f"/svc/v1/tickets/{esc['ticket_id']}"))["status"] == "escalated"
    assert h2.ok(h2.chat("what are your hours", ref="client:third"), 201)["action"] == "answered"   # still approved
    assert h2.svc.requests == {**hd.svc.requests, **{k: v for k, v in h2.svc.requests.items()
                                                     if k not in hd.svc.requests}}


def test_idempotent_replay_survives_restart(hd):
    body = {"request_id": "fixed-1", "brand": "zbm", "contact_ref": "client:acme", "text": "need help"}
    a = hd.ok(hd.post("/svc/v1/chat/messages", body, caller="hub"), 201)
    h2 = hd.restart()
    assert h2.ok(h2.post("/svc/v1/chat/messages", body, caller="hub"), 201) == a
    r = h2.post("/svc/v1/chat/messages", {**body, "text": "different"}, caller="hub")
    assert r.status_code == 409 and r.json()["detail"] == "REQUEST_ID_REUSED"
    assert len(h2.svc.tickets) == 1 and len([m for m in h2.svc.messages.values() if m["dir"] == "in"]) == 1


def test_idempotency_key_is_actor_operation_target_and_request_id(h):
    a = h.ok(h.chat("need help", request_id="same"), 201)
    b = h.ok(h.chat("need help", brand="zbc", request_id="same"), 201)        # another target: a new message
    assert a["message_id"] != b["message_id"]


def test_message_bodies_never_reach_the_ledger_or_the_log(hd):
    hd.article()
    hd.contact(email="secret.person@acme.test", phone="+13105551234")
    hd.ok(hd.chat("my very private question about hours"), 201)
    led = json.dumps(hd.ledger.events)
    assert "private question" not in led and "secret.person" not in led and "+13105551234" not in led
    raw = open(_log(hd.settings.data_dir), "rb").read()
    assert b"private question" not in raw and b"secret.person" in raw     # the contact lives in the local log only
    bodies = os.listdir(os.path.join(hd.settings.data_dir, "bodies"))
    assert len(bodies) == 2 and all(len(b) == 64 for b in bodies)


def test_a_body_is_stored_once(hd):
    hd.ok(hd.chat("same words", ref="client:a"), 201)
    hd.ok(hd.chat("same words", ref="client:b"), 201)
    assert len(os.listdir(os.path.join(hd.settings.data_dir, "bodies"))) == 1


def test_an_edited_body_file_is_never_returned(hd):
    r = hd.ok(hd.chat("original words"), 201)
    d = os.path.join(hd.settings.data_dir, "bodies")
    name = os.listdir(d)[0]
    with open(os.path.join(d, name), "wb") as fh:
        fh.write(b"forged words")
    t = hd.ok(hd.get(f"/svc/v1/tickets/{r['ticket_id']}"))
    assert t["messages"][0]["text"] is None


def test_orphan_bodies_are_removed_at_start(hd):
    hd.ledger.fail = True
    assert hd.chat("never committed").status_code == 503
    hd.ledger.fail = False
    assert len(os.listdir(os.path.join(hd.settings.data_dir, "bodies"))) == 1
    hd.restart()
    assert os.listdir(os.path.join(hd.settings.data_dir, "bodies")) == []


def test_audit_export_minimises_personal_data(hd):
    cid = hd.contact(email="dana@acme.test", phone="+13105551234", display_name="Dana Rivera",
                     timezone="America/Los_Angeles")
    hd.ok(hd.consent(cid), 201)
    ev = hd.ok(hd.get("/svc/v1/audit/events", caller="compliance_38"))
    text = json.dumps(ev)
    assert "dana@acme.test" not in text and "Dana" not in text and "+13105551234" not in text
    assert "client:acme" not in text and "America/Los_Angeles" not in text
    first = ev["events"][0]["data"]["effects"][0]
    assert len(first["email_sha256"]) == 64 and "response" not in ev["events"][0]["data"]


def test_every_line_is_anchored_and_typed_events_come_first(h):
    h.article()
    h.ok(h.chat("refund"), 201)
    types = [e["event_type"] for e in h.ledger.events]
    assert types.count("log_anchor") == len(h.svc.log)
    i_esc = types.index("escalation_opened")
    assert types[i_esc + 1:].count("log_anchor") >= 1                 # the anchor of that line follows its event


def test_ledger_unreadable_at_start_nothing_takes_effect(tmp_path):
    led = FakeLedger()
    led.fail_reads = True
    h = Harness(tmp_path, ledger=led)
    assert h.svc.integrity["ok"] is False
    r = h.chat("help")
    assert r.status_code == 503 and r.json()["detail"] == "INTEGRITY_UNVERIFIED"
    assert h.get("/health", caller=None).json() == {"status": "degraded"}


def test_truncated_log_is_detected_against_the_ledger(hd):
    for i in range(3):
        hd.ok(hd.chat(f"help {i}", ref=f"client:c{i}"), 201)
    path = _log(hd.settings.data_dir)
    lines = open(path, "rb").read().splitlines(keepends=True)
    open(path, "wb").write(b"".join(lines[:-1]))
    h2 = hd.restart()
    assert h2.svc.integrity["ok"] is False and "beyond the local log" in h2.svc.integrity["problem"]
    assert h2.chat("more").status_code == 503


def test_a_replaced_log_is_detected(hd):
    hd.ok(hd.chat("help"), 201)
    os.unlink(_log(hd.settings.data_dir))
    h2 = hd.restart()
    assert h2.svc.integrity["ok"] is False and "another service log" in h2.svc.integrity["problem"]


def test_an_edited_log_line_refuses_start(hd):
    hd.ok(hd.chat("help"), 201)
    path = _log(hd.settings.data_dir)
    raw = open(path, "rb").read().replace(b'"chat"', b'"sms"', 1)
    open(path, "wb").write(raw)
    with pytest.raises(store_mod.StoreCorrupt):
        hd.restart()


def test_a_blank_line_refuses_start_with_a_clear_message(hd):
    hd.ok(hd.chat("help"), 201)
    with open(_log(hd.settings.data_dir), "ab") as fh:
        fh.write(b"\n")
    with pytest.raises(store_mod.StoreCorrupt, match="empty line"):
        hd.restart()


def test_second_process_on_the_same_data_dir_refuses(hd):
    lock = store_mod.DataDirLock(hd.settings.data_dir)
    try:
        with pytest.raises(store_mod.StoreCorrupt, match="another service-py process"):
            store_mod.DataDirLock(hd.settings.data_dir)
    finally:
        lock.release()


# --------------------------------------------------------------------------------------------------- pending line

def _forge_pending_consent(h, d, contact_id):
    """An attacker with write access to SVC_DATA_DIR only: a valid, chained consent line for a contact."""
    lines = [ln for ln in open(_log(d), "rb").read().split(b"\n") if ln]
    rl = store_mod.RecordLog(None)
    rl._lines = lines
    _, line = rl.prepare("consent_granted", json.loads(lines[-1])["at"],
                         {"effects": [{"op": "consent_granted", "consent_id": "sv-cns-" + "f" * 40,
                                       "contact_id": contact_id, "channel": "sms", "source": "portal_form",
                                       "text_sha256": "0" * 64, "captured_at": "2026-10-01T00:00:00Z",
                                       "express": True}], "actor": "hub"})
    with open(os.path.join(d, "pending.line"), "wb") as fh:
        fh.write(line)


def test_a_forged_pending_line_is_never_anchored_or_applied(hd):
    d = hd.settings.data_dir
    cid = hd.contact(phone="+13105551234", timezone="America/Los_Angeles")
    _forge_pending_consent(hd, d, cid)
    anchors = len(hd.ledger.of_type("log_anchor"))
    h2 = hd.restart()
    assert h2.svc.integrity["ok"] is True
    assert h2.ok(h2.get(f"/svc/v1/contacts/{cid}/consents", caller="hub")) == []      # no consent: inert
    assert len(h2.ledger.of_type("log_anchor")) == anchors
    assert os.path.exists(os.path.join(d, "pending.discarded"))
    h2.ok(h2.chat("help"), 201)                                                     # the log moves on
    h2.svc._last_integrity_try = 0
    assert h2.svc.verify_integrity(force=True, always=True)["ok"] is True
    assert not os.path.exists(os.path.join(d, "pending.discarded"))


def test_a_forged_line_replacing_our_own_pending_line_is_ignored(tmp_path):
    class Lossy(FakeLedger):
        lose = 0

        def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary):
            if self.lose and event_type == "log_anchor":
                self.lose -= 1
                raise LedgerRecordError("lost")
            super().record_event(event_id, department, event_type, actor, subject_id, payload, summary)
    d = str(tmp_path / "d")
    led = Lossy()
    h = Harness(tmp_path, data_dir=d, ledger=led)
    cid = h.contact(phone="+13105551234", timezone="America/Los_Angeles")
    led.lose = 2
    assert h.chat("my own message").status_code == 503
    _forge_pending_consent(h, d, cid)
    h.svc._last_integrity_try = 0
    assert h.svc.verify_integrity(force=True, always=True)["ok"] is True
    assert h.ok(h.get(f"/svc/v1/contacts/{cid}/consents", caller="hub")) == []
    assert len(h.svc.tickets) == 1                                    # our own line was rolled forward


def test_a_lost_ledger_answer_stops_writes_and_is_rolled_forward(tmp_path):
    class Lossy(FakeLedger):
        lose = 0

        def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary):
            super().record_event(event_id, department, event_type, actor, subject_id, payload, summary)
            if self.lose and event_type == "log_anchor":          # recorded, but the answer is lost
                self.lose -= 1
                raise LedgerRecordError("answer lost")
    led = Lossy()
    h = Harness(tmp_path, data_dir=str(tmp_path / "d"), ledger=led)
    led.lose = 2
    body = {"request_id": "once", "brand": "zbm", "contact_ref": "client:acme", "text": "help"}
    assert h.post("/svc/v1/chat/messages", body, caller="hub").status_code == 503
    assert h.svc.integrity["ok"] is False
    h.svc._last_integrity_try = 0
    assert h.svc.verify_integrity(force=True, always=True)["ok"] is True
    again = h.ok(h.post("/svc/v1/chat/messages", body, caller="hub"), 201)    # the stored answer, once
    assert len(h.svc.tickets) == 1 and again["ticket_id"] in h.svc.tickets


def test_an_anchor_that_lands_after_restart_is_appended_then(tmp_path):
    class Deferred(FakeLedger):
        hold = False
        held = []

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
    assert h.chat("in flight").status_code == 503
    led.hold = False
    h2 = h.restart()
    assert not h2.svc.tickets and h2.svc.integrity["ok"] is True
    FakeLedger.record_event(led, *led.held[0])
    h2.svc._last_integrity_try = 0
    assert h2.svc.verify_integrity(force=True, always=True)["ok"] is True
    assert len(h2.svc.tickets) == 1


def test_a_failed_append_is_rolled_forward_never_written_twice(hd):
    hd.svc.log.fail_next_append = True
    assert hd.chat("help").status_code == 503
    assert hd.svc.integrity["ok"] is False
    hd.svc._last_integrity_try = 0
    assert hd.svc.verify_integrity(force=True, always=True)["ok"] is True
    assert len(hd.svc.tickets) == 1 and hd.svc.log.verify()
    assert hd.restart().svc.integrity["ok"] is True


def test_the_integrity_job_and_route(hd):
    hd.ok(hd.chat("help"), 201)
    r = hd.ok(hd.job("integrity"))
    assert r["integrity"]["ok"] is True and r["ledger_valid"] is True
    a = hd.ok(hd.get("/svc/v1/audit/integrity", caller="compliance_38"))
    assert a["integrity"]["ok"] and a["log_length"] == len(hd.svc.log)


def test_restart_with_wired_sender_does_not_resend(tmp_path):
    ports = Ports.default()
    sender = ports.senders["email"] = RecordingSender()
    h = Harness(tmp_path, data_dir=str(tmp_path / "d"), ports=ports)
    r = h.ok(h.email("help"), 201)
    h.ok(h.post(f"/svc/v1/tickets/{r['ticket_id']}/reply", {"request_id": rid(), "text": "ok"}, andre=True), 201)
    h.ok(h.job("outbound-tick"))
    h2 = h.restart()
    h2.ok(h2.job("outbound-tick"))
    assert len(sender.sent) == 1
