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
    bad_vol[k1]["band"] = {"low": 0, "high": 0, "interval": "garwood_poisson_95", "method": "x"}   # UNKNOWN + band
    r = box_with(t, plans, res, bad_ch, vout, bad_vol)
    assert [(i["verdict"], i["hold_reason"]) for i in r["items"]] == [("HELD", "THEO_PAYLOAD_INVALID"),
                                                                      ("HELD", "NAOMI_SELF_CONTRADICTION")]
    assert all(i["chain"] is None and i["volume"] is None for i in r["items"])
    lie = copy.deepcopy(ch)
    lie[k0][0]["pattern"] = "INVISIBLE_THEN_RECOMMENDED"
    lie[k0][0]["by_turn"][0]["client_mentioned"] = theo._rate(2, 3)       # but visible at turn one
    hi = copy.deepcopy(vol)
    sig = naomi._signal({**GSC, "impressions": 5}, item("a", "q", counts={**GSC, "impressions": 5}), True)
    hi[k1].update(status="DIRECTIONAL_PROXY", reliability="STRONG", confidence="MEDIUM", verified=True,
                  band={"low": 1, "high": 2, "interval": "garwood_poisson_95", "method": "x"},
                  signals=[sig])                                          # STRONG on 5, band without its count
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
    lose["client_recommended"] = theo._rate(0, 3)
    mixed = copy.deepcopy(ch[k][0])                                       # honestly unstable: 2 client + 1 RivalCo
    mixed.update(engine="google", unstable=True, client_recommended=theo._rate(2, 3),
                 final_recommendation={**mixed["final_recommendation"],
                                       "recommended": {"client": 2, "competitor": {"RivalCo": 1}}})
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



# ---------------------------------------------------------------------------------------------- review round 8

def _hold(field, value):
    t, plans, res, ch = _native_checked_chains()
    k = t[0]["topic_key"]
    vout, vol = volumes_for(t)
    lie = copy.deepcopy(ch)
    if isinstance(value, dict) and value.pop("_merge", False):
        lie[k][0][field].update(value)
    else:
        lie[k][0][field] = value
    return box_with(t, plans, res, lie, vout, vol)["items"][0]["hold_reason"]


def test_flagged_chains_counted_as_incomplete_cannot_hide_inside_agreed():
    # every chain is complete (3 of 3): three flagged-but-"incomplete" chains are impossible
    assert _hold("injection_suspected", {"_merge": True, "chains": 3, "complete": 0}) == \
        "THEO_STRUCTURAL_AGREED_INCLUDES_FLAGGED"
    assert _hold("too_long_to_scan", {"_merge": True, "chains": 3, "complete": 0}) == \
        "THEO_STRUCTURAL_AGREED_INCLUDES_TOO_LONG"


def test_the_flag_shapes_are_strict():
    for field in ("injection_suspected", "too_long_to_scan"):
        assert _hold(field, {"_merge": True, "extra": 1}) == "THEO_PAYLOAD_INVALID", field
        assert _hold(field, {"chains": 0, "complete": 0}) == "THEO_PAYLOAD_INVALID", field      # note missing
        assert _hold(field, {"chains": 0, "note": "x"}) == "THEO_PAYLOAD_INVALID", field         # complete missing
        assert _hold(field, {"_merge": True, "chains": True}) == "THEO_PAYLOAD_INVALID", field   # a bool is no count
        assert _hold(field, {"_merge": True, "chains": 1, "complete": 2}) == "THEO_PAYLOAD_INVALID", field
        assert _hold(field, {"_merge": True, "chains": 4, "complete": 0}) == "THEO_PAYLOAD_INVALID", field


def test_injection_suspected_guards_each_hold():
    assert _hold("injection_suspected", {"chains": 0, "note": "x"}) == "THEO_PAYLOAD_INVALID"       # no KeyError
    assert _hold("injection_suspected", {"_merge": True, "complete": True}) == "THEO_PAYLOAD_INVALID"
    assert _hold("injection_suspected", {"_merge": True, "complete": "x"}) == "THEO_PAYLOAD_INVALID"
    assert _hold("injection_suspected", {"_merge": True, "chains": 1, "complete": 2}) == "THEO_PAYLOAD_INVALID"
    assert _hold("injection_suspected", {"_merge": True, "chains": 4, "complete": 3}) == "THEO_PAYLOAD_INVALID"


# ---------------------------------------------------------------------------------------------- review round 9

def _verified_one():
    its = [item("a", PS2["prompts"][0], counts={**GSC, "impressions": 300, "source": "first_party:bing_webmaster"},
                status="MEASURED_NOT_TIERED")]
    t, plans = topics(ps={**PS, "prompts": PS2["prompts"][:1]}, items=its)
    res, ch = chains_for(t, plans)
    vout, vol = volumes_for(t, its, verified=True)
    return t, plans, res, ch, vout, vol


def _item_after(edit, engines=None, volume_edit=None):
    t, plans, res, ch, vout, vol = _verified_one()
    k = t[0]["topic_key"]
    lie, vl = copy.deepcopy(ch), copy.deepcopy(vol)
    if edit:
        edit(lie[k])
    if volume_edit:
        volume_edit(vl[k])
    b = zara.Crosscheck("zbm", RUN, t, engines)
    for m in ("theo", "naomi"):
        b.receive(m, k, lie[k] if m == "theo" else vl[k])
        b.member_contract(m, theo.output("zbm", RUN, t, plans, res, 3) if m == "theo" else vout)
        b.member_closed(m, "DONE")
    return b.result()["items"][0]


def test_zara_recomputes_stability_and_flags_a_lying_unstable_flag():
    assert _item_after(None)["verdict"] == "CLEARED"                       # the honest payload clears

    def two_winners(unstable, honest_rate=True):
        def e(cs):
            c = cs[0]
            c["final_recommendation"]["recommended"] = {"client": 2, "competitor": {"RivalCo": 1}}
            if honest_rate:
                c["client_recommended"] = theo._rate(2, 3)
            else:
                c["client_recommended"]["k"] = 2                           # the reviewer's repro: value left stale
            c["unstable"] = unstable
        return e
    i = _item_after(two_winners(False))
    assert i["verdict"] == "FLAGGED" and i["conflicts"] == ["UNSTABLE_CHAIN", "UNSTABLE_FLAG_MISMATCH"]
    assert i["downstream"] == {"passes": True, "decision": "INSUFFICIENT_EVIDENCE", "priority_basis": "volume_only"}
    i = _item_after(two_winners(True))                                     # honest flag: unstable, no mismatch
    assert i["verdict"] == "FLAGGED" and i["conflicts"] == ["UNSTABLE_CHAIN"]
    assert i["downstream"]["priority_basis"] == "chain_and_volume"
    i = _item_after(two_winners(False, honest_rate=False))
    assert i["verdict"] == "FLAGGED"
    assert i["conflicts"] == ["UNSTABLE_CHAIN", "UNSTABLE_FLAG_MISMATCH", "RATE_MISMATCH"]

    def claims_unstable(cs):
        cs[0]["unstable"] = True                                           # one winner: instability cannot exist
    i = _item_after(claims_unstable)
    assert i["verdict"] == "FLAGGED" and i["conflicts"] == ["UNSTABLE_FLAG_MISMATCH"]


def test_a_rate_whose_value_disagrees_with_its_counts_is_flagged_never_cleared():
    def stale(cs):
        cs[0]["by_turn"][-1]["client_mentioned"]["value"] = 0.5
    i = _item_after(stale)
    assert i["verdict"] == "FLAGGED" and i["conflicts"] == ["RATE_MISMATCH"]
    assert i["downstream"]["priority_basis"] == "volume_only"
    assert zara.stability({"recommended": {"client": 0, "competitor": {"A": 1}},
                           "named_first_only": {"client": 0, "competitor": {}}, "none": 1, "unknown": 0}) is True
    assert zara.stability({"recommended": {"client": 3, "competitor": {"A": 0}},
                           "named_first_only": {"client": 0, "competitor": {}}, "none": 0, "unknown": 0}) is False


def _set(path, value):
    def e(cs):
        d = cs[0]
        for p in path[:-1]:
            d = d[p]
        d[path[-1]] = value
    return e


def _held(edit, engines=None, volume_edit=None):
    i = _item_after(edit, engines, volume_edit)
    return i["verdict"], i["hold_reason"]


def test_extra_keys_are_rejected_in_every_theo_structure():
    for path in (("divergent_under_history",), ("final_recommendation",), ("by_turn", 0), ("client_recommended",),
                 ("by_turn", 0, "client_mentioned"), ("final_recommendation", "recommended")):
        assert _held(_set(path + ("extra",), 1)) == ("HELD", "THEO_PAYLOAD_INVALID"), path


def test_bools_and_negatives_are_no_counts_even_when_the_sums_balance():
    for path, value in ((("loop_stopped",), True), (("final_recommendation", "none"), True),
                        (("by_turn", 0, "client_mentioned", "k"), True), (("divergent_under_history", "divergent"),
                                                                         False),
                        (("divergent_under_history", "checked"), -1)):
        assert _held(_set(path, value)) == ("HELD", "THEO_PAYLOAD_INVALID"), path

    def balanced(cs):                                 # 4 + (-1) still sums to 3 complete chains
        cs[0]["final_recommendation"]["recommended"]["client"] = 4
        cs[0]["final_recommendation"]["none"] = -1
    assert _held(balanced) == ("HELD", "THEO_PAYLOAD_INVALID")

    def negative_rival(cs):
        cs[0]["final_recommendation"]["recommended"] = {"client": 4, "competitor": {"RivalCo": -1}}
    assert _held(negative_rival) == ("HELD", "THEO_PAYLOAD_INVALID")


def test_the_divergent_under_history_counts_and_every_note_are_checked():
    # each guard on its own (review round 9 mutation survivors): a bool count and a non-string note
    assert _held(_set(("divergent_under_history", "agreed_clean"), False)) == ("HELD", "THEO_PAYLOAD_INVALID")
    assert _held(_set(("divergent_under_history", "checked"), "0")) == ("HELD", "THEO_PAYLOAD_INVALID")
    for field in ("divergent_under_history", "injection_suspected", "too_long_to_scan"):
        for note in (None, 5, ["x"]):
            assert _held(_set((field, "note"), note)) == ("HELD", "THEO_PAYLOAD_INVALID"), (field, note)


def test_engines_must_be_run_engines_and_appear_once():
    assert _item_after(None, engines=["openai"])["verdict"] == "CLEARED"
    assert _held(None, engines=["anthropic"]) == ("HELD", "THEO_PAYLOAD_INVALID")
    assert _held(_set(("engine",), None)) == ("HELD", "THEO_PAYLOAD_INVALID")

    def twice(cs):
        cs.append(copy.deepcopy(cs[0]))
    assert _held(twice) == ("HELD", "THEO_PAYLOAD_INVALID")


def test_loop_stopped_not_complete_and_turns_planned_are_validated():
    for path, value in ((("loop_stopped",), 1), (("not_complete",), {"ENGINE_ERROR": 1}),
                        (("not_complete",), {"ENGINE_ERROR": 0}), (("not_complete",), "x"),
                        (("turns_planned",), 0), (("turns_planned",), theo.MAX_TURNS + 1),
                        (("turns_planned",), True), (("plan_flags",), [1]), (("unstable",), 0)):
        assert _held(_set(path, value)) == ("HELD", "THEO_PAYLOAD_INVALID"), (path, value)


def test_bad_types_are_held_never_crash():
    assert _held(_set(("client_recommended",), "x")) == ("HELD", "THEO_PAYLOAD_INVALID")
    assert _held(_set(("by_turn",), "x")) == ("HELD", "THEO_PAYLOAD_INVALID")
    assert _held(_set(("final_recommendation",), [])) == ("HELD", "THEO_PAYLOAD_INVALID")
    assert _held(None, volume_edit=lambda v: v.update(signals="x")) == ("HELD", "NAOMI_PAYLOAD_INVALID")
    assert _held(None, volume_edit=lambda v: v.update(flags=None)) == ("HELD", "NAOMI_PAYLOAD_INVALID")


def test_naomi_band_and_every_signal_are_strict_and_finite():
    def band(**kw):
        return lambda v: v["band"].update(kw)

    def sig(**kw):
        return lambda v: v["signals"][0].update(kw)
    assert _item_after(None)["verdict"] == "CLEARED"
    for e in (band(extra=1), band(low=float("nan")), band(high=float("inf")), band(low=True),
              sig(extra=1), sig(observed_impressions=True), sig(observed_impressions=-1),
              sig(band={"low": 0, "high": 1}), sig(band_per_day={"low": float("nan"), "high": 1}),
              sig(band_per_day={"low": 0, "high": 1, "x": 2}), sig(reliability="HUGE"),
              sig(reliability_if_verified="HUGE")):
        assert _held(None, volume_edit=e) == ("HELD", "NAOMI_PAYLOAD_INVALID")
