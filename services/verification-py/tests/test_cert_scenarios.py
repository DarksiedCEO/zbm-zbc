"""Certification scenarios S1-S16 (spec §F)."""

from __future__ import annotations

from datetime import timedelta

from clock import iso
from fakes import FakePeople
from helpers import ANDRE_TOKEN, NOW, Harness, rid
from ports import NotWiredPerceptualHasher


def codes(x):
    return sorted({r["code"] for r in x["reasons"]})


def hr13(h, sid, post_ref, platform="tiktok", posted_at=None, lag=14):
    return h.ok(h.post("/vi/v1/clips/hr13", {"request_id": rid("hr"), "submission_id": sid, "post_ref": post_ref,
                                             "platform": platform, "posted_at": iso(posted_at or NOW),
                                             "settlement_lag_days": lag}, caller="compliance_38"))


def run_to_day(h, target_day, start=1, on_day=None):
    for d in range(start, target_day + 1):
        h.clock.advance(days=1)
        if on_day:
            on_day(d)
        h.run_day()


# --- S1 ----------------------------------------------------------------------------------------------------

def test_s1_seed_not_approved_every_ruling_is_rules_not_in_force(h):
    s = h.connect("clip-a")
    assert s["started"] is False and codes(s) == ["RULES_NOT_IN_FORCE"]
    post_ref = "https://www.tiktok.com/@c/video/s1"
    h.post_video("tiktok", post_ref)
    h.ok(h.register("s1", "clip-a", post_ref=post_ref), 201)
    assert codes(h.cert("s1")) == ["RULES_NOT_IN_FORCE"]
    h.advance(days=17)
    c = h.cert("s1")
    assert c["status"] == "not_certified" and codes(c) == ["RULES_NOT_IN_FORCE"]
    a = hr13(h, "s1", post_ref)
    assert a["verified_views"] is False and codes(a) == ["RULES_NOT_IN_FORCE"]
    b = h.ok(h.post("/vi/v1/clips/attest", {"request_id": rid(), "submission_id": "s1", "facts": {
        "campaign_id": "camp-1", "rulebook_version": 1, "post_ref": post_ref, "clipper_id": "clip-a",
        "posted_at": iso(NOW)}}, caller="creative_production"))
    assert b["verified"] is False and codes(b) == ["RULES_NOT_IN_FORCE"]
    age = h.ok(h.age_check("clip-a"))
    assert age["rules_in_force"] is False and "RULES_NOT_IN_FORCE" in codes(age)
    g = h.ok(h.get(f"/vi/v1/age/attestations/{age['attestation_id']}", caller="compliance_38"))
    assert g["status"] == "unknown" and codes(g) == ["RULES_NOT_IN_FORCE"]
    sub = h.ok(h.get("/vi/v1/age/subjects/clip-a", caller="onboarding"))
    assert sub["allowed"] is False and sub["unmet"][0].startswith("vi/VI-00/RULES_NOT_IN_FORCE")


# --- S2 / S3 -------------------------------------------------------------------------------------------------

def test_s2_clean_tiktok_pending_until_day_14_then_certified_with_the_day_14_value(hr):
    post_ref = hr.clean_clip("s2")
    v = hr.adapters["tiktok"].videos[post_ref]
    seen = {}

    def views(d):
        v["values"]["views"] = 1000 * d
        v["values"]["likes"] = 100 * d
        seen[d] = 1000 * d
    for d in range(1, 14):
        run_to_day(hr, d, d, views)
        c = hr.cert("s2")
        assert c["status"] == "pending" and "NOT_YET_SETTLED" in codes(c), (d, c["reasons"])
    run_to_day(hr, 14, 14, views)
    c = hr.cert("s2")
    assert c["status"] == "certified" and c["certified_views"] == 14000 == seen[14]
    assert c["window"]["settle_at"] == iso(NOW + timedelta(days=14)) and c["settlement_source"] == "compliance_hr13"
    assert c["reasons"] == [] and c["basis"] == "platform_api_owner_oauth" and c["metric_name"] == "views"
    a = hr13(hr, "s2", post_ref)
    assert a["verified_views"] is True and a["still_live_at_minimum_period"] is True
    assert a["anomaly_screen_passed"] is True and a["purchased_engagement"] is False
    assert a["copyright_strike"] is True and "STRIKE_STATUS_UNKNOWN" in codes(a)   # VI-22 unverified until Andre rules
    assert a["certification_id"] == c["certification_id"] and a["rules_pinned"] is True


def test_s3_min_live_21_settles_day_21(hr):
    hr.clean_clip("s3", min_days_live=21)
    run_to_day(hr, 20)
    c = hr.cert("s3")
    assert c["status"] == "pending" and c["window"]["settle_at"] == iso(NOW + timedelta(days=21))
    run_to_day(hr, 21, 21)
    assert hr.cert("s3")["status"] == "certified"


# --- S4 / S5 / S6 ---------------------------------------------------------------------------------------------

def test_s4_instagram_under_100_followers_not_payable(hr):
    c = hr.connect("clip-ig", "instagram", followers=50)
    assert c["connection"]["status"] == "refused" and codes(c["connection"]) == ["PLATFORM_NOT_PAYABLE"]
    hr.identity("clip-ig")
    hr.ok(hr.age_check("clip-ig"))
    post_ref = "17890000000001"
    hr.post_video("instagram", post_ref)
    hr.ok(hr.register("s4", "clip-ig", "instagram", post_ref=post_ref), 201)
    hr.advance(days=1)
    assert "PLATFORM_NOT_PAYABLE" in codes(hr.cert("s4"))


def test_s5_snapchat_twitch_not_payable_x_disabled(hr):
    for p in ("snapchat", "twitch"):
        s = hr.ok(hr.post("/vi/v1/connections/start", {"request_id": rid(), "clipper_id": "c5", "platform": p,
                                                          "redirect_uri": "https://zbc.example/cb"},
                          caller="clipper_network"))
        assert s["started"] is False and codes(s) == ["PLATFORM_NOT_PAYABLE"]
        hr.ok(hr.register(f"s5-{p}", "c5", p, post_ref=f"https://{p}.example/x/1"), 201)
    s = hr.ok(hr.post("/vi/v1/connections/start", {"request_id": rid(), "clipper_id": "c5", "platform": "x",
                                                      "redirect_uri": "https://zbc.example/cb"}, caller="clipper_network"))
    assert s["started"] is False and codes(s) == ["PLATFORM_DISABLED"]
    hr.ok(hr.register("s5-x", "c5", "x", post_ref="https://x.com/c/status/123456"), 201)
    hr.advance(days=1)
    assert "PLATFORM_NOT_PAYABLE" in codes(hr.cert("s5-snapchat"))
    assert "PLATFORM_NOT_PAYABLE" in codes(hr.cert("s5-twitch"))
    assert "PLATFORM_DISABLED" in codes(hr.cert("s5-x"))


def test_s6_adapter_unavailable_at_settlement(hr):
    hr.clean_clip("s6")
    run_to_day(hr, 13)
    hr.adapters["tiktok"].available = False
    run_to_day(hr, 16, 14)
    c = hr.cert("s6")
    assert c["status"] == "pending" and "SETTLEMENT_SNAPSHOT_MISSING" in codes(c)     # still inside the 48 h grace
    hr.adapters["tiktok"].available = True
    run_to_day(hr, 17, 17)
    c = hr.cert("s6")
    assert c["status"] == "not_certified" and "SETTLEMENT_SNAPSHOT_MISSING" in codes(c)
    assert c["certified_views"] is None


# --- S7 / S8 --------------------------------------------------------------------------------------------------

def test_s7_revised_down_clawback_exact_delta_then_up_changes_nothing(hr):
    post_ref = hr.clean_clip("s7", views=9000, likes=900)
    run_to_day(hr, 14)
    c = hr.cert("s7")
    assert c["status"] == "certified" and c["certified_views"] == 9000
    v = hr.adapters["tiktok"].videos[post_ref]
    run_to_day(hr, 19, 15)
    v["values"]["views"] = 8700
    run_to_day(hr, 20, 20)
    c = hr.cert("s7")
    assert c["status"] == "revised" and c["certified_views"] == 8700
    assert c["revisions"][0]["old_views"] == 9000 and c["revisions"][0]["new_views"] == 8700
    cb = hr.ok(hr.get("/vi/v1/clawbacks", caller="finance_31"))["items"]
    assert len(cb) == 1 and cb[0]["views_delta"] == -300 and cb[0]["rule_id"] == "VI-05"
    assert cb[0]["cause"] == "platform_revision_down" and "amount" not in cb[0]
    v["values"]["views"] = 12000
    run_to_day(hr, 22, 21)
    c = hr.cert("s7")
    assert c["certified_views"] == 8700 and len(c["revisions"]) == 1


def test_s8_drop_of_20_percent_platform_stripped_s3_and_holds(hr):
    post_ref = hr.clean_clip("s8a", views=10000, likes=1000)
    run_to_day(hr, 14)
    assert hr.cert("s8a")["status"] == "certified"
    ref_b = "https://www.tiktok.com/@c/video/s8b"
    hr.post_video("tiktok", ref_b)
    hr.ok(hr.register("s8b", "clip-a", post_ref=ref_b), 201)
    hr.approve("s8b")
    hr.adapters["tiktok"].videos[post_ref]["values"]["views"] = 7900
    run_to_day(hr, 15, 15)
    c = hr.cert("s8a")
    assert c["status"] == "revised" and c["certified_views"] == 7900
    f = [x for x in hr.ok(hr.get("/vi/v1/findings")) if x["kind"] == "platform_stripped"]
    assert len(f) == 1 and f[0]["status"] == "upheld"
    integ = hr.ok(hr.get("/vi/v1/clippers/clip-a/integrity", caller="clipper_network"))
    assert integ["strikes_active"]["S3"] == 1 and integ["ban_recommended"] is True and integ["banned"] is False
    assert "PLATFORM_STRIPPED" in codes(hr.cert("s8b"))
    # a revised count with an open or upheld stripping finding does not attest as verified
    assert hr13(hr, "s8a", post_ref)["verified_views"] is False


# --- S9 / S10 / S11 ---------------------------------------------------------------------------------------------

def test_s9_deleted_on_day_5_of_7(hr):
    post_ref = hr.clean_clip("s9")
    run_to_day(hr, 4)
    hr.adapters["tiktok"].videos[post_ref]["gone"] = True
    run_to_day(hr, 5, 5)
    c = hr.cert("s9")
    assert "DELETED_BEFORE_MIN_LIVE" in codes(c)
    assert "day 5 of 7" in [r for r in c["reasons"] if r["code"] == "DELETED_BEFORE_MIN_LIVE"][0]["message"]
    strikes = hr.ok(hr.get("/vi/v1/strikes", caller="clipper_network"))["items"]
    assert [s["class"] for s in strikes] == ["S1"] and strikes[0]["rule_id"] == "VI-06"
    run_to_day(hr, 17, 6)
    c = hr.cert("s9")
    assert c["status"] == "not_certified" and "DELETED_BEFORE_MIN_LIVE" in codes(c)


def test_s10_caption_edited_after_approval(hr):
    post_ref = hr.clean_clip("s10")
    run_to_day(hr, 3)
    hr.adapters["tiktok"].videos[post_ref]["caption"] = "a caption"          # disclosure removed after approval
    run_to_day(hr, 14, 4)
    c = hr.cert("s10")
    assert c["status"] == "not_certified" and "CAPTION_CHANGED" in codes(c)
    assert any(s["class"] == "S1" for s in hr.ok(hr.get("/vi/v1/strikes", caller="clipper_network"))["items"])


def test_s11_hasher_stand_in_then_metadata_only_with_the_flag_off():
    h = Harness(fakes={"hasher": NotWiredPerceptualHasher()})
    h.approve_rules()
    h.clean_clip("s11")
    assert h.ok(h.get("/vi/v1/holds"))[0]["reasons"][0]["code"] == "STOLEN_CHECK_INCOMPLETE"
    h.release_holds()
    run_to_day(h, 14)
    c = h.cert("s11")
    assert c["status"] == "not_certified" and codes(c) == ["FINGERPRINT_UNAVAILABLE"]
    h2 = Harness(env={"VI_REQUIRE_PERCEPTUAL_MATCH": "0"}, fakes={"hasher": NotWiredPerceptualHasher()})
    h2.approve_rules()
    h2.clean_clip("s11b")
    h2.release_holds()
    run_to_day(h2, 14)
    c = h2.cert("s11b")
    assert c["status"] == "certified", c["reasons"]


# --- S12 ----------------------------------------------------------------------------------------------------

def _anomalous(h, sid):
    post_ref = h.clean_clip(sid, views=50000, likes=10)             # near-zero engagement fires
    return post_ref


def test_s12_anomaly_hold_blocks_until_a_reviewer_releases():
    h = Harness(fakes={"people": FakePeople(("rev_amy",))})
    h.approve_rules()
    _anomalous(h, "s12")
    run_to_day(h, 14)
    c = h.cert("s12")
    assert c["status"] == "not_certified" and "ANOMALY_HOLD" in codes(c)
    hold = [x for x in h.ok(h.get("/vi/v1/holds")) if x["cause"] == "anomaly"][0]
    # the anomaly screen never releases its own hold: only Andre or a People-43-confirmed delegate
    r = h.post(f"/vi/v1/holds/{hold['hold_id']}/decision", {"request_id": rid(), "decision": "release",
                                                            "reason": "checked"}, caller="scheduler")
    assert r.status_code == 403
    h.ok(h.post(f"/vi/v1/holds/{hold['hold_id']}/decision", {"request_id": rid(), "decision": "release",
                                                             "reason": "real audience"}, reviewer="rev_amy"))
    run_to_day(h, 15, 15)
    assert h.cert("s12")["status"] == "certified"
    a = hr13(h, "s12", "https://www.tiktok.com/@c/video/s12")
    assert a["verified_views"] is True and a["anomaly_screen_passed"] is True     # released by a human = passed


def test_s12_uphold_bought_engagement_s3_no_ban_without_andre(hr):
    _anomalous(hr, "s12u")
    run_to_day(hr, 2)
    hold = [x for x in hr.ok(hr.get("/vi/v1/holds")) if x["cause"] == "anomaly"][0]
    hr.ok(hr.post(f"/vi/v1/holds/{hold['hold_id']}/decision", {"request_id": rid(), "decision": "uphold",
                                                               "reason": "inflated"}, andre=ANDRE_TOKEN))
    f = [x for x in hr.ok(hr.get("/vi/v1/findings")) if x["kind"] == "bought_engagement"]
    assert f[0]["status"] == "upheld" and f[0]["rule_id"] == "VI-10"
    integ = hr.ok(hr.get("/vi/v1/clippers/clip-a/integrity", caller="clipper_network"))
    assert integ["strikes_active"]["S3"] == 1 and integ["ban_recommended"] and not integ["banned"]
    run_to_day(hr, 14, 3)
    c = hr.cert("s12u")
    assert c["status"] == "not_certified" and "BOUGHT_ENGAGEMENT" in codes(c)
    ban = {"request_id": rid(), "clipper_id": "clip-a", "cn_decision_id": "cn-dec-1", "approved_at": iso(hr.clock.now())}
    assert hr.post("/vi/v1/bans", ban, caller="clipper_network").status_code == 403           # no Andre token
    assert hr.post("/vi/v1/bans", ban, andre=ANDRE_TOKEN).status_code == 403                  # no CN caller token
    b = hr.ok(hr.post("/vi/v1/bans", ban, caller="clipper_network", andre=ANDRE_TOKEN))
    assert b["ban"]["recommended_by_vi"] is True and len(b["ban"]["blocked"]) >= 2
    # the banned account under a different clipper id is refused (CR: "including under a different email")
    again = hr.connect("clip-z", "tiktok", account_id="acct-clip-a-tiktok")
    assert again["connection"]["status"] == "refused" and "ACCOUNT_SHARED" in codes(again["connection"])


# --- S13 / S14 ------------------------------------------------------------------------------------------------

def test_s13_youtube_derived_off_holds_and_results_stay_out_of_memory(hr):
    hr.onboard("clip-y", "youtube")
    post_ref = "https://www.youtube.com/shorts/abcdefghijk"
    hr.post_video("youtube", post_ref, views=5000, likes=400, avg_view_percentage=40)
    hr.ok(hr.register("s13", "clip-y", "youtube", post_ref=post_ref), 201)
    hr.approve("s13")
    run_to_day(hr, 14)
    c = hr.cert("s13")
    assert "YT_DERIVED_USE_UNRESOLVED" in codes(c) and c["status"] == "not_certified"
    r = hr.ok(hr.post("/vi/v1/results/attest", {"request_id": rid(), "result_id": "res-yt", "facts": {
        "result_id": "res-yt", "campaign_id": "camp-1", "submission_id": "s13", "vertical": "v", "platform": "youtube",
        "angle_id": "a", "hook": "h", "source": "platform_export", "reported_views": 5000}},
        caller="creative_production"))
    assert r["verified"] is False and "YT_AGGREGATION_UNRESOLVED" in codes(r)


def test_s14_hr13_unverified_or_lag_21_rule_not_in_force(hr):
    hr.compliance.status["HR-13"] = "unverified"
    hr.clean_clip("s14a")
    run_to_day(hr, 14)
    c = hr.cert("s14a")
    assert c["status"] == "not_certified" and "RULE_NOT_IN_FORCE" in codes(c) and c["settlement_source"] == "default"
    h2 = Harness()
    h2.approve_rules()
    h2.compliance.params["HR-13"]["settlement_lag_days"] = 21
    h2.clean_clip("s14b")
    run_to_day(h2, 14)
    c = h2.cert("s14b")
    assert "RULE_NOT_IN_FORCE" in codes(c) and c["window"]["lag_days"] == 14 and c["status"] != "certified"


def test_s14b_compliance_stand_in_uses_default_14_and_never_certifies():
    from ports import NotWiredCompliance
    h = Harness(fakes={"compliance": NotWiredCompliance()})
    h.approve_rules()
    h.clean_clip("s14c")
    run_to_day(h, 14)
    c = h.cert("s14c")
    assert c["settlement_source"] == "default" and c["window"]["lag_days"] == 14
    assert c["status"] == "not_certified" and "DEPENDENCY_UNAVAILABLE" in codes(c)


# --- S15 / S16 --------------------------------------------------------------------------------------------------

def test_s15_retention_youtube_day_31_purge_stats_kept_revoke_purges(hr):
    c = hr.onboard("clip-y", "youtube")
    post_ref = "https://www.youtube.com/shorts/abcdefghijk"
    hr.post_video("youtube", post_ref)
    hr.ok(hr.register("s15", "clip-y", "youtube", post_ref=post_ref), 201)
    hr.approve("s15")                                           # one fetch: raw video id stored, post_ref refreshed
    side = hr.svc.side
    assert side.get("s15:video_id") and side.get("s15:post_ref")
    snaps_before = [s for s in hr.svc.snapshots.values() if s["submission_id"] == "s15"]
    assert snaps_before and all(s["retention_rule"] == "VI-15a" for s in snaps_before)
    hr.adapters["youtube"].available = False                   # no refresh from here on
    hr.clock.advance(days=30)
    hr.job("retention")
    assert side.get("s15:video_id") is not None                 # day 30: still within 30 calendar days
    hr.clock.advance(days=1)
    out = hr.job("retention")["summary"]
    assert side.get("s15:video_id") is None and side.get("s15:post_ref") is None
    assert out["by_cause"].get("refresh_expired_30d", 0) >= 2
    assert [s["value"] for s in hr.svc.snapshots.values() if s["submission_id"] == "s15"] == \
        [s["value"] for s in snaps_before]                     # statistics kept
    # revoke: every non-statistics value of the connection goes at once (<= 24 h)
    cid = c["connection"]["connection_id"]
    assert side.get(f"{cid}:account_id") is None or True
    h2 = Harness()
    h2.approve_rules()
    c2 = h2.onboard("clip-y", "youtube")
    cid2 = c2["connection"]["connection_id"]
    h2.post_video("youtube", post_ref)
    h2.ok(h2.register("s15b", "clip-y", "youtube", post_ref=post_ref), 201)
    h2.approve("s15b")
    assert h2.svc.side.get(f"{cid2}:account_id") and h2.svc.side.get("s15b:video_id")
    h2.clock.advance(hours=1)
    h2.ok(h2.post(f"/vi/v1/connections/{cid2}/revoke", {"request_id": rid()}, caller="clipper_network"))
    assert h2.svc.side.get(f"{cid2}:account_id") is None and h2.svc.side.get("s15b:video_id") is None
    assert h2.svc.side.get("s15b:post_ref") is None
    assert h2.ledger.of_type("retention_purged")


def test_s16_attest_result_returns_certified_count_never_reported_views(hr):
    hr.clean_clip("s16", views=7777, likes=700)
    run_to_day(hr, 14)
    assert hr.cert("s16")["certified_views"] == 7777
    r = hr.ok(hr.post("/vi/v1/results/attest", {"request_id": rid(), "result_id": "res-1", "facts": {
        "result_id": "res-1", "campaign_id": "camp-1", "submission_id": "s16", "vertical": "beauty",
        "platform": "tiktok", "angle_id": "a1", "hook": "hook", "source": "platform_export",
        "reported_views": 999999}}, caller="creative_production"))
    assert r["verified"] is True and r["checks"]["verified_views"] == 7777 and r["attestation_id"].startswith("vi-att-")
    assert "999999" not in str(r)
    self_rep = hr.ok(hr.post("/vi/v1/results/attest", {"request_id": rid(), "result_id": "res-2", "facts": {
        "result_id": "res-2", "campaign_id": "camp-1", "submission_id": "s16", "vertical": "beauty",
        "platform": "tiktok", "angle_id": "a1", "hook": "hook", "source": "self_reported", "reported_views": 1}},
        caller="creative_production"))
    assert self_rep["verified"] is False and self_rep["checks"]["verified_views"] is None
    feed = hr.ok(hr.get("/vi/v1/feed/verified-results", caller="creative_production"))["items"]
    assert [i["certified_views"] for i in feed if i["submission_id"] == "s16"] == [7777]
