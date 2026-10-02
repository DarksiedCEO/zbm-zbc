"""AEGIS round 16 findings on V&I (N16-1, N16-3, N16-6 V&I side, N16-7, N16-8, N16-12). Each test was written
first and failed on integration-2026-09-24 @ 923e20c (evidence in the fix17 scratch folder)."""

from __future__ import annotations

import threading
from datetime import timedelta

import httpx
import pytest

from clock import iso
from fakes import FakeCompliance
from helpers import ANDRE_TOKEN, NOW, Harness, rid
from ports import AdapterAnswer, AgeProviderAnswer, RegisterRow
from test_cert_scenarios import run_to_day


def _new(**kw):
    h = Harness(**kw)
    h.approve_rules()
    return h


# ============================================================ N16-1 wall-clock budget; no remote call under the lock

class _Drip(httpx.SyncByteStream):
    """An answer that drips one byte every ``gap_s`` until the test ends it (``done``) — never on its own."""

    def __init__(self, gap_s: float, done: threading.Event):
        self.gap, self.done = gap_s, done

    def __iter__(self):
        while not self.done.wait(self.gap):
            yield b" "


def test_n16_1_compliance_client_has_a_total_wall_clock_deadline_not_a_per_chunk_one():
    """Wave 25 (scout B M2): ordered by state, not by a wall-clock bound a starved runner can break. The answer drips
    a byte every 0.9 s (each gap inside the 1.0 s budget) and never ends until the test ends it: a TOTAL budget
    returns `unavailable` while it is still dripping; a per-chunk deadline would read until the test gave up."""
    from compliance_client import HttpComplianceRegister

    done = threading.Event()
    c = HttpComplianceRegister("http://c.test", "svc", "caller", timeout=1.0,
                               transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=_Drip(0.9, done))))
    got = {}
    t = threading.Thread(target=lambda: got.update(row=c.row("HR-13")))
    t.start()
    t.join(60)                                        # a bound on a stall, never on the answer's speed
    returned_while_dripping = not t.is_alive() and not done.is_set()
    done.set()
    t.join(60)
    assert returned_while_dripping, "the client read the dripping answer past its 1.0 s budget (a per-chunk deadline)"
    assert got["row"].available is False


class _SlowCompliance(FakeCompliance):
    """A Compliance read that does not return until the test releases it (``inside`` is set once a read is in it)."""

    def __init__(self):
        super().__init__()
        self.block = False
        self.inside = threading.Event()
        self.release = threading.Event()

    def row(self, obligation_id):
        if self.block:
            self.inside.set()
            self.release.wait(120)
        return super().row(obligation_id)


def test_n16_1_certify_job_does_not_hold_the_lock_while_compliance_is_slow():
    """Wave 25 (scout B M2/M3): ordered by state, not by a sleep and a wall-clock bound. The GET is issued only once
    the certify job is INSIDE the Compliance read (0f017a7 slept 0.3 s and hoped — a GET issued before the job got
    there was fast for the wrong reason), and it must answer while that read is still held open: a lock held across
    the read would keep the GET waiting until the test releases it."""
    slow = _SlowCompliance()
    h = Harness(fakes={"compliance": slow})
    h.approve_rules()
    h.clean_clip("s1")
    slow.block = True
    done = {}
    t = threading.Thread(target=lambda: done.update(r=h.post("/vi/v1/jobs/certify/run", {"request_id": rid()},
                                                             caller="scheduler")))
    t.start()
    try:
        assert slow.inside.wait(60), "the certify job never reached the Compliance read"
        got = {}
        g = threading.Thread(target=lambda: got.update(r=h.get("/vi/v1/holds")))
        g.start()
        g.join(60)                                    # a bound on a stall, never on the answer's speed
        answered_while_held = not g.is_alive() and not slow.release.is_set()
    finally:
        slow.release.set()
        t.join(60)
    g.join(60)
    assert answered_while_held, "an unrelated GET waited behind a Compliance read held open (the lock was held)"
    assert got["r"].status_code == 200 and done["r"].status_code == 200


def test_n16_1_hr13_is_read_once_per_certify_run_not_once_per_submission():
    h = _new()
    for i in range(3):
        h.clean_clip(f"s{i}", clipper=f"c{i}")
    h.compliance.calls.clear()
    h.job("certify")
    assert h.compliance.calls.count("HR-13") == 1, h.compliance.calls


def test_n16_1_cached_row_past_its_expiry_is_expired_not_verified():
    class Expiring(FakeCompliance):
        def row(self, oid):
            r = super().row(oid)
            return RegisterRow(r.available, oid, r.register_version, r.effective_status, r.parameters, None,
                               iso(NOW - timedelta(days=200))[:10], iso(NOW - timedelta(days=1))[:10])

    h = Harness(fakes={"compliance": Expiring()})
    h.approve_rules()
    h.clean_clip("s1")
    h.job("certify")
    msgs = [r["message"] for r in h.cert("s1")["reasons"] if r["code"] == "RULE_NOT_IN_FORCE"]
    assert any("expired" in m for m in msgs), msgs


# ============================================================ N16-3 age subjects namespaced per caller

def test_n16_3_another_caller_cannot_decide_a_clipper_network_subject():
    h = _new()
    h.age.answer = AgeProviderAnswer("inconclusive")
    assert h.ok(h.age_check("cn-clp-x", caller="clipper_network"))["result"] == "inconclusive"
    h.age.answer = AgeProviderAnswer("adult", None, None, True, "fake-age", "prov-ref-1", None)
    assert h.ok(h.age_check("cn-clp-x", caller="onboarding"))["result"] == "adult"
    cn = h.ok(h.get("/vi/v1/age/subjects/cn-clp-x", caller="clipper_network"))
    ob = h.ok(h.get("/vi/v1/age/subjects/cn-clp-x", caller="onboarding"))
    assert cn["allowed"] is False and ob["allowed"] is True


def test_n16_3_a_minor_filed_by_another_caller_does_not_lock_or_purge_the_clipper():
    h = _new()
    h.onboard("cn-clp-v")
    assert h.ok(h.get("/vi/v1/age/subjects/cn-clp-v", caller="clipper_network"))["allowed"] is True
    assert h.ok(h.age_check("cn-clp-v", dob="2012-01-01", caller="onboarding"))["result"] == "minor"
    assert h.ok(h.get("/vi/v1/age/subjects/cn-clp-v", caller="clipper_network"))["allowed"] is True
    conns = h.ok(h.get("/vi/v1/connections", caller="clipper_network", clipper_id="cn-clp-v"))
    items = conns["items"] if isinstance(conns, dict) else conns
    assert [c["status"] for c in items] == ["active"]
    # and the onboarding namespace keeps its own minor lock
    assert h.ok(h.age_check("cn-clp-v", caller="onboarding"))["result"] == "minor"


def test_n16_3_minor_lock_follows_the_identity_not_the_bare_id():
    h = _new()
    h.identity("cn-clp-a", "same.person@example.com")
    assert h.ok(h.age_check("cn-clp-a", dob="2011-03-03"))["result"] == "minor"
    h.identity("cn-clp-b", "same.person@example.com")            # the same email under a new clipper id
    r = h.ok(h.age_check("cn-clp-b"))                               # adult DOB this time
    assert r["result"] == "minor", r
    assert h.ok(h.get("/vi/v1/age/subjects/cn-clp-b", caller="clipper_network"))["allowed"] is False


# ============================================================ N16-6 (V&I side): request_id echo and routes CN reads

def test_n16_6_write_routes_echo_the_request_id_and_rules_pinned():
    h = _new()
    b = {"request_id": rid("st"), "clipper_id": "e1", "platform": "tiktok", "redirect_uri": "https://zbc.example/cb"}
    st = h.ok(h.post("/vi/v1/connections/start", b, caller="clipper_network"))
    assert st["request_id"] == b["request_id"] and st["rules_pinned"] is True
    state = st["authorization_url"].split("state=")[1].split("&")[0]
    h.adapters["tiktok"].next_account("acct-e1")
    cb = {"request_id": rid("cc"), "state": state, "code": "good-code"}
    cc = h.ok(h.post("/vi/v1/connections/complete", cb, caller="clipper_network"))
    assert cc["request_id"] == cb["request_id"] and cc["rules_pinned"] is True
    rb = {"request_id": rid("rv")}
    rv = h.ok(h.post(f"/vi/v1/connections/{cc['connection']['connection_id']}/revoke", rb, caller="clipper_network"))
    assert rv["request_id"] == rb["request_id"]
    ib = {"request_id": rid("id"), "clipper_id": "e1", "email": "e1@example.com"}
    ic = h.ok(h.post("/vi/v1/identity/checks", ib, caller="clipper_network"))
    assert ic["request_id"] == ib["request_id"] and ic["rules_pinned"] is True
    ab = {"request_id": rid("ag"), "subject_id": "e1", "dob": "1990-01-01", "dob_field_neutral": True,
          "method": "photo_id_match", "provider_session_ref": "s"}
    ag = h.ok(h.post("/vi/v1/age/checks", ab, caller="clipper_network"))
    assert ag["request_id"] == ab["request_id"] and ag["status"] == "adult" and ag["rules_pinned"] is True
    sub = h.ok(h.get("/vi/v1/age/subjects/e1", caller="clipper_network"))
    assert sub["status"] == "adult" and sub["attestation_id"] == ag["attestation_id"]


def test_n16_6_finding_and_certification_reads_exist_for_clipper_network():
    h = _new()
    h.clean_clip("s1", clipper="c1")
    h.identity("c2", "c1@example.com")                               # duplicate of c1's email -> a finding on c2
    fid = [f for f in h.ok(h.get("/vi/v1/findings")) if f["clipper_id"] == "c2"][0]["finding_id"]
    f = h.ok(h.get(f"/vi/v1/findings/{fid}", caller="clipper_network"))
    assert f["finding_id"] == fid and f["clipper_id"] == "c2" and f["rules_pinned"] is True
    assert f["evidence_ids"], "an identity finding must carry the evidence id of the check that found it"
    cs = h.ok(h.get("/vi/v1/certifications", caller="clipper_network", clipper_id="c1"))
    assert cs["clipper_id"] == "c1" and [c["submission_id"] for c in cs["certifications"]] == ["s1"]
    assert "revision_watch_end" in cs["certifications"][0]


def test_n16_6_ban_answer_echoes_request_and_clipper():
    h = _new()
    h.onboard("bn")
    b = {"request_id": rid("ban"), "clipper_id": "bn", "cn_decision_id": "cn-band-1", "approved_at": iso(NOW)}
    r = h.ok(h.post("/vi/v1/bans", b, caller="clipper_network", andre=ANDRE_TOKEN))
    assert r["request_id"] == b["request_id"] and r["clipper_id"] == "bn" and r["rules_pinned"] is True


# ============================================================ N16-7 a forged version event does not brick start-up

def _durable(tmp_path):
    x = Harness(data_dir=str(tmp_path / "d"))
    x.approve_rules()
    x.clean_clip("r1")
    run_to_day(x, 1)
    return x


def _restart(x, tmp_path, **env):
    return Harness(data_dir=str(tmp_path / "d"), ledger=x.ledger, clock=x.clock, fakes=x.fakes, env=env or None)


def test_n16_7_forged_rules_version_event_needs_andre_void_not_a_brick(tmp_path):
    x = _durable(tmp_path)
    ep = x.svc.log.epoch
    fake = f"vi-ver-{ep}-99-{'a' * 32}"
    x.ledger.record_event(fake, "verification_integrity", "rules_version_published", "andre", "rules:v99",
                          {"version": 99}, "forged")
    with pytest.raises(RuntimeError, match="only Andre"):
        _restart(x, tmp_path)                                       # normal start refuses ...
    y = _restart(x, tmp_path, VI_RECONCILE_MODE="1")                 # ... but reconcile mode starts
    plan = y.client.get("/vi/v1/reconcile", headers=y.headers(andre=ANDRE_TOKEN)).json()
    assert fake in plan["voidable"]["event_ids"] and not plan["fatal"]
    r = y.post("/vi/v1/reconcile", {"request_id": rid("rec"), "head_sha256": plan["head_sha256"],
                                    "void_lines": plan["voidable"]["lines"],
                                    "void_event_ids": plan["voidable"]["event_ids"]}, andre=ANDRE_TOKEN)
    assert r.status_code == 200, r.text
    rec = x.ledger.of_type("reconcile")[-1]
    assert rec["actor"] == "andre"
    z = _restart(x, tmp_path)
    assert z.ok(z.get("/health"))["rules_version"] == 1


def test_n16_7_genuine_version_events_are_honoured_on_a_clean_restart(tmp_path):
    x = _durable(tmp_path)
    y = _restart(x, tmp_path)
    assert y.ok(y.get("/health"))["rules_version"] == 1 and y.ok(y.get("/vi/v1/integrity"))["status"] == "green"


# ============================================================ N16-8 latent certification / age gaps

class _NoHashAdapter:
    def __init__(self, inner, endpoint):
        self.inner, self.endpoint = inner, endpoint
        self.videos = inner.videos
        self.next_account = inner.next_account

    def account(self, vault, ref):
        return self.inner.account(vault, ref)

    def fetch(self, *a):
        ans = self.inner.fetch(*a)
        if ans.available:
            return AdapterAnswer(True, ans.values, ans.country_values, ans.video, ans.live_state, self.endpoint,
                                 None, 1, 200)
        return ans


@pytest.mark.parametrize("endpoint", ["not-a-real-endpoint", "tiktok_display_v2_video_list"])
def test_n16_8_a_count_without_a_response_hash_never_certifies(endpoint):
    h = _new()
    wrapped = _NoHashAdapter(h.adapters["tiktok"], endpoint)
    h.adapters["tiktok"] = wrapped
    h.svc.ports.adapters["tiktok"] = wrapped
    h.clean_clip("s2", views=5000)
    run_to_day(h, 14)
    c = h.cert("s2")
    assert c["status"] != "certified" and c["certified_views"] is None, c


def test_n16_8_provider_without_dob_consistency_is_not_adult():
    h = _new()
    h.age.answer = AgeProviderAnswer("adult", None, None, None, "fake-age", "prov-ref-2", None)
    assert h.ok(h.age_check("s-dc"))["result"] != "adult"


def test_n16_8_stale_provider_result_is_not_adult():
    h = _new()
    h.age.answer = AgeProviderAnswer("adult", None, None, True, "fake-age", "prov-ref-3", "2023-01-01T00:00:00Z")
    assert h.ok(h.age_check("s-old"))["result"] != "adult"
    h.age.answer = AgeProviderAnswer("adult", None, None, True, "fake-age", "prov-ref-4", iso(NOW - timedelta(days=3)))
    assert h.ok(h.age_check("s-fresh"))["result"] == "adult"


def test_n16_8_adult_attestation_expires_and_needs_a_recheck():
    h = _new()
    aid = h.ok(h.age_check("s-exp"))["attestation_id"]
    h.clock.advance(days=364)
    assert h.ok(h.get(f"/vi/v1/age/attestations/{aid}", caller="compliance_38"))["status"] == "adult"
    h.clock.advance(days=2)
    assert h.ok(h.get(f"/vi/v1/age/attestations/{aid}", caller="compliance_38"))["status"] == "unknown"
    s = h.ok(h.get("/vi/v1/age/subjects/s-exp", caller="clipper_network"))
    assert s["allowed"] is False and any("expired" in u for u in s["unmet"])
    assert h.ok(h.age_check("s-exp"))["result"] == "adult"          # a re-check restores it
    assert h.ok(h.get("/vi/v1/age/subjects/s-exp", caller="clipper_network"))["allowed"] is True


# ============================================================ N16-12 e-mail normalisation

@pytest.mark.parametrize("variant", ["alice.smith@example.com.", "ALICE.SMITH@EXAMPLE.COM.", "alice.smith@EXAMPLE.com"])
def test_n16_12_trailing_dot_and_case_fold_to_the_same_identity(variant):
    h = _new()
    h.identity("id-1", "Alice.Smith@Example.com")
    assert h.identity("id-2", variant)["status"] == "finding"


def test_n16_12_plus_tags_and_dots_stay_distinct_by_spec():
    h = _new()
    h.identity("id-1", "alice.smith@example.com")
    assert h.identity("id-2", "alice.smith+zbc@example.com")["status"] == "clear"
    assert h.identity("id-3", "alicesmith@example.com")["status"] == "clear"


def test_n16_6_every_other_write_route_echoes_the_request_id():
    h = _new()
    h.onboard("e2")
    h.post_video("tiktok", "https://www.tiktok.com/@c/video/e2s")
    sub = {"request_id": rid("sub"), "submission_id": "e2s", "campaign_id": "camp-1", "rulebook_version": 1,
           "clipper_id": "e2", "platform": "tiktok", "post_ref": "https://www.tiktok.com/@c/video/e2s",
           "posted_at": iso(h.clock.now()), "min_days_live": 7, "collab_permitted": False}   # no media: a hold opens
    assert h.ok(h.post("/vi/v1/submissions", sub, caller="creative_production"), 201)["request_id"] == sub["request_id"]
    ap = {"request_id": rid("ap")}
    assert h.ok(h.post("/vi/v1/submissions/e2s/approval", ap, caller="creative_production"))["request_id"] == ap["request_id"]
    jb = {"request_id": rid("job")}
    assert h.ok(h.post("/vi/v1/jobs/liveness/run", jb, caller="scheduler"))["request_id"] == jb["request_id"]
    hold = [x for x in h.ok(h.get("/vi/v1/holds")) if x["status"] == "open"][0]
    hd = {"request_id": rid("hd"), "decision": "release", "reason": "reviewed"}
    assert h.ok(h.post(f"/vi/v1/holds/{hold['hold_id']}/decision", hd, andre=ANDRE_TOKEN))["request_id"] == hd["request_id"]
    row = dict(h.svc.current.by_id()["VI-15c"], statement="Tightened TikTok retention statement.")
    pp = {"request_id": rid("rp"), "kind": "amend", "target_id": "VI-15c", "proposed_row": row}
    p = h.ok(h.post("/vi/v1/rules/proposals", pp, andre=ANDRE_TOKEN), 201)
    assert p["request_id"] == pp["request_id"]
    dd = {"request_id": rid("dd"), "decisions": [{"proposal_id": p["proposal"]["proposal_id"], "decision": "reject",
                                                  "content_sha256": p["proposal"]["content_sha256"]}]}
    assert h.ok(h.post("/vi/v1/rules/decisions", dd, andre=ANDRE_TOKEN))["request_id"] == dd["request_id"]
