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


def volumes_for(t, items=()):
    out = naomi.run(t, list(items), "zbm", RUN, {"state": "UNAVAILABLE"})
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
    assert i["verdict"] == "FLAGGED" and i["conflicts"] == ["STRONG_CHAIN_THIN_VOLUME"]
    assert i["downstream"] == {"passes": True, "decision": "INSUFFICIENT_EVIDENCE", "priority_basis": "chain_only"}
    j = r["items"][1]
    assert j["conflicts"] == ["STRONG_CHAIN_UNKNOWN_VOLUME"] and j["volume"]["status"] == "UNKNOWN"
    assert r["checks"]["checked_on_arrival"] == 2 and r["status"] == "OK"


def test_a_cleared_topic_with_moderate_volume_may_be_tested_never_acted_on():
    its = [item("a", PS2["prompts"][0], counts={**GSC, "impressions": 300, "source": "bing_webmaster"},
                status="MEASURED_NOT_TIERED")]
    t, plans = topics(ps={**PS, "prompts": PS2["prompts"][:1]}, items=its)
    res, ch = chains_for(t, plans)
    vout, vol = volumes_for(t, its)
    i = box_with(t, plans, res, ch, vout, vol)["items"][0]
    assert i["verdict"] == "CLEARED" and i["downstream"]["decision"] == "TEST"
    assert i["downstream"]["priority_basis"] == "chain_and_volume"


def test_volume_without_any_complete_chain_is_flagged():
    from answer_adapter import EngineError
    its = [item("a", PS2["prompts"][0], counts=dict(GSC))]
    t, plans = topics(ps={**PS, "prompts": PS2["prompts"][:1]}, items=its)
    res, ch = chains_for(t, plans, script={1: EngineError("ENGINE_UNAVAILABLE")}, n=1)
    vout, vol = volumes_for(t, its)
    i = box_with(t, plans, res, ch, vout, vol)["items"][0]
    assert i["verdict"] == "FLAGGED" and i["conflicts"] == ["VOLUME_WITHOUT_CHAIN"]
    assert i["downstream"]["priority_basis"] == "volume_only"


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
    assert [i["hold_reason"] for i in r["items"]] == ["THEO_SELF_CONTRADICTION", "NAOMI_SELF_CONTRADICTION"]
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
    lose.update(engine="anthropic", pattern="NOT_RECOMMENDED",
                final_recommendation={**lose["final_recommendation"], "client": 0, "competitor": {"RivalCo": 3}})
    lose["by_turn"][0]["client_mentioned"]["k"] = 0
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
