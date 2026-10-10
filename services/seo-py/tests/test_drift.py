"""Wave 2 stage 2: search-truth drift — each changed finding between two runs gets exactly one class by the first
rule whose evidence holds (agents/drift.py R1..R7); UNKNOWN when none does."""

from __future__ import annotations

import copy

import pytest

from agents import drift
from fixture_server import home_html, install_site
from helpers import rid
from test_audits import audit, harness, own

pytestmark = pytest.mark.local_http


def two_runs(tmp_path, srv, change=None):
    install_site(srv)
    h = harness(tmp_path, srv)
    own(h)
    a = h.ok(audit(h), 201)
    if change:
        change(srv)
    b = h.ok(audit(h), 201)
    return h, a, b


def by_code(d):
    return {c["code"]: c for c in d["changes"]}


def test_identical_runs_have_no_changes(tmp_path, srv):
    h, a, b = two_runs(tmp_path, srv)
    d = h.ok(h.get(f"/tenants/zbm/audits/{b['audit_id']}/drift", params={"against": a["audit_id"]}))
    assert d["changes"] == [] and d["unchanged"] > 0 and d["rules_version"] == drift.RULES_VERSION
    assert d["older"] == a["audit_id"]


def test_page_content_change_is_real_change(tmp_path, srv):
    h, a, b = two_runs(tmp_path, srv, lambda s: s.html("site.test", "/", home_html(canonical=None, ld=None)))
    d = drift.compare(a["report"], b["report"])
    c = by_code(d)["CANONICAL_MISSING"]
    assert c["change"] == "APPEARED" and c["class"] == "REAL_CHANGE"
    assert {"canonicals", "jsonld"} <= set(c["evidence"]["changed_fields"])
    assert by_code(d)["STRUCTURED_DATA_ABSENT"]["class"] == "REAL_CHANGE"


def test_fetch_failure_is_tool_failure(tmp_path, srv):
    h, a, b = two_runs(tmp_path, srv, lambda s: s.redirect("site.test", "/about", "http://127.0.0.1/"))
    d = drift.compare(a["report"], b["report"])
    gone = [c for c in d["changes"] if c["url"] == "http://site.test/about"]
    assert gone and all(c["class"] == "TOOL_FAILURE" for c in gone)
    assert d["overturned_by_agent"] == {}            # appeared/changed only: nothing vanished as non-real


def test_robots_change_is_real_change(tmp_path, srv):
    h, a, b = two_runs(tmp_path, srv, lambda s: s.text("site.test", "/robots.txt",
                                                        "User-agent: GPTBot\nDisallow: /\n"))
    c = [x for x in drift.compare(a["report"], b["report"])["changes"]
         if x["code"] == "ROBOTS_BLOCKS_AI_OR_DATASET_CRAWLER"][0]
    assert c["class"] == "REAL_CHANGE" and len(set(c["evidence"]["robots_sha256"])) == 2


def test_rule_table_change_with_same_page_is_measurement_error(tmp_path, srv):
    h, a, _ = two_runs(tmp_path, srv)
    A = a["report"]
    B = copy.deepcopy(A)
    B["audit_id"] = "seo-aud-" + "1" * 40
    B["versions"]["schema_rules"] = "2099-01-01.1"
    roman_b = next(e for e in B["agents"] if e["agent"] == "roman")
    f = next(f for f in roman_b["findings"] if f["url"] == "http://site.test/")
    f["severity"], f["decision"] = "high", "ACT"
    c = next(x for x in drift.compare(A, B)["changes"] if x["code"] == f["code"])
    assert c["class"] == "MEASUREMENT_ERROR" and c["evidence"]["changed"]["schema_rules"][1] == "2099-01-01.1"
    B["versions"]["schema_rules"] = A["versions"]["schema_rules"]
    c = next(x for x in drift.compare(A, B)["changes"] if x["code"] == f["code"])
    assert c["class"] == "UNKNOWN"                   # same page, same rules: no cause claimed


def test_vanished_finding_after_a_ruler_change_counts_as_overturned(tmp_path, srv):
    h, a, _ = two_runs(tmp_path, srv)
    A = a["report"]
    B = copy.deepcopy(A)
    B["audit_id"] = "seo-aud-" + "2" * 40
    B["versions"]["bot_families"] = "2099-01-01.1"
    sel = next(e for e in B["agents"] if e["agent"] == "roman")
    removed = sel["findings"].pop(0)
    d = drift.compare(A, B)
    c = next(x for x in d["changes"] if x["code"] == removed["code"] and x["url"] == removed["url"])
    assert c["change"] == "DISAPPEARED" and c["class"] == "MEASUREMENT_ERROR"
    assert d["overturned_by_agent"] == {"roman": 1}


# ---------------------------------------------------------------------------------------------- answer engines

def callum_report(aid, ps_sha, versions, cit, men, cls="STRENGTH", outcome="OK"):
    return {"audit_id": aid, "tenant_id": "zbm", "domain": "site.test", "scheme": "http", "pages": {},
            "versions": {}, "prompt_set": {"sha256": ps_sha},
            "agents": [{"agent": "callum", "outcome": outcome,
                        "findings": [{"code": f"CITATION_{cls}", "severity": "info", "decision": "WATCH",
                                      "url": None, "detail": {"engine": "openai", "prompt_index": 0}}],
                        "facts": {"engines": {"openai": {"prompts": [{"model_versions": versions,
                                                                      "citation_ci95": cit,
                                                                      "mention_ci95": men}]}}}}]}


@pytest.mark.parametrize("b_args,expected", [
    (("p2", ["m@1"], [0.3, 0.9], [0.3, 0.9], "MENTIONED_NOT_CITED"), "MODEL_DRIFT"),     # prompt set changed
    (("p1", ["m@2"], [0.3, 0.9], [0.3, 0.9], "MENTIONED_NOT_CITED"), "MODEL_DRIFT"),     # model version changed
    (("p1", ["m@1"], [0.2, 0.8], [0.4, 0.95], "MENTIONED_NOT_CITED"), "SAMPLING_NOISE"),  # overlapping bands
    (("p1", ["m@1"], [0.0, 0.2], [0.0, 0.2], "ABSENT"), "SURFACE_DRIFT"),                # disjoint bands
    (("p1", ["m@1"], None, None, "INSUFFICIENT_EVIDENCE"), "UNKNOWN"),                   # no interval
    (("p1", ["m@1"], [0.0, 0.2], [0.0, 0.2], "ABSENT", "NOT_CONNECTED"), "TOOL_FAILURE"),
])
def test_answer_engine_rules(b_args, expected):
    a = callum_report("seo-aud-" + "a" * 40, "p1", ["m@1"], [0.5, 0.95], [0.5, 0.95])
    b = callum_report("seo-aud-" + "b" * 40, *b_args)
    classes = {c["class"] for c in drift.compare(a, b)["changes"]}
    assert classes == {expected}


def test_every_class_is_in_the_closed_list():
    assert set(drift.CLASSES) == {"REAL_CHANGE", "MEASUREMENT_ERROR", "MODEL_DRIFT", "SURFACE_DRIFT",
                                  "SAMPLING_NOISE", "TOOL_FAILURE", "UNKNOWN"}


# ---------------------------------------------------------------------------------------------- the route

def test_drift_route_refusals_and_tenant_scope(tmp_path, srv):
    install_site(srv)
    h = harness(tmp_path, srv)
    own(h)
    a = h.ok(audit(h), 201)
    h.ok(h.post("/tenants/zbm/domains", {"request_id": rid(), "domains": ["site.test", "www.site.test"]},
                andre=True))
    b = h.ok(audit(h, domain="www.site.test"), 201)
    h.refused(h.get(f"/tenants/zbm/audits/{b['audit_id']}/drift", params={"against": a["audit_id"]}), 409,
              "DRIFT_NOT_COMPARABLE")
    h.tenant("acme", domains=("acme.example",))
    h.refused(h.get(f"/tenants/acme/audits/{a['audit_id']}/drift", params={"against": a["audit_id"]}), 404,
              "AUDIT_NOT_FOUND")
    h.refused(h.get(f"/tenants/zbm/audits/{a['audit_id']}/drift", params={"against": a["audit_id"]},
                    caller="hub", tenant="acme"), 404, "TENANT_NOT_FOUND")
    assert h.get(f"/tenants/zbm/audits/{a['audit_id']}/drift", params={"against": "nope"}).status_code == 422
