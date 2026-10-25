"""golive-combined: the Search Console connector (golive-gsc) and the Perplexity adapter (golive-perplexity, Wave 4)
together in one service. Each branch proves its own rules alone; these prove they still hold side by side:

- the audit outcome: Osei's search_performance section stays out of the overall outcome (ADR 0017 GSC-7) while
  Callum's Perplexity outcome (FAILED on repeated protocol errors / NO_ANSWERS) still decides it;
- startup: the Search Console MAX_REQUESTS consistency refusal and the Perplexity key refusals both apply when both
  connectors are configured;
- /status shows both connectors.

No network: the fake Google of gsc_fakes.py and the fake Perplexity of test_perplexity.py."""

from __future__ import annotations

import httpx
import pytest
from gsc_fakes import FakeGoogle, write_key_file
from helpers import base_env, rid
from test_gsc import PROP, bind, gsc_harness, section
from test_perplexity import BUDGET, KEY, FakePerplexity, _key_file, engine

import config as config_mod
from envelope import worst_outcome


def _both(tmp_path, gfake: FakeGoogle, pfake: FakePerplexity, **env):
    h = gsc_harness(tmp_path, gfake, SEO_PERPLEXITY_API_KEY=KEY, SEO_PROBE_SAMPLES="3", **BUDGET, **env)
    h.ports.engines["perplexity"] = engine(pfake)
    h.tenant("acme", "own", ("acme.test",))
    h.ok(bind(h, "acme", PROP))
    ps = h.ok(h.post("/tenants/acme/prompt-sets", {"request_id": rid(), "name": "core", "brand_terms": ["Acme"],
                                                   "competitor_domains": ["competitor.test"],
                                                   "prompts": ["q one about seo", "q two about seo"],
                                                   "engines": ["perplexity"]}), 201)
    return h, ps["prompt_set_id"]


def _audit(h, psid):
    return h.ok(h.post("/tenants/acme/audits", {"request_id": rid(), "domain": "acme.test", "paths": ["/"],
                                                "prompt_set_id": psid}, andre=True), 201)


def _callum(view):
    return next(e for e in view["report"]["agents"] if e["agent"] == "callum")


def test_status_shows_both_connectors(tmp_path):
    h, _ = _both(tmp_path, FakeGoogle(), FakePerplexity())
    st = h.ok(h.get("/status"))
    assert st["search_console"]["state"] == "connected"
    assert st["answer_engines"]["perplexity"]["state"] == "connected"
    assert st["answer_engines"]["perplexity"]["key_source"] == "env"
    assert KEY not in str(st)


def test_a_killed_search_console_never_hides_a_failed_perplexity_run(tmp_path):
    """provider:gsc KILLED inside its own section; Callum FAILED (two protocol errors in a row): the audit is FAILED,
    not KILLED. With the GSC-7 exclusion missing it would be KILLED; with Callum's outcome dropped it would not be
    FAILED."""
    pfake = FakePerplexity(httpx.Response(200, json={"unreadable": 1}))
    h, psid = _both(tmp_path, FakeGoogle(), pfake)
    h.ok(h.switch("provider:gsc"))
    view = _audit(h, psid)
    rep = view["report"]
    assert (section(view)["outcome"], section(view)["reason"]) == ("KILLED", "GSC_KILLED")
    assert rep["search_performance"]["outcome"] == "KILLED"
    assert _callum(view)["outcome"] == "FAILED" and len(pfake.requests) == 2
    assert rep["outcome"] == "FAILED"
    assert rep["outcome"] == worst_outcome(e["outcome"] for e in rep["agents"] if e["task"] != "search_performance")
    assert "search_performance" in rep["outcome_rule"]


def test_an_ok_search_console_read_never_lifts_a_failed_perplexity_run(tmp_path):
    pfake = FakePerplexity(httpx.Response(200, json={"unreadable": 1}))
    h, psid = _both(tmp_path, FakeGoogle(), pfake)
    view = _audit(h, psid)
    assert section(view)["outcome"] == "OK"
    assert _callum(view)["outcome"] == "FAILED"
    assert view["report"]["outcome"] == "FAILED"


def test_startup_applies_the_search_console_and_the_perplexity_refusals_together(tmp_path):
    keyfile = write_key_file(tmp_path / "sa.json")
    both = {"SEO_GSC_SERVICE_ACCOUNT_FILE": keyfile, "SEO_PERPLEXITY_API_KEY": KEY, **BUDGET}
    s = config_mod.load(base_env(**both))
    assert s.gsc_key is not None and s.perplexity_key is not None
    with pytest.raises(RuntimeError, match="SEO_GSC_MAX_REQUESTS"):          # GSC: budget below the page caps
        config_mod.load(base_env(**both, SEO_GSC_MAX_REQUESTS="6"))
    with pytest.raises(RuntimeError, match="both set"):                      # Perplexity: the key set two ways
        config_mod.load(base_env(**both, SEO_PERPLEXITY_API_KEY_FILE=_key_file(tmp_path)))
    with pytest.raises(RuntimeError, match="SEO_GSC_SERVICE_ACCOUNT_FILE"):   # GSC: the old variable name
        config_mod.load(base_env(**both, SEO_SEARCH_CONSOLE_CREDENTIALS_FILE=keyfile))
    with pytest.raises(RuntimeError) as e:                                   # Perplexity: the key is not a token
        config_mod.load(base_env(**{**both, "SEO_PERPLEXITY_API_KEY": base_env()["SEO_SERVICE_TOKEN"]}))
    assert "differ from every token" in str(e.value)
    s = config_mod.load(base_env(SEO_GSC_SERVICE_ACCOUNT_FILE=keyfile,
                                 SEO_PERPLEXITY_API_KEY_FILE=_key_file(tmp_path)))
    assert s.perplexity_key is not None and s.gsc_key is not None             # the key file is no longer NOT_BUILT
