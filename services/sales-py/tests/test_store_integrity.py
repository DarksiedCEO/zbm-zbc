"""Record-first storage (ADR 0013 decision 4; security-py's design as fixed in ADR 0012 rounds 1-5): restart keeps
state, every line is anchored, a forged pending line stays inert, a truncated or replaced log stops writes."""

from __future__ import annotations

import json
import os

import pytest

import store as store_mod
from helpers import FakeLedger, Harness, rid, secret_file, wired_ports
from ledger import LedgerRecordError


def durable(tmp_path, **kw):
    return Harness(tmp_path, data_dir=str(tmp_path / "d"), **kw)


def populate(h):
    lead = h.lead()
    h.ok(h.consent(lead["contact_id"]), 201)
    h.ok(h.price("zbm.revenue_recovery_engagement", price="2500.00"))
    h.ok(h.post("/sales/v1/suppressions", {"request_id": rid(), "email": "gone@else.test", "reason": "manual"},
                "hub"), 201)
    t = h.template()
    return lead, t


def test_restart_keeps_everything(tmp_path):
    h = durable(tmp_path)
    lead, t = populate(h)
    h2 = h.restart()
    assert h2.svc.integrity["ok"] is True
    c = h2.ok(h2.get(f"/sales/v1/contacts/{lead['contact_id']}"))
    assert c["consent"]["sms:zbm"] is True
    assert h2.ok(h2.get("/sales/v1/pricebook/zbm"))[2]["approved"] or any(
        x["approved"] for x in h2.ok(h2.get("/sales/v1/pricebook/zbm")))
    assert len(h2.svc.suppression) == 1 and h2.svc.templates[t["template_id"]]["versions"]["1"]["status"] == "approved"
    assert h2.svc.leads.keys() == h.svc.leads.keys()


def test_replay_answers_the_same_after_restart_and_409_on_a_changed_body(tmp_path):
    h = durable(tmp_path)
    body = {"request_id": rid(), "source": "inbound", "contact": {"name": "A", "email": "a@b.test"},
            "evidence": {"kind": "site_form", "ref": "x", "captured_at": "2026-10-06T10:00:00Z"}}
    a = h.ok(h.post("/sales/v1/leads", body, "hub"), 201)
    h2 = h.restart()
    assert h2.ok(h2.post("/sales/v1/leads", body, "hub"), 201)["lead_id"] == a["lead_id"]
    h2.refused(h2.post("/sales/v1/leads", {**body, "contact": {"name": "B", "email": "a@b.test"}}, "hub"), 409,
               "REQUEST_ID_REUSED")


def test_every_log_line_is_anchored_on_the_ledger(tmp_path):
    h = durable(tmp_path)
    populate(h)
    assert len(h.ledger.of_type("log_anchor")) == len(h.svc.log)
    assert h.ok(h.get("/sales/v1/audit/integrity"))["integrity"]["ok"] is True


def test_ledger_down_nothing_takes_effect(tmp_path):
    h = durable(tmp_path)
    h.ledger.fail = True
    r = h.post("/sales/v1/leads", {"request_id": rid(), "source": "inbound", "contact": {"name": "A",
                                                                                         "email": "a@b.test"},
                                   "evidence": {"kind": "site_form", "ref": "x",
                                                "captured_at": "2026-10-06T10:00:00Z"}}, "hub")
    assert r.status_code == 503 and not h.svc.leads


def test_truncated_log_stops_every_write(tmp_path):
    h = durable(tmp_path)
    populate(h)
    path = tmp_path / "d" / "sales_log.jsonl"
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(b"".join(lines[:-2]))
    h2 = h.restart()
    assert h2.svc.integrity["ok"] is False
    assert h2.client.get("/health").json() == {"status": "degraded"}
    h2.refused(h2.post("/sales/v1/suppressions", {"request_id": rid(), "email": "x@y.test", "reason": "manual"},
                       "hub"), 503, "INTEGRITY_UNVERIFIED")


def test_replaced_log_detected(tmp_path):
    h = durable(tmp_path)
    populate(h)
    os.unlink(tmp_path / "d" / "sales_log.jsonl")
    h2 = h.restart()
    assert h2.svc.integrity["ok"] is False and "another sales log" in h2.svc.integrity["problem"]


def test_edited_log_line_refuses_start(tmp_path):
    h = durable(tmp_path)
    populate(h)
    path = tmp_path / "d" / "sales_log.jsonl"
    path.write_bytes(path.read_bytes().replace(b'"manual"', b'"spoofd"'))
    with pytest.raises(store_mod.StoreCorrupt):
        h.restart()


def test_forged_pending_line_is_never_anchored_or_applied(tmp_path):
    """An attacker with write access to the data directory only forges a valid, chained line that would delete a
    suppression's effect by binding another PII key fingerprint — or grant a consent: it stays inert."""
    d = tmp_path / "d"
    h = durable(tmp_path)
    lead, _ = populate(h)
    lines = [ln for ln in (d / "sales_log.jsonl").read_bytes().split(b"\n") if ln]
    rl = store_mod.RecordLog(None)
    rl._lines = lines
    key = "phone:" + "1" * 64 + "|sms|zbc"
    _, line = rl.prepare("consent_granted", json.loads(lines[-1])["at"],
                         {"key": key, "contact_id": lead["contact_id"], "channel": "sms", "brand": "zbc",
                          "source": "web_form", "captured_at": "2026-10-06T10:00:00Z", "consent_text_version": "v",
                          "consent_text_sha256": "b" * 64, "actor": "hub"})
    (d / "pending.line").write_bytes(line)
    anchors = len(h.ledger.of_type("log_anchor"))
    h2 = h.restart()
    assert h2.svc.integrity["ok"] is True and key not in h2.svc.consents
    assert len(h2.ledger.of_type("log_anchor")) == anchors
    assert (d / "pending.discarded").exists()
    h2.ok(h2.post("/sales/v1/suppressions", {"request_id": rid(), "email": "x@y.test", "reason": "manual"}, "hub"),
          201)
    h2.svc._last_integrity_try = 0
    h2.svc.verify_integrity(force=True, always=True)
    assert not (d / "pending.discarded").exists() and key not in h2.svc.consents


def test_lost_ledger_answer_is_rolled_forward(tmp_path):
    class Lossy(FakeLedger):
        lose = 0

        def record_event(self, event_id, department, event_type, actor, subject_id, payload, summary):
            if self.lose and event_type == "log_anchor":
                self.lose -= 1
                raise LedgerRecordError("lost")
            super().record_event(event_id, department, event_type, actor, subject_id, payload, summary)
    led = Lossy()
    h = durable(tmp_path, ledger=led)
    led.lose = 2
    r = h.post("/sales/v1/suppressions", {"request_id": rid(), "email": "x@y.test", "reason": "manual"}, "hub")
    assert r.status_code == 503 and h.svc.integrity["ok"] is False
    h.svc._last_integrity_try = 0
    assert h.svc.verify_integrity(force=True, always=True)["ok"] is True
    assert len(h.svc.suppression) == 1                     # the line that may have landed is in, exactly once
    assert len(h.ledger.of_type("log_anchor")) == len(h.svc.log)


def test_a_failed_disk_write_cuts_back_and_stops_writes(tmp_path):
    h = durable(tmp_path)
    h.svc.log.fail_next_append = True
    r = h.post("/sales/v1/suppressions", {"request_id": rid(), "email": "x@y.test", "reason": "manual"}, "hub")
    assert r.status_code == 503
    h.svc._last_integrity_try = 0
    assert h.svc.verify_integrity(force=True, always=True)["ok"] is True
    assert len(h.svc.suppression) == 1


def test_second_process_on_the_same_data_dir_refuses(tmp_path):
    d = str(tmp_path / "d")
    first = store_mod.DataDirLock(d)
    with pytest.raises(store_mod.StoreCorrupt, match="another sales-py process"):
        store_mod.DataDirLock(d)
    first.release()


def test_changed_pii_key_refuses_start(tmp_path):
    k1 = secret_file(tmp_path, "k1", b"first-key-0123456789abcdefghijklmnopqrstuv")
    k2 = secret_file(tmp_path, "k2", b"second-key-0123456789abcdefghijklmnopqrstu")
    h = durable(tmp_path, SALES_PII_HASH_KEY_FILE=k1)
    populate(h)
    with pytest.raises(store_mod.StoreCorrupt, match="different key"):
        h.restart(SALES_PII_HASH_KEY_FILE=k2)
    assert h.restart(SALES_PII_HASH_KEY_FILE=k1).svc.integrity["ok"] is True


def test_blank_line_refuses_start(tmp_path):
    h = durable(tmp_path)
    populate(h)
    with open(tmp_path / "d" / "sales_log.jsonl", "ab") as fh:
        fh.write(b"\n")
    with pytest.raises(store_mod.StoreCorrupt, match="empty line"):
        h.restart()


def test_suppression_survives_restart_and_still_blocks(tmp_path):
    h = durable(tmp_path, ports=wired_ports())
    lead = h.lead()
    h.ok(h.post("/sales/v1/suppressions", {"request_id": rid(), "contact_id": lead["contact_id"], "reason": "manual"},
                "dashboard"), 201)
    h2 = h.restart()
    t = h2.template()
    h2.refused(h2.post("/sales/v1/outreach/email", {"request_id": rid(), "contact_id": lead["contact_id"],
                                                    "template_id": t["template_id"], "version": 1}, "sales_agent"),
               403, "SUPPRESSED")


def test_audit_export_minimises_personal_data(tmp_path):
    h = durable(tmp_path)
    populate(h)
    blob = json.dumps(h.ok(h.get("/sales/v1/audit/export?limit=1000", "compliance_38")))
    assert "jane@acme-shop.test" not in blob and "3105550100" not in blob and "Jane Doe" not in blob
    assert "email_hash" in blob and "name_sha256" in blob
    raw = (tmp_path / "d" / "sales_log.jsonl").read_text()
    assert "jane@acme-shop.test" in raw          # the local log is the system of record; only the export is minimised


def test_nothing_is_written_before_the_pii_key_is_bound(tmp_path):
    led = FakeLedger()
    led.fail = True                                   # reads work, writes fail: the binding cannot be recorded
    h = durable(tmp_path, ledger=led)
    assert h.svc.integrity["ok"] is False and "fingerprint" in h.svc.integrity["problem"]
    led.fail = False
    h.svc._last_integrity_try = 0
    assert h.svc.verify_integrity(force=True, always=True)["ok"] is True
    assert h.svc.log.records[0]["kind"] == "pii_key_bound"
