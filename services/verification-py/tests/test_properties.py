"""Property: NO certification (and no positive HR-13 attestation) while ANY required input is absent, expired,
unverified or a stand-in. Each toggle below removes one input from an otherwise clean flow that certifies;
every single toggle and random combinations of them must leave the clip uncertified, with reasons."""

from __future__ import annotations

import random

import pytest

import ports as P
from clock import iso
from helpers import ANDRE_TOKEN, NOW, Harness, rid
from test_cert_scenarios import run_to_day

STAND_INS = {
    "vault": lambda: P.NotWiredTokenVault(),
    "adapter": lambda: {p: P.NotWiredAdapter(p) for p in ("youtube", "tiktok", "instagram", "x")},
    "hasher": lambda: P.NotWiredPerceptualHasher(),
    "age": lambda: P.NotWiredAgeAssuranceProvider(),
    "compliance": lambda: P.NotWiredCompliance(),
    "finance": lambda: P.NotBuiltFinance31(),
    "cn": lambda: P.NotBuiltClipperNetwork(),
}
ROWS = ("HR-13", "CQ-11", "US-FTC-465-08", "HR-02", "AGE-01", "HR-03")
TOGGLES = ([f"standin:{k}" for k in STAND_INS] + [f"row_unverified:{r}" for r in ROWS] +
           [f"row_expired:{r}" for r in ROWS] + ["row_unavailable:HR-13", "row_unavailable:US-FTC-465-08",
           "no_identity", "no_age", "no_approval", "no_rules", "no_min_live", "vi_rule_unverified:VI-07",
           "liveness_gap", "no_connection", "age_minor_later", "identity_duplicate"])
# The media intake alone is not a required input: with a wired perceptual hasher the stolen-content check is
# complete without the exact SHA-256 fallback (spec C.8).


def run_case(toggles: set) -> tuple[dict, dict]:
    fakes = {}
    for t in toggles:
        if t.startswith("standin:"):
            k = t.split(":")[1]
            fakes["adapters" if k == "adapter" else k] = STAND_INS[k]()
    h = Harness(fakes=fakes)
    if "no_rules" not in toggles:
        h.approve_rules()
    for t in toggles:
        kind, _, arg = t.partition(":")
        if not hasattr(h.compliance, "status"):
            continue                                   # the stand-in already answers unavailable
        if kind == "row_unverified":
            h.compliance.status[arg] = "unverified"
        elif kind == "row_expired":
            h.compliance.status[arg] = "expired"
        elif kind == "row_unavailable":
            h.compliance.unavailable.add(arg)
    if "vi_rule_unverified:VI-07" in toggles and "no_rules" not in toggles:
        row = dict(next(r for r in h.svc.current.rows if r["rule_id"] == "VI-07"), status="unverified")
        p = h.ok(h.post("/vi/v1/rules/proposals", {"request_id": rid(), "kind": "amend", "target_id": "VI-07",
                                                   "proposed_row": row}, andre=ANDRE_TOKEN), 201)["proposal"]
        h.ok(h.post("/vi/v1/rules/decisions", {"request_id": rid(), "decisions": [
            {"proposal_id": p["proposal_id"], "content_sha256": p["content_sha256"], "decision": "approve"}]},
            andre=ANDRE_TOKEN))
    if "no_connection" not in toggles:
        h.connect("clip-a")
    if "identity_duplicate" in toggles:
        h.identity("clip-older", "clip-a@example.com")     # an older identity already holds this email
        h.clock.advance(minutes=1)
    if "no_identity" not in toggles:
        h.identity("clip-a")
    if "no_age" not in toggles:
        h.ok(h.age_check("clip-a"))
    post_ref = "https://www.tiktok.com/@c/video/p1"
    h.post_video("tiktok", post_ref, views=5000, likes=500)
    h.ok(h.register("p1", "clip-a", post_ref=post_ref, min_days_live=None if "no_min_live" in toggles else 7), 201)
    if "no_approval" not in toggles:
        h.approve("p1")

    def on_day(d):
        ad = h.adapters.get("tiktok")
        if "liveness_gap" in toggles and hasattr(ad, "available"):
            ad.available = d != 3
        if d == 5 and "age_minor_later" in toggles:
            h.ok(h.age_check("clip-a", dob="2012-01-01"))
    run_to_day(h, 17, on_day=on_day)
    c = h.cert("p1")
    a = h.ok(h.post("/vi/v1/clips/hr13", {"request_id": rid(), "submission_id": "p1", "post_ref": post_ref,
                                          "platform": "tiktok", "posted_at": iso(NOW), "settlement_lag_days": 14},
                    caller="compliance_38"))
    return c, a


def test_baseline_clean_flow_certifies():
    c, a = run_case(set())
    assert c["status"] == "certified" and a["verified_views"] is True


@pytest.mark.parametrize("toggle", TOGGLES)
def test_no_certification_when_one_input_is_missing(toggle):
    c, a = run_case({toggle})
    assert c["status"] != "certified" and c["certified_views"] is None and c["reasons"], (toggle, c["status"])
    assert a["verified_views"] is False and a["reasons"], toggle


@pytest.mark.parametrize("seed", range(12))
def test_no_certification_for_random_combinations(seed):
    rng = random.Random(seed)
    toggles = set(rng.sample(TOGGLES, rng.randint(2, 4)))
    c, a = run_case(toggles)
    assert c["status"] != "certified" and c["reasons"], (toggles, c["status"])
    assert a["verified_views"] is False, toggles
    for r in c["reasons"]:
        assert set(r) == {"code", "rule_id", "evidence_ids", "message", "source_url"} and len(r["message"]) <= 200
