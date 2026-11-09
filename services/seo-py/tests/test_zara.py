"""Zara — the fireteam lead (ADR 0049): real-time pairing (checked on arrival / at close), the conflict rules, holds
for invalid, contradictory, foreign or duplicate member output, PARTIAL when a member fails (the missing half is never
filled in), NOTHING_OBSERVED, and arrival-order independence under real threads. Pure."""

from __future__ import annotations

import copy
import threading

from agents import fireteam_topics, naomi, theo, zara
from test_naomi_volume import GSC
from test_theo import PS, WIN, Clock, Port, item

RUN = "seo-cnv-" + "4" * 40
PS2 = {**PS, "prompts": ["who recovers amazon reimbursement revenue", "best fba reimbursement agency"]}


def topics(ps=PS2, items=()):
    t = fireteam_topics.plan(ps, list(items))
    return t, {x["topic_key"]: theo.plan_turns(x, list(items)) for x in t}


def chains_for(t, plans, script=WIN, n=3):
    from test_theo import CTX
    res = theo.run_chains(t, plans, {"openai": Port(script)}, ["openai"], n, lambda **k: None, None,
                          theo.CallCap(1000), CTX, Clock())
    return res, {c["topic_key"]: [c] for c in res["chains"]}


def volumes_for(t, items=(), verified=False):
    out = naomi.run(t, list(items), "zbm", RUN, {"state": "UNAVAILABLE"},
                    verification={"state": "VERIFIED" if verified else "NOT_VERIFIED", "reason": None})
    return out, {v["topic_key"]: v for v in out["topics"]}


def box_with(t, plans, res, ch, vout, vol, order=("theo", "naomi")):
    b = zara.Crosscheck("zbm", RUN, t)
    for m in order:
        for x in t:
            b.receive(m, x["topic_key"], ch[x["topic_key"]] if m == "theo" else vol[x["topic_key"]])
        b.member_contract(m, theo.output("zbm", RUN, t, plans, res, 3) if m == "theo" else vout)
        b.member_closed(m, "DONE")
    return b.result()


def test_a_strong_chain_with_thin_volume_is_flagged_and_never_ranked_by_volume():
    its = [item("a", PS2["prompts"][0], counts=dict(GSC))]
    t, plans = topics(items=its)
    res, ch = chains_for(t, plans)
    vout, vol = volumes_for(t, its)
    r = box_with(t, plans, res, ch, vout, vol)
    i = r["items"][0]
    assert i["verdict"] == "FLAGGED" and i["conflicts"] == ["STRONG_CHAIN_THIN_VOLUME", "HANDOFF_NOT_VERIFIED"]
    assert i["downstream"] == {"passes": True, "decision": "INSUFFICIENT_EVIDENCE", "priority_basis": "chain_only"}
    j = r["items"][1]
    assert j["conflicts"] == ["STRONG_CHAIN_UNKNOWN_VOLUME"] and j["volume"]["status"] == "UNKNOWN"
    assert r["checks"]["checked_on_arrival"] == 2 and r["status"] == "OK"


def test_a_cleared_topic_with_verified_moderate_volume_may_be_tested_never_acted_on():
    its = [item("a", PS2["prompts"][0], counts={**GSC, "impressions": 300, "source": "first_party:bing_webmaster"},
                status="MEASURED_NOT_TIERED")]
    t, plans = topics(ps={**PS, "prompts": PS2["prompts"][:1]}, items=its)
    res, ch = chains_for(t, plans)
    vout, vol = volumes_for(t, its, verified=True)
    i = box_with(t, plans, res, ch, vout, vol)["items"][0]
    assert i["verdict"] == "CLEARED" and i["downstream"]["decision"] == "TEST"
    assert i["downstream"]["priority_basis"] == "chain_and_volume"


def test_an_unverified_hand_off_blocks_test_and_volume_priority_even_at_a_huge_count():
    its = [item("a", PS2["prompts"][0],
                counts={**GSC, "impressions": 9_000_000, "source": "first_party:bing_webmaster"},
                status="MEASURED_NOT_TIERED")]
    t, plans = topics(ps={**PS, "prompts": PS2["prompts"][:1]}, items=its)
    res, ch = chains_for(t, plans)
    vout, vol = volumes_for(t, its)                                       # not verified
    v = vol[t[0]["topic_key"]]
    assert v["reliability"] == "THIN" and v["signals"][0]["reliability_if_verified"] == "STRONG"
    i = box_with(t, plans, res, ch, vout, vol)["items"][0]
    assert i["verdict"] == "FLAGGED" and "HANDOFF_NOT_VERIFIED" in i["conflicts"]
    assert i["downstream"] == {"passes": True, "decision": "INSUFFICIENT_EVIDENCE", "priority_basis": "chain_only"}
    forged = copy.deepcopy(vol)                                           # a payload claiming STRONG unverified
    forged[t[0]["topic_key"]].update(reliability="STRONG", confidence="MEDIUM")
    assert box_with(t, plans, res, ch, vout, forged)["items"][0]["hold_reason"] == "NAOMI_SELF_CONTRADICTION"


def test_volume_without_any_complete_chain_is_flagged():
    from answer_adapter import EngineError
    its = [item("a", PS2["prompts"][0], counts=dict(GSC))]
    t, plans = topics(ps={**PS, "prompts": PS2["prompts"][:1]}, items=its)
    res, ch = chains_for(t, plans, script={1: EngineError("ENGINE_UNAVAILABLE")}, n=1)
    vout, vol = volumes_for(t, its)
    i = box_with(t, plans, res, ch, vout, vol)["items"][0]
    assert i["verdict"] == "FLAGGED" and i["conflicts"] == ["VOLUME_WITHOUT_CHAIN", "HANDOFF_NOT_VERIFIED"]
    assert i["downstream"]["priority_basis"] == "none"                     # unverified volume is never a basis
    vout, vol = volumes_for(t, its, verified=True)
    i = box_with(t, plans, res, ch, vout, vol)["items"][0]
    assert i["conflicts"] == ["VOLUME_WITHOUT_CHAIN"] and i["downstream"]["priority_basis"] == "volume_only"


def test_nothing_observed_is_not_cleared():
    from answer_adapter import EngineError
    t, plans = topics(ps={**PS, "prompts": PS2["prompts"][:1]})
    res, ch = chains_for(t, plans, script={1: EngineError("ENGINE_UNAVAILABLE")}, n=1)
    vout, vol = volumes_for(t)
    r = box_with(t, plans, res, ch, vout, vol)
    assert r["items"][0]["verdict"] == "NOTHING_OBSERVED" and r["items"][0]["downstream"]["passes"] is False
    assert r["status"] == "HELD_ALL" and zara.envelope_for(r)["outcome"] == "INSUFFICIENT_EVIDENCE"


def test_one_member_failing_is_partial_and_its_half_is_never_filled_in():
    t, plans = topics()
    res, ch = chains_for(t, plans)
    b = zara.Crosscheck("zbm", RUN, t)
    for x in t:
        b.receive("theo", x["topic_key"], ch[x["topic_key"]])
    b.member_contract("theo", theo.output("zbm", RUN, t, plans, res, 3))
    b.member_closed("theo", "DONE")
    b.member_closed("naomi", "FAILED", "MEMBER_FAILED")
    r = b.result()
    assert r["status"] == "PARTIAL" and r["checks"]["checked_at_close"] == 2
    for i in r["items"]:
        assert i["verdict"] == "PARTIAL" and i["volume"] is None and i["volume_missing"] == "NAOMI_FAILED:MEMBER_FAILED"
        assert i["downstream"] == {"passes": True, "decision": "INSUFFICIENT_EVIDENCE", "priority_basis": "chain_only"}
    env = zara.envelope_for(r)
    assert env["outcome"] == "PARTIAL" and "ZARA_MEMBER_MISSING" in [f["code"] for f in env["findings"]]


def test_both_members_missing_holds_everything():
    t, _ = topics()
    b = zara.Crosscheck("zbm", RUN, t)
    b.member_closed("theo", "KILLED", "KILLED_GLOBAL")
    b.member_closed("naomi", "RESTRICTED", "AGENT_RESTRICTED")
    r = b.result()
    assert {i["hold_reason"] for i in r["items"]} == {"BOTH_MEMBERS_MISSING"} and r["status"] == "HELD_ALL"


def test_contradictory_member_outputs_are_held_never_repaired():
    t, plans = topics()
    res, ch = chains_for(t, plans)
    vout, vol = volumes_for(t)
    k0, k1 = t[0]["topic_key"], t[1]["topic_key"]
    bad_ch = copy.deepcopy(ch)
    bad_ch[k0][0]["complete"] = 99                                        # more complete chains than chains
    bad_vol = copy.deepcopy(vol)
    bad_vol[k1]["band"] = {"low": 0, "high": 0}                           # UNKNOWN with a band
    r = box_with(t, plans, res, bad_ch, vout, bad_vol)
    assert [(i["verdict"], i["hold_reason"]) for i in r["items"]] == [("HELD", "THEO_SELF_CONTRADICTION"),
                                                                      ("HELD", "NAOMI_SELF_CONTRADICTION")]
    assert all(i["chain"] is None and i["volume"] is None for i in r["items"])
    lie = copy.deepcopy(ch)
    lie[k0][0]["pattern"] = "INVISIBLE_THEN_RECOMMENDED"
    lie[k0][0]["by_turn"][0]["client_mentioned"]["k"] = 2                 # but visible at turn one
    hi = copy.deepcopy(vol)
    hi[k1].update(status="DIRECTIONAL_PROXY", reliability="STRONG", confidence="MEDIUM", band={"low": 1, "high": 2},
                  signals=[{"observed_impressions": 5}])                  # STRONG on 5, band without its count
    r = box_with(t, plans, res, lie, vout, hi)
    assert [i["hold_reason"] for i in r["items"]] == ["THEO_NATIVE_PATTERN_WITHOUT_BASIS", "NAOMI_SELF_CONTRADICTION"]
    hi[k1].update(confidence="HIGH")                                      # outside Naomi's vocabulary: never HIGH
    assert box_with(t, plans, res, ch, vout, hi)["items"][1]["hold_reason"] == "NAOMI_PAYLOAD_INVALID"


def test_a_payload_of_another_tenant_or_run_is_held():
    t, plans = topics()
    res, ch = chains_for(t, plans)
    vout, vol = volumes_for(t)
    r = box_with(t, plans, res, ch, {**vout, "tenant_id": "other"}, vol)
    assert {i["hold_reason"] for i in r["items"]} == {"NAOMI_TENANT_OR_RUN_MISMATCH"}
    b = zara.Crosscheck("zbm", RUN, t)
    for x in t:
        b.receive("theo", x["topic_key"], ch[x["topic_key"]])
        b.receive("naomi", x["topic_key"], vol[x["topic_key"]])
    b.member_contract("theo", theo.output("zbm", "seo-cnv-" + "9" * 40, t, plans, res, 3))
    assert {i["hold_reason"] for i in b.result()["items"]} == {"THEO_TENANT_OR_RUN_MISMATCH"}


def test_unplanned_and_duplicate_payloads():
    t, plans = topics()
    _res, ch = chains_for(t, plans)
    _vout, vol = volumes_for(t)
    b = zara.Crosscheck("zbm", RUN, t)
    b.receive("naomi", "tpc-ffffffffffffffff", {"anything": 1})
    for x in t:
        b.receive("theo", x["topic_key"], ch[x["topic_key"]])
        b.receive("naomi", x["topic_key"], vol[x["topic_key"]])
    b.receive("naomi", t[0]["topic_key"], vol[t[0]["topic_key"]])          # a second payload for a checked topic
    r = b.result()
    assert r["unplanned_payloads"] == [{"member": "naomi", "topic_key": "tpc-ffffffffffffffff"}]
    assert r["items"][0]["hold_reason"] == "DUPLICATE_PAYLOAD" and r["items"][1]["verdict"] != "HELD"


def test_engines_disagreeing_and_unstable_chains_are_flagged():
    t, plans = topics(ps={**PS, "prompts": PS2["prompts"][:1]})
    k = t[0]["topic_key"]
    res, ch = chains_for(t, plans)
    lose = copy.deepcopy(ch[k][0])
    lose.update(engine="anthropic", pattern="NOT_RECOMMENDED", client_outcome="NOT_RECOMMENDED",
                final_recommendation={**lose["final_recommendation"],
                                      "recommended": {"client": 0, "competitor": {"RivalCo": 3}}})
    lose["client_recommended"] = {**lose["client_recommended"], "k": 0}
    mixed = copy.deepcopy(ch[k][0])
    mixed.update(engine="google", unstable=True)
    vout, vol = volumes_for(t)
    r = box_with(t, plans, res, {k: ch[k] + [lose, mixed]}, vout, vol)
    assert set(r["items"][0]["conflicts"]) == {"STRONG_CHAIN_UNKNOWN_VOLUME", "UNSTABLE_CHAIN", "ENGINES_DISAGREE"}


def test_the_result_does_not_depend_on_arrival_order_under_threads():
    its = [item("a", PS2["prompts"][0], counts=dict(GSC))]
    t, plans = topics(items=its)
    res, ch = chains_for(t, plans)
    vout, vol = volumes_for(t, its)
    want = box_with(t, plans, res, ch, vout, vol)
    for order in (("naomi", "theo"), ("theo", "naomi")):
        b = zara.Crosscheck("zbm", RUN, t)

        def feed(m, b=b):
            for x in reversed(t) if m == "naomi" else t:
                b.receive(m, x["topic_key"], ch[x["topic_key"]] if m == "theo" else vol[x["topic_key"]])
            b.member_contract(m, theo.output("zbm", RUN, t, plans, res, 3) if m == "theo" else vout)
            b.member_closed(m, "DONE")
        th = [threading.Thread(target=feed, args=(m,)) for m in order]
        for x in th:
            x.start()
        for x in th:
            x.join()
        got = b.result()
        assert got["items"] == want["items"] and got["status"] == want["status"]
        assert got["checks"]["checked_on_arrival"] + got["checks"]["checked_at_close"] == len(t)


def test_priya_topics_feed_back_to_priya_without_query_text():
    its = [item("a", "how do i get amazon to reimburse lost inventory", counts=dict(GSC))]
    t, plans = topics(ps={**PS, "prompts": PS2["prompts"][:1]}, items=its)
    res, ch = chains_for(t, plans)
    vout, vol = volumes_for(t, its)
    r = box_with(t, plans, res, ch, vout, vol)
    fb = r["to_priya"]["items"]
    assert [x["priya_key"] for x in fb] == [its[0]["key"]] and fb[0]["volume_reliability"] == "THIN"
    assert "reimburse" not in repr(r)


def test_a_stacked_chain_claiming_true_multi_turn_invisibility_is_held():
    t, plans = topics(ps={**PS, "prompts": PS2["prompts"][:1]})
    res, ch = chains_for(t, plans)
    k = t[0]["topic_key"]
    assert ch[k][0]["pattern"] == "INVISIBLE_AT_OPENING_RECOMMENDED_STACKED"
    lie = copy.deepcopy(ch)
    lie[k][0]["pattern"] = "INVISIBLE_THEN_RECOMMENDED"
    vout, vol = volumes_for(t)
    assert box_with(t, plans, res, lie, vout, vol)["items"][0]["hold_reason"] == "THEO_NATIVE_PATTERN_WITHOUT_BASIS"
    strong = copy.deepcopy(ch)
    strong[k][0].update(complete=3, strength="STRONG", client_outcome="MIXED", pattern="MIXED")
    assert box_with(t, plans, res, strong, vout, vol)["items"][0]["hold_reason"] == "THEO_SELF_CONTRADICTION"


def test_a_payload_arriving_after_its_member_was_closed_is_never_used():
    t, plans = topics(ps={**PS, "prompts": PS2["prompts"][:1]})
    _res, ch = chains_for(t, plans)
    _vout, vol = volumes_for(t)
    b = zara.Crosscheck("zbm", RUN, t)
    b.receive("naomi", t[0]["topic_key"], vol[t[0]["topic_key"]])
    b.member_closed("naomi", "DONE")
    b.member_closed("theo", "TIMED_OUT", "MEMBER_TIMED_OUT")
    b.receive("theo", t[0]["topic_key"], ch[t[0]["topic_key"]])              # too late
    r = b.result()
    assert r["late_payloads"] == [{"member": "theo", "topic_key": t[0]["topic_key"]}]
    assert r["items"][0]["chain"] is None and r["items"][0]["chain_missing"] == "THEO_TIMED_OUT:MEMBER_TIMED_OUT"


def test_a_partial_item_with_unverified_volume_names_the_blocking_conflict():
    its = [item("a", PS2["prompts"][0], counts=dict(GSC))]
    t, _plans = topics(ps={**PS, "prompts": PS2["prompts"][:1]}, items=its)
    vout, vol = volumes_for(t, its)
    b = zara.Crosscheck("zbm", RUN, t)
    b.receive("naomi", t[0]["topic_key"], vol[t[0]["topic_key"]])
    b.member_contract("naomi", vout)
    b.member_closed("naomi", "DONE")
    b.member_closed("theo", "FAILED", "MEMBER_FAILED")
    i = b.result()["items"][0]
    assert i["verdict"] == "PARTIAL" and i["conflicts"] == ["HANDOFF_NOT_VERIFIED"]
    assert i["downstream"]["priority_basis"] == "none"



# ---------------------------------------------------------------------------------------------- review round 6

def _native_checked_chains(stacked_check=True):
    from test_theo import CTX, NativePort
    t, plans = topics(ps={**PS, "prompts": PS2["prompts"][:1]})
    res = theo.run_chains(t, plans, {"openai": NativePort(WIN)}, ["openai"], 3, lambda **k: None, None,
                          theo.CallCap(1000), {**CTX, "stacked_check": stacked_check}, Clock())
    return t, plans, res, {c["topic_key"]: [c] for c in res["chains"]}


def test_zara_holds_a_native_claim_without_the_structural_basis():
    t, plans, res, ch = _native_checked_chains()
    k = t[0]["topic_key"]
    assert ch[k][0]["pattern"] == "INVISIBLE_THEN_RECOMMENDED"
    assert ch[k][0]["divergent_under_history"]["agreed_clean"] == 3
    vout, vol = volumes_for(t)
    assert box_with(t, plans, res, ch, vout, vol)["items"][0]["verdict"] != "HELD"      # the honest payload passes
    for agreed in (2, 0):                                                   # fewer agreeing chains than complete
        lie = copy.deepcopy(ch)
        lie[k][0]["divergent_under_history"]["agreed_clean"] = agreed
        assert box_with(t, plans, res, lie, vout, vol)["items"][0]["hold_reason"] == \
            "THEO_NATIVE_PATTERN_WITHOUT_BASIS"
    t2, plans2, res2, unchecked = _native_checked_chains(stacked_check=False)
    k2 = t2[0]["topic_key"]
    assert unchecked[k2][0]["pattern"] == "INVISIBLE_AT_OPENING_RECOMMENDED_UNCHECKED"
    lie = copy.deepcopy(unchecked)
    lie[k2][0]["pattern"] = "INVISIBLE_THEN_RECOMMENDED"                   # claims it; the check never ran
    vout2, vol2 = volumes_for(t2)
    assert box_with(t2, plans2, res2, lie, vout2, vol2)["items"][0]["hold_reason"] == \
        "THEO_NATIVE_PATTERN_WITHOUT_BASIS"


def test_zara_holds_impossible_structural_counts():
    t, plans, res, ch = _native_checked_chains()
    k = t[0]["topic_key"]
    vout, vol = volumes_for(t)

    def hold(**dv):
        lie = copy.deepcopy(ch)
        lie[k][0]["divergent_under_history"].update(dv)
        return box_with(t, plans, res, lie, vout, vol)["items"][0]["hold_reason"]
    assert hold(checked=4) == "THEO_STRUCTURAL_CHECKED_EXCEEDS_COMPLETE"            # 4 checked of 3 complete
    assert hold(checked=2, divergent=3, agreed_clean=0) == "THEO_STRUCTURAL_DIVERGENT_EXCEEDS_CHECKED"
    assert hold(checked=3, divergent=1, agreed_clean=3) == "THEO_STRUCTURAL_AGREED_EXCEEDS_UNDIVERGED"


# ---------------------------------------------------------------------------------------------- review round 7

def test_an_agreeing_count_that_includes_flagged_chains_is_held():
    t, plans, res, ch = _native_checked_chains()
    k = t[0]["topic_key"]
    vout, vol = volumes_for(t)
    lie = copy.deepcopy(ch)                                     # agreed_clean 3 and INVISIBLE_THEN_RECOMMENDED kept
    lie[k][0]["injection_suspected"].update(chains=3, complete=3)
    assert box_with(t, plans, res, lie, vout, vol)["items"][0]["hold_reason"] == \
        "THEO_STRUCTURAL_AGREED_INCLUDES_FLAGGED"


def test_an_agreeing_count_that_includes_too_long_chains_is_held():
    t, plans, res, ch = _native_checked_chains()
    k = t[0]["topic_key"]
    vout, vol = volumes_for(t)
    lie = copy.deepcopy(ch)
    lie[k][0]["too_long_to_scan"].update(chains=3, complete=3)
    assert box_with(t, plans, res, lie, vout, vol)["items"][0]["hold_reason"] == \
        "THEO_STRUCTURAL_AGREED_INCLUDES_TOO_LONG"


def test_the_too_long_to_scan_shape_is_validated():
    t, plans, res, ch = _native_checked_chains()
    k = t[0]["topic_key"]
    vout, vol = volumes_for(t)
    for bad in ("garbage", {"chains": -1, "complete": 0}, {"chains": 4, "complete": 0}, {"chains": "3", "complete": 0},
                {"chains": 1, "complete": 2}, {"complete": 0}):
        lie = copy.deepcopy(ch)
        lie[k][0]["too_long_to_scan"] = bad
        assert box_with(t, plans, res, lie, vout, vol)["items"][0]["hold_reason"] == "THEO_PAYLOAD_INVALID", bad
