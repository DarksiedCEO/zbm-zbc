"""
Lead rulings 20:35 PT (card C2-OV3b) under OWNER DECISION 19:20 PT, both directions per new type:
  * OV3-1: the owner's own request fields (/paths[], /domain, /scheme) are typed with the SAME grammar the API
    validated (svc_audits.request_path_ok + client_report._REQ_PATH / _DOMAIN / REQ_SCHEMES) and shown; a value that
    fails it takes the CV route.
  * OV3-2: code_text covers every code-authored methodology (entity_check, svc_audits incl. Osei, svc_gsc) and the
    /not_connected_why/* strings, each read from its module; the entity_check per-field results are typed ``code``.
    Callum's filled methodology stays CV.
All values synthetic.
"""

from __future__ import annotations

import pytest

import client_report as cr
import report_fields as rf
import svc_audits
import svc_gsc
from agents import entity_check
from report_fields import AUDIT_REPORT
from svc_vaulted import _shown, owner_view

TOKEN = "".join(("zq7Rk2", "Lm9Xw4", "Tp8N"))          # synthetic value-shaped token (CV-3 valueish)
VALUEISH_PATH = "/Kp3x9QzR"         # synthetic 1-segment path the CV masks; the request grammar admits it


def _cv(v):
    return _shown(v, {})


def _ov(d):
    return owner_view(d, doc=AUDIT_REPORT)


def test_the_valueish_request_path_is_masked_by_the_cv_alone():
    assert _cv(VALUEISH_PATH) != VALUEISH_PATH and svc_audits.request_path_ok(VALUEISH_PATH)


@pytest.mark.parametrize("p", [VALUEISH_PATH, "/", "/pricing", "/a/b?x=1"])
def test_a_request_path_in_the_api_grammar_is_shown(p):
    assert _ov({"paths": [p]})["paths"] == [p]


@pytest.mark.parametrize("p", ["//evil.test/x", f"/a#{TOKEN}", "/a\\b", "no-slash", "/a b", "/" + "x" * 501, ""])
def test_a_request_path_outside_the_api_grammar_takes_the_cv_route(p):
    assert not svc_audits.request_path_ok(p) or not cr._REQ_PATH.fullmatch(p)
    assert _ov({"paths": [p]})["paths"] == [_cv(p)]


def test_request_path_type_is_bound_to_paths():
    assert _ov({"note": VALUEISH_PATH})["note"] == _cv(VALUEISH_PATH)
    assert owner_view({"paths": [VALUEISH_PATH]}, doc=None)["paths"] == [_cv(VALUEISH_PATH)]
    assert _ov({"paths": [7]})["paths"] == [7]


@pytest.mark.parametrize("d", ["site.test", "www.example.test"])
def test_a_request_domain_is_shown(d):
    assert _ov({"domain": d})["domain"] == d


@pytest.mark.parametrize("d", ["a.b", f"{TOKEN}@site.test", "Site.Test", "x" * 254])
def test_a_domain_outside_the_grammar_takes_the_cv_route(d):
    assert not cr._DOMAIN.fullmatch(d)
    assert _ov({"domain": d})["domain"] == _cv(d)


@pytest.mark.parametrize("s,shown", [("https", True), ("http", True), ("ftp", False), (TOKEN, False)])
def test_scheme_both_directions(s, shown):
    assert _ov({"scheme": s})["scheme"] == (s if shown else _cv(s))


def _module_methods():
    return [entity_check.METHODOLOGY, svc_gsc.METHOD, *svc_audits.METHODOLOGIES]


@pytest.mark.parametrize("i", range(len(svc_audits.METHODOLOGIES) + 2))
def test_each_code_authored_methodology_is_shown(i):
    m = _module_methods()[i]
    assert _ov({"agents": [{"methodology": m}]})["agents"][0]["methodology"] == m


def test_some_extended_methodology_would_be_masked_by_the_cv():
    assert any(_cv(m) != m for m in _module_methods())


@pytest.mark.parametrize("m", [entity_check.METHODOLOGY + f" {TOKEN}", svc_audits.M_HYGIENE[:-1],
                               svc_audits.M_KILLED.upper()])
def test_an_off_set_methodology_takes_the_cv_route(m):
    got = _ov({"agents": [{"methodology": m}]})["agents"][0]["methodology"]
    assert got == _cv(m) and TOKEN not in got


def test_not_connected_why_both_directions():
    why = dict(svc_audits.NOT_CONNECTED_WHY)
    assert _ov({"not_connected_why": why})["not_connected_why"] == why
    assert any(_cv(v) != v for v in why.values())
    bad = {"render": why["render"] + f" {TOKEN}", "first_party": why["render"]}
    got = _ov({"not_connected_why": bad})["not_connected_why"]
    assert got["render"] == _cv(bad["render"]) and TOKEN not in got["render"]
    # a code string from the set is allowed under any key of the block; nowhere else
    assert got["first_party"] == why["render"]
    assert _ov({"agents": [{"reason": why["first_party"]}]})["agents"][0]["reason"] == _cv(why["first_party"])


def test_callum_filled_methodology_stays_cv():
    from agents import probes
    m = probes.METHODOLOGY.format(ps="synthetic-set", ver="1", sha="ab" * 8, n=3, domain="site.test",
                                  m=probes.MIN_USABLE)
    assert m not in rf.code_texts(AUDIT_REPORT, ("agents", "[]", "methodology"))
    assert _ov({"agents": [{"methodology": m}]})["agents"][0]["methodology"] == _cv(m)


@pytest.mark.parametrize("v,shown", [("match", True), ("MISMATCH", True), (TOKEN, False)])
def test_entity_field_results_typed_code_both_directions(v, shown):
    got = _ov({"agents": [{"facts": {"fields": {"name": v}}}]})["agents"][0]["facts"]["fields"]["name"]
    assert got == (v if shown else _cv(v))
