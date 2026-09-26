"""Retention purge job details, strikes and escalation, overturns, identity/age interplay, oEmbed fallback,
anomaly signals, stolen-content matches and Instagram collabs."""

from __future__ import annotations

import json
from datetime import timedelta

from fakes import FakeClipperNetwork
from helpers import ANDRE_TOKEN, NOW, Harness, rid
from test_cert_scenarios import codes, run_to_day


# --- retention ---------------------------------------------------------------------------------------------------

def test_retention_after_revision_watch_end_only_hashes_remain(hr):
    hr.clean_clip("rt1")
    run_to_day(hr, 14)
    cert = hr.cert("rt1")
    assert cert["status"] == "certified" and hr.svc.side.get("rt1:post_ref")
    run_to_day(hr, 31, 15)
    assert hr.svc.side.get("rt1:post_ref") is None and hr.svc.side.get("rt1:video_id") is None
    sub = hr.svc.submissions["rt1"]
    assert len(sub["post_ref_sha256"]) == 64 and len(sub["video_id_sha256"]) == 64
    assert hr.cert("rt1")["certified_views"] == cert["certified_views"]
    ev = hr.ledger.of_type("retention_purged")
    assert ev and all(set(e["payload"]) == {"cause", "counts", "keys_sha256"} for e in ev)
    assert "tiktok.com" not in json.dumps([e["payload"] for e in ev])


def test_retention_refreshes_a_live_connection_or_purges_it(hr):
    hr.onboard("clip-a")
    cid = [c for c in hr.svc.connections.values() if c["status"] == "active"][0]["connection_id"]
    hr.clock.advance(days=31)
    hr.job("retention")
    assert hr.svc.side.get(f"{cid}:account_id")                     # refreshed by a new account fetch
    hr.adapters["tiktok"].available = True
    hr.vault.refs.clear()                                           # the vault lost the grant: refresh fails
    hr.clock.advance(days=31)
    hr.job("retention")
    assert hr.svc.side.get(f"{cid}:account_id") is None


def test_cover_image_and_caption_text_never_stored(hr):
    hr.clean_clip("rt2")
    run_to_day(hr, 3)
    text = hr.all_text()
    assert "cover-https" not in text and "a caption #ad" not in text
    assert not [e for _, e in hr.svc.side.all() if e["field"] not in ("post_ref", "video_id", "share_url", "account_id")]


# --- strikes -------------------------------------------------------------------------------------------------------

def test_three_s1_within_90_days_escalate_to_s2_and_two_s2_to_s3(hr):
    hr.onboard("clip-e")
    for i in range(3):
        ref = f"https://www.tiktok.com/@c/video/e{i}"
        hr.post_video("tiktok", ref)
        hr.ok(hr.register(f"e{i}", "clip-e", post_ref=ref), 201)
        hr.approve(f"e{i}")
        hr.clock.advance(minutes=1)
    run_to_day(hr, 2)
    for i in range(3):
        hr.adapters["tiktok"].videos[f"https://www.tiktok.com/@c/video/e{i}"]["gone"] = True
    run_to_day(hr, 3, 3)
    integ = hr.ok(hr.get("/vi/v1/clippers/clip-e/integrity", caller="clipper_network"))
    classes = sorted(s["class"] for s in integ["strikes"])
    assert classes == ["S1", "S1", "S1", "S2"]
    esc = [s for s in integ["strikes"] if s["class"] == "S2"][0]
    assert len(esc["escalates"]) == 3 and integ["ban_recommended"] is False
    # a second S2 (hash mismatch) → S3 with a ban recommendation, still no ban
    ref = "https://www.tiktok.com/@c/video/e9"
    hr.post_video("tiktok", ref)
    hr.ok(hr.register("e9", "clip-e", post_ref=ref), 201)
    hr.approve("e9")
    hr.adapters["tiktok"].videos[ref]["video_id"] = "swapped"
    run_to_day(hr, 17, 4)
    integ = hr.ok(hr.get("/vi/v1/clippers/clip-e/integrity", caller="clipper_network"))
    assert integ["strikes_active"]["S3"] >= 1 and integ["ban_recommended"] is True and integ["banned"] is False


def test_overturning_a_finding_lapses_its_strike_and_hold(hr):
    hr.connect("clip-1", "tiktok", account_id="shared")
    hr.connect("clip-2", "tiktok", account_id="shared")
    f = [x for x in hr.ok(hr.get("/vi/v1/findings")) if x["kind"] == "account_shared"][0]
    hr.ok(hr.post(f"/vi/v1/findings/{f['finding_id']}/decision", {"request_id": rid(), "decision": "uphold",
                                                                  "reason": "same person"}, andre=ANDRE_TOKEN))
    integ = hr.ok(hr.get("/vi/v1/clippers/clip-2/integrity", caller="clipper_network"))
    assert integ["strikes_active"]["S3"] == 1
    assert hr.post(f"/vi/v1/findings/{f['finding_id']}/decision", {"request_id": rid(), "decision": "release",
                                                                   "reason": "x"}, andre=ANDRE_TOKEN).status_code == 422
    hr.ok(hr.post(f"/vi/v1/findings/{f['finding_id']}/decision", {"request_id": rid(), "decision": "overturn",
                                                                  "reason": "appeal won"}, andre=ANDRE_TOKEN))
    integ = hr.ok(hr.get("/vi/v1/clippers/clip-2/integrity", caller="clipper_network"))
    assert integ["strikes_active"]["S3"] == 0 and integ["open_holds"] == []
    assert hr.ledger.of_type("strike_lapsed")


def test_duplicate_identity_finding_invalidates_an_adult_attestation(hr):
    hr.identity("older", "same@example.com")
    a = hr.ok(hr.age_check("newer"))
    g = hr.ok(hr.get(f"/vi/v1/age/attestations/{a['attestation_id']}", caller="compliance_38"))
    assert g["status"] == "adult"
    hr.clock.advance(minutes=1)
    hr.identity("newer", "same@example.com")
    g = hr.ok(hr.get(f"/vi/v1/age/attestations/{a['attestation_id']}", caller="compliance_38"))
    assert g["status"] == "unknown" and "DUPLICATE_IDENTITY" in codes(g)
    s = hr.ok(hr.get("/vi/v1/age/subjects/newer", caller="onboarding"))
    assert s["allowed"] is False and s["unmet"]


# --- liveness fallback -----------------------------------------------------------------------------------------------

def test_tiktok_oembed_fallback_counts_at_most_two_consecutive_days():
    h = Harness(env={"VI_OEMBED_ENABLED": "1"})
    h.approve_rules()
    h.onboard("clip-a")
    ref = "https://www.tiktok.com/@c/video/oe"
    h.post_video("tiktok", ref, share_url="https://www.tiktok.com/@c/video/oe")
    h.ok(h.register("oe", "clip-a", post_ref=ref), 201)
    h.approve("oe")
    run_to_day(h, 1)
    h.adapters["tiktok"].available = False
    run_to_day(h, 3, 2)
    assert "LIVENESS_GAP" not in codes(h.cert("oe"))
    states = [h.svc.liveness[("oe", (NOW + timedelta(days=d)).date().isoformat())]["state"] for d in (2, 3)]
    assert states == ["live_public_fallback", "live_public_fallback"]
    run_to_day(h, 4, 4)
    assert "LIVENESS_GAP" in codes(h.cert("oe"))
    assert h.oembed.calls == 3
    del ref


# --- anomaly signals ---------------------------------------------------------------------------------------------------

def test_velocity_spike_against_the_clippers_own_baseline():
    h = Harness(env={"VI_ANOM_MIN_HISTORY": "2"})
    h.approve_rules()
    h.onboard("clip-v")
    for i in range(2):
        ref = f"https://www.tiktok.com/@c/video/b{i}"
        v = h.post_video("tiktok", ref, views=100, likes=10)
        h.ok(h.register(f"b{i}", "clip-v", post_ref=ref), 201)
        h.approve(f"b{i}")

    def grow(d):
        for i in range(2):
            h.adapters["tiktok"].videos[f"https://www.tiktok.com/@c/video/b{i}"]["values"]["views"] = 100 + 50 * d
    run_to_day(h, 14, on_day=grow)
    assert all(h.cert(f"b{i}")["status"] == "certified" for i in range(2))
    ref = "https://www.tiktok.com/@c/video/spike"
    h.post_video("tiktok", ref, views=100, likes=10)
    h.ok(h.register("spike", "clip-v", post_ref=ref), 201)
    h.approve("spike")
    h.adapters["tiktok"].videos[ref]["values"].update(views=100, likes=10)
    run_to_day(h, 15, 15)
    h.adapters["tiktok"].videos[ref]["values"].update(views=9000, likes=900)     # +8,900 in 24 h vs a 50/day baseline
    run_to_day(h, 16, 16)
    scr = h.svc.screens["spike"]
    assert scr["signals"]["velocity"]["status"] == "fired" and scr["decision"] == "hold"
    assert scr["decision_rate"]["evaluated"] <= scr["decision_rate"]["applicable"]
    assert "ANOMALY_HOLD" in codes(h.cert("spike"))


def test_youtube_derived_signals_on_geography_and_cap_proximity():
    h = Harness(env={"VI_YT_DERIVED_SIGNALS": "1", "VI_REQUIRE_PERCEPTUAL_MATCH": "0"},
                fakes={"cn": FakeClipperNetwork(cap=20000)})
    h.approve_rules()
    h.onboard("clip-y", "youtube")
    ref = "https://www.youtube.com/shorts/abcdefghijk"
    h.post_video("youtube", ref, views=20100, likes=2000, country={"US": 1000, "BR": 19100})
    h.adapters["youtube"].videos[ref]["values"].update(avg_view_percentage=50)
    h.ok(h.register("yt", "clip-y", "youtube", post_ref=ref, target_regions=["US", "GB"]), 201)
    h.approve("yt")
    run_to_day(h, 14)
    scr = h.svc.screens["yt"]
    assert scr["signals"]["geography"]["status"] == "fired"
    assert scr["signals"]["cap_proximity"]["status"] == "fired"
    assert "YT_DERIVED_USE_UNRESOLVED" not in codes(h.cert("yt")) and "ANOMALY_HOLD" in codes(h.cert("yt"))


def test_clipper_network_unavailable_is_insufficient_signal():
    from ports import NotBuiltClipperNetwork
    h = Harness(fakes={"cn": NotBuiltClipperNetwork()})
    h.approve_rules()
    h.clean_clip("cn")
    run_to_day(h, 14)
    assert "INSUFFICIENT_SIGNAL" in codes(h.cert("cn"))


# --- stolen content ------------------------------------------------------------------------------------------------------

def test_stolen_match_across_clippers_holds_the_later_and_names_the_presumed_original(hr):
    hr.onboard("orig")
    hr.post_video("tiktok", "https://www.tiktok.com/@o/video/1")
    hr.ok(hr.register("o1", "orig", post_ref="https://www.tiktok.com/@o/video/1", media_ref="same-file"), 201)
    hr.media.shas["same-file-o2"] = hr.media.sha256("same-file-o1")
    hr.onboard("copier")
    r = hr.ok(hr.register("o2", "copier", post_ref="https://www.tiktok.com/@x/video/2", media_ref="same-file"), 201)
    assert r["stolen_check"] == "match" and r["holds"]
    f = [x for x in hr.ok(hr.get("/vi/v1/findings")) if x["kind"] == "stolen_content"][0]
    assert f["presumed_original"] == "o1" and f["subject_id"] == "o2" and f["status"] == "open"
    hold = [x for x in hr.ok(hr.get("/vi/v1/holds")) if x["subject_id"] == "o2"][0]
    hr.ok(hr.post(f"/vi/v1/holds/{hold['hold_id']}/decision", {"request_id": rid(), "decision": "uphold",
                                                               "reason": "copied"}, andre=ANDRE_TOKEN))
    assert [s["class"] for s in hr.ok(hr.get("/vi/v1/strikes", caller="clipper_network"))["items"]] == ["S2"]
    hr.advance(days=1)
    assert "STOLEN_MATCH" in codes(hr.cert("o2"))


def test_seed_clip_match_holds(hr):
    hr.onboard("clip-s")
    hr.media.shas["seed-a"] = hr.media.sha256("file-s1")
    r = hr.ok(hr.register("s1", "clip-s", media_ref="file", seed_media_refs=["seed-a"]), 201)
    assert r["stolen_check"] == "match"


# --- instagram collab ----------------------------------------------------------------------------------------------------

def test_instagram_collab_unknown_or_true_not_certifiable_unless_permitted():
    for collab, permitted, expect in ((None, False, True), (True, False, True), (True, True, False)):
        h = Harness(env={"VI_REQUIRE_PERCEPTUAL_MATCH": "0"})
        h.approve_rules()
        h.onboard("clip-i", "instagram")
        ref = "17890000000009"
        h.post_video("instagram", ref, is_collab=collab)
        h.ok(h.register("ig", "clip-i", "instagram", post_ref=ref, collab_permitted=permitted), 201)
        h.approve("ig")
        run_to_day(h, 14)
        assert ("COLLAB_POST" in codes(h.cert("ig"))) is expect, (collab, permitted)


def test_x_enabled_without_a_budget_is_quota_exhausted():
    h = Harness(env={"VI_PLATFORM_X_ENABLED": "1"})
    h.approve_rules()
    h.onboard("clip-x", "x")
    ref = "https://x.com/c/status/1234567"
    h.post_video("x", ref)
    h.ok(h.register("x1", "clip-x", "x", post_ref=ref), 201)
    fp = h.approve("x1")["fingerprint"]
    assert fp["available"] is False and fp["reasons"][0]["code"] == "QUOTA_EXHAUSTED"


def test_upheld_bought_engagement_on_a_certified_clip_voids_it_with_a_whole_count_clawback(hr):
    """No route opens a bought-engagement finding on an already-certified clip in this build (the anomaly
    screen runs before settlement); the finding is placed through the service's own record-first plumbing here,
    then decided through the public route."""
    import service as S
    hr.clean_clip("vd", views=6400, likes=640)
    run_to_day(hr, 14)
    assert hr.cert("vd")["status"] == "certified"
    now = hr.clock.now()
    op = S.Op(hr.svc, "test-open-finding", "intel_06_engagement_anomaly", "vd")
    fid = S.rid("fnd", "test", "vd")
    op.record(S.derived_id("fnd", fid, "open"), "finding_opened", "intel_06_engagement_anomaly", "vd",
              {"finding_id": fid, "kind": "bought_engagement", "rule_id": "VI-10"}, "Finding opened (test)")
    op.add("finding", {"finding_id": fid, "kind": "bought_engagement", "code": "BOUGHT_ENGAGEMENT", "subject_kind": "clip",
                       "subject_id": "vd", "clipper_id": "clip-a", "status": "open", "evidence_ids": [], "rule_id": "VI-10",
                       "opened_at": S.iso(now), "decided_by": None, "decided_at": None, "decision_note_sha256": None})
    hr.svc._commit(op)
    hr.ok(hr.post(f"/vi/v1/findings/{fid}/decision", {"request_id": rid(), "decision": "uphold",
                                                      "reason": "confirmed purchase"}, andre=ANDRE_TOKEN))
    c = hr.cert("vd")
    assert c["status"] == "voided" and c["certified_views"] == 0 and codes(c) == ["VOIDED"]
    cb = hr.ok(hr.get("/vi/v1/clawbacks", caller="finance_31"))["items"]
    assert [(x["views_delta"], x["cause"], x["rule_id"]) for x in cb] == [(-6400, "void_upheld_fraud", "VI-10")]
    feed = hr.ok(hr.get("/vi/v1/feed/verified-results", caller="creative_production"))["items"]
    assert [i["status"] for i in feed if i["submission_id"] == "vd"] == ["certified", "voided"]


def test_tampered_platform_data_store_is_never_used_for_a_fetch(hr):
    post_ref = hr.clean_clip("tp")
    other = "https://www.tiktok.com/@c/video/more-views"
    hr.post_video("tiktok", other, views=10_000_000, likes=1_000_000)
    hr.svc.side.entries["tp:post_ref"]["value"] = other            # someone edits the unanchored side store
    run_to_day(hr, 2)
    fetches = [f for f in hr.svc.fetches.values() if f["submission_id"] == "tp" and f["purpose"] != "approval"]
    assert fetches and all(f["cause"] == "platform_data_mismatch" for f in fetches)
    assert all(c[0] != other for c in hr.adapters["tiktok"].calls)
    del post_ref
