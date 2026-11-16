"""
Lead ruling 20:15 PT (card C2-OV3) under OWNER DECISION 19:20 PT: code-written fixed text is a typed machine field
(``code_text``) in the owner's view, and the client document is an owner display surface rendered through the SAME
owner-view routing as the API. Both directions: the modules' own methodology / limitation strings are shown on their
typed paths; anything off the closed set (and the same strings on any other path or with no doc kind) takes the CV
route; crawled free text with a value-shaped token is masked in BOTH the API owner view and the client document.
All values synthetic.
"""

from __future__ import annotations

import json

import pytest

import client_report as cr
import report_fields as rf
import svc_audits
import svc_gsc
from agents import delia, entity_check, roman, selene
from envelope import finding
from report_fields import AUDIT_REPORT
from svc_vaulted import _shown, owner_view
from test_client_report import _audit, _harness, _report

MASK = "[hidden: looks like a credential]"
TOKEN = "".join(("zq7Rk2", "Lm9Xw4", "Tp8N"))          # synthetic value-shaped token (CV-3 valueish)
METHODS = [m.methodology_text() for m in (selene, delia, roman)]


def _doc(methodology=None, limits=(), top=()):
    return {"agents": [{"agent": "x", "methodology": methodology, "limitations": list(limits), "findings": []}],
            "limitations": list(top)}


def _cv(v):
    return _shown(v, {})


def test_the_closed_set_is_read_from_the_modules_not_copied():
    # ruling 20:35 PT OV3-2 (C2-OV3b): + entity_check / svc_audits / svc_gsc methodologies, read from the modules
    assert rf.code_texts(AUDIT_REPORT, ("agents", "[]", "methodology")) == frozenset(
        METHODS + [entity_check.METHODOLOGY, svc_gsc.METHOD] + list(svc_audits.METHODOLOGIES))
    assert rf.code_texts(AUDIT_REPORT, ("not_connected_why", "render")) == frozenset(svc_audits.NOT_CONNECTED_WHY.values())
    assert rf.code_texts(AUDIT_REPORT, ("limitations", "[]")) == frozenset(svc_gsc.LIMITS)
    assert rf.code_texts(AUDIT_REPORT, ("agents", "[]", "limitations", "[]")) == frozenset(svc_gsc.LIMITS)
    assert rf.code_texts(AUDIT_REPORT, ("agents", "[]", "findings")) == frozenset()
    assert rf.code_texts("drift_report", ("agents", "[]", "methodology")) == frozenset()
    assert "code_text" in rf.TYPES


def test_the_cv_alone_would_mask_some_of_these_strings():
    # the type matters: at least one methodology and one GSC limitation changes on the CV route
    assert any(_cv(m) != m for m in METHODS)
    assert any(_cv(x) != x for x in svc_gsc.LIMITS)


@pytest.mark.parametrize("i", range(3))
def test_each_module_methodology_is_shown_on_its_typed_path(i):
    got = owner_view(_doc(methodology=METHODS[i]), doc=AUDIT_REPORT)
    assert got["agents"][0]["methodology"] == METHODS[i]


def test_gsc_limitations_are_shown_on_both_limitation_paths():
    got = owner_view(_doc(limits=svc_gsc.LIMITS, top=svc_gsc.LIMITS), doc=AUDIT_REPORT)
    assert got["agents"][0]["limitations"] == list(svc_gsc.LIMITS) and got["limitations"] == list(svc_gsc.LIMITS)


def _off_template():
    m = METHODS[0]
    return [m + " ", " " + m, m[:-1], m + f" {TOKEN}", m.replace(m.split()[0], TOKEN, 1), m.upper(),
            m + "​", METHODS[0] + METHODS[1]]


@pytest.mark.parametrize("v", _off_template())
def test_an_off_template_methodology_takes_the_cv_route(v):
    got = owner_view(_doc(methodology=v), doc=AUDIT_REPORT)["agents"][0]["methodology"]
    assert got == _cv(v)
    if TOKEN in v:
        assert TOKEN not in got and MASK in got


def test_an_off_set_limitation_takes_the_cv_route():
    v = svc_gsc.LIMITS[0] + f" {TOKEN}"
    got = owner_view(_doc(limits=[v], top=[v]), doc=AUDIT_REPORT)
    assert got["agents"][0]["limitations"] == [_cv(v)] == got["limitations"] and TOKEN not in json.dumps(got)


def test_code_text_is_bound_to_its_own_path():
    lim = next(x for x in svc_gsc.LIMITS if _cv(x) != x)
    meth = next(m for m in METHODS if _cv(m) != m)
    got = owner_view(_doc(methodology=lim, limits=[meth], top=[meth]), doc=AUDIT_REPORT)
    assert got["agents"][0]["methodology"] == _cv(lim)
    assert got["agents"][0]["limitations"] == [_cv(meth)] == got["limitations"]
    # free-text paths and a non-string value never take the type
    d = {"agents": [{"findings": [{"detail": {"note": meth}}], "methodology": 7}]}
    out = owner_view(d, doc=AUDIT_REPORT)["agents"][0]
    assert out["findings"][0]["detail"]["note"] == _cv(meth) and out["methodology"] == 7


@pytest.mark.parametrize("doc", [None, "drift_report", "log_report", "seo_log", "unknown"])
def test_no_or_other_doc_kind_takes_the_cv_route(doc):
    meth = next(m for m in METHODS if _cv(m) != m)
    assert owner_view(_doc(methodology=meth), doc=doc)["agents"][0]["methodology"] == _cv(meth)


def _inject(h):
    """Every vault read of an audit report gets one synthetic crawled robots rule carrying a value-shaped token
    (after the HMAC check: the stored blob is untouched)."""
    orig = h.svc._vaulted_open

    def opened(*a, **k):
        obj = orig(*a, **k)
        if isinstance(obj, dict) and isinstance(obj.get("agents"), list) and obj["agents"]:
            obj = json.loads(json.dumps(obj))
            obj["agents"][0].setdefault("findings", []).append(json.loads(json.dumps(finding(
                "ROBOTS_BLOCKS_AI_OR_DATASET_CRAWLER", "info", "measured", "WATCH", url="http://site.test/robots.txt",
                capability="P2", detail={"token": "ExampleBot", "purpose": "ai_training", "blocked_paths": ["/"],
                                         "rules": [f"Disallow: /private-{TOKEN}/"]}), default=str)))
        return obj
    h.svc._vaulted_open = opened


@pytest.mark.local_http
@pytest.mark.parametrize("fmt", ["html", "markdown"])
def test_crawled_free_text_is_masked_in_the_api_view_and_the_client_document(tmp_path, srv, fmt):
    from fixture_server import install_site
    install_site(srv)
    h = _harness(tmp_path, srv)
    a = _audit(h)
    _inject(h)
    view = h.ok(h.get(f"/tenants/zbm/audits/{a['audit_id']}"))
    doc = h.ok(_report(h, "zbm", a["audit_id"], fmt=fmt))["content"]
    assert TOKEN not in json.dumps(view) and TOKEN not in doc
    rules = [f["detail"]["rules"] for ag in view["report"]["agents"] for f in ag.get("findings", [])
             if f.get("code") == "ROBOTS_BLOCKS_AI_OR_DATASET_CRAWLER" and f["detail"].get("token") == "ExampleBot"]
    assert rules and MASK in rules[0][0]
    # the document is the render of the API owner view, byte for byte; the module text is shown in both
    assert cr.render(view, "zbm", fmt)[0].decode() == doc
    shown_methods = [ag["methodology"] for ag in view["report"]["agents"] if ag.get("methodology") in METHODS]
    assert shown_methods and any(_cv(m) != m for m in shown_methods)
    # the shown module text reaches the document: the same view with those methodologies on the CV route renders
    # a different document
    alt = json.loads(json.dumps(view))
    for ag in alt["report"]["agents"]:
        if ag.get("methodology") in METHODS:
            ag["methodology"] = _cv(ag["methodology"])
    assert cr.render(alt, "zbm", fmt)[0].decode() != doc
