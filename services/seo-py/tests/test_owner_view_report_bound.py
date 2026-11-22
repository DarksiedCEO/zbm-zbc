"""
Lead rulings 20:55 PT (card C2-OV3c) under OWNER DECISION 19:20 PT, both directions per item:
  * Q1: Callum's methodology is code_text only when it EXACTLY equals probes.METHODOLOGY rendered from the SAME
    report (prompt-set name / version / sha256[:16] / samples_per_prompt from that agent's facts, version + sha256
    equal to the report's /prompt_set, /domain, probes.MIN_USABLE); any other fill -> CV.
  * F1: entity_check /agents[]/facts/fields keys (<path>#<field>) shown when <path> is one of the report's own
    /paths[] (req_path grammar) and <field> is in svc_entity.FIELDS; any other key -> CV.
  * F2: /search_performance/section/window/timezone (search_performance.TIMEZONE), by_page_template templates[]
    template (search_performance._PLACEHOLDER set, logs rule), entity_check on_site sha256 (hash).
  * F3: the request schema rejects a non-str path before request_path_ok; request_path_ok(non-str) -> PAGES_INVALID.
All values synthetic.
"""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

import models
import svc_audits
from agents import entity_check, probes, search_performance
from errors import Invalid
from report_fields import AUDIT_REPORT, LOG_REPORT
from svc_entity import FIELDS
from svc_vaulted import _shown, owner_view

TOKEN = "".join(("zq7Rk2", "Lm9Xw4", "Tp8N"))                      # synthetic value-shaped token (CV-3 valueish)
SHA = "3f" * 31 + "a1"                         # synthetic 64-hex hash
VPATH = "/Kp3x9QzR"                            # synthetic valueish request path


def _cv(v):
    return _shown(v, {})


def _ov(d):
    return owner_view(d, doc=AUDIT_REPORT)


def _method(name="synthetic-set", ver=2, sha=SHA, n=4, domain="site.test"):
    return probes.METHODOLOGY.format(ps=name, ver=ver, sha=sha[:16], n=n, domain=domain, m=probes.MIN_USABLE)


def _report(m=None, **over):
    facts = {"prompt_set": {"prompt_set_id": "pst-synthetic", "name": "synthetic-set", "version": 2, "sha256": SHA},
             "samples_per_prompt": 4}
    r = {"domain": "site.test", "paths": ["/"], "prompt_set": {"prompt_set_id": "pst-synthetic", "version": 2,
                                                               "sha256": SHA},
         "agents": [{"agent": probes.AGENT_CALLUM, "methodology": _method() if m is None else m, "facts": facts}]}
    for k, v in over.items():
        r[k] = v
    return r


def _meth(r):
    return _ov(r)["agents"][0]["methodology"]


def test_callum_methodology_from_the_same_report_is_shown_and_cv_alone_would_mask_it():
    r = _report()
    assert _cv(r["agents"][0]["methodology"]) != r["agents"][0]["methodology"]
    assert _meth(r) == _method()


@pytest.mark.parametrize("m", [_method(domain="other.test"), _method(n=5), _method(name="other-set"),
                               _method(ver=3), _method(sha="4e" * 31 + "b2"), _method() + " " + TOKEN,
                               _method() + probes.WARN_COMMON.format(terms="synthetic"), _method().replace("Each", "Any")])
def test_callum_methodology_with_any_other_fill_goes_cv(m):
    assert _meth(_report(m)) == _cv(m)


def _mutated(fn):
    r = _report()
    fn(r)
    return r


@pytest.mark.parametrize("fn", [
    lambda r: r.update(domain="other.test"),
    lambda r: r["prompt_set"].update(version=3),
    lambda r: r["prompt_set"].update(sha256="4e" * 31 + "b2"),
    lambda r: r.update(prompt_set=None),
    lambda r: r["agents"][0]["facts"].pop("samples_per_prompt"),
    lambda r: r["agents"][0]["facts"].update(samples_per_prompt=True),
    lambda r: r["agents"][0]["facts"]["prompt_set"].update(name=7),
    lambda r: r["agents"][0].update(agent="selene"),
])
def test_callum_methodology_when_the_report_fields_disagree_goes_cv(fn):
    r = _mutated(fn)
    assert _meth(r) == _cv(_method())


def test_callum_bound_text_is_path_bound():
    r = _report()
    r["agents"][0]["reason"] = _method()
    r["notes"] = _method()
    got = _ov(r)
    assert got["agents"][0]["reason"] == _cv(_method()) and got["notes"] == _cv(_method())
    assert owner_view(copy.deepcopy(r), doc=LOG_REPORT)["agents"][0]["methodology"] == _cv(_method())


def _entity(keys, paths=(VPATH, "/")):
    return {"paths": list(paths), "agents": [{"agent": entity_check.AGENT, "facts": {"fields": dict.fromkeys(keys,
                                                                                                          "match")}}]}


def test_entity_field_keys_of_the_reports_own_paths_are_shown():
    keys = [f"{p}#{f}" for p in (VPATH, "/") for f in FIELDS]
    assert any(_cv(k) != k for k in keys)
    got = _ov(_entity(keys))["agents"][0]["facts"]["fields"]
    assert sorted(got) == sorted(keys)


@pytest.mark.parametrize("key", [f"{VPATH}#{TOKEN}", f"/{TOKEN}#telephone", f"{VPATH}#telephone#x", TOKEN])
def test_entity_field_keys_outside_the_closed_set_go_cv(key):
    got = _ov(_entity([key]))["agents"][0]["facts"]["fields"]
    assert list(got) == [_cv(key)]


def test_entity_field_keys_are_bound_to_entity_check_and_the_fields_block():
    k = f"{VPATH}#telephone"
    assert _cv(k) != k
    other = _entity([k])
    other["agents"][0]["agent"] = "selene"
    assert list(_ov(other)["agents"][0]["facts"]["fields"]) == [_cv(k)]
    moved = {"paths": [VPATH], "agents": [{"agent": entity_check.AGENT, "facts": {"other": {k: 1}}}]}
    assert list(_ov(moved)["agents"][0]["facts"]["other"]) == [_cv(k)]
    assert list(_ov(_entity([k], paths=["/"]))["agents"][0]["facts"]["fields"]) == [_cv(k)]


def _sp(tz=None, template=None):
    sec = {"window": {"timezone": search_performance.TIMEZONE if tz is None else tz},
           "by_page_template": {"templates": [{"template": "/" if template is None else template}]}}
    return _ov({"search_performance": {"section": sec}})["search_performance"]["section"]


def test_timezone_is_the_modules_constant_and_shown():
    assert _sp()["window"]["timezone"] == search_performance.TIMEZONE


@pytest.mark.parametrize("tz", [f"Europe/{TOKEN}", TOKEN, "america/los_angeles"])
def test_other_timezones_go_cv(tz):
    assert _sp(tz=tz)["window"]["timezone"] == _cv(tz)


@pytest.mark.parametrize("t", ["/users/{slug}/profile", "/{file}.pdf", "/p/{number}/{uuid}", "/"])
def test_gsc_templates_with_the_modules_placeholders_are_shown(t):
    assert search_performance._PLACEHOLDER.fullmatch("{slug}")
    assert _sp(template=t)["by_page_template"]["templates"][0]["template"] == t


@pytest.mark.parametrize("t", [f"/users/{{slug}}/{TOKEN}", f"/{{nope}}/{TOKEN}", "users/{slug}", f"/{TOKEN}"])
def test_gsc_templates_off_grammar_go_cv(t):
    assert _sp(template=t)["by_page_template"]["templates"][0]["template"] == _cv(t)


def test_gsc_template_rule_is_path_bound():
    t = "/users/{slug}/" + "Kp3x9QzR"
    got = owner_view({"envelope": {"facts": {"families": {"x": {"top_path_templates": [{"template": t}]}}}}},
                     doc=LOG_REPORT)
    assert got["envelope"]["facts"]["families"]["x"]["top_path_templates"][0]["template"] == _cv(t)


def _on_site(v, key="sha256"):
    r = {"agents": [{"findings": [{"detail": {"on_site": {key: v, "untrusted": True}}}]}]}
    return _ov(r)["agents"][0]["findings"][0]["detail"]["on_site"]


def test_on_site_hash_is_shown_and_cv_alone_differs_or_not():
    assert _on_site(SHA)["sha256"] == SHA and _on_site(SHA)["untrusted"] is True


@pytest.mark.parametrize("v", [TOKEN, "not a hash " + TOKEN])
def test_on_site_non_hash_goes_cv(v):
    assert _on_site(v)["sha256"] == _cv(v)


def test_on_site_text_stays_free_text_even_when_hash_shaped():
    assert _on_site("site text " + TOKEN, key="text")["text"] == _cv("site text " + TOKEN)


@pytest.mark.parametrize("model", [models.AuditCreate, models.ScheduleCreate])
def test_request_schema_rejects_a_non_str_path_before_request_path_ok(model):
    with pytest.raises(ValidationError) as ei:
        model.model_validate({"paths": [1]})
    assert any(tuple(e["loc"][:2]) == ("paths", 0) for e in ei.value.errors())
    with pytest.raises(ValidationError) as ok:
        model.model_validate({"paths": ["/"]})
    assert not any(tuple(e["loc"][:1]) == ("paths",) for e in ok.value.errors())


@pytest.mark.parametrize("p", [1, None, b"/"])   # an unhashable path: schema-only (see HANDOFF 5ap F3b)
def test_request_path_ok_false_for_non_str_is_pages_invalid(p):
    assert svc_audits.request_path_ok(p) is False
    with pytest.raises(Invalid) as ei:
        svc_audits._check_paths([p], 5)
    assert ei.value.reason == "PAGES_INVALID"
    svc_audits._check_paths(["/"], 5)
