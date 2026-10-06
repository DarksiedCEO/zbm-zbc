"""Record-first storage (ADR 0015 decision 4: security-py's design as fixed in ADR 0012 rounds 1-5, with service-py's
closed-instance and claim / adopt rules from ADR 0014 rounds 5-5e)."""

from __future__ import annotations

import json
import os
import threading

import pytest

import api
import config as config_mod
import store as store_mod
from errors import Unavailable
from helpers import FakeLedger, Harness, base_env, rid, wired_ports, write_key
from ledger import LedgerRecordError


def durable(tmp_path, **kw):
    kw.setdefault("ports", wired_ports())
    return Harness(tmp_path, data_dir=str(tmp_path / "d"), **kw)


def populate(h):
    inf = h.creator()
    t = h.template()
    h.ok(h.email(inf, t), 201)
    h.ok(h.post("/suppressions", {"request_id": rid(), "email": "gone@else.test"}, caller="hub"), 201)
    c = h.campaign()
    b = h.brief(c)
    d = h.ok(h.deal(inf, c, b, fee="7000.00"), 201)
    return inf, t, d


def test_restart_keeps_everything(tmp_path):
    h = durable(tmp_path)
    inf, t, d = populate(h)
    h2 = h.restart()
    assert h2.svc.integrity["ok"] is True
    assert h2.svc.influencers.keys() == h.svc.influencers.keys()
    assert h2.svc.deals[d["deal_id"]]["status"] == "pending_andre"
    assert len(h2.svc.suppression) == 1 and h2.svc.templates[t["template_id"]]["versions"]["1"]["status"] == "approved"
    assert h2.svc.influencers[inf["influencer_id"]]["adult_attested"] is True


def test_replay_answers_the_same_after_restart(tmp_path):
    h = durable(tmp_path)
    body = {"request_id": rid(), "brand": "zbm", "name": "Fall", "kind": "influencer"}
    a = h.ok(h.post("/campaigns", body, caller="influencer_agent"), 201)
    h2 = h.restart()
    assert h2.ok(h2.post("/campaigns", body, caller="influencer_agent"), 201)["campaign_id"] == a["campaign_id"]
    h2.code(h2.post("/campaigns", {**body, "name": "x"}, caller="influencer_agent"), 409, "REQUEST_ID_REUSED")


def test_every_log_line_is_anchored(tmp_path):
    h = durable(tmp_path)
    populate(h)
    assert len(h.ledger.of_type("log_anchor")) == len(h.svc.log)
    assert h.ok(h.get("/audit/integrity", caller="compliance_38"))["integrity"]["ok"] is True


def test_ledger_down_nothing_takes_effect(tmp_path):
    h = durable(tmp_path)
    h.ledger.fail = True
    h.code(h.application(), 503, "LEDGER_UNAVAILABLE")
    assert not h.svc.influencers


def test_truncated_log_stops_every_write(tmp_path):
    h = durable(tmp_path)
    populate(h)
    path = tmp_path / "d" / "influencer_log.jsonl"
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(b"".join(lines[:-2]))
    h2 = h.restart()
    assert h2.svc.integrity["ok"] is False
    assert h2.client.get("/health").json() == {"status": "degraded"}
    h2.code(h2.post("/suppressions", {"request_id": rid(), "email": "x@y.test"}, caller="hub"), 503,
            "INTEGRITY_UNVERIFIED")


def test_replaced_log_detected(tmp_path):
    h = durable(tmp_path)
    populate(h)
    os.unlink(tmp_path / "d" / "influencer_log.jsonl")
    h2 = h.restart()
    assert h2.svc.integrity["ok"] is False and "another influencer log" in h2.svc.integrity["problem"]


def test_edited_or_blank_line_refuses_start(tmp_path):
    h = durable(tmp_path)
    populate(h)
    path = tmp_path / "d" / "influencer_log.jsonl"
    raw = path.read_bytes()
    path.write_bytes(raw.replace(b'"7000.00"', b'"0700.00"'))
    with pytest.raises(store_mod.StoreCorrupt):
        h.restart()
    path.write_bytes(raw + b"\n")
    with pytest.raises(store_mod.StoreCorrupt, match="empty line"):
        Harness(tmp_path, data_dir=str(tmp_path / "d"))
    path.write_bytes(raw)
    assert Harness(tmp_path, data_dir=str(tmp_path / "d"), ledger=h.ledger).svc.integrity["ok"] is True


def test_forged_pending_line_is_never_anchored_or_applied(tmp_path):
    """Someone with write access to the data directory forges a valid, chained line approving a $7,000 deal: it stays
    inert."""
    d = tmp_path / "d"
    h = durable(tmp_path)
    inf, t, deal = populate(h)
    lines = [ln for ln in (d / "influencer_log.jsonl").read_bytes().split(b"\n") if ln]
    rl = store_mod.RecordLog(None)
    rl._lines = lines
    _, line = rl.prepare("deal_approved", json.loads(lines[-1])["at"],
                         {"deal_id": deal["deal_id"], "material": {"mc_id": "x", "deal_id": deal["deal_id"],
                                                                    "influencer_id": inf["influencer_id"]},
                          "actor": "andre"})
    (d / "pending.line").write_bytes(line)
    anchors = len(h.ledger.of_type("log_anchor"))
    h.svc.close()
    h2 = Harness(tmp_path, data_dir=str(d), ledger=h.ledger, ports=h.ports)
    assert h2.svc.integrity["ok"] is True and h2.svc.deals[deal["deal_id"]]["status"] == "pending_andre"
    assert len(h2.ledger.of_type("log_anchor")) == anchors and (d / "pending.discarded").exists()


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
    r = h.post("/suppressions", {"request_id": rid(), "email": "x@y.test"}, caller="hub")
    assert r.status_code == 503 and h.svc.integrity["ok"] is False
    h.svc._last_integrity_try = 0
    assert h.svc.verify_integrity(force=True, always=True)["ok"] is True
    assert len(h.svc.suppression) == 1 and len(h.ledger.of_type("log_anchor")) == len(h.svc.log)


def test_a_failed_disk_write_cuts_back_and_is_rolled_forward(tmp_path):
    h = durable(tmp_path)
    h.svc.log.fail_next_append = True
    assert h.post("/suppressions", {"request_id": rid(), "email": "x@y.test"}, caller="hub").status_code == 503
    h.svc._last_integrity_try = 0
    assert h.svc.verify_integrity(force=True, always=True)["ok"] is True and len(h.svc.suppression) == 1


def test_changed_pii_key_refuses_start(tmp_path):
    h = durable(tmp_path)
    populate(h)
    other = tmp_path / "other.key"
    fd = os.open(other, os.O_WRONLY | os.O_CREAT, 0o600)
    os.write(fd, os.urandom(32).hex().encode())
    os.close(fd)
    h.svc.close()
    with pytest.raises(store_mod.StoreCorrupt, match="different key"):
        Harness(tmp_path, data_dir=str(tmp_path / "d"), ledger=h.ledger, INF_PII_HASH_KEY_FILE=str(other))
    assert Harness(tmp_path, data_dir=str(tmp_path / "d"), ledger=h.ledger).svc.integrity["ok"] is True


def test_nothing_is_written_before_the_key_is_bound(tmp_path):
    led = FakeLedger()
    led.fail = True
    h = durable(tmp_path, ledger=led)
    assert h.svc.integrity["ok"] is False and "fingerprint" in h.svc.integrity["problem"]
    led.fail = False
    h.svc._last_integrity_try = 0
    assert h.svc.verify_integrity(force=True, always=True)["ok"] is True
    assert h.svc.log.records[0]["kind"] == "pii_key_bound"


def test_the_local_log_is_the_system_of_record_and_only_the_export_is_minimised(tmp_path):
    h = durable(tmp_path)
    populate(h)
    assert "creator@example.test" in (tmp_path / "d" / "influencer_log.jsonl").read_text()
    assert "creator@example.test" not in json.dumps(h.ok(h.get("/audit/export?limit=1000", caller="compliance_38")))
    assert (os.stat(tmp_path / "d").st_mode & 0o777) == 0o700


# ------------------------------------------------------------------------------------------------ one instance, close()

def _env(tmp_path, d):
    key = tmp_path / "pii.key"
    return base_env(INF_NON_PRODUCTION=None, INF_DATA_DIR=str(d),
                    INF_PII_HASH_KEY_FILE=str(key) if key.exists() else write_key(key))


def test_a_second_build_in_the_same_process_is_refused(tmp_path):
    d = tmp_path / "d"
    _, s1 = api.build(_env(tmp_path, d))
    with pytest.raises(store_mod.DataDirBusy):
        api.build(_env(tmp_path, d))
    s1.close()
    _, s2 = api.build(_env(tmp_path, d))
    s2.close()


def test_a_build_that_fails_after_the_claim_gives_it_back(tmp_path):
    d = tmp_path / "d"
    _, s1 = api.build(_env(tmp_path, d))
    s1.close()
    with open(d / "influencer_log.jsonl", "ab") as fh:
        fh.write(b"\n")
    with pytest.raises(store_mod.StoreCorrupt, match="empty line"):
        api.build(_env(tmp_path, d))
    assert config_mod._HELD[os.path.realpath(d)].claimed is False


def test_a_claim_token_is_adopted_once_and_a_stale_one_releases_nothing(tmp_path):
    h = durable(tmp_path)
    lock = h.settings.data_dir_lock
    assert lock.adopt(h.svc._lock_token) is None                  # already adopted
    assert lock.release_claim("stale-token") is False and lock.claimed is True
    h.svc.close()
    assert lock.claimed is False


def test_a_closed_instance_never_writes(tmp_path):
    h = durable(tmp_path)
    populate(h)
    n, events = len(h.svc.log), len(h.ledger.events)
    h.svc.close()
    h.code(h.application(email="new@example.test"), 503, "SERVICE_CLOSED")
    with pytest.raises(Unavailable) as e:
        h.svc._commit("job_ran", {"job": "x"}, "scheduler")
    assert e.value.reason == "SERVICE_CLOSED"
    rec, line = h.svc.log.prepare("job_ran", "2026-10-06T18:00:00Z", {"actor": "scheduler"})
    with pytest.raises(store_mod.StoreWriteError, match="closed"):
        h.svc.log.append_prepared(rec, line)
    with pytest.raises(store_mod.StoreWriteError, match="closed"):
        h.svc.log.write_pending(line)
    h.svc.record_refusal("x", "ANDRE_APPROVAL_INVALID")
    h.code(h.job("integrity"), 503, "SERVICE_CLOSED")
    h.code(h.job("send-queue"), 503, "SERVICE_CLOSED")
    h.code(h.job("payout-retry"), 503, "SERVICE_CLOSED")
    h.code(h.get("/audit/integrity", caller="compliance_38"), 503, "SERVICE_CLOSED")
    h.code(h.get("/audit/export", caller="compliance_38"), 503, "SERVICE_CLOSED")
    assert h.client.get("/health").status_code == 503
    assert h.svc.verify_integrity(force=True, always=True)["problem"] == "this service instance is closed"
    assert len(h.svc.log) == n and len(h.ledger.events) == events
    h.code(h.post(f"/templates/{'if-tpl-' + '0' * 40}/versions/1/approve",
                  {"request_id": rid(), "content_sha256": "0" * 64}, andre="wrong-" + "w" * 40), 403,
           "ANDRE_APPROVAL_INVALID")
    assert len(h.ledger.events) == events                         # the refusal is not recorded by a closed instance
    assert Harness(tmp_path, data_dir=str(tmp_path / "d"), ledger=h.ledger).svc.integrity["ok"]


# ------------------------------------------------------------------------------------------------ ledger verify

def test_ledger_valid_is_always_the_real_verdict(tmp_path):
    h = durable(tmp_path)
    h.ledger.verify_result = False
    out = h.ok(h.job("integrity"))
    assert out["ledger_valid"] is False and out["integrity"]["ok"] is True
    assert h.ok(h.get("/audit/integrity", caller="compliance_38"))["ledger_valid"] is False


def test_a_slow_ledger_verify_runs_outside_the_service_lock(tmp_path):
    class Slow(FakeLedger):
        def __init__(self):
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def verify(self):
            self.entered.set()
            self.release.wait(10)
            return True
    led = Slow()
    h = durable(tmp_path, ledger=led)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", h.job("integrity")))
    t.start()
    assert led.entered.wait(10)
    h.ok(h.post("/campaigns", {"request_id": rid(), "brand": "zbm", "name": "n", "kind": "influencer"},
                caller="influencer_agent"), 201)                   # not blocked by the verify in flight
    assert t.is_alive()                                            # the verify is still waiting: it held no lock
    led.release.set()
    t.join(10)
    assert out["r"].status_code == 200 and out["r"].json()["ledger_valid"] is True


def test_close_during_verify_answers_503_and_writes_nothing(tmp_path):
    class Closing(FakeLedger):
        svc = None

        def verify(self):
            self.svc.close()
            return True
    led = Closing()
    h = durable(tmp_path, ledger=led)
    led.svc = h.svc
    n = len(h.svc.log)
    h.code(h.job("integrity"), 503, "SERVICE_CLOSED")
    assert len(h.svc.log) == n


def test_jobs_one_at_a_time_and_unknown(h):
    h.code(h.post("/jobs/nope/run", {"request_id": rid()}, caller="scheduler"), 422, "JOB_UNKNOWN")
    h.svc._tick_lock.acquire()
    try:
        h.code(h.job("send-queue"), 409, "JOB_RUNNING")
    finally:
        h.svc._tick_lock.release()
    h.ok(h.job("send-queue"))
