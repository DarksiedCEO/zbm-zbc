"""Certification scenarios, spec §H items 1-16 (no network; every port is a fake)."""

from datetime import timedelta

import pytest

from fakes import FakeSanctions, PassingFinance, PassingVerification
from helpers import (ANDRE_TOKEN, NOW, SEED_IDS, Harness, brand_facts, client_facts, clip_facts, creator_facts,
                     passing_ports, publish_facts, rid, unmet_codes, unmet_ids)
from ports import NotBuiltFinance31, NotBuiltLegal37, NotWiredAccessibilityChecker, NotWiredSanctionsProvider


def _payout_ready(h: Harness, platform="youtube", **clip_over):
    h.activate_creator()
    h.activate_brand()
    return h.review("zbc_clip", rid("sub"), clip_facts(platform=platform, **clip_over))


# H.1 -------------------------------------------------------------------------------------

def test_h01_seed_not_approved_blocks_all_three_gates(h):
    a = h.rule("client-1", "client", client_facts()).json()
    p = h.review("zbc_clip", "sub-1", clip_facts()).json()
    w = h.review("zbm_work", "work-1", publish_facts()).json()
    for r in (a, p, w):
        assert r["allowed"] is False
        assert [(u["obligation_id"], u["code"]) for u in r["unmet"]] == [("HR-04", "register_not_in_force")]
    assert h.get("/health").json()["register_version_in_force"] is None


# H.2 -------------------------------------------------------------------------------------

def test_h02_seed_approved_with_andre_token_is_version_1(h):
    seed = [p for p in h.inbox() if p["kind"] == "seed"][0]
    assert len(seed["proposed_rows"]) == 118
    out = h.approve(seed)
    assert out["register_version"] == 1
    body = h.get("/health").json()
    assert body == {"status": "ok", "service": "compliance-py", "register_version_in_force": 1, "in_memory": True,
                    "seed_pinned": True, "production": True}   # AEGIS N14-13
    assert len(h.ledger.of_type("register_version_published")) == 1
    assert len(h.ledger.of_type("seed_loaded")) == 1


# H.3 -------------------------------------------------------------------------------------

def test_h03_clean_us_clipper_is_allowed(hs):
    r = hs.activate_creator()
    assert r["allowed"] is True, r["unmet_lines"]
    assert r["unmet"] == [] and r["register_version"] == 1 and r["ruling_id"].startswith("cmp-rul-")
    assert len(r["ruling_id"]) == len("cmp-rul-") + 26
    assert hs.ledger.of_type("activation_ruling")[-1]["event_id"] == r["ledger_event_id"]


# H.4 -------------------------------------------------------------------------------------

def test_h04_clean_clip_blocked_by_exactly_the_three_counsel_rows_then_allowed_after_memos(hs):
    r = _payout_ready(hs).json()
    assert r["allowed"] is False
    assert unmet_codes(r) == {("CQ-01", "rule_not_in_force"), ("CQ-03", "rule_not_in_force"),
                              ("CQ-11", "rule_not_in_force")}
    assert r["reason"].startswith("3 unmet: compliance_38/CQ-01/rule_not_in_force")
    memos = [hs.memo_supersede(c) for c in ("CQ-01", "CQ-03", "CQ-11")]
    assert hs.approve(*memos)["register_version"] == 2
    r2 = hs.review("zbc_clip", rid("sub"), clip_facts()).json()
    assert r2["allowed"] is True, r2["unmet_lines"]
    assert r2["reason"] == "allowed under register v2"


# H.5 -------------------------------------------------------------------------------------

def test_h05_expired_platform_row_blocks_tiktok_payout(hs):
    hs.activate_creator()
    hs.activate_brand(platforms=("youtube", "tiktok"))  # AEGIS N14-2 sweep: the clip's platform must be activated
    hs.approve(*[hs.memo_supersede(c) for c in ("CQ-01", "CQ-03", "CQ-11")])
    ok = hs.review("zbc_clip", rid("sub"), clip_facts(platform="tiktok")).json()
    assert ok["allowed"] is True, ok["unmet_lines"]
    hs.clock.at = NOW.replace(month=10, day=26, hour=0, minute=1)
    r = hs.review("zbc_clip", rid("sub"), clip_facts(platform="tiktok")).json()
    tt01 = [u for u in r["unmet"] if u["obligation_id"] == "PLT-TT-01"]
    assert tt01 and tt01[0]["code"] == "rule_not_in_force" and tt01[0]["row_status"] == "expired"
    assert "expired on 2026-10-26" in tt01[0]["message"]
    assert r["allowed"] is False
    assert any(e["subject_id"] == "obligation:PLT-TT-01" for e in hs.ledger.of_type("obligation_expired"))


# H.6 -------------------------------------------------------------------------------------

def test_h06_instagram_clip_blocked_citing_meta_row(hs):
    hs.activate_creator(accounts=[{"platform": "instagram", "handle_sha256": "e" * 64}])
    hs.activate_brand(platforms=("instagram",))
    r = hs.review("zbc_clip", rid("sub"), clip_facts(platform="instagram")).json()
    meta = [u for u in r["unmet"] if u["obligation_id"] == "PLT-META-01"]
    assert meta and meta[0]["code"] == "rule_not_in_force" and meta[0]["row_status"] == "unverified"
    assert meta[0]["source_url"] == "https://www.facebook.com/business/help/788022387934546"


# H.7 -------------------------------------------------------------------------------------

@pytest.mark.parametrize("disclosure, oid, code, url_part", [
    ({"platform_toggle_evidence_ref": None}, "PLT-TT-01", "platform_toggle", "tiktok.com/legal/page/global/bc-policy"),
    ({"in_video_label_text": "collab"}, "US-FTC-D101-02", "label_vocabulary", "disclosures-101-social-media-influencers"),
    ({"in_video_label_start_s": 8}, "US-FTC-D101-01", "in_video_label", "disclosures-101-social-media-influencers"),
])
def test_h07_disclosure_failures_cite_the_right_rows_with_urls(hs, disclosure, oid, code, url_part):
    r = _payout_ready(hs, platform="tiktok", disclosure=disclosure).json()
    hit = [u for u in r["unmet"] if u["obligation_id"] == oid and u["code"] == code]
    assert hit, r["unmet_lines"]
    assert url_part in hit[0]["source_url"]
    assert r["allowed"] is False


def test_h07b_voice_without_audio_disclosure_cites_d101_01(hs):
    r = _payout_ready(hs, disclosure={"voice_present": True, "audio_disclosure_present": False}).json()
    assert ("US-FTC-D101-01", "in_video_label") in unmet_codes(r)


# H.8 -------------------------------------------------------------------------------------

def test_h08_under_18_attestation_is_a_hard_block_and_no_guardian_field_exists():
    ports = None
    x = Harness()
    x.ports.verification = PassingVerification(age_status="minor")
    x.approve_seed()
    x.run_controls()
    s = x.screen("clipper-9")
    r = x.rule("clipper-9", "zbc_creator", creator_facts(s["screen_id"])).json()
    assert r["allowed"] is False
    hr02 = [u for u in r["unmet"] if u["obligation_id"] == "HR-02"]
    assert hr02 and hr02[0]["code"] == "age_18_plus" and "no guardian path" in hr02[0]["message"]
    for guardian in ({"guardian_consent": True}, {"age": {"verification_attestation_id": "a", "method": "photo_id_match",
                                                         "dob_field_neutral": True, "guardian_id": "g"}}):
        f = creator_facts(s["screen_id"], **guardian)
        assert x.rule("clipper-9", "zbc_creator", f).status_code == 422
    assert ports is None


# H.9 -------------------------------------------------------------------------------------

@pytest.mark.parametrize("country, region, expect_ids", [
    ("FR", None, {"HR-07", "FR-LOI-2023-451"}),
    ("CA", "CA-QC", {"HR-07", "HR-05"}),
    ("CA", None, {"HR-07", "HR-05"}),
    ("IR", None, {"HR-07", "US-OFAC-04"}),
    ("UA", "UA-43", {"HR-07", "US-OFAC-04"}),
    ("BR", None, {"HR-07"}),
])
def test_h09_refused_jurisdictions(hs, country, region, expect_ids):
    r = hs.rule("client-x", "client", client_facts(country=country, region=region)).json()
    assert r["allowed"] is False
    refused = {u["obligation_id"] for u in r["unmet"] if u["code"] == "jurisdiction_class"}
    assert expect_ids <= refused, r["unmet_lines"]


def test_h09_gb_operates_and_is_allowed(hs):
    r = hs.rule("client-gb", "client", client_facts(country="GB", region=None, targets=("GB",))).json()
    assert r["allowed"] is True, r["unmet_lines"]


def test_h09_italy_with_eu_kit_allowed_and_germany_blocked_by_its_label_row(hs):
    it = hs.rule("client-it", "client", client_facts(country="IT", region=None, targets=("IT",),
                                                     eu_kit_version_acknowledged=1, msa_eu_clause=True)).json()
    assert it["allowed"] is True, it["unmet_lines"]
    no_kit = hs.rule("client-it2", "client", client_facts(country="IT", region=None, targets=("IT",),
                                                          eu_kit_version_acknowledged=0, msa_eu_clause=True)).json()
    assert ("HR-06", "eu_kit") in unmet_codes(no_kit)
    de = hs.rule("client-de", "client", client_facts(country="DE", region=None, targets=("DE",),
                                                     eu_kit_version_acknowledged=1, msa_eu_clause=True)).json()
    assert de["allowed"] is False
    assert ("DE-UWG-5A", "rule_not_in_force") in unmet_codes(de)


def test_h09_italian_clipper_with_eu_kit_activates(hs):
    s = hs.screen("clipper-it", country="IT", region=None)
    ok = hs.rule("clipper-it", "zbc_creator", creator_facts(s["screen_id"], country="IT", region=None,
                                                            network_country_signal="IT", eu_kit_version_acknowledged=1))
    hs.ports.finance.form_kind = "w8ben"
    r = hs.rule("clipper-it", "zbc_creator", creator_facts(s["screen_id"], country="IT", region=None, tax_form_kind="w8ben",
                                                           network_country_signal="IT", eu_kit_version_acknowledged=1)).json()
    assert r["allowed"] is True, r["unmet_lines"]
    assert ok.status_code == 200


# H.10 ------------------------------------------------------------------------------------

def test_h10_canadian_creator_recruited_cold_is_blocked_by_hr08(hs):
    s = hs.screen("clipper-ca", country="CA", region="CA-ON")
    r = hs.rule("clipper-ca", "zbc_creator", creator_facts(s["screen_id"], country="CA", region="CA-ON",
                                                           network_country_signal="CA", recruitment_channel="outbound_cold")).json()
    assert ("HR-08", "no_cold_outbound_ca") in unmet_codes(r)
    inbound = hs.rule("clipper-ca", "zbc_creator", creator_facts(s["screen_id"], country="CA", region="CA-ON",
                                                                 network_country_signal="CA")).json()
    assert ("HR-08", "no_cold_outbound_ca") not in unmet_codes(inbound)


def test_h10_client_with_cold_outbound_to_canada_is_blocked(hs):
    r = hs.rule("client-1", "client", client_facts(outbound_cold_contact_countries=["CA"])).json()
    assert ("HR-08", "no_cold_outbound_ca") in unmet_codes(r)


# H.11 ------------------------------------------------------------------------------------

def test_h11_sanctions_stand_in_blocks_payout():
    x = Harness()
    x.ports.sanctions = NotWiredSanctionsProvider()
    x.approve_seed()
    x.run_controls()
    x.approve(*[x.memo_supersede(c) for c in ("CQ-01", "CQ-03", "CQ-11")])
    x.activate_brand()
    s = x.screen("clipper-1")
    assert s["result"] == "unavailable"
    x.rule("clipper-1", "zbc_creator", creator_facts(s["screen_id"]))
    r = x.review("zbc_clip", rid("sub"), clip_facts()).json()
    assert r["allowed"] is False
    assert "control_red:C-04" in {u["code"] for u in r["unmet"]}


def test_h11_stale_screen_blocks_with_rescreen_required(hs):
    hs.approve(*[hs.memo_supersede(c) for c in ("CQ-01", "CQ-03", "CQ-11")])
    hs.activate_creator()
    hs.activate_brand()
    assert hs.review("zbc_clip", rid("sub"), clip_facts()).json()["allowed"] is True
    hs.clock.advance(days=2)
    hs.run_controls()  # keeps C-04/C-11 green: only the screen is stale
    r = hs.review("zbc_clip", rid("sub"), clip_facts()).json()
    stale = [u for u in r["unmet"] if u["obligation_id"] in ("US-OFAC-01", "US-OFAC-02")]
    assert stale and all("re-screen required" in u["message"] for u in stale)
    assert r["allowed"] is False


def test_h11_potential_match_opens_hold_until_andre_releases(hs):
    hs.approve(*[hs.memo_supersede(c) for c in ("CQ-01", "CQ-03", "CQ-11")])
    hs.activate_creator()
    hs.activate_brand()
    hs.ports.sanctions.result = "potential_match"
    s = hs.screen("clipper-1")
    assert s["hold_id"] and s["result"] == "potential_match"
    r = hs.review("zbc_clip", rid("sub"), clip_facts()).json()
    assert f"hold_open:{s['hold_id']}" in {u["code"] for u in r["unmet"]}
    # the hold does not go away because a later screen is clear
    hs.ports.sanctions.result = "clear"
    hs.screen("clipper-1")
    r = hs.review("zbc_clip", rid("sub"), clip_facts()).json()
    assert f"hold_open:{s['hold_id']}" in {u["code"] for u in r["unmet"]}
    rel = hs.post(f"/compliance/v1/holds/{s['hold_id']}/release", {"request_id": rid("rel"), "reason": "false positive: DOB differs"},
                  andre=ANDRE_TOKEN)
    assert rel.status_code == 200, rel.text
    assert hs.review("zbc_clip", rid("sub"), clip_facts()).json()["allowed"] is True


def test_h11_match_is_a_permanent_block(hs):
    hs.ports.sanctions.result = "match"
    s = hs.screen("clipper-1")
    rel = hs.post(f"/compliance/v1/holds/{s['hold_id']}/release", {"request_id": rid("rel"), "reason": "x"}, andre=ANDRE_TOKEN)
    assert rel.status_code == 409


# H.12 ------------------------------------------------------------------------------------

def test_h12_finance_stand_in_blocks_payout_citing_bwh(hs):
    hs.activate_creator()
    hs.activate_brand()
    hs.ports.finance = NotBuiltFinance31()
    r = hs.review("zbc_clip", rid("sub"), clip_facts()).json()
    assert ("US-IRS-BWH", "dependency_unavailable:finance_31") in unmet_codes(r)
    assert r["allowed"] is False


# H.13 ------------------------------------------------------------------------------------

def test_h13_site_publish_accessibility_tracking_and_legal(hs):
    hs.ports.accessibility = NotWiredAccessibilityChecker()
    hs.post("/compliance/v1/accessibility/checks", {"request_id": rid("a"), "asset_ref": "site-1", "asset_type": "site",
                                                    "content_sha256": "d" * 64, "owner_id": "client-1"},
            caller="creative_production")
    r = hs.review("zbm_work", "w-1", publish_facts()).json()
    assert ("HR-09", "accessibility_pass") in unmet_codes(r)
    tr = hs.review("zbm_work", "w-2", publish_facts(flags={"uses_tracking": True},
                                                    tracking={"tracking_disclosed": True, "consent_banner_present": False,
                                                              "consent_before_nonessential": True, "opt_out_present": True})).json()
    assert ("HR-10", "consent_banner") in unmet_codes(tr)
    hs.ports.legal = NotBuiltLegal37()
    lg = hs.review("zbm_work", "w-3", publish_facts()).json()
    assert ("HR-11", "dependency_unavailable:legal_37") in unmet_codes(lg)


def test_h13_site_publish_allowed_with_passing_providers(hs):
    hs.activate_client_for_publish()  # AEGIS N14-2: publish needs the client's current activation
    a = hs.post("/compliance/v1/accessibility/checks", {"request_id": rid("a"), "asset_ref": "site-1", "asset_type": "site",
                                                        "content_sha256": "d" * 64, "owner_id": "client-1"},
                caller="creative_production")
    assert a.status_code == 200 and a.json()["passed"] is True
    r = hs.review("zbm_work", "w-1", publish_facts()).json()
    assert r["allowed"] is True, r["unmet_lines"]


# H.14 is in test_watcher.py (regression fixtures) ------------------------------------------------

# H.15 ------------------------------------------------------------------------------------

def test_h15_control_past_sla_is_red_and_c04_red_blocks_payout_and_creator_activation(hs):
    c04 = hs.get("/compliance/v1/controls/C-04").json()
    assert c04["status"] == "green"
    hs.clock.advance(hours=25)
    assert hs.get("/compliance/v1/controls/C-04").json()["status"] == "red"
    assert hs.get("/compliance/v1/controls/C-04").json()["reason"] == "sla_expired"
    s = hs.screen("clipper-1")
    act = hs.rule("clipper-1", "zbc_creator", creator_facts(s["screen_id"])).json()
    assert "control_red:C-04" in {u["code"] for u in act["unmet"]}
    pay = hs.review("zbc_clip", rid("sub"), clip_facts()).json()
    assert "control_red:C-04" in {u["code"] for u in pay["unmet"]}
    c04_item = [u for u in pay["unmet"] if u["code"] == "control_red:C-04"][0]
    assert c04_item["obligation_id"] == "US-OFAC-02"


def test_h15_never_run_controls_are_red_and_c11_blocks_every_gate():
    x = Harness()
    x.approve_seed()
    assert all(c["status"] == "red" for c in x.get("/compliance/v1/controls").json())
    r = x.rule("client-1", "client", client_facts()).json()
    assert "control_red:C-11" in {u["code"] for u in r["unmet"]}


# H.16 ------------------------------------------------------------------------------------

def test_h16_every_unmet_item_cites_an_id_in_the_version_in_force(hs):
    responses = []
    s = hs.screen("clipper-1")
    responses.append(hs.rule("clipper-1", "zbc_creator", {}).json())
    responses.append(hs.rule("clipper-1", "zbc_creator", creator_facts(s["screen_id"], country="FR", region=None)).json())
    responses.append(hs.rule("camp-1", "zbc_brand", brand_facts(platforms=("tiktok", "x"), targets=("DE", "CA", "IR"),
                                                                client_category="gambling")).json())
    responses.append(hs.review("zbc_clip", "sub-1", {}).json())
    responses.append(hs.review("zbc_clip", "sub-2", clip_facts(platform="twitch")).json())
    responses.append(hs.review("zbm_work", "w-1", {}).json())
    for t in ("ad_video", "email_campaign", "sms_campaign", "subscription_checkout", "chatbot", "dm_campaign"):
        responses.append(hs.review("zbm_work", f"w-{t}", publish_facts(asset_type=t, targets=("CA-ON", "US"))).json())
    ids_in_force = {r["id"] for r in hs.svc.current.rows}
    assert ids_in_force == SEED_IDS
    for r in responses:
        assert r["allowed"] is False
        for u in r["unmet"]:
            assert u["obligation_id"] in ids_in_force, u
            assert set(u) == {"code", "obligation_id", "obligation_title", "source_url", "row_status", "message"}
            assert len(u["message"]) <= 200
        for line in r["unmet_lines"]:
            assert line.startswith("compliance_38/") and len(line) <= 400


def test_h16_fact_missing_items_name_the_key_and_cite_hr03(hs):
    r = hs.rule("clipper-1", "zbc_creator", {}).json()
    keys = {u["code"] for u in r["unmet"] if u["obligation_id"] == "HR-03"}
    for k in ("fact_missing:age.method", "fact_missing:sanctions_screen_id", "fact_missing:jurisdiction.declared_country",
              "fact_missing:flags.political_content", "fact_missing:accounts"):
        assert k in keys
