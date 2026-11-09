"""Naomi — Prompt Volume (ADR 0049): the exact Garwood band, the estimator (always a band, never a bare number;
UNKNOWN, never 0, without a signal; confidence never HIGH), and the Priya hand-off mirror (agents/priya_handoff.py)
including its consumer rules (tenant, self-contradicting counts). Pure."""

from __future__ import annotations

import copy
import math

from agents import fireteam_topics, naomi, priya_handoff
from test_theo import PS, item

GSC = {"impressions": 7, "clicks": 1, "date_range": {"start": "2026-10-05", "end": "2026-11-01"},
       "source": "first_party:search_console"}


def handoff(*its, tenant="zbm"):
    return {"schema": priya_handoff.SCHEMA, "tenant_id": tenant, "run_id": "seo-kwr-" + "1" * 40,
            "generated_at": "2026-11-08T00:00:00Z", "consumers": {}, "items": list(its),
            "rule": "an item is a candidate to measure, not a measured demand"}


# ---------------------------------------------------------------------------------------------- the band

def test_garwood_matches_published_values_rounded_outward():
    # exact 95% Poisson limits (Garwood 1936): 0 -> [0, 3.689]; 1 -> [0.0253, 5.572]; 10 -> [4.795, 18.390]
    assert naomi.garwood(0) == {"low": 0.0, "high": 3.69, "interval": "garwood_poisson_95",
                                "method": "exact (Poisson CDF bisection)"}
    b = naomi.garwood(1)
    assert (b["low"], b["high"]) == (0.02, 5.58)
    b = naomi.garwood(10)
    assert (b["low"], b["high"]) == (4.79, 18.4)


def test_the_band_always_holds_the_count_and_widens_with_it():
    prev = None
    for k in (0, 1, 2, 5, 19, 20, 99, 500, 1000, 1001, 5000, 10 ** 6):
        b = naomi.garwood(k)
        assert b["low"] <= k <= b["high"]
        if prev is not None:
            assert b["high"] - b["low"] >= prev
        prev = b["high"] - b["low"]
    assert naomi.garwood(100)["method"].startswith("exact") and naomi.garwood(101)["method"].startswith("Wilson")


def test_the_approximation_above_the_threshold_is_never_narrower_than_the_exact_band():
    for k in (101, 102, 150, 200, 333, 500, 777, 1000, 2000):
        ex, ap = naomi.garwood_exact(k), naomi.garwood_approx(k)
        assert ap["low"] <= ex["low"] and ap["high"] >= ex["high"], (k, ex, ap)
        assert ap["high"] - ap["low"] <= (ex["high"] - ex["low"]) * 1.15         # outward, but not absurdly


# ---------------------------------------------------------------------------------------------- the estimator

def _topic(items=(), ps=PS):
    return fireteam_topics.plan(ps, list(items))


def test_no_signal_is_unknown_never_zero():
    t = _topic()
    est = naomi.estimate_topic(t[0], None)
    assert est["status"] == "UNKNOWN" and est["band"] is None and est["unknown_reason"] == "NO_OWNED_SIGNAL"
    assert est["reliability"] == "NONE" and est["confidence"] == "NONE"
    assert 0 not in [v for v in est.values() if isinstance(v, int)]


def test_a_derived_priya_item_is_unknown():
    it = item("a", PS["prompts"][0], origin="derived", status="UNKNOWN_NOT_MEASURED")
    t = _topic([it])
    assert t[0]["priya_key"] == it["key"]                                   # the prompt-set topic carries its key
    est = naomi.estimate_topic(t[0], fireteam_topics.item_for(t[0], [it]))
    assert est["status"] == "UNKNOWN" and est["unknown_reason"] == "PRIYA_DERIVED_NOT_MEASURED"


def test_an_observed_thin_item_is_a_directional_proxy_band_with_low_confidence():
    it = item("a", PS["prompts"][0], counts=dict(GSC))
    t = _topic([it])
    est = naomi.estimate_topic(t[0], it)
    assert est["status"] == "DIRECTIONAL_PROXY" and est["reliability"] == "THIN" and est["confidence"] == "LOW"
    assert est["band"]["low"] <= 7 <= est["band"]["high"] and est["unit"] == "impressions_on_this_property"
    s = est["signals"][0]
    assert s["observed_impressions"] == 7 and s["window_days"] == 28
    assert s["provenance"]["verified_against_priya_record"] is False
    assert "PROXY_NOT_PROMPT_VOLUME" in est["flags"] and "value" not in est      # never a bare number


def test_an_unverified_signal_is_capped_thin_and_confidence_is_never_high():
    big = {**GSC, "impressions": 50_000, "clicks": 10, "source": "first_party:bing_webmaster"}
    it = item("a", PS["prompts"][0], counts=big, status="MEASURED_NOT_TIERED")
    est = naomi.estimate_topic(_topic([it])[0], it)
    assert est["reliability"] == "THIN" and est["confidence"] == "LOW" and est["verified"] is False
    assert "HANDOFF_NOT_VERIFIED" in est["flags"] and est["signals"][0]["reliability_if_verified"] == "STRONG"
    assert est["signals"][0]["data_class"] == "claimed"
    est = naomi.estimate_topic(_topic([it])[0], it, verified=True)
    assert est["reliability"] == "STRONG" and est["confidence"] == "MEDIUM" and "BING_WINDOW_DIFFERS" in est["flags"]
    assert "HANDOFF_NOT_VERIFIED" not in est["flags"]


def test_context_from_the_latest_audit_is_never_a_topic_estimate():
    rep = {"search_performance": {"window": {"start": "2026-10-05", "end": "2026-11-01", "days": 28},
                                  "by_query_class": {"classes": {"question": {"impressions": 420, "clicks": 9}}}}}
    c = naomi.context_from_audit(rep, "seo-aud-" + "2" * 40)
    assert c["state"] == "OK" and c["impressions"] == 420 and c["band"]["low"] < 420 < c["band"]["high"]
    assert naomi.context_from_audit(None, None)["state"] == "UNAVAILABLE"
    out = naomi.run(_topic(), [], "zbm", "seo-cnv-" + "3" * 40, c)
    assert out["topics"][0]["status"] == "UNKNOWN"


# ---------------------------------------------------------------------------------------------- the hand-off mirror

def test_a_valid_handoff_passes_and_its_field_set_is_priyas():
    doc = handoff(item("a", "how much is fba reimbursement", counts=dict(GSC)),
                  item("b", "fba reimbursement near me", origin="derived", status="UNKNOWN_NOT_MEASURED"))
    assert priya_handoff.validate(doc, "zbm") == []
    # PR #50 (15917fc8) priya.HANDOFF_ITEM_KEYS, copied: the mirror must stay in lock-step
    assert set(priya_handoff.ITEM_KEYS) == {"key", "query", "origin", "emerging_kinds", "volume_status",
                                            "observed_counts", "evidence", "requested_measurements"}


def test_a_handoff_of_another_tenant_is_refused_whole():
    assert priya_handoff.validate(handoff(tenant="other"), "zbm") == ["tenant_id:HANDOFF_TENANT_MISMATCH"]


def test_a_self_contradicting_count_is_refused():
    bad = item("a", "q one", counts={**GSC, "impressions": 25})                # BELOW_THRESHOLD but 25 >= 20
    assert priya_handoff.validate(handoff(bad), "zbm") == ["items[0].observed_counts:HANDOFF_COUNT_INCONSISTENT"]
    worse = item("a", "q one", counts={**GSC, "clicks": 9})                     # clicks > impressions
    assert priya_handoff.validate(handoff(worse), "zbm") == ["items[0].observed_counts"]
    derived = item("a", "q one", origin="derived", counts=dict(GSC))
    assert "items[0].observed_counts" in priya_handoff.validate(handoff(derived), "zbm")
    nan = item("a", "q one", counts={**GSC, "impressions": math.inf})
    assert priya_handoff.validate(handoff(nan), "zbm") == ["items[0].observed_counts"]


def test_every_item_type_is_bounded_and_checked_against_how_priya_builds_it():
    forged = item("a", "q one", counts={**GSC, "impressions": 10 ** 12, "source": "first_party:bing_webmaster"},
                  status="MEASURED_NOT_TIERED")                            # the review's 10^12 forgery
    assert priya_handoff.validate(handoff(forged), "zbm") == ["items[0].observed_counts"]
    wrong_src = item("a", "q one", counts={**GSC, "source": "first_party:search_console"}, status="MEASURED_NOT_TIERED")
    assert priya_handoff.validate(handoff(wrong_src), "zbm") == ["items[0].observed_counts.source"]
    bing_below = item("a", "q one", counts={**GSC, "source": "first_party:bing_webmaster"})
    assert priya_handoff.validate(handoff(bing_below), "zbm") == ["items[0].observed_counts.source"]
    future = item("a", "q one", counts={**GSC, "date_range": {"start": "2026-11-01", "end": "2026-12-01"}})
    assert priya_handoff.validate(handoff(future), "zbm") == ["items[0].observed_counts.date_range"]
    inverted = item("a", "q one", counts={**GSC, "date_range": {"start": "2026-11-01", "end": "2026-10-01"}})
    assert priya_handoff.validate(handoff(inverted), "zbm") == ["items[0].observed_counts.date_range"]
    unmeasured = item("a", "q one", status="UNKNOWN_NOT_MEASURED")             # observed but "not measured"
    assert priya_handoff.validate(handoff(unmeasured), "zbm") == ["items[0].volume_status"]
    derived_wrong = item("a", "q one", origin="derived", status="BELOW_THRESHOLD")
    assert priya_handoff.validate(handoff(derived_wrong), "zbm") == ["items[0].volume_status"]
    ok = item("a", "q one", counts={**GSC, "impressions": 9_000_000, "source": "first_party:bing_webmaster"},
              status="MEASURED_NOT_TIERED")
    assert priya_handoff.validate(handoff(ok), "zbm") == []


def test_verification_against_priyas_record():
    doc = handoff(item("a", "q one", counts=dict(GSC)))
    h = priya_handoff.canonical_sha(doc)
    rec = {"run_id": doc["run_id"], "tenant_id": "zbm", "status": "completed", "handoff_sha256": h,
           "handoff_items": 1, "handoff_record": "priya.handoff_record/1"}
    v = priya_handoff.verify
    assert v(doc, "zbm", doc["run_id"], h, rec) == {"state": "VERIFIED", "reason": None}
    assert v(doc, "zbm", None, None, rec)["state"] == "NOT_VERIFIED"
    assert v(doc, "zbm", doc["run_id"], h, None)["reason"] == "PRIYA_RECORD_NOT_FOUND"
    assert v(doc, "zbm", doc["run_id"], h, {**rec, "tenant_id": "acme"})["reason"] == "PRIYA_RECORD_NOT_FOUND"
    assert v(doc, "zbm", doc["run_id"], h, {**rec, "handoff_sha256": None})["reason"] == \
        "PRIYA_RECORD_HAS_NO_HANDOFF_HASH"
    assert v(doc, "zbm", doc["run_id"], h, {**rec, "status": "running"})["reason"] == "PRIYA_RUN_NOT_COMPLETED"
    assert v(doc, "zbm", doc["run_id"], "0" * 64, rec) == {"state": "FAILED", "reason": "DOCUMENT_HASH_MISMATCH"}
    tampered = copy.deepcopy(doc)
    tampered["items"][0]["observed_counts"]["impressions"] = 19
    assert v(tampered, "zbm", doc["run_id"], priya_handoff.canonical_sha(tampered), rec)["reason"] == \
        "RECORD_HASH_MISMATCH"
    assert v(doc, "zbm", "seo-kwr-" + "9" * 40, h, rec)["reason"] == "RUN_ID_MISMATCH"


def test_wrong_schema_untrusted_flag_and_duplicate_keys_are_refused():
    assert priya_handoff.validate({"schema": "priya-emerging-handoff/2"}, "zbm") == ["schema"]
    it = item("a", "q", counts=dict(GSC))
    it["query"]["untrusted"] = False
    assert priya_handoff.validate(handoff(it), "zbm") == ["items[0].query"]
    a = item("a", "q one", counts=dict(GSC))
    assert priya_handoff.validate(handoff(a, dict(a)), "zbm") == ["items[1].key"]
    assert priya_handoff.validate(handoff(item("b", "q two")), "zbm") == ["items[0].volume_status"]   # no counts


def test_verification_rejects_a_record_of_another_contract_run_or_item_count():
    """Review round 9: the record must be Priya's contract, for the claimed run, with a real whole item count."""
    doc = handoff(item("a", "q one", counts=dict(GSC)))
    h = priya_handoff.canonical_sha(doc)
    rec = {"run_id": doc["run_id"], "tenant_id": "zbm", "status": "completed", "handoff_sha256": h,
           "handoff_items": 1, "handoff_record": "priya.handoff_record/1"}
    v = priya_handoff.verify
    assert v(doc, "zbm", doc["run_id"], h, rec)["state"] == "VERIFIED"
    for bad, reason in (({"handoff_record": "priya.handoff_record/2"}, "RECORD_CONTRACT_MISMATCH"),
                        ({"handoff_record": None}, "RECORD_CONTRACT_MISMATCH"),
                        ({"run_id": "seo-kwr-" + "2" * 40}, "RECORD_RUN_ID_MISMATCH"),
                        ({"handoff_items": True}, "RECORD_ITEMS_MISMATCH"),
                        ({"handoff_items": 2}, "RECORD_ITEMS_MISMATCH"),
                        ({"handoff_items": "1"}, "RECORD_ITEMS_MISMATCH")):
        assert v(doc, "zbm", doc["run_id"], h, {**rec, **bad}) == {"state": "FAILED", "reason": reason}, bad
    missing = {k: x for k, x in rec.items() if k != "handoff_items"}
    assert v(doc, "zbm", doc["run_id"], h, missing) == {"state": "FAILED", "reason": "RECORD_ITEMS_MISMATCH"}
    missing = {k: x for k, x in rec.items() if k != "handoff_record"}
    assert v(doc, "zbm", doc["run_id"], h, missing)["reason"] == "RECORD_CONTRACT_MISMATCH"


def test_validation_rejects_extra_top_level_and_query_keys():
    doc = handoff(item("a", "q one", counts=dict(GSC)))
    assert priya_handoff.validate(doc, "zbm") == []
    assert priya_handoff.validate({**doc, "extra": 1}, "zbm") == ["keys"]
    short = {k: x for k, x in doc.items() if k != "rule"}
    assert priya_handoff.validate(short, "zbm") == ["keys"]
    it = item("a", "q one", counts=dict(GSC))
    it["query"]["extra"] = "x"
    assert priya_handoff.validate(handoff(it), "zbm") == ["items[0].query"]
