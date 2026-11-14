"""SPEC section 10: Osei drift (B-DR-01..08), against the rule-order reference of SPEC 10 (oracles via the
generator's ground truth: each pair is built so that one rule's evidence holds)."""

from __future__ import annotations

import copy

import pytest

from agents import drift
from common import check_ran, rng

U = "http://site.test/"


def f(code, url=U, sev="low", dec="ACT", **detail):
    return {"code": code, "url": url, "severity": sev, "decision": dec, "data_class": "measured", "detail": detail}


def env(agent, outcome="OK", findings=(), facts=None):
    return {"agent": agent, "task": "x", "outcome": outcome, "findings": list(findings), "facts": facts or {}}


def report(aid, envs, pages=None, versions=None, key_id="k1"):
    return {"audit_id": aid, "tenant_id": "zbm", "domain": "site.test", "scheme": "http", "agents": envs,
            "pages": pages if pages is not None else {"/": {"fetch": {"state": "OK", "status": 200,
                                                                         "final_url": U, "x_robots_tag": None},
                                                               "fingerprint": {"title_len": 10, "h1": 1}}},
            "versions": versions or {"bot_families": "v1", "schema_rules": "v1"}, "prompt_set": None,
            "codes_key_id": key_id}


def _delia_facts(**over):
    base = {"sitemaps_read": 1, "sitemap_urls": 2, "sitemap_fetch_failures": 0, "llms_txt": {"state": "present"},
            "robots_unchecked": 0}
    base.update(over)
    return base


def test_B_DR_01_identical_runs_no_changes():
    r = rng(7, "dr01")
    for i in range(50):
        fs = [f(r.choice(("NOINDEX", "H1_COUNT", "TITLE_LENGTH")), U + "p%d" % j, token=r.choice(("a", "b")))
              for j in range(r.randint(0, 8))]
        a = report("a", [env("selene", "OK", fs), env("roman", "OK", copy.deepcopy(fs))])
        b = copy.deepcopy(a)
        b["audit_id"] = "b"
        assert drift.compare(a, b)["changes"] == []
    check_ran("B-DR-01", 50)


def test_B_DR_01_other_tenant_not_comparable():
    a = report("a", [])
    b = report("b", [])
    b["tenant_id"] = "acme"
    with pytest.raises(drift.NotComparable):
        drift.compare(a, b)


def test_B_DR_02_every_change_has_one_class_and_evidence():
    a = report("a", [env("selene", "OK", [f("NOINDEX")])])
    b = report("b", [env("selene", "OK", [f("H1_COUNT")])])
    for c in drift.compare(a, b)["changes"]:
        assert c["class"] in drift.CLASSES and c["evidence"].get("rule", "").startswith("R")


# 16 failure kinds: each makes the observation the finding rests on a failed / incomplete own observation
KINDS = [
    ("outcome_failed", "selene"), ("outcome_killed", "selene"), ("page_fetch_timeout", "selene"),
    ("own_fetch_state", "selene"), ("unreadable_code", "delia"), ("unreachable_code", "selene"),
    ("unchecked_code", "selene"), ("incomplete_code", "selene"), ("approximate_code", "selene"),
    ("page_not_read", "selene"), ("blocked_unreachable", "selene"), ("robots_unreachable", "selene"),
    ("robots_too_large", "selene"), ("llms_unreadable", "delia"), ("sitemap_unreadable", "delia"),
    ("sitemap_any_failure", "delia")]


def _pair(kind: str, agent: str, side: str, r) -> tuple:
    url = U + "robots.txt" if kind.startswith("robots") else U
    base_f = f("NOINDEX" if agent == "selene" else "SITEMAP_NAMESPACE", url)
    a_envs = [env(agent, "OK", [base_f], _delia_facts() if agent == "delia" else
                  {"robots": {"status_class": "ok", "text_sha256": "h"}})]
    b_envs = copy.deepcopy(a_envs)
    bad = b_envs if side == "b" else a_envs
    e = bad[0]
    pages_bad = None
    if kind == "outcome_failed":
        e["outcome"] = "FAILED"
    elif kind == "outcome_killed":
        e["outcome"] = "KILLED"
    elif kind == "page_fetch_timeout":
        pages_bad = {"/": {"fetch": {"state": "TIMEOUT", "status": None, "final_url": U, "x_robots_tag": None},
                           "fingerprint": None}}
    elif kind == "own_fetch_state":
        e["findings"] = [f("CANONICAL_TARGET_NOT_200", url, "high", "ACT", fetch_state="TIMEOUT")]
    elif kind == "unreadable_code":
        e["findings"] = [f("SITEMAP_UNREADABLE", U + "sitemap.xml", "medium", "INSUFFICIENT_EVIDENCE")]
    elif kind == "unreachable_code":
        e["findings"] = [f("ROBOTS_UNREACHABLE", U + "robots.txt", "critical", "ACT")]
    elif kind == "unchecked_code":
        e["findings"] = [f("CANONICAL_TARGET_UNCHECKED", url, "info", "INSUFFICIENT_EVIDENCE")]
    elif kind == "incomplete_code":
        e["findings"] = [f("ROBOTS_CHECK_INCOMPLETE", U + "robots.txt", "info", "INSUFFICIENT_EVIDENCE")]
    elif kind == "approximate_code":
        e["findings"] = [f("ROBOTS_BLOCK_APPROXIMATE", U + "robots.txt", "info", "INSUFFICIENT_EVIDENCE",
                           token="Googlebot")]
    elif kind == "page_not_read":
        e["findings"] = [f("PAGE_NOT_READ_ROBOTS_CHECK_INCOMPLETE", url, "info", "INSUFFICIENT_EVIDENCE")]
    elif kind == "blocked_unreachable":
        e["findings"] = [f("PAGE_BLOCKED_FOR_OUR_CRAWLER", url, "info", "INSUFFICIENT_EVIDENCE",
                           reason="unreachable")]
    elif kind == "robots_unreachable":
        e["facts"]["robots"]["status_class"] = "unreachable"
        e["findings"] = []
    elif kind == "robots_too_large":
        e["facts"]["robots"]["status_class"] = "incomplete"
        e["findings"] = []
    elif kind == "llms_unreadable":
        e["facts"]["llms_txt"] = {"state": "unreadable"}
        e["findings"] = [f("LLMS_TXT_FORMAT", U + "llms.txt", "low", "TEST")]
    elif kind == "sitemap_unreadable":
        e["findings"] = [f("SITEMAP_UNREADABLE", U + "s.xml", "medium", "INSUFFICIENT_EVIDENCE"),
                         f("SITEMAP_TOO_MANY_URLS", U + "s.xml", "high", "ACT")]
    elif kind == "sitemap_any_failure":
        e["facts"]["sitemap_fetch_failures"] = 1
        e["findings"] = [f("SITEMAP_LOC_OTHER_HOST", None, "medium", "ACT")]
    a = report("a", a_envs, pages=pages_bad if side == "a" else None)
    b = report("b", b_envs, pages=pages_bad if side == "b" else None)
    return a, b


@pytest.mark.parametrize("seed", [7, 11])
def test_B_DR_03_A_28_no_real_change_on_an_own_failed_observation(seed):
    r = rng(seed, "dr03")
    ran = 0
    for kind, agent in KINDS:
        for site in range(8):
            for side in ("a", "b"):
                a, b = _pair(kind, agent, side, r)
                out = drift.compare(a, b)
                for c in out["changes"]:
                    assert c["class"] != "REAL_CHANGE", (kind, side, c)
                ran += 1
    check_ran("B-DR-03", ran)
    check_ran("A-28", ran)


def test_B_DR_04_ruler_moved_page_same_is_r2_both_moved_is_r3():
    a = report("a", [env("roman", "OK", [f("STRUCTURED_DATA_ERRORS")])])
    b = report("b", [env("roman", "OK", [])], versions={"bot_families": "v1", "schema_rules": "v2"})
    for _ in range(20):
        assert [c["class"] for c in drift.compare(a, b)["changes"]] == ["MEASUREMENT_ERROR"]
    b2 = copy.deepcopy(b)
    b2["pages"]["/"]["fingerprint"]["h1"] = 2
    for _ in range(20):
        c = drift.compare(a, b2)["changes"][0]
        assert c["class"] == "UNKNOWN" and c["evidence"]["rule"] == "R3"


def test_B_DR_05_B_CD_02_codes_key_rotation_is_r2_never_a_change():
    a = report("a", [env("selene", "OK", [f("CANONICAL_OTHER_HOST", canonical="{c:host:" + "1" * 16 + "}")])])
    b = report("b", [env("selene", "OK", [f("CANONICAL_OTHER_HOST", U + "x", canonical="{c:host:" + "2" * 16 +
                                            "}")])], key_id="k2")
    b["pages"]["/"]["fingerprint"]["h1"] = 9
    out = drift.compare(a, b)
    assert out["changes"] and all(c["class"] == "MEASUREMENT_ERROR" and c["evidence"].get("ruler") == "codes_key"
                                  for c in out["changes"])
    assert out["counts"]["REAL_CHANGE"] == 0


def _callum(models, ci, ci2=None):
    return env("callum", "OK", [f("CITED_SHARE", None, "low", "TEST", engine="e", prompt_index=0)],
               {"engines": {"e": {"prompts": [{"index": 0, "model_versions": models, "citation_ci95": ci,
                                               "mention_ci95": ci2 if ci2 is not None else ci}]}}})


def test_B_DR_06_answer_engine_rules():
    a = report("a", [_callum(["m1"], [0.1, 0.3])])
    for models, ci, want in ((["m2"], [0.1, 0.3], ("MODEL_DRIFT",)), (["m1"], [0.2, 0.4], ("SAMPLING_NOISE",)),
                             (["m1"], [0.6, 0.9], ("SURFACE_DRIFT",)), (["m1"], None, ("UNKNOWN",))):
        b = report("b", [_callum(models, ci)])
        b["agents"][0]["findings"][0]["severity"] = "medium"
        got = [c["class"] for c in drift.compare(a, b)["changes"]]
        assert tuple(got) == want, (models, ci, got)
    check_ran("B-DR-06", 4 * 10)


def test_B_DR_07_real_change_controls():
    ran = real = 0
    for i in range(100):
        a = report("a", [env("selene", "OK", [])])
        b = report("b", [env("selene", "OK", [f("NOINDEX", sev="high", dec="TEST")])])
        b["pages"]["/"]["fetch"]["x_robots_tag"] = "noindex"
        got = [c["class"] for c in drift.compare(a, b)["changes"]]
        ran += 1
        real += got == ["REAL_CHANGE"]
        assert set(got) <= {"REAL_CHANGE", "UNKNOWN"}
    assert real >= 95
    check_ran("B-DR-07", ran)


def test_B_DR_08_overturned_counts_only_vanished_r2_r5():
    a = report("a", [env("roman", "OK", [f("STRUCTURED_DATA_ERRORS")])])
    b = report("b", [env("roman", "OK", [])], versions={"bot_families": "v1", "schema_rules": "v2"})
    assert drift.compare(a, b)["overturned_by_agent"] == {"roman": 1}
    a2 = report("a", [env("roman", "FAILED", [f("STRUCTURED_DATA_ERRORS")])])
    assert drift.compare(a2, b)["overturned_by_agent"] == {}
    assert drift.OVERTURNING == ("MEASUREMENT_ERROR", "SAMPLING_NOISE")


@pytest.mark.parametrize("kind,agent", KINDS)
def test_B_DR_03_each_own_failure_kind_is_r1_tool_failure(kind, agent):
    """B-DR-03 exactly: the change of the finding that rests on the failed observation is R1 TOOL_FAILURE (not
    merely "not REAL_CHANGE": another class would hide that this service's own observation failed). For a kind
    whose evidence is a fact (robots status, llms.txt state, sitemap failures), every change of that agent is."""
    r = rng(5, "dr03x")
    for side in ("a", "b"):
        a, b = _pair(kind, agent, side, r)
        bad = (a if side == "a" else b)["agents"][0]["findings"]
        own = {x["code"] for x in bad} - {"NOINDEX", "SITEMAP_NAMESPACE"}
        for c in drift.compare(a, b)["changes"]:
            if own and c["code"] not in own:
                continue
            assert c["class"] == "TOOL_FAILURE" and c["evidence"].get("rule") == "R1", (kind, side, c)


@pytest.mark.parametrize("code", ["PAGE_NOT_READ_BUDGET", "PAGE_NOT_READ_ROBOTS_CHECK_INCOMPLETE"])
def test_B_DR_03_every_page_not_read_code_is_r1(code):
    """B-DR-03 names the prefix ``PAGE_NOT_READ_*``: any such finding is R1, whatever its suffix."""
    a = report("a", [env("selene", "OK", [])])
    b = report("b", [env("selene", "OK", [f(code, U + "x", "info", "INSUFFICIENT_EVIDENCE")])])
    (c,) = drift.compare(a, b)["changes"]
    assert c["class"] == "TOOL_FAILURE" and c["evidence"]["rule"] == "R1"


def test_B_DR_06_noise_needs_both_intervals_to_overlap():
    """B-DR-06: SAMPLING_NOISE only when the citation AND the mention intervals overlap; one apart is SURFACE_DRIFT."""
    a = report("a", [_callum(["m1"], [0.1, 0.3], [0.1, 0.3])])
    for ci, ci2, want in (([0.2, 0.4], [0.6, 0.9], "SURFACE_DRIFT"), ([0.6, 0.9], [0.2, 0.4], "SURFACE_DRIFT"),
                          ([0.2, 0.4], [0.2, 0.4], "SAMPLING_NOISE")):
        b = report("b", [_callum(["m1"], ci, ci2)])
        b["agents"][0]["findings"][0]["severity"] = "medium"
        assert [c["class"] for c in drift.compare(a, b)["changes"]] == [want], (ci, ci2)
