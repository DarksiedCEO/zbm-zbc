"""Naomi — Prompt Volume (ADR 0049): the exact Garwood band, the estimator (always a band, never a bare number;
UNKNOWN, never 0, without a signal; confidence never HIGH), and the Priya hand-off mirror (agents/priya_handoff.py)
including its consumer rules (tenant, self-contradicting counts). Pure."""

from __future__ import annotations

import math

from agents import fireteam_topics, naomi, priya_handoff
from test_theo import PS, item

GSC = {"impressions": 7, "clicks": 1, "date_range": {"start": "2026-10-05", "end": "2026-11-01"},
       "source": "search_console"}


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
    lo, hi = naomi.garwood(1000), naomi.garwood(1001)                      # the approximation errs outward
    assert hi["low"] <= lo["low"] + 1 and hi["high"] >= lo["high"]


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


def test_confidence_is_never_high_even_for_a_large_count():
    big = {**GSC, "impressions": 50_000, "clicks": 10, "source": "bing_webmaster"}
    it = item("a", PS["prompts"][0], counts=big, status="MEASURED_NOT_TIERED")
    est = naomi.estimate_topic(_topic([it])[0], it)
    assert est["reliability"] == "STRONG" and est["confidence"] == "MEDIUM" and "BING_WINDOW_DIFFERS" in est["flags"]


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


def test_wrong_schema_untrusted_flag_and_duplicate_keys_are_refused():
    assert priya_handoff.validate({"schema": "priya-emerging-handoff/2"}, "zbm") == ["schema"]
    it = item("a", "q", counts=dict(GSC))
    it["query"]["untrusted"] = False
    assert priya_handoff.validate(handoff(it), "zbm") == ["items[0].query"]
    a = item("a", "q one", counts=dict(GSC))
    assert priya_handoff.validate(handoff(a, dict(a)), "zbm") == ["items[1].key"]
    assert priya_handoff.validate(handoff(item("b", "q two")), "zbm") == ["items[0].volume_status"]   # no counts
