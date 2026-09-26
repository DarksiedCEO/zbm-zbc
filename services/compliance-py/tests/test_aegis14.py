"""
AEGIS round 14 findings on Compliance (38): one or more reproductions per
finding (N14-1 ... N14-15b), written to FAIL on the code at bc38d0a and pass
after the fix. The probe each one mirrors is named in its docstring
(review14/r2/p/*.py).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from clock import FixedClock, iso
from fakes import FakeA11y
from helpers import (NOW, SEED_PATH, SEED_ROWS, Harness, brand_facts, client_facts, clip_facts, creator_facts, deep,
                     publish_facts, rid, unmet_codes, unmet_ids)
from ports import A11yAnswer

UTC = timezone.utc


def ready(h: Harness | None = None) -> Harness:
    """Seed approved, controls run, CQ-01/03/11 memos approved (payout reachable)."""
    h = h or Harness()
    h.approve_seed()
    h.run_controls()
    h.approve(*[h.memo_supersede(c) for c in ("CQ-01", "CQ-03", "CQ-11")])
    h.run_controls()
    return h


def seed_row(oid: str) -> dict:
    return dict(next(r for r in SEED_ROWS if r["id"] == oid))


def pay(h, **over):
    return h.review("zbc_clip", rid("sub"), clip_facts(**over)).json()


def a11y_pass(h, sha="d" * 64):
    r = h.post("/compliance/v1/accessibility/checks", {"request_id": rid("a"), "asset_ref": "site-1", "asset_type": "site",
                                                       "content_sha256": sha, "owner_id": "client-1"}, caller="creative_production")
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------- N14-1 latest activation ruling wins

def test_n14_1_payout_blocked_after_clipper_reactivation_is_refused():
    """pa_gates A1: an earlier ALLOWED clipper activation must not survive a later refused one."""
    h = ready()
    h.activate_creator()
    h.activate_brand()
    assert pay(h)["allowed"] is True
    s = h.screen("clipper-1", country="FR", region=None)
    ra = h.rule("clipper-1", "zbc_creator", creator_facts(s["screen_id"], country="FR", region=None)).json()
    assert ra["allowed"] is False
    j = pay(h)
    assert j["allowed"] is False
    assert ("HR-02", "fact_missing:clipper_activation") in unmet_codes(j)
    # a later ALLOWED re-activation makes it current again
    h.activate_creator()
    assert pay(h)["allowed"] is True


def test_n14_1_payout_blocked_after_campaign_reactivation_is_refused():
    """pa_gates A1b."""
    h = ready()
    h.activate_creator()
    h.activate_brand()
    rb = h.rule("camp-1", "zbc_brand", brand_facts(targets=("US", "FR"))).json()
    assert rb["allowed"] is False
    j = pay(h)
    assert j["allowed"] is False
    assert ("HR-03", "fact_missing:campaign_activation") in unmet_codes(j)


def test_n14_1_publish_blocked_after_client_reactivation_is_refused():
    h = ready()
    h.activate_client(platforms=("web", "youtube"))
    a11y_pass(h)
    assert h.review("zbm_work", "w1", publish_facts()).json()["allowed"] is True
    assert h.rule("client-1", "client", client_facts(country="FR", region=None)).json()["allowed"] is False
    j = h.review("zbm_work", "w1b", publish_facts()).json()
    assert j["allowed"] is False and ("HR-03", "fact_missing:client_activation") in unmet_codes(j)


# ---------------------------------------------------------------- N14-2 publish needs a current client activation

@pytest.mark.parametrize("case", ["never", "refused_site", "refused_ad_static"])
def test_n14_2_publish_requires_an_allowed_client_activation(case):
    """pa_gates A10e/A10f/A10g."""
    h = ready()
    a11y_pass(h)
    if case == "never":
        f = publish_facts(client="client-never", flags={"uses_tracking": True})
    else:
        h.rule("client-fr", "client", client_facts(country="FR", region=None))
        f = publish_facts(client="client-fr") if case == "refused_site" else \
            publish_facts(asset_type="ad_static", client="client-fr", platforms=("youtube",))
    j = h.review("zbm_work", rid("w"), f).json()
    assert j["allowed"] is False
    assert ("HR-03", "fact_missing:client_activation") in unmet_codes(j)


@pytest.mark.parametrize("over", [{"targets": ("GB",)}, {"platforms": ("tiktok",)}, {"targets": ("US", "GB")}])
def test_n14_2_publish_target_and_platform_must_be_covered_by_the_activation(over):
    h = ready()
    h.activate_client(targets=("US",), platforms=("web",))
    a11y_pass(h)
    assert h.review("zbm_work", rid("w"), publish_facts()).json()["allowed"] is True
    j = h.review("zbm_work", rid("w"), publish_facts(**over)).json()
    assert j["allowed"] is False
    assert ("HR-03", "fact_missing:client_activation_scope") in unmet_codes(j)


def test_n14_2_sweep_payout_platform_must_be_covered_by_the_campaign_activation():
    h = ready()
    h.activate_creator()
    h.activate_brand(platforms=("youtube",))
    j = pay(h, platform="tiktok")
    assert j["allowed"] is False
    assert ("HR-03", "fact_missing:campaign_activation_scope") in unmet_codes(j)


# ---------------------------------------------------------------- N14-3 ISO 3166-2 validation

def resolve(h, country, region):
    r = h.post("/compliance/v1/jurisdictions/resolve", {"request_id": rid("j"), "person": {
        "declared_country": country, "declared_region": region, "attested": True, "attestation_ref": "a1"}},
        caller="onboarding")
    assert r.status_code == 200, r.text
    return r.json()["answers"][0]


@pytest.mark.parametrize("country,region", [("CA", "CA-PQ"), ("CA", "CA-QUE"), ("US", "US-XX"), ("GB", "GB-ZZZ"),
                                            ("CA", "CA-QC"), ("XX", None), ("FX", None), ("IT", "IT-ZZ")])
def test_n14_3_unknown_or_refused_codes_refuse(country, region):
    """pa_gates A5 (CA-PQ, CA-QUE operated; US-XX / GB-ZZZ operated)."""
    h = ready()
    a = resolve(h, country, region)
    assert a["class"] == "refuse", a


@pytest.mark.parametrize("country,region,cls", [("US", "US-CA", "operate"), ("GB", "GB-ENG", "operate"),
                                                ("CA", "CA-ON", "operate"), ("IT", "IT-RM", "conditional"),
                                                ("GB", None, "operate"), ("IT", None, "conditional")])
def test_n14_3_real_codes_still_resolve(country, region, cls):
    h = ready()
    assert resolve(h, country, region)["class"] == cls


def test_n14_3_campaign_target_legacy_quebec_code_blocked():
    """pa_gates A5c."""
    h = ready()
    j = h.rule("camp-pq", "zbc_brand", brand_facts(targets=("US", "CA-PQ"))).json()
    assert j["allowed"] is False and "HR-07" in unmet_ids(j)


def test_n14_3_sweep_screen_and_register_codes_validated():
    h = ready()
    body = {"request_id": rid("scr"), "subject_id": "x-1", "role": "payee", "legal_name": "X", "aliases": [],
            "country": "CA", "region": "CA-PQ"}
    assert h.post("/compliance/v1/sanctions/screen", body, caller="onboarding").status_code == 422
    row = {**seed_row("PLT-TT-01"), "id": "ZZ-NEW-1", "status": "unverified", "verified_at": None, "expires_at": None,
           "applies_when": {"jurisdictions": ["CA-PQ"]}}
    assert h.propose({"kind": "new", "proposed_row": row}).status_code == 422


def test_n14_3_subdivision_data_ships_with_its_source():
    from jurisdictions import SUBDIVISIONS, SOURCE, is_known
    assert "iso-codes" in SOURCE
    for c in ("CA", "US", "GB", "FR", "DE", "NL", "IE", "ES", "IT"):
        assert SUBDIVISIONS.get(c), c
    assert is_known("CA-QC") and is_known("US-CA") and not is_known("CA-PQ") and not is_known("CA-QUE")


# ---------------------------------------------------------------- N14-4 local log head vs the ledger

def _durable(tmp_path):
    x = Harness(data_dir=str(tmp_path / "d"))
    ready(x)
    return x


def test_n14_4_truncated_local_log_refuses_to_start(tmp_path):
    """pb_authority B4: truncate the tail (a hold and a version) and restart."""
    x = _durable(tmp_path)
    n = len(x.svc.log)
    x.ports.sanctions.result = "potential_match"
    x.screen("clipper-1")
    pr = x.propose({"kind": "amend", "target_id": "PLT-YT-01", "proposed_row": {
        **seed_row("PLT-YT-01"), "status": "unverified", "verified_at": None, "expires_at": None}}).json()["proposal"]
    x.approve(pr)
    logf = tmp_path / "d" / "compliance_log.jsonl"
    lines = [ln for ln in logf.read_bytes().split(b"\n") if ln]
    logf.write_bytes(b"\n".join(lines[:n]) + b"\n")
    with pytest.raises(RuntimeError, match="truncat|behind the ledger"):
        Harness(data_dir=str(tmp_path / "d"), ledger=x.ledger, clock=x.clock)


def test_n14_4_register_version_behind_the_ledger_refuses_to_start(tmp_path):
    x = _durable(tmp_path)
    v = x.get("/health").json()["register_version_in_force"]
    n = len(x.svc.log)
    x.approve(x.memo_supersede("CQ-02"))
    assert x.get("/health").json()["register_version_in_force"] == v + 1
    # drop the decision line AND its ledger anchor: only the published version is left to tell
    logf = tmp_path / "d" / "compliance_log.jsonl"
    lines = [ln for ln in logf.read_bytes().split(b"\n") if ln]
    logf.write_bytes(b"\n".join(lines[:n]) + b"\n")
    x.ledger.events[:] = [e for e in x.ledger.events if e["event_type"] != "local_log_appended"
                          or int(e["event_id"].split("-")[3]) <= n]
    with pytest.raises(RuntimeError, match="register version"):
        Harness(data_dir=str(tmp_path / "d"), ledger=x.ledger, clock=x.clock)


def test_n14_4_empty_log_against_a_ledger_that_anchors_one_refuses(tmp_path):
    x = _durable(tmp_path)
    (tmp_path / "d" / "compliance_log.jsonl").write_bytes(b"")
    with pytest.raises(RuntimeError):
        Harness(data_dir=str(tmp_path / "d"), ledger=x.ledger, clock=x.clock)


def test_n14_4_clean_restart_still_starts(tmp_path):
    x = _durable(tmp_path)
    y = Harness(data_dir=str(tmp_path / "d"), ledger=x.ledger, clock=x.clock, ports=x.ports)
    assert y.get("/health").json()["register_version_in_force"] == x.get("/health").json()["register_version_in_force"]
    c11 = y.run_controls()["results"]["C-11"]
    assert c11["result"] == "pass", c11


def test_n14_4_failed_append_after_anchor_does_not_brick_restart(tmp_path):
    x = _durable(tmp_path)
    x.svc.log.fail_next_append = True
    r = x.rule("client-z", "client", client_facts())
    assert r.status_code == 503
    assert x.ledger.of_type("local_commit_failed")      # the anchored line was withdrawn on the ledger
    y = Harness(data_dir=str(tmp_path / "d"), ledger=x.ledger, clock=x.clock, ports=x.ports)
    assert y.get("/health").json()["register_version_in_force"] is not None


def test_n14_4_c11_red_when_the_ledger_does_not_anchor_the_local_log(tmp_path):
    x = _durable(tmp_path)
    assert x.run_controls()["results"]["C-11"]["result"] == "pass"
    x.ledger.events[:] = [e for e in x.ledger.events if e["event_type"] != "local_log_appended"]
    c11 = x.run_controls()["results"]["C-11"]
    assert c11["result"] == "fail" and "anchor" in c11["detail"]


# ---------------------------------------------------------------- N14-5 future evidence dates

@pytest.mark.parametrize("proposer", ["legal_37", "andre"])
@pytest.mark.parametrize("days", [2, 1826])
def test_n14_5_future_verified_at_refused_for_every_proposer(proposer, days):
    """pa_gates A3."""
    h = ready()
    fut = (NOW + timedelta(days=days)).date().isoformat()
    row = seed_row("PLT-YT-01")
    ev = {"source_url": row["source_url"], "fetched_at": fut + "T00:00:00Z", "snapshot_sha256": "a" * 64,
          "normalized_text_sha256": "b" * 64, "quoted_excerpt": "x", "doc_number": None}
    body = {"kind": "reverify", "target_id": "PLT-YT-01", "proposed_row": {**row, "verified_at": fut}, "evidence": ev}
    r = h.propose(body, caller="legal_37") if proposer == "legal_37" else h.propose(body)
    assert r.status_code == 422, r.text


def test_n14_5_one_day_tolerance_is_accepted():
    h = ready()
    d = (NOW + timedelta(days=1)).date().isoformat()
    row = seed_row("PLT-YT-01")
    ev = {"source_url": row["source_url"], "fetched_at": d + "T00:00:00Z", "snapshot_sha256": "a" * 64,
          "normalized_text_sha256": "b" * 64, "quoted_excerpt": "x", "doc_number": None}
    r = h.propose({"kind": "reverify", "target_id": "PLT-YT-01", "proposed_row": {**row, "verified_at": d}, "evidence": ev})
    assert r.status_code == 201, r.text


def test_n14_5_sweep_future_accessibility_result_does_not_pass():
    class FutureA11y(FakeA11y):
        def check(self, asset_ref, asset_type, content_sha256):
            a = super().check(asset_ref, asset_type, content_sha256)
            return A11yAnswer(**{**a.__dict__, "checked_at": iso(NOW + timedelta(days=400))})
    h = Harness()
    h.ports.accessibility = FutureA11y()
    ready(h)
    h.activate_client(platforms=("web",))
    a11y_pass(h)
    h.clock.advance(days=60)
    h.run_controls()
    j = h.review("zbm_work", rid("w"), publish_facts()).json()
    assert ("HR-09", "accessibility_pass") in unmet_codes(j)


# ---------------------------------------------------------------- N14-6 negated disclosure labels

@pytest.mark.parametrize("text", ["not sponsored", "This is NOT an ad", "no #ad here", "ad-free content", "ad free",
                                  "Not an ad, just kidding #ad", "isn’t sponsored", "never #sponsored", "without ad"])
def test_n14_6_negated_or_compound_labels_fail(text):
    """pa_gates A7."""
    h = ready()
    h.activate_creator()
    h.activate_brand()
    j = pay(h, disclosure={"in_video_label_text": text})
    assert j["allowed"] is False and ("US-FTC-D101-02", "label_vocabulary") in unmet_codes(j), text


@pytest.mark.parametrize("text", ["#ad", "#ad Sponsored", "Sponsored: great product", "(#ad)", "Advertisement", "＃ＡＤ",
                                  "#ad — not affiliated with the brand"])
def test_n14_6_standalone_labels_still_pass(text):
    h = ready()
    h.activate_creator()
    h.activate_brand()
    j = pay(h, disclosure={"in_video_label_text": text})
    assert j["allowed"] is True, (text, j["unmet"])


def test_n14_6_sweep_negated_local_label_fails():
    h = ready()
    h.activate_creator()
    kit = seed_row("HR-06")["parameters"]["eu_kit_version"]
    h.activate_brand("camp-it", targets=("US", "IT"), eu_kit_version_acknowledged=kit, msa_eu_clause=True)
    ok = pay(h, campaign="camp-it", disclosure={"in_video_label_text": "#ad Pubblicità"})
    assert ("IT-AGCOM", "local_label") not in unmet_codes(ok)
    j = pay(h, campaign="camp-it", disclosure={"in_video_label_text": "#ad non Pubblicità"})
    assert ("IT-AGCOM", "local_label") in unmet_codes(j)


# ---------------------------------------------------------------- N14-9 weakening proposals

def test_n14_9_gate_removal_is_flagged_and_needs_acknowledgment():
    """pa_gates A4b/A4c."""
    h = ready()
    pr = h.propose({"kind": "amend", "target_id": "CQ-02", "proposed_row": {**seed_row("CQ-02"), "gates": ["control"]}},
                   caller="legal_37")
    assert pr.status_code == 201, pr.text
    p = pr.json()["proposal"]
    assert p["weakening"] is True and "gates_removed" in p["weakening_reasons"]
    inbox = {x["proposal_id"]: x for x in h.inbox()}
    assert inbox[p["proposal_id"]]["weakening"] is True
    v = h.get("/health").json()["register_version_in_force"]
    r = h.decide([{"proposal_id": p["proposal_id"], "content_sha256": p["content_sha256"], "decision": "approve"}])
    assert r.status_code == 422 and h.get("/health").json()["register_version_in_force"] == v
    r = h.decide([{"proposal_id": p["proposal_id"], "content_sha256": p["content_sha256"], "decision": "approve",
                   "acknowledge_weakening": True}])
    assert r.status_code == 200 and r.json()["register_version"] == v + 1


def test_n14_9_counsel_question_cannot_be_flipped_to_verified_by_amend():
    """pa_gates A4d."""
    h = ready()
    url = "https://example.com/not-a-memo"
    new = {**seed_row("CQ-02"), "status": "verified", "source_kind": "guidance", "source_quality": "primary",
           "source_url": url, "verified_at": "2026-09-26", "check": "engine_invariant"}
    ev = {"source_url": url, "fetched_at": "2026-09-26T00:00:00Z", "snapshot_sha256": "c" * 64,
          "normalized_text_sha256": "d" * 64, "quoted_excerpt": "trust me", "doc_number": None}
    for kind in ("amend", "reverify"):
        r = h.propose({"kind": kind, "target_id": "CQ-02", "proposed_row": new, "evidence": ev}, caller="legal_37")
        assert r.status_code == 422, (kind, r.text)
    # also refused: a quiet re-label while unverified (the next step would be a plain reverify)
    relabel = {**seed_row("CQ-02"), "source_kind": "guidance"}
    assert h.propose({"kind": "amend", "target_id": "CQ-02", "proposed_row": relabel}, caller="legal_37").status_code == 422
    # a supersede whose replacement is not a counsel memo row is refused
    bad = {**new, "id": "CQ-02-X1", "source_kind": "statute"}
    r = h.propose({"kind": "supersede", "target_id": "CQ-02", "proposed_row": bad, "evidence": ev}, caller="legal_37")
    assert r.status_code == 422, r.text


def test_n14_9_longer_shelf_life_and_later_effective_date_are_weakening():
    """pa_gates A4e."""
    h = ready()
    row = seed_row("PLT-TT-01")
    ev = {"source_url": row["source_url"], "fetched_at": "2026-09-26T00:00:00Z", "snapshot_sha256": "e" * 64,
          "normalized_text_sha256": "f" * 64, "quoted_excerpt": "x", "doc_number": None}
    p = h.propose({"kind": "amend", "target_id": "PLT-TT-01", "proposed_row": {**row, "source_kind": "statute"},
                   "evidence": ev}, caller="legal_37").json()["proposal"]
    assert p["weakening"] is True and "source_kind_longer_shelf_life" in p["weakening_reasons"]
    p = h.propose({"kind": "amend", "target_id": "PLT-TT-01", "proposed_row": {**row, "effective_date": "2099-01-01"},
                   "evidence": ev}, caller="legal_37").json()["proposal"]
    assert p["weakening"] is True and "effective_date_later" in p["weakening_reasons"]


def test_n14_9_strengthening_and_memo_path_are_not_weakening():
    h = ready()
    row = seed_row("PLT-YT-01")
    p = h.propose({"kind": "amend", "target_id": "PLT-YT-01", "proposed_row": {
        **row, "status": "unverified", "verified_at": None, "expires_at": None}}).json()["proposal"]
    assert p["weakening"] is False
    assert h.memo_supersede("CQ-04")["weakening"] is False
    r = h.propose({"kind": "retire", "target_id": "US-FTC-HBNR"}).json()["proposal"]
    assert r["weakening"] is True and "rule_retired" in r["weakening_reasons"]


def test_n14_9_control_catalog_weakening():
    h = ready()
    c14 = [c for c in h.get("/compliance/v1/controls").json() if c["control_id"] == "C-14"][0]
    base = {k: c14[k] for k in ("control_id", "title", "owner_department", "owner_intelligence", "test", "evidence",
                                "sla_hours", "blocks_gates", "obligation_ids")}
    stricter = h.propose({"kind": "control", "target_id": "C-14", "proposed_row": {**base, "sla_hours": 48}}).json()["proposal"]
    assert stricter["weakening"] is False
    c04 = [c for c in h.get("/compliance/v1/controls").json() if c["control_id"] == "C-04"][0]
    base4 = {k: c04[k] for k in base}
    looser = h.propose({"kind": "control", "target_id": "C-04", "proposed_row": {**base4, "blocks_gates": ["activation"]}}
                       ).json()["proposal"]
    assert looser["weakening"] is True and "control_blocks_fewer_gates" in looser["weakening_reasons"]


# ---------------------------------------------------------------- N14-11 watcher flood cap

FTC = "https://www.ftc.gov/feeds/press-release-consumer-protection.xml"


def _rss(items):
    body = "".join(f"<item><title>{t}</title><description>{d}</description><link>{l}</link><guid>{g}</guid></item>"
                   for t, d, l, g in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'.encode()


def test_n14_11_hostile_feed_is_capped_with_one_summary_proposal():
    """pd_watcher D4: 599 matching items drafted 4000 proposals."""
    h = Harness(env={"COMPLIANCE_WATCHER_ENABLED": "1"})
    h.approve_seed()
    h.ports.fetcher.pages[FTC] = _rss([("baseline", "none", "https://www.ftc.gov/a", "g0")])
    h.post("/compliance/v1/watcher/run", {"request_id": rid("w")}, caller="scheduler")
    h.ports.fetcher.pages[FTC] = _rss([(f"privacy endorsement testimonial consumer review item {i}", "privacy",
                                        f"https://www.ftc.gov/n/{i}", f"g{i}") for i in range(1, 600)])
    j = h.post("/compliance/v1/watcher/run", {"request_id": rid("w")}, caller="scheduler").json()
    assert len(j["proposals"]) <= 50
    notices = [p for p in h.inbox() if p["kind"] == "watch_notice"]
    assert len(notices) == 1 and notices[0]["watch"]["source_id"] == "ftc-press-consumer"
    assert notices[0]["watch"]["undrafted"] > 0
    v = h.get("/health").json()["register_version_in_force"]
    h.approve(notices[0])
    assert h.get("/health").json()["register_version_in_force"] == v


def test_n14_11_caps_are_configurable():
    h = Harness(env={"COMPLIANCE_WATCHER_ENABLED": "1", "COMPLIANCE_WATCHER_MAX_PROPOSALS_PER_CYCLE": "5",
                     "COMPLIANCE_WATCHER_MAX_PROPOSALS_PER_SOURCE": "3"})
    h.approve_seed()
    h.ports.fetcher.pages[FTC] = _rss([("baseline", "none", "https://www.ftc.gov/a", "g0")])
    h.post("/compliance/v1/watcher/run", {"request_id": rid("w")}, caller="scheduler")
    h.ports.fetcher.pages[FTC] = _rss([(f"privacy endorsement item {i}", "privacy", f"https://www.ftc.gov/n/{i}", f"g{i}")
                                       for i in range(1, 30)])
    j = h.post("/compliance/v1/watcher/run", {"request_id": rid("w")}, caller="scheduler").json()
    assert len(j["proposals"]) <= 3


# ---------------------------------------------------------------- N14-13 pinned seed

def test_n14_13_seed_path_with_its_own_hash_refuses_without_explicit_opt_in(tmp_path):
    """pb_authority B3b."""
    s = json.loads(SEED_PATH.read_bytes())
    s["rows"] = [r for r in s["rows"] if r["id"] != "CQ-01"]
    d = json.dumps(s).encode()
    p = tmp_path / "seed.json"
    p.write_bytes(d)
    env = {"COMPLIANCE_SEED_PATH": str(p), "COMPLIANCE_SEED_SHA256": hashlib.sha256(d).hexdigest()}
    with pytest.raises(RuntimeError):
        Harness(env=env)
    with pytest.raises(RuntimeError):
        Harness(env={"COMPLIANCE_SEED_PATH": str(p), "COMPLIANCE_ALLOW_UNPINNED_SEED": "1"})  # no hash stated
    x = Harness(env={**env, "COMPLIANCE_ALLOW_UNPINNED_SEED": "1"})
    hl = x.get("/health").json()
    assert hl["seed_pinned"] is False and hl["production"] is False
    x.approve_seed()
    j = x.rule("client-1", "client", client_facts()).json()
    assert j["seed_pinned"] is False


def test_n14_13_pinned_seed_is_marked_pinned():
    h = ready()
    assert h.get("/health").json()["seed_pinned"] is True
    assert h.rule("client-1", "client", client_facts()).json()["seed_pinned"] is True


# ---------------------------------------------------------------- N14-14 UTC dates

def test_n14_14_expiry_uses_utc_whatever_the_clock_timezone():
    """pa_gates A2: 17:00-07:00 on 10-25 is 00:00Z on 10-26; PLT-YT rows expire then."""
    x = ready()
    x.activate_creator()
    x.activate_brand()
    at = datetime(2026, 10, 25, 17, 0, 0, tzinfo=timezone(timedelta(hours=-7)))
    x.clock.at = at
    x.run_controls()
    x.screen("clipper-1")
    j = pay(x, posted_at=iso(at - timedelta(days=20)))
    assert ("PLT-YT-01", "rule_not_in_force") in unmet_codes(j)
    row = x.get("/compliance/v1/register/PLT-YT-01").json()["row"]
    assert row["effective_status"] == "expired"


# ---------------------------------------------------------------- N14-15(b) replay re-evaluates when inputs changed

def test_n14_15b_replay_after_a_hold_opened_is_not_the_stale_allowed_ruling():
    """pb_authority B7."""
    x = ready()
    x.activate_creator()
    x.activate_brand()
    fixed = rid("rev")
    r1 = x.review("zbc_clip", "sub-idem", clip_facts(), request_id=fixed).json()
    assert r1["allowed"] is True
    same = x.review("zbc_clip", "sub-idem", clip_facts(), request_id=fixed).json()
    assert same == r1                                   # nothing changed: the stored answer
    x.ports.sanctions.result = "potential_match"
    x.screen("clipper-1")
    r2 = x.review("zbc_clip", "sub-idem", clip_facts(), request_id=fixed)
    assert r2.status_code == 200 and r2.json()["allowed"] is False
    assert r2.json()["ruling_id"] != r1["ruling_id"]


def test_n14_15b_replay_after_register_change_is_re_evaluated():
    x = ready()
    x.activate_creator()
    x.activate_brand()
    fixed = rid("rev")
    r1 = x.review("zbc_clip", "sub-v", clip_facts(), request_id=fixed).json()
    assert r1["allowed"] is True
    x.approve(x.propose({"kind": "amend", "target_id": "PLT-YT-01", "proposed_row": {
        **seed_row("PLT-YT-01"), "status": "unverified", "verified_at": None, "expires_at": None}}).json()["proposal"])
    r2 = x.review("zbc_clip", "sub-v", clip_facts(), request_id=fixed).json()
    assert r2["allowed"] is False and r2["register_version"] == r1["register_version"] + 1


def test_n14_15b_retry_after_store_failure_and_state_change_is_answered():
    """pb_authority B6c (was 503: the deterministic ruling id already held a different ruling on the ledger)."""
    x = ready()
    x.activate_creator()
    x.activate_brand()
    fixed = rid("rev")
    x.svc.log.fail_next_append = True
    assert x.review("zbc_clip", "sub-lf", clip_facts(), request_id=fixed).status_code == 503
    x.ports.sanctions.result = "potential_match"
    x.screen("clipper-1")
    r = x.review("zbc_clip", "sub-lf", clip_facts(), request_id=fixed)
    assert r.status_code == 200 and r.json()["allowed"] is False


def test_n14_15b_idempotency_survives_restart(tmp_path):
    """pb_authority B7b: after a restart a reused request_id with a different body is 409 (was 503)."""
    x = Harness(data_dir=str(tmp_path / "d"))
    x.approve_seed()
    x.run_controls()
    a = x.rule("client-z", "client", client_facts(), request_id="same-rid")
    y = Harness(data_dir=str(tmp_path / "d"), ledger=x.ledger, clock=x.clock)
    assert y.rule("client-z", "client", client_facts(targets=("FR",)), request_id="same-rid").status_code == 409
    b = y.rule("client-z", "client", client_facts(), request_id="same-rid")
    assert b.status_code == 200 and b.json() == a.json()


def test_n14_4_rewritten_chain_without_a_hold_refuses_to_start(tmp_path):
    """A whole-log rewrite with recomputed hashes (hold dropped, lines re-marked as legacy/unanchored)."""
    from store import RecordLog
    x = _durable(tmp_path)
    x.ports.sanctions.result = "potential_match"
    x.screen("clipper-1")
    recs = [r for r in x.svc.log.iter_records() if r["kind"] != "hold_open"]
    forged = RecordLog(str(tmp_path / "forged"))
    for r in recs:
        forged.append(r["kind"], r["at"], {**r["data"], "anchored": False})
    (tmp_path / "d" / "compliance_log.jsonl").write_bytes((tmp_path / "forged" / "compliance_log.jsonl").read_bytes())
    with pytest.raises(RuntimeError, match="refusing to start"):
        Harness(data_dir=str(tmp_path / "d"), ledger=x.ledger, clock=x.clock)
