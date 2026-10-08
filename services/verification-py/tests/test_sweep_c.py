"""Bug sweep C (Oct 6 2026, integration 5d49ee9) — verification-py. Each test pins one finding; every one of them FAILS
on 5d49ee9 (the sweep's probes were lost; these are re-derived from the code):

  C-1  one failed / wedged certify item blocks every later clip (head-of-line blocking)
  C-2  the same post registered under several submissions is certified (paid) once per submission
  C-3  OAuth access lost after certification leaves the clip "certified" (it dodges the revision watch)
  R6   evidence ids leave out the payload hash: a retry after any state change is a permanent 409/503
  E-5/F-3  the old store: one fsync error bricks the log; no single-writer lock; no inert close()
  M    slow I/O under the lock; quadratic stolen check; new identities skip velocity; plus-tag minor-lock split;
       Andre's override does not stick; reads append log lines; cap overshoot is "clear"
"""

from __future__ import annotations

import os
import threading

import pytest

import config as config_mod
from fakes import FakeClipperNetwork
from helpers import ANDRE_TOKEN, Harness, rid
from ledger import LedgerNotRecorded
from ports import AgeProviderAnswer
from test_cert_scenarios import codes, hr13, run_to_day


def _two_clips(hr):
    hr.clean_clip("w1")
    hr.onboard("clip-b")
    ref = "https://www.tiktok.com/@c/video/w2"
    hr.post_video("tiktok", ref)
    hr.ok(hr.register("w2", "clip-b", post_ref=ref), 201)
    hr.approve("w2")
    run_to_day(hr, 13)
    hr.clock.advance(days=1)
    for j in ("liveness", "metrics", "revisions", "anomaly"):
        hr.job(j)


def _fail_once(h, pred):
    """The ledger refuses the first event matching ``pred`` (an outage for exactly that write)."""
    real = h.ledger.record_event
    state = {"n": 0}

    def flaky(event_id, department, event_type, actor, subject_id, payload, summary):
        if state["n"] == 0 and pred(event_type, subject_id):
            state["n"] += 1
            raise LedgerNotRecorded("simulated outage for one write")
        return real(event_id, department, event_type, actor, subject_id, payload, summary)
    h.ledger.record_event = flaky
    return state


# ============================================================================================ C-1

def test_c1_a_failed_commit_then_a_state_change_never_wedges_the_certify_job(hr):
    _two_clips(hr)
    st = _fail_once(hr, lambda t, s: t == "local_log_appended")       # w1's certification line loses its anchor
    hr.post("/vi/v1/jobs/certify/run", {"request_id": rid()}, caller="scheduler")
    assert st["n"] == 1
    hr.compliance.version += 1                                       # the register moves on: new payload content
    hr.ok(hr.post("/vi/v1/jobs/certify/run", {"request_id": rid()}, caller="scheduler"))
    assert hr.cert("w1")["status"] == "certified" and hr.cert("w2")["status"] == "certified"


def test_c1_one_item_that_keeps_failing_never_blocks_the_others_and_is_dead_lettered_for_andre():
    h = Harness(env={"VI_JOB_ITEM_MAX_FAILURES": "2"})
    h.approve_rules()
    _two_clips(h)
    real = h.ledger.record_event
    broken = {"on": True}

    def refuse_w1(event_id, department, event_type, actor, subject_id, payload, summary):
        if broken["on"] and event_type == "certification_issued" and subject_id == "w1":
            raise LedgerNotRecorded("w1's certification cannot be recorded")
        return real(event_id, department, event_type, actor, subject_id, payload, summary)
    h.ledger.record_event = refuse_w1
    r = h.ok(h.post("/vi/v1/jobs/certify/run", {"request_id": rid()}, caller="scheduler"))
    assert h.cert("w2")["status"] == "certified"                       # w2 is not behind w1
    assert r["summary"]["failed"] == 1 and r["already_ran"] is False
    h.clock.advance(days=1)
    h.ok(h.post("/vi/v1/jobs/certify/run", {"request_id": rid()}, caller="scheduler"))
    dead = h.ok(h.get("/vi/v1/jobs/dead-letter", caller="scheduler"))["items"]
    assert [(d["job"], d["subject_id"]) for d in dead] == [("certify", "w1")]
    assert h.ledger.of_type("job_item_dead_lettered")
    broken["on"] = False
    h.clock.advance(hours=1)
    h.ok(h.post("/vi/v1/jobs/certify/run", {"request_id": rid()}, caller="scheduler"))
    assert h.cert("w1")["status"] == "pending"                         # parked: only Andre puts it back
    bad = h.post("/vi/v1/jobs/certify/dead-letter/w1/requeue", {"request_id": rid()}, caller="scheduler")
    assert bad.status_code == 403
    h.ok(h.post("/vi/v1/jobs/certify/dead-letter/w1/requeue", {"request_id": rid()}, andre=ANDRE_TOKEN))
    assert h.ok(h.get("/vi/v1/jobs/dead-letter", caller="scheduler"))["items"] == []
    h.ok(h.post("/vi/v1/jobs/certify/run", {"request_id": rid()}, caller="scheduler"))
    assert h.cert("w1")["status"] == "certified"


def test_c1_a_wedged_platform_call_times_out_and_neither_blocks_the_job_nor_other_requests():
    h = Harness(env={"VI_PORT_CALL_TIMEOUT_S": "1"})
    h.approve_rules()
    h.clean_clip("w1")
    h.onboard("clip-b")
    ref = "https://www.tiktok.com/@c/video/w2"
    h.post_video("tiktok", ref)
    h.ok(h.register("w2", "clip-b", post_ref=ref), 201)
    h.approve("w2")
    run_to_day(h, 13)
    h.clock.advance(days=1)                       # settlement day: the certify job makes the settlement fetches
    ad = h.adapters["tiktok"]
    real = ad.fetch
    gate = threading.Event()

    def wedge(vault, vref, acct, video_ref, metrics, hint):
        if video_ref.endswith("/w1"):
            gate.wait(60)
        return real(vault, vref, acct, video_ref, metrics, hint)
    ad.fetch = wedge
    out = {}
    t = threading.Thread(target=lambda: out.update(r=h.post("/vi/v1/jobs/certify/run", {"request_id": rid()},
                                                              caller="scheduler")))
    try:
        t.start()
        t.join(30)
        assert not t.is_alive(), "the certify job is still stuck behind w1's wedged platform call"
        assert out["r"].status_code == 200
        assert h.cert("w2")["status"] == "certified"
        assert h.cert("w1")["status"] != "certified"
    finally:
        gate.set()
        t.join(60)


# ============================================================================================ C-2

def test_c2_the_same_post_under_another_submission_is_refused_whatever_the_url_spelling(hr):
    ref = hr.clean_clip("d1")
    assert hr.register("d2", "clip-a", post_ref=ref, campaign_id="camp-2").status_code == 409
    for variant in (ref.replace("https://www.", "https://") + "?lang=en&is_from_webapp=1",
                    ref.replace("https://www.tiktok.com", "HTTPS://WWW.TIKTOK.COM") + "/",
                    "https://m.tiktok.com/@someone-else/video/d1#frag"):
        r = hr.register(f"d-{len(variant)}", "clip-a", post_ref=variant, campaign_id="camp-3")
        assert r.status_code == 409, variant
        assert r.json()["duplicate_of"] == "d1"
    hr.onboard("clip-z")                                              # another clipper claiming the same post
    assert hr.register("d9", "clip-z", post_ref=ref, campaign_id="camp-9").status_code == 409


def test_c2_the_same_video_behind_a_different_reference_is_certified_once(hr):
    ref = hr.clean_clip("v1")
    short = "https://vm.tiktok.com/ZMabcdef/"                         # resolves to the same video (platform id)
    hr.post_video("tiktok", short, video_id=hr.adapters["tiktok"].videos[ref]["video_id"])
    hr.ok(hr.register("v2", "clip-a", post_ref=short, campaign_id="camp-2"), 201)
    hr.approve("v2")
    run_to_day(hr, 14)
    a, b = hr.cert("v1"), hr.cert("v2")
    assert a["status"] == "certified"
    assert b["status"] != "certified" and "DUPLICATE_POST" in codes(b)


# ============================================================================================ C-3

def _certified(hr, sid="r1"):
    hr.clean_clip(sid)
    run_to_day(hr, 14)
    assert hr.cert(sid)["status"] == "certified"
    return [c for c in hr.svc.connections.values() if c["status"] == "active"][0]


def test_c3_revoking_access_after_certification_suspends_payment_and_alerts_andre(hr):
    con = _certified(hr)
    hr.ok(hr.post(f"/vi/v1/connections/{con['connection_id']}/revoke", {"request_id": rid()}, caller="clipper_network"))
    c = hr.cert("r1")
    assert c["status"] == "suspended" and "CONNECTION_REVOKED" in codes(c) and c["certified_views"] is not None
    alerts = [x for x in hr.ok(hr.get("/vi/v1/holds")) if x["subject_id"] == "r1" and x["cause"] == "access_lost"]
    assert alerts and alerts[0]["status"] == "open"
    assert hr13(hr, "r1", "https://www.tiktok.com/@c/video/r1")["verified_views"] is False
    # reconnecting does not reinstate by itself: the revocation purged the raw post reference (VI-15b), so V&I
    # cannot re-read the post; the clip stays suspended until Andre releases the access-lost hold
    hr.connect("clip-a", account_id="acct-clip-a-tiktok")
    run_to_day(hr, 16, 15)
    assert hr.cert("r1")["status"] == "suspended"
    assert hr.post(f"/vi/v1/holds/{alerts[0]['hold_id']}/decision",
                   {"request_id": rid(), "decision": "release", "reason": "checked"}).status_code == 403
    hr.ok(hr.post(f"/vi/v1/holds/{alerts[0]['hold_id']}/decision",
                  {"request_id": rid(), "decision": "release", "reason": "post checked by hand"}, andre=ANDRE_TOKEN))
    c = hr.cert("r1")
    assert c["status"] == "certified" and c["certified_views"] == 5000


def test_c3_a_grant_lost_at_the_vault_suspends_after_the_missed_revision_checks(hr):
    _certified(hr, "r2")
    hr.vault.refs.clear()                                              # the platform/vault no longer gives a token
    run_to_day(hr, 17, 15)
    c = hr.cert("r2")
    assert c["status"] == "suspended" and "CONNECTION_REVOKED" in codes(c)
    # the clipper reconnects the SAME account: the next revision check reads the same video and reinstates
    hr.connect("clip-a", account_id="acct-clip-a-tiktok")
    run_to_day(hr, 18, 18)
    assert hr.cert("r2")["status"] == "certified"
    assert [x["status"] for x in hr.svc.holds.values() if x["subject_id"] == "r2" and x["cause"] == "access_lost"] \
        == ["released"]


def test_c3_reconnecting_another_account_never_reinstates(hr):
    _certified(hr, "r3")
    hr.vault.refs.clear()
    run_to_day(hr, 16, 15)
    assert hr.cert("r3")["status"] == "suspended"
    hr.adapters["tiktok"].videos["https://www.tiktok.com/@c/video/r3"]["author_id"] = "a-different-account"
    hr.connect("clip-a", account_id="a-different-account")
    run_to_day(hr, 17, 15)
    assert hr.cert("r3")["status"] == "suspended"


# ============================================================================================ R6-M1 evidence

def test_r6_retry_after_a_state_change_is_a_new_event_never_a_lasting_409(hr):
    _two_clips(hr)
    _fail_once(hr, lambda t, s: t == "local_log_appended")
    hr.post("/vi/v1/jobs/certify/run", {"request_id": rid()}, caller="scheduler")
    hr.compliance.version += 1
    hr.ok(hr.post("/vi/v1/jobs/certify/run", {"request_id": rid()}, caller="scheduler"))
    ev = hr.ok(hr.get("/vi/v1/audit/evidence", event_type="certification_issued"))
    w1 = [e for e in ev["events"] if e["subject_id"] == "w1"]
    assert [e["status"] for e in w1].count("attempted") == 1          # the try whose line never made it
    assert hr.cert("w1")["status"] == "certified"
    assert hr.svc.certs[hr.svc.cert_by_sub["w1"]]["ledger_event_id"] in \
        [e["event_id"] for e in w1 if e["status"] == "committed"]
    for e in hr.ledger.of_type("certification_issued"):
        assert "rk" in e["payload"] and "seq" in e["payload"]
        assert not any(k.endswith("_at") for k in e["payload"])


def test_r6_restart_after_a_retried_commit_starts_without_a_reconcile(tmp_path):
    x = Harness(data_dir=str(tmp_path / "d"))
    x.approve_rules()
    _two_clips(x)
    _fail_once(x, lambda t, s: t == "local_log_appended")
    x.post("/vi/v1/jobs/certify/run", {"request_id": rid()}, caller="scheduler")
    x.compliance.version += 1
    x.ok(x.post("/vi/v1/jobs/certify/run", {"request_id": rid()}, caller="scheduler"))
    y = Harness(data_dir=str(tmp_path / "d"), ledger=x.ledger, clock=x.clock, fakes=x.fakes)
    assert y.ok(y.get("/health"))["reconcile_required"] is False
    assert y.cert("w1")["status"] == "certified"


# ============================================================================================ E-5 / F-3 store

def test_store_one_fsync_error_never_bricks_the_log(tmp_path, monkeypatch):
    import store as store_mod
    d = str(tmp_path / "d")
    h = Harness(data_dir=d)
    h.approve_rules()
    real = os.fsync
    hit = {"n": 0}

    def flaky(fd):
        hit["n"] += 1
        if hit["n"] == 1:
            raise OSError(5, "simulated EIO on fsync")
        return real(fd)
    monkeypatch.setattr(store_mod.os, "fsync", flaky)
    r = h.post("/vi/v1/identity/checks", {"request_id": rid(), "clipper_id": "q1", "email": "q1@example.com"},
               caller="clipper_network")
    assert r.status_code == 503 and hit["n"] >= 1
    monkeypatch.setattr(store_mod.os, "fsync", real)
    h.identity("q2")
    assert h.svc.log.verify()
    with open(os.path.join(d, store_mod.LOG_NAME), "rb") as fh:
        assert fh.read().count(b"\n") == len(h.svc.log)


def test_store_a_short_write_is_cut_back_and_refused(tmp_path, monkeypatch):
    import store as store_mod
    d = str(tmp_path / "d")
    h = Harness(data_dir=d)
    h.approve_rules()
    real = os.pwrite
    monkeypatch.setattr(store_mod.os, "pwrite", lambda fd, data, off: real(fd, data[: len(data) // 2], off))
    r = h.post("/vi/v1/identity/checks", {"request_id": rid(), "clipper_id": "q3", "email": "q3@example.com"},
               caller="clipper_network")
    assert r.status_code == 503
    monkeypatch.setattr(store_mod.os, "pwrite", real)
    h.identity("q4")
    assert h.svc.log.verify() and h.svc.log.fault is None


def test_store_a_symlinked_log_is_never_written_through(tmp_path):
    import store as store_mod
    d = str(tmp_path / "d")
    h = Harness(data_dir=d)
    h.approve_rules()
    p = os.path.join(d, store_mod.LOG_NAME)
    target = str(tmp_path / "elsewhere.jsonl")
    os.replace(p, target)
    os.symlink(target, p)
    size = os.path.getsize(target)
    r = h.post("/vi/v1/identity/checks", {"request_id": rid(), "clipper_id": "q5", "email": "q5@example.com"},
               caller="clipper_network")
    assert r.status_code == 503 and os.path.getsize(target) == size


def test_single_writer_and_an_inert_closed_instance(tmp_path):
    import api
    d = str(tmp_path / "d")
    h = Harness(data_dir=d)
    h.approve_rules()
    with pytest.raises(Exception) as ei:
        api.build_service(config_mod.load(h.env), h.clock, None, h.ledger)
    assert "data-directory claim" in str(ei.value) or "already holds this data directory" in str(ei.value)
    h.svc.close()
    assert h.svc.closed
    r = h.post("/vi/v1/identity/checks", {"request_id": rid(), "clipper_id": "q6", "email": "q6@example.com"},
               caller="clipper_network")
    assert r.status_code == 503
    h2 = Harness(data_dir=d, ledger=h.ledger, clock=h.clock, fakes=h.fakes)
    assert h2.svc.rules_version == 1


# ============================================================================================ mediums

def test_m_integrity_reads_the_ledger_outside_the_service_lock(hr):
    seen = {}
    real = hr.ledger.entries

    def probe(got):
        ok = hr.svc.lock.acquire(blocking=False)
        got.append(ok)
        if ok:
            hr.svc.lock.release()

    def entries():
        got: list = []
        t = threading.Thread(target=probe, args=(got,))
        t.start()
        t.join(5)
        seen.setdefault("free", []).append(bool(got and got[0]))
        return real()
    hr.ledger.entries = entries
    assert hr.ok(hr.get("/vi/v1/integrity"))["status"] == "green"
    assert seen["free"] and all(seen["free"])


def test_m_stolen_check_is_one_recorded_crossing_per_registration_not_one_per_clip(hr):
    for i in range(12):
        hr.onboard(f"cl-{i}")
        hr.post_video("tiktok", f"https://www.tiktok.com/@c/video/st{i}")
        hr.ok(hr.register(f"st{i}", f"cl-{i}", post_ref=f"https://www.tiktok.com/@c/video/st{i}"), 201)
    n = len(hr.ledger.of_type("crossing_hasher_requested"))
    hr.onboard("cl-last")
    hr.ok(hr.register("st-last", "cl-last", post_ref="https://www.tiktok.com/@c/video/stlast"), 201)
    # video_signature + ONE batched comparison (12 other clips), never 12 recorded crossings
    assert len(hr.ledger.of_type("crossing_hasher_requested")) - n <= 2


def test_m_a_new_identity_spike_is_measured_against_the_platform_not_skipped():
    h = Harness(env={"VI_ANOM_MIN_HISTORY": "2"})
    h.approve_rules()
    for i in range(2):                                                 # two established clippers' certified clips
        h.onboard(f"old-{i}")
        ref = f"https://www.tiktok.com/@c/video/b{i}"
        h.post_video("tiktok", ref, views=100, likes=10)
        h.ok(h.register(f"b{i}", f"old-{i}", post_ref=ref), 201)
        h.approve(f"b{i}")

    def grow(d):
        for i in range(2):
            h.adapters["tiktok"].videos[f"https://www.tiktok.com/@c/video/b{i}"]["values"]["views"] = 100 + 50 * d
    run_to_day(h, 14, on_day=grow)
    h.onboard("brand-new")                                            # no certified history of its own
    ref = "https://www.tiktok.com/@c/video/nspike"
    h.post_video("tiktok", ref, views=100, likes=10)
    h.ok(h.register("nspike", "brand-new", post_ref=ref), 201)
    h.approve("nspike")
    run_to_day(h, 15, 15)
    h.adapters["tiktok"].videos[ref]["values"].update(views=9000, likes=900)
    run_to_day(h, 16, 16)
    assert h.svc.screens["nspike"]["signals"]["velocity"]["status"] == "fired"


def test_m_a_plus_tag_address_cannot_split_off_from_a_minor_identity(hr):
    hr.connect("kid")
    hr.identity("kid", "Kid.Name@gmail.com")
    hr.age.answer = AgeProviderAnswer("minor", None, None, True, "p", "r")
    hr.ok(hr.age_check("kid", dob="2012-01-01"))
    hr.age.answer = AgeProviderAnswer("adult", None, None, True, "p", "r2")
    hr.identity("kid2", "kidname+alt@gmail.com")
    assert hr.ok(hr.age_check("kid2"))["status"] == "minor"
    hr.identity("kid3", "kid.name+x@googlemail.com")
    assert hr.ok(hr.age_check("kid3"))["status"] == "minor"


def test_m_andres_overturn_of_an_identity_finding_sticks(hr):
    hr.onboard("old-a")
    hr.connect("new-b")
    hr.identity("new-b", "old-a@example.com")
    f = [x for x in hr.ok(hr.get("/vi/v1/findings")) if x["kind"] == "duplicate_identity"][0]
    hr.ok(hr.post(f"/vi/v1/findings/{f['finding_id']}/decision", {"request_id": rid(), "decision": "overturn",
                                                                  "reason": "different people"}, andre=ANDRE_TOKEN))
    hr.identity("new-b", "old-a@example.com")                          # the same evidence, checked again
    assert hr.svc.findings[f["finding_id"]]["status"] == "overturned"
    assert not [h for h in hr.svc.holds.values() if h["subject_id"] == "new-b" and h["status"] == "open"]


def test_m_reads_write_no_log_line_and_repeat_reads_add_nothing_to_the_ledger(hr):
    a = hr.ok(hr.age_check("clip-r"))
    n = len(hr.svc.log)
    hr.ok(hr.get("/vi/v1/age/subjects/clip-r", caller="onboarding"))
    hr.ok(hr.get(f"/vi/v1/age/attestations/{a['attestation_id']}", caller="compliance_38"))
    assert len(hr.svc.log) == n                                         # a read never appends a log line
    led = len(hr.ledger.events)                                         # (each answer is recorded once: ADR 0007 #23)
    for _ in range(3):
        hr.ok(hr.get("/vi/v1/age/subjects/clip-r", caller="onboarding"))
        hr.ok(hr.get(f"/vi/v1/age/attestations/{a['attestation_id']}", caller="compliance_38"))
    assert len(hr.svc.log) == n and len(hr.ledger.events) == led


def test_m_overshooting_the_view_cap_is_not_cleared():
    h = Harness(env={"VI_REQUIRE_PERCEPTUAL_MATCH": "0"}, fakes={"cn": FakeClipperNetwork(cap=20000)})
    h.approve_rules()
    h.onboard("c")
    ref = "https://www.tiktok.com/@c/video/cap"
    h.post_video("tiktok", ref, views=30000, likes=3000)
    h.ok(h.register("cap", "c", post_ref=ref), 201)
    h.approve("cap")
    run_to_day(h, 15)
    assert h.svc.screens["cap"]["signals"]["cap_proximity"]["status"] == "fired"
    assert h.cert("cap")["status"] != "certified"
