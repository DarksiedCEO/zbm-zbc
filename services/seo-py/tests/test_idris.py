"""Idris — Competitor Visibility agent (ADR 0048): the candidate extractor and the share-of-voice benchmark (pure), the
emerging-competitor rule, and the service (competitor sets, observations recorded from Callum's citation runs, the
reuse rules, dismissals, tenant isolation, kill switches, replay). No network and no key: engines are scripted fakes
or the real adapters on httpx.MockTransport with SYNTHETIC fixtures. No elapsed-time assertion."""

from __future__ import annotations

import json
import os

import pytest

import answer_adapter as aa
import test_answer_engines as te
import test_callum as tc
import test_perplexity as base
from agents import callum, idris
from helpers import rid
from ports import ProviderAnswer

DOMAIN = "site.test"
PS = {"prompt_set_id": "seo-pst-" + "a" * 40, "name": "core", "version": 1, "sha256": "b" * 64,
      "brand_terms": ["Z Best Media"], "competitor_domains": [],
      "prompts": ["who recovers marketplace revenue", "best amazon reimbursement agency"],
      "engines": ["openai", "anthropic"]}
CS = {"version": 1, "sha256": "c" * 64, "emerging_detection": True, "exclude_terms": [],
      "competitors": [{"competitor_id": "cmp-acme", "name": "Acme Recovery", "terms": ["Acme Recovery"],
                       "term_rules": [], "domains": ["acme.test"]},
                      {"competitor_id": "cmp-target", "name": "Target", "terms": ["Target"], "term_rules": [],
                       "domains": []}]}
CID = "seo-cit-" + "c" * 40
CTX = {"client_terms": ["Z Best Media"], "client_label": "site", "tracked_terms": ["Acme Recovery"],
       "exclude_keys": set(), "prompt_words": {"best", "shopify", "reimbursement", "agency"}}


def ans(text, cites=(), status="ANSWER", provider="openai"):
    return ProviderAnswer(status, text, list(cites), "m", "1", cite_from_text=False, provider=provider,
                          asked_at="2026-10-09T18:00:00Z")


def engines(**per_engine):
    """engine -> list of (prompt_index, ProviderAnswer | ("FAILED", code))"""
    out = {}
    for eng, samples in per_engine.items():
        out.update(tc.collected(*samples, engine=eng))
    return out


def observe(eng, cs=CS, ps=PS, run_id=CID, at="2026-10-09T18:00:00Z"):
    return idris.observe(eng, tc.off(), cs, ps, DOMAIN, run_id, at, "2026-10-09T19:00:00Z")


def compute(obs, history=(), dismissals=None, previous=None, outcome="OK"):
    return idris.compute(obs, list(history), dismissals or {}, previous, {"outcome": outcome},
                         "seo-vis-" + "d" * 40, "2026-10-09T18:00:00Z", "fresh")


def scope(env, eng="openai", pi=0):
    return next(s for s in env["facts"]["benchmark"]["by_engine_prompt"] if s["engine"] == eng
                and s["prompt_index"] == pi)


def keys(found):
    return [c["key"] for c in found]


# ---------------------------------------------------------------------------------------------- the extractor

def test_the_extractor_finds_brand_like_names_with_their_signals():
    t = ("Here are options:\n1. **Refund Rangers** - great for sellers.\n2. **Getida** helps with claims.\n"
         "- Seller Labs Inc offers tools.\nThe HubSpot blog covers it.")
    found = idris.candidates(t, CTX)
    assert keys(found) == ["refundrangers", "getida", "sellerlabsinc", "hubspot"]
    sig = {c["key"]: c["signals"] for c in found}
    assert sig["refundrangers"] == ["BOLD", "LIST_HEAD"] and sig["sellerlabsinc"] == ["CORPORATE_SUFFIX", "LIST_HEAD"]
    assert sig["hubspot"] == ["MIXED_CASE"]


@pytest.mark.parametrize("text,reason", [
    ("We use Zoom daily and Monday too.", "GENERIC"),                      # common words, weekdays
    ("California has many agencies.", "SENTENCE_INITIAL"),                  # only ever starts a sentence
    ("Sellers on Shopify often file claims.", "TOPIC_TERM"),                # the prompt's own topic
    ("Threads on Reddit discuss it.", "PUBLISHER"),
    ("I asked ChatGPT and Gemini about it.", "ENGINE_SELF"),
    ("Try Z-Best Media for claims.", "CLIENT_LOOKALIKE"),
    ("Try Z Bеst Media for claims.", "CLIENT_LOOKALIKE"),             # a Cyrillic e
    ("Try Z Bset Media for claims.", "CLIENT_LOOKALIKE"),                   # one edit away
    ("Try Z Best Media Group for claims.", "CLIENT_VARIANT"),
    ("Try Acme Recovery for claims.", "TRACKED"),
    ("## Best Revenue Recovery Agencies In Long Beach\nnothing else", None),
    ("See Best Revenue Recovery Agencies In Long Beach Today for more.", "TITLE_RUN"),
])
def test_the_extractor_excludes_what_is_not_a_new_brand(text, reason):
    ex = {}
    found = idris.candidates(text, CTX, ex)
    assert found == [], found
    if reason:
        assert ex.get(reason), ex


def test_tenant_exclusions_and_a_name_that_recurs_mid_sentence():
    ex = {}
    assert keys(idris.candidates("Getida is fine. Many use Getida.", {**CTX, "exclude_keys": {"getida"}}, ex)) == []
    assert ex == {"EXCLUDED_BY_TENANT": 1}                                  # once per name per answer
    assert keys(idris.candidates("Getida is fine. Many use Getida.", CTX)) == ["getida"]   # not sentence-initial only


def test_the_extractor_is_bounded_per_answer():
    text = " ".join(f"use Brand{i:03d}x now." for i in range(100))
    assert len(idris.candidates(text, CTX)) == idris.MAX_CANDIDATES_PER_ANSWER


# ---------------------------------------------------------------------------------------------- share of voice

def test_presence_mention_share_and_citation_share_are_separate_with_wilson_intervals():
    e = engines(openai=[
        (0, ans("Z Best Media and Acme Recovery both help.", ["https://site.test/a", "https://acme.test/b"])),
        (0, ans("Acme Recovery is popular.", ["https://acme.test/c"])),
        (0, ans("Acme Recovery or Z Best Media.", ["https://acme.test/d"])),
        (0, ans("Nobody in particular.", ["https://directory.test/"]))])
    s = scope(compute(observe(e)))
    assert s["usable_answers"] == 4
    c, a = s["brands"]["client"], s["brands"]["cmp-acme"]
    assert (c["presence"]["k"], c["presence"]["n"]) == (2, 4) and c["presence"]["ci95"] == idris.wilson(2, 4)
    assert (a["presence"]["k"], a["presence"]["n"]) == (3, 4)
    assert (c["mention_share"]["k"], c["mention_share"]["n"]) == (2, 5)       # brand-answer pairs: 2 + 3
    assert c["mention_share"]["value"] + a["mention_share"]["value"] == pytest.approx(1.0)
    assert "not independent" in c["mention_share"]["note"]
    assert (c["citation_share"]["k"], c["citation_share"]["n"]) == (1, 4)    # tracked-domain citations: 1 + 3
    assert (a["citation_share"]["k"], a["citation_share"]["n"]) == (3, 4)
    assert c["provenance"]["citation_run_id"] == CID and c["provenance"]["engine"] == "openai"


def test_unknown_never_zero_when_answers_are_too_few_or_no_brand_is_named():
    few = scope(compute(observe(engines(openai=[(0, ans("Z Best Media helps.")), (0, ans("Acme Recovery."))]))))
    for b in few["brands"].values():
        assert b["presence"]["value"] is None and b["presence"]["unknown"] == "INSUFFICIENT_ANSWERS"
        assert b["mention_share"]["value"] is None and b["citation_share"]["value"] is None
    none = scope(compute(observe(engines(openai=[(0, ans("No names here."))] * 3))))
    c = none["brands"]["client"]
    assert c["presence"]["value"] == 0.0                                       # a measured zero presence
    assert c["mention_share"]["value"] is None and c["mention_share"]["unknown"] == "NO_BRAND_MENTIONED"
    assert c["citation_share"]["unknown"] == "NO_TRACKED_BRAND_CITED"
    # a brand that is simply not named while others are: a measured 0% share with its interval
    some = scope(compute(observe(engines(openai=[(0, ans("Acme Recovery helps."))] * 3))))
    assert some["brands"]["client"]["mention_share"]["value"] == 0.0
    assert some["brands"]["client"]["mention_share"]["ci95"] == idris.wilson(0, 3)


def test_a_competitor_named_in_a_negative_context_is_still_a_mention_with_the_cue_reported():
    e = engines(openai=[(0, ans("Z Best Media is a reliable choice for sellers who need help. Avoid Acme Recovery, "
                                "many complaints."))] * 3)
    obs = observe(e)
    s = scope(compute(obs))
    a = s["brands"]["cmp-acme"]
    assert a["presence"]["k"] == 3 and a["negative_cue_mentions"] == 3           # presence, never subtracted
    assert s["brands"]["client"]["negative_cue_mentions"] == 0
    row = next(r for r in obs["rows"] if r["brand_id"] == "cmp-acme")
    assert row["negative_cue"] is True and row["confidence"] == "HIGH"


def test_a_common_word_competitor_name_needs_confirmation_like_a_client_brand():
    e = engines(openai=[(0, ans("Target the right audience. Z Best Media helps."))] * 3)
    s = scope(compute(observe(e)))
    t = s["brands"]["cmp-target"]
    assert t["presence"]["k"] == 0 and t["low_confidence_mentions"] == 3
    assert t["presence"]["value"] is None and t["presence"]["unknown"] == "MENTIONS_UNCONFIRMED"
    assert idris.set_warnings(CS)[0]["term"] == "Target"
    ruled = {**CS, "competitors": [CS["competitors"][0],
                                   {**CS["competitors"][1], "term_rules": [
                                       {"term": "Target", "case_sensitive": True, "context_terms": ["store"],
                                        "negative_terms": [], "common_word": None}]}]}
    s2 = scope(compute(observe(engines(openai=[(0, ans("Target store returns. Z Best Media."))] * 3), cs=ruled)))
    assert s2["brands"]["cmp-target"]["presence"]["k"] == 3 and idris.set_warnings(ruled) == []


def test_a_client_lookalike_configured_as_a_competitor_is_excluded_at_run_time():
    cs = {**CS, "competitors": [{"competitor_id": "cmp-fake", "name": "ZBest Media", "terms": ["ZBest Media"],
                                 "term_rules": [], "domains": []},
                                {"competitor_id": "cmp-dom", "name": "Other", "terms": ["Otherco"], "term_rules": [],
                                 "domains": ["sіte.test"]}]}
    env = compute(observe(engines(openai=[(0, ans("ZBest Media and Z Best Media."))] * 3), cs=cs))
    assert {x["brand_id"]: x["excluded"] for x in env["facts"]["excluded_tracked"]} == {
        "cmp-fake": "CLIENT_LOOKALIKE", "cmp-dom": "CLIENT_DOMAIN"}
    assert set(scope(env)["brands"]) == {"client"}
    assert any(f["code"] == "IDRIS_TRACKED_COMPETITOR_EXCLUDED" for f in env["findings"])


def test_citations_lookalike_unresolved_and_publisher_hosts_are_never_a_brands():
    e = engines(openai=[(0, ans("Z Best Media and Acme Recovery.",
                                ["https://sіte.test/", "https://acme.test.evil.test/",
                                 "https://vertexaisearch.cloud.google.com/grounding-api-redirect/X",
                                 "https://www.reddit.com/r/x"]))] * 3)
    s = scope(compute(observe(e)))
    assert s["client_lookalike_citations"] == 3 and s["unresolved_citations"] == 3 and s["publisher_citations"] == 3
    c = s["brands"]["client"]["citation_share"]
    assert c["value"] is None and c["unknown"] == "CITATIONS_UNRESOLVED"
    assert s["brands"]["cmp-acme"]["cited_answers"] == 0                 # acme.test.evil.test is not acme.test


def test_failed_engines_are_inconclusive_rows_and_unknown_never_zero():
    e = engines(openai=[(0, ans("Z Best Media.")), (0, ans("Z Best Media.")), (0, ans("Acme Recovery."))],
                anthropic=[(0, ("FAILED", "ENGINE_UNAVAILABLE")), (0, ("FAILED", "ENGINE_RATE_LIMITED"))])
    e["anthropic"]["outcome"] = "FAILED"
    obs = observe(e)
    env = compute(obs, outcome="PARTIAL")
    ant = scope(env, "anthropic")
    assert ant["usable_answers"] == 0 and ant["inconclusive_calls"] == 2
    assert all(b["presence"]["value"] is None for b in ant["brands"].values())
    assert [r["class"] for r in obs["rows"] if r["engine"] == "anthropic"] == ["INCONCLUSIVE", "INCONCLUSIVE"]
    assert any(f["code"] == "ENGINE_RUN_STOPPED" and f["detail"]["engine"] == "anthropic" for f in env["findings"])
    assert env["outcome"] == "PARTIAL" and scope(env)["brands"]["client"]["presence"]["k"] == 2


def test_every_row_has_engine_model_prompt_time_and_an_evidence_pointer():
    obs = observe(engines(openai=[(0, ans("Z Best Media and Acme Recovery.", ["https://acme.test/x"]))] * 3))
    assert obs["rows"]
    for r in obs["rows"]:
        assert r["engine"] == "openai" and r["model"] == "m" and r["prompt_id"] == f"{PS['prompt_set_id']}:v1:0"
        assert r["observed_at"] and r["provider_asked_at"] and r["refresh_tier"] == "near_real_time"
        ev = r["evidence"]
        assert ev["citation_run_id"] == CID and ev["sample"] == r["sample"] and len(ev["answer_sha256"]) == 64
    assert "Z Best Media" not in json.dumps(obs["rows"])                    # pointers and counts, never the text


# ---------------------------------------------------------------------------------------------- emerging competitors

LIST = "Options:\n1. **Refund Rangers** - claims.\n2. Z Best Media also helps."


def test_an_emerging_competitor_needs_answers_spread_and_a_brand_signal_then_is_folded_in():
    one = observe(engines(openai=[(0, ans(LIST))] * 2))
    em = compute(one)["facts"]["emerging"]
    assert em["items"] == [] and em["watch"][0]["key"] == "refundrangers" and "answers" in em["watch"][0]["missing"]
    single_engine = observe(engines(openai=[(0, ans(LIST))] * 3))
    em = compute(single_engine)["facts"]["emerging"]
    assert em["items"] == [] and em["watch"][0]["missing"] == ["spread"]
    both = observe(engines(openai=[(0, ans(LIST))] * 2, anthropic=[(0, ans(LIST))]))
    env = compute(both)
    it = env["facts"]["emerging"]["items"][0]
    assert (it["key"], it["state"], it["confidence"], it["folded_into_benchmark"]) == \
        ("refundrangers", "EMERGING_COMPETITOR", "MEDIUM", True)
    assert it["co_mentioned_with_client"] == 3 and it["evidence"][0]["excerpt"]["untrusted"] is True
    s = scope(env)
    assert s["brands"]["emg:refundrangers"]["presence"]["k"] == 2
    assert s["brands"]["emg:refundrangers"]["citation_share"]["unknown"] == "NO_VERIFIED_DOMAIN"
    assert any(f["code"] == "IDRIS_EMERGING_COMPETITOR" and f["data_class"] == "inferred" for f in env["findings"])
    assert any(r["brand_id"] == "emg:refundrangers" and r["evidence"]["citation_run_id"] == CID
               for r in env["facts"]["rows"])


def test_recurrence_across_runs_and_a_domain_hint_raise_confidence():
    earlier = observe(engines(openai=[(0, ans(LIST, ["https://refundrangers.test/"]))] * 3),
                      run_id="seo-cit-" + "1" * 40, at="2026-10-08T18:00:00Z")
    now = observe(engines(openai=[(0, ans(LIST, ["https://refundrangers.test/"]))] * 2,
                          anthropic=[(0, ans(LIST))]))
    it = compute(now, history=[earlier])["facts"]["emerging"]["items"][0]
    assert it["runs"] == 2 and it["answers"] == 6 and "DOMAIN_HINT" in it["signals"] and it["confidence"] == "HIGH"
    assert it["domain_hints_unverified"] == ["refundrangers.test"] and it["first_seen_at"] == "2026-10-08T18:00:00Z"


def test_a_candidate_without_a_brand_signal_stays_on_watch():
    text = "Sellers in Pasadena and Z Best Media. Pasadena is busy."
    env = compute(observe(engines(openai=[(0, ans(text))] * 3, anthropic=[(0, ans(text))] * 3)))
    em = env["facts"]["emerging"]
    assert em["items"] == [] and em["watch"][0]["key"] == "pasadena" and em["watch"][0]["missing"] == ["brand_signal"]
    assert "emg:pasadena" not in scope(env)["brands"]


def test_a_dismissed_competitor_that_reappears_is_listed_never_flagged_or_folded():
    obs = observe(engines(openai=[(0, ans(LIST))] * 2, anthropic=[(0, ans(LIST))]))
    env = compute(obs, dismissals={"refundrangers": {"reason": "NOT_A_COMPETITOR", "by": "hub", "at": "x"}})
    em = env["facts"]["emerging"]
    assert em["items"] == [] and em["watch"] == []
    assert em["dismissed_seen"][0]["key"] == "refundrangers" and em["dismissed_seen"][0]["answers_this_run"] == 3
    assert "emg:refundrangers" not in scope(env)["brands"]
    assert not any(f["code"] == "IDRIS_EMERGING_COMPETITOR" for f in env["findings"])


def test_at_most_ten_emerging_competitors_are_folded_in():
    text = "\n".join(f"- **Brand{c}co** helps." for c in "ABCDEFGHIJKL")
    env = compute(observe(engines(openai=[(0, ans(text))] * 2, anthropic=[(0, ans(text))])))
    items = env["facts"]["emerging"]["items"]
    assert len(items) == 12 and sum(i["folded_into_benchmark"] for i in items) == idris.MAX_EMERGING_FOLDED
    assert len([b for b in env["facts"]["brands"] if b["origin"] == "emerging"]) == idris.MAX_EMERGING_FOLDED


def test_client_lookalikes_named_in_answers_are_reported_apart():
    env = compute(observe(engines(openai=[(0, ans("Try Z-Best Media or Z Best Media."))] * 3)))
    assert env["facts"]["client_lookalikes_seen"] == [{"key": "zbestmedia", "display": "Z-Best Media", "answers": 3}]
    assert any(f["code"] == "IDRIS_CLIENT_LOOKALIKE_NAMED" for f in env["findings"])


# ---------------------------------------------------------------------------------------------- opportunity framing

def test_gaps_are_framed_as_opportunities_and_a_closing_gap_is_reported():
    first = compute(observe(engines(openai=[(0, ans("Acme Recovery helps."))] * 4 + [(0, ans("Z Best Media."))])))
    o = first["facts"]["opportunities"]["items"][0]
    assert o["leader"]["brand_id"] == "cmp-acme" and o["gap_answers"] == 3 and o["trend"] == "NO_PREVIOUS"
    assert o["headline"].startswith("Room to grow: Acme Recovery appears in 4 of 5 openai answers")
    second = compute(observe(engines(openai=[(0, ans("Acme Recovery helps."))] * 3 +
                                     [(0, ans("Z Best Media."))] * 2), run_id="seo-cit-" + "2" * 40),
                     previous=first["facts"])
    o2 = second["facts"]["opportunities"]["items"][0]
    assert o2["trend"] == "GAP_CLOSING" and o2["previous_gap"] == o["gap"] and o2["within_noise"] is True
    assert "closing" in o2["headline"]
    g = next(f for f in second["findings"] if f["code"] == "IDRIS_GAP_CLOSING")
    assert g["decision"] == "INSUFFICIENT_EVIDENCE"                          # within the noise: never claimed
    third = compute(observe(engines(openai=[(0, ans("Z Best Media."))] * 5), run_id="seo-cit-" + "3" * 40),
                    previous=second["facts"])
    st = third["facts"]["strengths"][0]
    assert st["trend"] == "GAP_CLOSED" and st["headline"].startswith("You lead")


# ---------------------------------------------------------------------------------------------- the service

class Scripted(tc.Scripted):
    def __init__(self, *script):
        super().__init__(*script)


ANSWER = ans("Options:\n1. **Refund Rangers** - claims.\n2. Z Best Media helps. Acme Recovery too.",
             ["https://site.test/x", "https://acme.test/y"])


def svc(tmp_path, data_dir=None, **engines_):
    h, _ = tc.service(tmp_path, data_dir=data_dir)
    fakes = {}
    for name, script in (engines_ or {"openai": [ANSWER], "anthropic": [ANSWER]}).items():
        fakes[name] = h.ports.engines[name] = Scripted(*script)
    psid = tc.prompt_set(h, engines=tuple(sorted(fakes)))
    return h, fakes, psid


def comp_set(h, tid="zbm", competitors=None, caller="seo_agent", **extra):
    return h.post(f"/tenants/{tid}/competitor-sets", {"request_id": rid(), "competitors": competitors if
                                                      competitors is not None else
                                                      [{"name": "Acme Recovery", "domains": ["acme.test"]}],
                                                      **extra}, caller=caller)


def vis(h, psid, tid="zbm", caller="seo_agent", andre=False, domain=DOMAIN, **extra):
    return h.post(f"/tenants/{tid}/competitor-visibility-runs", {"request_id": rid(), "domain": domain,
                                                                 "prompt_set_id": psid, **extra},
                  caller=caller, andre=andre)


def asked(fakes):
    return sum(f.asked for f in fakes.values())


def test_a_fresh_run_reads_callums_answers_and_records_first(tmp_path):
    h, fakes, psid = svc(tmp_path)
    h.ok(comp_set(h), 201)
    v = h.ok(vis(h, psid), 201)
    assert v["source"] == "fresh" and asked(fakes) == 6 and v["result"]["agent"] == "idris"
    res = v["result"]
    assert res["outcome"] == "OK" and res["facts"]["contract"] == "idris.share_of_voice/1"
    cr = h.ok(h.get(f"/tenants/zbm/citation-runs/{v['citation_run_id']}"))
    assert cr["status"] == "completed" and cr["result"]["agent"] == "callum"          # one collection, both agents
    kinds = [e["event_type"] for e in h.ledger.events if e["event_type"] != "log_anchor"]
    assert kinds.index("citation_run_requested") < kinds.index("citation_run_recorded") \
        < kinds.index("competitor_observations_recorded") < kinds.index("competitor_visibility_recorded")
    tot = res["facts"]["benchmark"]["totals"]["brands"]
    assert tot["client"]["presence"]["k"] == 6 and tot[idris.competitor_id("Acme Recovery")]["presence"]["k"] == 6


def test_reuse_rules_no_engine_is_asked_again_for_a_recent_matching_run(tmp_path):
    h, fakes, psid = svc(tmp_path)
    h.ok(comp_set(h), 201)
    first = h.ok(vis(h, psid), 201)
    n = asked(fakes)
    again = h.ok(vis(h, psid), 201)
    assert again["source"] == "reused" and again["citation_run_id"] == first["citation_run_id"] and asked(fakes) == n
    narrow = h.ok(vis(h, psid, engines=["openai"]), 201)                   # narrowing reuses, restricted to openai
    assert narrow["source"] == "reused" and set(narrow["result"]["facts"]["benchmark"]["by_engine"]) == {"openai"}
    assert h.ok(vis(h, psid, max_age_minutes=0), 201)["source"] == "fresh"           # 0 = always fresh
    n = asked(fakes)
    h.clock.advance(minutes=61)                                            # past the near-real-time window
    assert h.ok(vis(h, psid), 201)["source"] == "fresh" and asked(fakes) == n + 6
    n = asked(fakes)
    h.ok(comp_set(h, competitors=[{"name": "Acme Recovery", "domains": ["acme.test"]}, {"name": "Getida"}]), 201)
    assert h.ok(vis(h, psid), 201)["source"] == "fresh" and asked(fakes) == n + 6   # a new set needs new answers
    # a citation run Callum ran on its own also records Idris's observations, so it is reusable
    n = asked(fakes)
    h.clock.advance(minutes=61)
    cr = h.ok(tc.run(h, psid), 201)
    v = h.ok(vis(h, psid), 201)
    assert v["source"] == "reused" and v["citation_run_id"] == cr["citation_run_id"] and asked(fakes) == n + 6


def test_no_competitor_set_means_callum_is_unchanged_and_idris_refuses(tmp_path):
    h, fakes, psid = svc(tmp_path)
    h.ok(tc.run(h, psid), 201)
    assert h.ledger.of_type("competitor_observations_recorded") == [] and h.svc.competitor_observations == {}
    h.refused(vis(h, psid), 404, "COMPETITOR_SET_NOT_FOUND")
    h.refused(h.get("/tenants/zbm/competitor-set"), 404, "COMPETITOR_SET_NOT_FOUND")


def test_the_competitor_set_limits_and_the_client_is_never_a_competitor(tmp_path):
    h, fakes, psid = svc(tmp_path)
    twenty = [{"name": f"Rival {i:02d}"} for i in range(20)]
    h.ok(comp_set(h, competitors=twenty), 201)
    h.refused(comp_set(h, competitors=twenty + [{"name": "Rival 20"}]), 422, "COMPETITOR_LIMIT")
    for bad in ([{"name": "Z-Best Media"}], [{"name": "Z Bеst Media"}], [{"name": "Lookalike", "domains": [
            "blog.site.test"]}], [{"name": "Parent", "domains": ["xn--ste-jhd.test"]}]):
        h.refused(comp_set(h, competitors=bad), 422, "COMPETITOR_IS_CLIENT")
    for bad in ([{"name": "Acme"}, {"name": "ACME"}], [{"name": "Acme", "term_rules": [{"term": "Other"}]}],
                [{"name": "Acme", "domains": ["co.uk"]}]):
        h.refused(comp_set(h, competitors=bad), 422, "COMPETITOR_SET_INVALID")
    view = h.ok(h.get("/tenants/zbm/competitor-set"))
    assert view["current"] == 1 and view["limits"] == {"tracked_max": 20, "emerging_folded_max": 10}
    h.refused(comp_set(h, competitors=twenty), 409, "COMPETITOR_SET_EXISTS")


def test_common_word_competitor_terms_warn_on_the_set(tmp_path):
    h, fakes, psid = svc(tmp_path)
    v = h.ok(comp_set(h, competitors=[{"name": "Target"}]), 201)
    assert v["warnings"][0]["code"] == "BRAND_TERM_MAY_BE_COMMON_WORD" and v["warnings"][0]["term"] == "Target"


def test_dismissal_by_owner_or_tenant_and_a_dismissed_candidate_reappearing(tmp_path):
    h, fakes, psid = svc(tmp_path)
    h.ok(comp_set(h), 201)
    v = h.ok(vis(h, psid), 201)
    assert [i["key"] for i in v["result"]["facts"]["emerging"]["items"]] == ["refundrangers"]
    h.refused(h.post("/tenants/zbm/competitor-set/dismissals", {"request_id": rid(), "key": "neverseen",
                                                                "reason": "OTHER"}), 404, "CANDIDATE_NOT_FOUND")
    h.tenant("acme", domains=("other.test",))
    h.refused(h.post("/tenants/zbm/competitor-set/dismissals", {"request_id": rid(), "key": "refundrangers",
                                                                "reason": "OTHER"}, caller="hub", tenant="acme"),
              404, "TENANT_NOT_FOUND")                                     # never another tenant's
    d = h.ok(h.post("/tenants/zbm/competitor-set/dismissals", {"request_id": rid(), "key": "refundrangers",
                                                               "reason": "NOT_A_COMPETITOR"}, caller="hub",
                    tenant="zbm"), 201)
    assert d["dismissed"]["by"] == "hub" and d["dismissed"]["reason"] == "NOT_A_COMPETITOR"
    h.refused(h.post("/tenants/zbm/competitor-set/dismissals", {"request_id": rid(), "key": "refundrangers",
                                                                "reason": "OTHER"}, caller="dashboard"),
              409, "CANDIDATE_ALREADY_DISMISSED")
    h.clock.advance(minutes=61)
    again = h.ok(vis(h, psid), 201)
    em = again["result"]["facts"]["emerging"]
    assert again["source"] == "fresh" and em["items"] == []
    assert em["dismissed_seen"][0]["key"] == "refundrangers" and em["dismissed_seen"][0]["answers_this_run"] == 6
    assert h.ok(h.get("/tenants/zbm/competitor-set"))["dismissals"][0]["key"] == "refundrangers"


def test_tenant_isolation_and_authorisation(tmp_path):
    h, fakes, psid = svc(tmp_path)
    h.ok(comp_set(h), 201)
    v = h.ok(vis(h, psid), 201)
    vid = v["visibility_run_id"]
    h.tenant("acme", domains=("other.test",))
    missing = h.get(f"/tenants/acme/competitor-visibility-runs/{'seo-vis-' + '0' * 40}")
    other = h.get(f"/tenants/acme/competitor-visibility-runs/{vid}")
    assert other.status_code == missing.status_code == 404 and other.json() == missing.json()
    assert h.get(f"/tenants/zbm/competitor-visibility-runs/{vid}", caller="hub", tenant="acme").status_code == 404
    assert h.ok(h.get(f"/tenants/zbm/competitor-visibility-runs/{vid}", caller="hub", tenant="zbm"))[
        "visibility_run_id"] == vid
    assert h.ok(h.get("/tenants/acme/competitor-visibility-runs")) == []
    h.refused(h.get("/tenants/acme/competitor-set"), 404, "COMPETITOR_SET_NOT_FOUND")
    assert h.post("/tenants/zbm/competitor-visibility-runs", {"request_id": rid(), "domain": DOMAIN,
                                                              "prompt_set_id": psid}, caller="hub",
                  tenant="zbm").status_code == 403                         # the hub never starts a run
    assert comp_set(h, caller="hub").status_code == 403
    h.refused(vis(h, psid, domain="other.test"), 403, "DOMAIN_NOT_AUTHORIZED")
    h.ok(comp_set(h, tid="acme"), 201)
    h.refused(vis(h, psid, tid="acme", domain="other.test"), 404, "PROMPT_SET_NOT_FOUND")
    acme_ps = tc.prompt_set(h, tid="acme", engines=("openai",))
    h.refused(vis(h, acme_ps, tid="acme", domain="other.test"), 403, "ANDRE_APPROVAL_REQUIRED")   # Callum's rules


def test_a_restricted_idris_is_refused_and_callum_runs_without_observations(tmp_path):
    h, fakes, psid = svc(tmp_path)
    h.ok(comp_set(h), 201)
    h.ok(h.post("/agents/idris/lifecycle", {"request_id": rid(), "to": "restricted", "reason": "QUALITY_REVIEW",
                                            "expected_state": "active"}, caller="dashboard", andre=True))
    h.refused(vis(h, psid), 403, "AGENT_RESTRICTED")
    h.ok(tc.run(h, psid), 201)
    assert h.svc.competitor_observations == {}


def test_kill_switches_refuse_the_run(tmp_path):
    h, fakes, psid = svc(tmp_path)
    h.ok(comp_set(h), 201)
    for name in ("capability:ai_probe", "write", "tenant:zbm", "global"):
        h.ok(h.switch(name))
        assert vis(h, psid).status_code == 403 and asked(fakes) == 0
        h.ok(h.switch(name, engaged=False, andre=True))

    def engage(*_a, **_k):
        h.svc.switches["capability"]["ai_probe"] = True
        return ANSWER
    fakes["openai"].script = [engage]
    fakes["openai"].ask = lambda prompt, guard=None, meter=None: engage()
    v = h.post(f"/tenants/zbm/competitor-visibility-runs", {"request_id": rid(), "domain": DOMAIN,
                                                             "prompt_set_id": psid})
    assert v.status_code == 403                                            # the citation run was interrupted
    h.svc.switches["capability"]["ai_probe"] = False
    cr = h.ok(h.get("/tenants/zbm/citation-runs"))[-1]
    assert cr["status"] == "interrupted" and h.svc.competitor_observations == {}


def test_engines_failing_end_to_end_are_unknown_never_zero(tmp_path):
    h, fakes, psid = svc(tmp_path, openai=[ANSWER], anthropic=[aa.EngineError("ENGINE_AUTH_REFUSED")])
    h.ok(comp_set(h), 201)
    v = h.ok(vis(h, psid), 201)
    res = v["result"]
    assert res["outcome"] == "PARTIAL"
    ant = res["facts"]["benchmark"]["by_engine"]["anthropic"]
    assert ant["usable_answers"] == 0 and all(b["presence"]["value"] is None for b in ant["brands"].values())
    assert any(f["code"] == "ENGINE_RUN_STOPPED" for f in res["findings"])


def test_an_observer_failure_never_loses_callums_run(tmp_path, monkeypatch):
    h, fakes, psid = svc(tmp_path)
    h.ok(comp_set(h), 201)
    monkeypatch.setattr(idris, "observe", lambda *a, **k: 1 / 0)
    v = h.ok(vis(h, psid), 201)
    assert v["result"]["outcome"] == "INSUFFICIENT_EVIDENCE" and v["result"]["reason"] == "IDRIS_OBSERVATION_FAILED"
    assert h.ok(h.get(f"/tenants/zbm/citation-runs/{v['citation_run_id']}"))["status"] == "completed"
    failed = h.svc.competitor_observations[v["citation_run_id"]]["failed"]
    assert failed["error_type"] == "ZeroDivisionError" and "idris" in failed["where"]


def test_replay_idempotency_and_no_answer_text_in_the_ledger_or_data_directory(tmp_path):
    h, fakes, psid = svc(tmp_path, data_dir=str(tmp_path / "data"),
                         openai=[ans("Z Best Media and Acme Recovery. " + "filler " * 60 + "tailmarker")])
    h.ok(comp_set(h), 201)
    body = {"request_id": rid(), "domain": DOMAIN, "prompt_set_id": psid}
    a = h.ok(h.post("/tenants/zbm/competitor-visibility-runs", body), 201)
    b = h.ok(h.post("/tenants/zbm/competitor-visibility-runs", body), 201)
    assert a == b and asked(fakes) == 3
    h.refused(h.post("/tenants/zbm/competitor-visibility-runs", {**body, "max_age_minutes": 5}), 409,
              "REQUEST_ID_REUSED")
    h2 = h.restart()
    again = h2.ok(h2.get(f"/tenants/zbm/competitor-visibility-runs/{a['visibility_run_id']}"))
    assert again["result_sha256"] == a["result_sha256"] and again["result"] == a["result"]
    assert h2.svc.competitor_observations.keys() == h.svc.competitor_observations.keys()
    blob = ""
    for root, _d, files in os.walk(tmp_path / "data"):
        for f in files:
            blob += open(os.path.join(root, f), "rb").read().decode("utf-8", "replace")
    assert "tailmarker" not in blob                                      # whole answers are never stored
    led = json.dumps([e["_payload"] for e in h.ledger.events])
    assert "Acme Recovery" not in led and "Z Best Media" not in led


def test_the_registry_lists_idris_with_one_job_inputs_and_outputs(tmp_path):
    h, fakes, psid = svc(tmp_path)
    d = h.ok(h.get("/department"))
    reg = d["agents"]["idris"]["registry"]
    assert reg["assignment"].startswith("competitor visibility") and reg["inputs"] and reg["outputs"]
    assert "idris" not in d["not_built_agents"]
    assert d["agents"]["idris"]["scorecard"]["visibility_runs"] == {"runs": 0, "reused": 0, "fresh": 0,
                                                                    "outcomes": {}}
    assert "visibility_runs" not in d["agents"]["callum"]["scorecard"]


def test_the_real_adapters_answers_are_read_the_same_way(tmp_path):
    h, fakes = tc.service(tmp_path, scripts={"openai": [base.ok(te.fixture("openai"))],
                                             "anthropic": [base.ok(te.fixture("anthropic"))],
                                             "google": [base.ok(te.fixture("google"))]})
    psid = tc.prompt_set(h)
    h.ok(comp_set(h, competitors=[{"name": "Competitor", "terms": ["Competitor Co"], "domains": ["competitor.test"]}]),
         201)
    v = h.ok(vis(h, psid), 201)
    res = v["result"]
    assert res["outcome"] == "OK" and all(len(f.requests) == 3 for f in fakes.values())
    by = res["facts"]["benchmark"]["by_engine"]
    assert by["openai"]["brands"]["client"]["presence"]["k"] == 3
    assert by["google"]["unresolved_citations"] > 0                         # Gemini redirects: never a brand's
