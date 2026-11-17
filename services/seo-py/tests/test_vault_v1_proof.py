"""core-spec/2 V-1 / T-2 / T-6 proof (C2-9b, lead ruling 17:55 PT F9-1 read (a): proof, not re-architecture).

A synthetic canary is planted in every free-text input of the CL-1 writers (svc_citations / svc_vaulted: provider answer
excerpt, cited title, hint, prompt text, brand term; svc_optext / svc_audits: entity values, prompt-set content;
svc_logs: robots.txt text). After a REAL run every T-3 sink (every data-directory file, every ledger payload, every
captured log record, the error bodies) is scanned in every Wave-1 D-05 form (sa.forms): 0 hits. Every log record and
ledger payload passes the closed-type guard (no field points at a single free-text value outside a vault document: the
T-2 list (ii) is empty). V-5 smoke (positive control; full V-5 is C2-10): the owner finds its own canaries, another
tenant finds none. Canaries are generated letter tokens (legitimate words, so no masker hides them) and checked at run
time not to be a substring of any service source text (D-05). Failures report canary names, never values."""

from __future__ import annotations

import json
import logging
import os
import random
import string

import pytest

import sink_guard as sg
import test_logs as tl
import test_sink_audits as sa
import test_vault_wiring as vw
from helpers import invoice_id, rid
from ports import ProviderAnswer

SEED = 20261114
_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")


def _service_text() -> str:
    out = []
    for root, _d, files in os.walk(_SRC):
        for f in files:
            if f.endswith(".py"):
                with open(os.path.join(root, f), encoding="utf-8") as fh:
                    out.append(fh.read().lower())
    return "\n".join(out)


def canaries(names, seed: int = SEED) -> dict:
    """name -> a generated 18-letter token; each is checked not to occur in any service source text (D-05)."""
    rng, text = random.Random(seed), _service_text()
    out = {n: "qz" + "".join(rng.choice(string.ascii_lowercase) for _ in range(16)) for n in names}
    assert len(set(out.values())) == len(out)
    assert [n for n, c in out.items() if c in text] == []
    return out


def _guard_clean(h) -> list:
    """Every (kind, path) of a log record or ledger payload that is not a closed-type machine value (expected [])."""
    bad = [(r["kind"], p) for r in h.svc.log.iter_records() for p, _why in sg.violations(r["kind"], r["data"])]
    bad += [("ledger:" + e["event_type"], p) for e in h.ledger.events
            for p, _why in sg.violations("ledger:" + e["event_type"], e["_payload"])]
    return bad


# ------------------------------------------------------------------------------------- citation run (svc_citations)

CIT = ("excerpt", "title", "hint", "prompt", "brand")


def citation_world(tmp_path, c: dict):
    url = f"https://competitor.test/guides/{vw.MARK}-heron"
    a = ProviderAnswer("ANSWER", f"Z Best Media recovers revenue and {c['excerpt']} explains claims.", [url], "m",
                       "1", cite_from_text=False, provider="openai", asked_at="2026-10-09T18:00:00Z")
    a.citation_titles = {url: f"The {c['title']} guide"}
    a.citation_hints = {url: f"see {c['hint']}"}
    v = vw.synthetic()
    h = vw.harness(tmp_path)
    h.ports.engines["openai"] = vw.Port([a] + vw.answers(v))
    h.ok(h.post("/tenants/zbm/domains", {"request_id": rid(), "domains": [vw.DOMAIN]}, andre=True))
    psid = h.ok(h.post("/tenants/zbm/prompt-sets", {
        "request_id": rid(), "name": "core", "brand_terms": ["Z Best Media", c["brand"]],
        "competitor_domains": ["competitor.test"], "prompts": [f"who recovers marketplace revenue {c['prompt']}"],
        "engines": ["openai"]}), 201)["prompt_set_id"]
    run = h.ok(h.post("/tenants/zbm/citation-runs", {"request_id": rid(), "domain": vw.DOMAIN,
                                                     "prompt_set_id": psid}), 201)
    return h, run


def test_v1_citation_free_text_is_in_no_sink_in_any_form_and_the_owner_finds_it(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    c = canaries(CIT)
    h, run = citation_world(tmp_path, c)
    cid = run["citation_run_id"]
    assert run["status"] == "completed"
    h.tenant("acme", domains=("other.test",))
    own = json.dumps([h.ok(h.get(f"/tenants/zbm/citation-runs/{cid}", caller="hub", tenant="zbm")),
                      h.ok(h.get(f"/tenants/zbm/citation-runs/{cid}/mnc-priority", caller="hub", tenant="zbm"))], ensure_ascii=False)
    errors = [h.get(f"/tenants/acme/citation-runs/{cid}"), h.get(f"/tenants/acme/citation-runs/{cid}/mnc-priority")]
    assert [r.status_code for r in errors] == [404, 404]
    other = json.dumps(h.ok(h.get("/tenants/acme/citation-runs"))) + "".join(r.text for r in errors)
    # T-6 / V-1: 0 canaries in any sink, any D-05 form; (ii): no sink field holds a free-text value
    assert sa.found(sa.written(h, caplog) + other, c) == []
    assert _guard_clean(h) == []
    # V-5 smoke: the owner's vaulted document and display hold them; another tenant's view holds none
    doc = json.dumps(h.svc.vault.get_blob("zbm", h.svc.citation_runs[cid]["result_ref"]), ensure_ascii=False)
    assert [n for n in ("excerpt", "title", "prompt") if c[n] not in doc] == []
    assert [n for n in ("excerpt", "title", "prompt") if c[n] not in own] == []
    with pytest.raises(Exception):
        h.svc.vault.get_blob("acme", h.svc.citation_runs[cid]["result_ref"])


# ------------------------------------------------------------------ entity + prompt set + audit (svc_optext / audits)

AUD = ("entity_name", "entity_street", "prompt", "brand")


@pytest.mark.local_http                                 # the fixture site is an in-process server on 127.0.0.1
def test_v1_operator_text_and_audit_report_are_in_no_sink_and_the_owner_finds_them(tmp_path, srv, caplog):
    caplog.set_level(logging.DEBUG)
    c = canaries(AUD, SEED + 1)
    h = sa.harness(tmp_path, srv, own=False)
    h.tenant("acme", domains=(sa.DOMAIN,))
    h.ok(h.post("/tenants/acme/finance-client", {"request_id": rid(), "finance_client_id": "acme-party-1"},
                andre=True))
    h.ok(h.post("/tenants/acme/entity", {"request_id": rid(), "fields": [
        {"field": "name", "value": f"Acme {c['entity_name']}", "source": "founder_statement", "provenance": "stated",
         "authority": "first_party"},
        {"field": "street_address", "value": f"12 {c['entity_street']} St", "source": "founder_statement",
         "provenance": "stated", "authority": "first_party"}]}, andre=True), 201)
    ps = h.ok(h.post("/tenants/acme/prompt-sets", {
        "request_id": rid(), "name": "core", "brand_terms": ["Z Best Media", c["brand"]], "competitor_domains": [],
        "prompts": [f"who recovers marketplace revenue {c['prompt']}"], "engines": ["openai"]},
        caller="dashboard"), 201)
    a = h.ok(sa.audit(h, "acme", invoice_id=invoice_id(), prompt_set_id=ps["prompt_set_id"]), 201)
    assert a["status"] == "completed"
    aid = a["audit_id"]
    h.tenant("beta", domains=("other.test",))
    own = json.dumps([h.ok(h.get(f"/tenants/acme/audits/{aid}")), h.ok(h.get("/tenants/acme/entity"))],
                     ensure_ascii=False)
    errors = [h.get(f"/tenants/beta/audits/{aid}"), h.get("/tenants/beta/entity")]
    assert [r.status_code for r in errors] == [404, 404]          # another tenant: the same 404 as nothing at all
    other = "".join(r.text for r in errors)
    assert sa.found(sa.written(h, caplog) + other, c) == []
    assert _guard_clean(h) == []
    report = json.dumps(h.svc._audit_recorded("acme", aid)["report"], ensure_ascii=False)
    assert [n for n in ("entity_name", "prompt") if c[n] not in report] == []
    assert [n for n in ("entity_name", "entity_street", "prompt") if c[n] not in own] == []


# --------------------------------------------------------------------------------------- log ingest (svc_logs)

@pytest.mark.local_http                                 # the fixture site is an in-process server on 127.0.0.1
def test_v1_robots_text_is_in_no_sink_and_only_the_owner_reads_it(tmp_path, srv, caplog):
    caplog.set_level(logging.DEBUG)
    c = canaries(("robots",), SEED + 2)
    tl.install_site(srv, robots=f"User-agent: *\nDisallow: /private\n# {c['robots']}\n"
                    "Sitemap: http://site.test/sitemap.xml\n")
    h = tl.harness(tmp_path, srv, data_dir=str(tmp_path / "data"))
    tl.own(h)
    h.ok(tl.audit(h), 201)
    iid = h.ok(tl.ingest(h), 201)["ingest_id"]
    h.ok(tl.send(h, iid, 1, tl.b64([tl.clf("66.249.66.1", "/", 200, tl.GOOGLE_UA)]), last=True))
    h.ok(h.post(f"/tenants/zbm/log-ingests/{iid}/finish", {"request_id": rid()}))
    assert sa.found(sa.written(h, caplog), c) == []
    assert _guard_clean(h) == []
    ref = h.svc.log_ingests[iid]["inputs_ref"]
    assert c["robots"] in h.svc._ingest_inputs("zbm", iid)["robots_text"]
    with pytest.raises(Exception):
        h.svc.vault.get_blob("acme", ref)
