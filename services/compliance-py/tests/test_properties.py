"""
Property-style tests: no gate ever returns allowed when any feeding row is
expired or unverified, whichever rows and however many (spec §0.9, B.3).

Method: build an ALLOWED baseline ruling for each gate/lane, take the rows it
evaluated (every row that applied), then publish test-only register versions
in which one row, or a random subset of rows (seeded), is expired or
unverified, and re-run the identical request. Each must be blocked and cite
every altered row. A second property drops single required facts.
"""

import copy
import random
from datetime import date, timedelta

import pytest

from helpers import (Harness, client_facts, clip_facts, creator_facts, publish_facts, rid)
from register import SHELF_LIFE_DAYS, Version, rows_sha256

TRIALS = 40


def _publish_version(svc, mutate):
    v = svc.current
    rows = {r["id"]: copy.deepcopy(r) for r in v.rows}
    mutate(rows)
    new = sorted(rows.values(), key=lambda r: r["id"])
    svc.versions.append(Version(v.version + 1, v.created_at, "andre", ("test",), rows_sha256(new), v.version_sha256,
                                tuple(new)))


def _pop_version(svc):
    svc.versions.pop()


def _unverify(r):
    r.update(status="unverified", verified_at=None, expires_at=None)


def _expire(r, today):
    life = SHELF_LIFE_DAYS[r["source_kind"]]
    va = today - timedelta(days=life + 1)
    r.update(verified_at=va.isoformat(), expires_at=(va + timedelta(days=life)).isoformat())


def _baselines():
    """(name, harness, callable issuing the identical request with a fresh request_id)."""
    out = []
    x = Harness()
    x.approve_seed()
    x.run_controls()
    s = x.screen("clipper-1")
    cf = creator_facts(s["screen_id"])
    out.append(("creator", x, lambda h=x, f=cf: h.rule("clipper-1", "zbc_creator", f)))

    y = Harness()
    y.approve_seed()
    y.run_controls()
    it = client_facts(country="IT", region=None, targets=("IT", "GB", "US-NY"), eu_kit_version_acknowledged=1,
                      msa_eu_clause=True, platforms=("youtube", "tiktok", "x"),
                      claims={"claims_present": True, "health_or_earnings_claim": False, "claim_file_id": "cf-1",
                              "claim_file_approved": True})
    out.append(("client_it", y, lambda h=y, f=it: h.rule("client-it", "client", f)))

    z = Harness()
    z.approve_seed()
    z.run_controls()
    z.approve(*[z.memo_supersede(c) for c in ("CQ-01", "CQ-03", "CQ-11")])
    z.activate_creator()
    z.activate_brand(platforms=("youtube", "tiktok"), targets=("US", "GB", "CA-ON"))
    pf = clip_facts(platform="tiktok")
    out.append(("payout", z, lambda h=z, f=pf: h.review("zbc_clip", rid("sub"), f)))

    w = Harness()
    w.approve_seed()
    w.run_controls()
    w.activate_client_for_publish()  # AEGIS N14-2: publish needs the client's current activation
    w.post("/compliance/v1/accessibility/checks", {"request_id": rid(), "asset_ref": "v", "asset_type": "ad_video",
                                                   "content_sha256": "d" * 64, "owner_id": "client-1"},
           caller="creative_production")
    vf = publish_facts(asset_type="ad_video", platforms=("youtube",), flags={"paid_or_endorsement": True},
                       disclosure={"platform_toggle_evidence_ref": "t-1", "in_video_label_text": "#ad",
                                   "in_video_label_start_s": 0, "voice_present": True, "audio_disclosure_present": True})
    out.append(("publish_video", w, lambda h=w, f=vf: h.review("zbm_work", "work-v", f)))
    sf = publish_facts(asset_type="site", flags={"uses_tracking": True})
    out.append(("publish_site", w, lambda h=w, f=sf: h.review("zbm_work", "work-s", f)))
    return out


BASELINES = _baselines()


@pytest.mark.parametrize("name, h, call", BASELINES, ids=[b[0] for b in BASELINES])
def test_baselines_are_allowed(name, h, call):
    r = call().json()
    assert r["allowed"] is True, (name, r["unmet_lines"])


def _evaluated(h, call):
    r = call().json()
    assert r["allowed"] is True, r["unmet_lines"]
    return h.svc.rulings[r["ruling_id"]]["row_ids_evaluated"]


@pytest.mark.parametrize("name, h, call", BASELINES, ids=[b[0] for b in BASELINES])
def test_each_single_evaluated_row_unverified_or_expired_blocks(name, h, call):
    ids = _evaluated(h, call)
    assert len(ids) >= 8, ids
    today = h.clock.today()
    for oid in ids:
        for how in ("unverified", "expired"):
            row = h.svc.current.by_id()[oid]
            if how == "expired" and SHELF_LIFE_DAYS[row["source_kind"]] is None:
                continue
            _publish_version(h.svc, lambda rows, o=oid, k=how: _unverify(rows[o]) if k == "unverified" else _expire(rows[o], today))
            try:
                r = call().json()
            finally:
                _pop_version(h.svc)
            assert r["allowed"] is False, (name, oid, how)
            cited = {(u["obligation_id"], u["code"]) for u in r["unmet"]}
            assert (oid, "rule_not_in_force") in cited, (name, oid, how, r["unmet_lines"][:5])


@pytest.mark.parametrize("name, h, call", BASELINES, ids=[b[0] for b in BASELINES])
def test_random_subsets_of_expired_and_unverified_rows_always_block(name, h, call):
    ids = _evaluated(h, call)
    rng = random.Random(f"compliance-{name}")
    today = h.clock.today()
    for _ in range(TRIALS):
        chosen = rng.sample(ids, rng.randint(1, min(6, len(ids))))
        how = {o: rng.choice(("unverified", "expired")) for o in chosen}

        def mutate(rows):
            for o, k in how.items():
                if k == "expired" and SHELF_LIFE_DAYS[rows[o]["source_kind"]] is not None:
                    _expire(rows[o], today)
                else:
                    _unverify(rows[o])
        _publish_version(h.svc, mutate)
        try:
            r = call().json()
        finally:
            _pop_version(h.svc)
        assert r["allowed"] is False, (name, how)
        cited = {u["obligation_id"] for u in r["unmet"] if u["code"] == "rule_not_in_force"}
        assert set(chosen) <= cited, (name, how, cited)


def test_the_passage_of_time_alone_expires_rows_and_blocks():
    name, h, call = BASELINES[0]
    assert call().json()["allowed"] is True
    saved = h.clock.at
    try:
        h.clock.at = saved.replace(year=2027, month=6)
        h.run_controls()
        r = call().json()
        assert r["allowed"] is False
        assert any(u["code"] == "rule_not_in_force" and u["row_status"] == "expired" for u in r["unmet"])
    finally:
        h.clock.at = saved


OPTIONAL = {"network_country_signal", "owner_screen_ids", "claims.claim_file_id", "hbnr_clause_signed",
            "eu_kit_version_acknowledged", "msa_eu_clause", "consent_document_id", "personal_use_attested",
            "music.track_or_license_id", "music.source", "claim_file_id", "claim_file_approved",
            "disclosure.audio_disclosure_present", "disclosure.platform_toggle_evidence_ref"}


def _paths(d, prefix=""):
    for k, v in d.items():
        p = f"{prefix}{k}"
        yield p
        if isinstance(v, dict):
            yield from _paths(v, p + ".")


def _drop(d, path):
    d = copy.deepcopy(d)
    parts = path.split(".")
    cur = d
    for p in parts[:-1]:
        cur = cur[p]
    del cur[parts[-1]]
    return d


@pytest.mark.parametrize("which", ["creator", "payout", "publish_site"])
def test_dropping_any_required_fact_blocks_and_names_it(which):
    name, h, call = next(b for b in BASELINES if b[0] == which)
    base_facts = call.__defaults__[1]
    for path in _paths(base_facts):
        if path in OPTIONAL or path.split(".")[0] in OPTIONAL:
            continue
        f = _drop(base_facts, path)
        if which == "creator":
            r = h.rule("clipper-1", "zbc_creator", f).json()
        elif which == "payout":
            r = h.review("zbc_clip", rid("sub"), f).json()
        else:
            r = h.review("zbm_work", "work-s", f).json()
        assert r["allowed"] is False, (which, path)
        codes = {u["code"] for u in r["unmet"]}
        assert any(c.startswith("fact_missing:" + path) or c.startswith("fact_missing:" + path.split(".")[0])
                   for c in codes), (which, path, codes)


def test_flags_are_never_defaulted():
    name, h, call = next(b for b in BASELINES if b[0] == "creator")
    f = call.__defaults__[1]
    for flag in list(f["flags"]):
        g = copy.deepcopy(f)
        del g["flags"][flag]
        r = h.rule("clipper-1", "zbc_creator", g).json()
        assert r["allowed"] is False and f"fact_missing:flags.{flag}" in {u["code"] for u in r["unmet"]}
    assert date.today()


def test_every_flag_a_row_can_name_is_a_required_fact_derived_or_lane_fixed():
    """ADR 0006 choice 2: flags are never defaulted. For every gate/lane, each
    flag named by any seed row feeding that gate is covered by exactly one of:
    a required fact, a flag Compliance derives, or a documented lane-fixed value."""
    import facts as F
    from helpers import SEED_ROWS
    from intelligences.i02_activation_gate import LANE_FIXED_FLAGS
    claims = {"claims_present", "health_or_earnings_claim"}
    covered = {
        ("activation", "client"): set(F.ACTIVATION_FLAGS) | claims | set(LANE_FIXED_FLAGS["client"]),
        ("activation", "zbc_brand"): set(F.ACTIVATION_FLAGS) | claims | set(LANE_FIXED_FLAGS["zbc_brand"]),
        ("activation", "zbc_creator"): set(F.ACTIVATION_FLAGS) | {"entity_payee", "foreign_payee", "es_special_relevance"}
        | set(LANE_FIXED_FLAGS["zbc_creator"]),
        ("payout", "zbc_clip"): set(F.CLIP_FLAGS) | set(F.ACTIVATION_FLAGS) | claims
        | {"entity_payee", "foreign_payee", "paid_or_endorsement", "music_present"},
        ("publish", "zbm_work"): set(F.PUBLISH_FLAGS) | {"music_present"},
    }
    for (gate, who), have in covered.items():
        for r in SEED_ROWS:
            aw = r["applies_when"]
            if gate not in r["gates"]:
                continue
            if gate == "activation" and aw.get("lanes") and who not in aw["lanes"]:
                continue
            if gate != "activation" and aw.get("subject_kinds") and who not in aw["subject_kinds"]:
                continue
            for flag in (aw.get("flags_any") or []) + (aw.get("flags_none") or []):
                assert flag in have, (gate, who, r["id"], flag)
