"""Bug sweep C (Oct 6 2026, integration 5d49ee9) — clipper-network-py. Each test pins one finding and FAILS on
5d49ee9 (the sweep's probes were lost; these are re-derived from the code):

  R6   evidence ids leave out the payload hash or carry a timestamp: a retry after a state change is a lasting 409/503
  E-5/F-3  the old store: one fsync error bricks the log; no single-writer lock; no inert close()
  M    integrity(): ledger verify/entries and the chain re-read held the service lock
  M    the CN-26 money blocklist is bypassed with homoglyphs / invisible characters
"""

from __future__ import annotations

import os
import threading

import pytest

import config as config_mod
from helpers import ANDRE_TOKEN, Harness, rid
from ledger import LedgerNotRecorded
from textguard import money_or_earnings


def _fail_once(h, pred):
    real = h.ledger.record_event
    state = {"n": 0}

    def flaky(event_id, department, event_type, actor, subject_id, payload, summary):
        if state["n"] == 0 and pred(event_type):
            state["n"] += 1
            raise LedgerNotRecorded("simulated outage for one write")
        return real(event_id, department, event_type, actor, subject_id, payload, summary)
    h.ledger.record_event = flaky
    return state


def _banned(h):
    cid = h.admitted_clipper()
    h.vi.add_strike(cid, "S3")
    prop = h.run("/cn/v1/discipline/sync").json()["ban_proposals"][0]
    return cid, prop


# ============================================================================================ R6-M1 evidence

def test_r6_ban_approval_retried_after_its_line_failed_and_the_state_moved_is_not_a_lasting_409():
    h = Harness().ready()
    cid, prop = _banned(h)
    st = _fail_once(h, lambda t: t == "local_log_appended")
    body = {"request_id": rid(), "proposal_id": prop, "decision": "approve", "note": "upheld"}
    assert h.post(f"/cn/v1/clippers/{cid}/ban-decision", body, andre=ANDRE_TOKEN).status_code == 503
    assert st["n"] == 1
    h.clock.advance(minutes=5)                        # the queued notice's send window (and its payload) move on
    r = h.post(f"/cn/v1/clippers/{cid}/ban-decision", {**body, "request_id": rid()}, andre=ANDRE_TOKEN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"
    ev = h.get("/cn/v1/audit/evidence", event_type="ban_approved_by_andre").json()
    assert ev["counts"]["committed"] == 1
    for e in h.ledger.of_type("ban_approved_by_andre"):
        assert "rk" in e["payload"] and "seq" in e["payload"]


def test_r6_a_retried_data_export_is_the_same_evidence_not_a_new_clock_stamped_event():
    """The premise "a lasting 409" does not hold for ``data_exported``: its id carried the wall clock, so every retry
    was a NEW ledger event (duplicate evidence of one export, none named by a line), never a 409. Fixed the same way:
    no clock in the id; a retry of the same export is the same event (the ledger answers 200)."""
    h = Harness().ready()
    cid = h.admitted_clipper()
    _fail_once(h, lambda t: t == "local_log_appended")
    assert h.get(f"/cn/v1/clippers/{cid}/export", caller="hub").status_code == 503
    h.clock.advance(minutes=3)
    assert h.get(f"/cn/v1/clippers/{cid}/export", caller="hub").status_code == 200
    assert len(h.ledger.of_type("data_exported")) == 1


def test_r6_restart_after_a_retried_commit_needs_no_reconcile(tmp_path):
    x = Harness(data_dir=str(tmp_path / "d")).ready()
    cid, prop = _banned(x)
    _fail_once(x, lambda t: t == "local_log_appended")
    body = {"request_id": rid(), "proposal_id": prop, "decision": "approve", "note": "upheld"}
    x.post(f"/cn/v1/clippers/{cid}/ban-decision", body, andre=ANDRE_TOKEN)
    x.clock.advance(minutes=5)
    assert x.post(f"/cn/v1/clippers/{cid}/ban-decision", {**body, "request_id": rid()},
                  andre=ANDRE_TOKEN).status_code == 200
    y = Harness(data_dir=str(tmp_path / "d"), ledger=x.ledger, clock=x.clock, ports=x.ports)
    assert y.get("/health").json()["reconcile_required"] is False
    assert y.get("/cn/v1/integrity").json()["ok"] is True


def test_r6_admission_retry_after_its_ruling_was_recorded_but_not_committed_is_issued(tmp_path):
    x = Harness(data_dir=str(tmp_path / "d")).ready()
    cid = x.ready_applicant()
    x.ports.finance.form = False                       # first outcome: not admitted
    _fail_once(x, lambda t: t == "local_log_appended")
    fixed = rid("adm")
    assert x.admit(cid, request_id=fixed).status_code == 503
    x.ports.finance.form = True                        # the state moved: the same request now admits
    r = x.admit(cid, request_id=fixed)
    assert r.status_code == 200 and r.json()["admitted"] is True, r.text
    y = Harness(data_dir=str(tmp_path / "d"), ledger=x.ledger, clock=x.clock, ports=x.ports)
    assert y.get("/health").json()["reconcile_required"] is False


# ============================================================================================ E-5 / F-3 store

def test_store_one_fsync_error_never_bricks_the_log(tmp_path, monkeypatch):
    import store as store_mod
    d = str(tmp_path / "d")
    h = Harness(data_dir=d).ready()
    real = os.fsync
    hit = {"n": 0}

    def flaky(fd):
        # only the record log's fsync fails (the contact store fsyncs through the same os module)
        if os.readlink(f"/proc/self/fd/{fd}").endswith(store_mod.LOG_NAME) and hit["n"] == 0:
            hit["n"] += 1
            raise OSError(5, "simulated EIO on fsync")
        return real(fd)
    monkeypatch.setattr(store_mod.os, "fsync", flaky)
    assert h.apply("one@example.com").status_code == 503 and hit["n"] >= 1
    monkeypatch.setattr(store_mod.os, "fsync", real)
    assert h.apply("two@example.com").status_code == 201
    assert h.svc.log.verify()
    with open(os.path.join(d, store_mod.LOG_NAME), "rb") as fh:
        assert fh.read().count(b"\n") == len(h.svc.log)


def test_store_a_short_write_is_cut_back_and_refused(tmp_path, monkeypatch):
    import store as store_mod
    h = Harness(data_dir=str(tmp_path / "d")).ready()
    real = os.pwrite
    monkeypatch.setattr(store_mod.os, "pwrite", lambda fd, data, off: real(fd, data[: len(data) // 2], off))
    assert h.apply("three@example.com").status_code == 503
    monkeypatch.setattr(store_mod.os, "pwrite", real)
    assert h.apply("four@example.com").status_code == 201
    assert h.svc.log.verify() and h.svc.log.fault is None


def test_single_writer_and_an_inert_closed_instance(tmp_path):
    import api
    d = str(tmp_path / "d")
    h = Harness(data_dir=d).ready()
    with pytest.raises(Exception) as ei:
        api.build_service(config_mod.load(_env(h)), h.clock, h.ports, h.ledger)
    assert "data-directory claim" in str(ei.value) or "already holds this data directory" in str(ei.value)
    h.svc.close()
    assert h.svc.closed
    assert h.apply("five@example.com").status_code == 503
    h2 = Harness(data_dir=d, ledger=h.ledger, clock=h.clock, ports=h.ports)
    assert h2.svc.version_number == h.svc.version_number


def _env(h) -> dict:
    from helpers import base_env
    return {**base_env(), "CN_DATA_DIR": h.data_dir}


# ============================================================================================ mediums

def test_m_integrity_reads_the_ledger_outside_the_service_lock(hs):
    seen = []
    real_verify, real_entries = hs.ledger.verify, hs.ledger.entries

    def free() -> bool:
        got: list = []

        def probe():
            ok = hs.svc.lock.acquire(blocking=False)
            got.append(ok)
            if ok:
                hs.svc.lock.release()
        t = threading.Thread(target=probe)
        t.start()
        t.join(5)
        return bool(got and got[0])

    def verify():
        seen.append(free())
        return real_verify()

    def entries():
        seen.append(free())
        return real_entries()
    hs.ledger.verify, hs.ledger.entries = verify, entries
    assert hs.get("/cn/v1/integrity").json()["ok"] is True
    assert seen and all(seen)


@pytest.mark.parametrize("text", [
    "еarn cаsh fast",                  # Cyrillic е, а
    "pаyout every week",               # Cyrillic а
    "g​uaranteed views",          # zero-width space
    "ear­n more",                 # soft hyphen
    "gυaranteed",                      # Greek upsilon
    "éarnings",                        # diacritic
    "Ꮲayout",                          # Cherokee P
    "5 υsd",                           # Greek upsilon in a currency code
])
def test_m_the_money_blocklist_sees_through_homoglyphs_and_invisible_characters(text):
    assert money_or_earnings(text), text


def test_m_a_homoglyph_money_word_in_a_display_name_is_refused(hs):
    r = hs.apply("hg@example.com", display_name="Еarn Cаsh")
    assert r.status_code == 422, r.text


@pytest.mark.parametrize("text", ["Clip Person", "José García", "Иван Петров", "Zoë Ng", "Pay Out"])
def test_m_ordinary_names_still_pass(text):
    assert money_or_earnings(text) == []


def test_c3_a_suspended_vi_certification_is_read_not_refused_and_never_counts_toward_a_tier():
    """verification-py's C-3 fix adds the ``suspended`` status (access lost after certification); the thin client
    refused any status it did not know, so ONE suspended certification made the whole answer unavailable."""
    import httpx
    from httpclients import HttpVerificationIntegrity

    def ok(req):
        return httpx.Response(200, json={"clipper_id": "cn-clp-1", "rules_pinned": True, "certifications": [
            {"certification_id": "vi-cert-A", "submission_id": "s1", "campaign_id": "c1", "platform": "tiktok",
             "clipper_id": "cn-clp-1", "status": "suspended", "certified_views": 5000,
             "revision_watch_end": "2026-11-01T00:00:00Z"}]})
    vi = HttpVerificationIntegrity("http://vi.test", "svc", "caller", transport=httpx.MockTransport(ok))
    a = vi.certifications("cn-clp-1")
    assert a.available and a.certifications[0].status == "suspended"
