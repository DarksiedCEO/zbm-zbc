"""Sink job D: the last free text leaves the log. Entity records, prompt sets and log ingests are vault blobs; the
log and the ledger keep ids, schema names, counts, codes, refs and keyed HMACs. Python logging is a sink too
(log_sink.py); an error body repeats nothing the caller sent; every hash field in the log and the ledger has a
decision (``HASH_FIELDS``); there is no legacy list.

Every sensitive-looking value is GENERATED at run time; failures report names and counts only."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import string

import pytest

import api
import helpers
import log_sink
import sink_guard as sg
import svc_entity
import svc_vaulted
import test_logs as tl
import test_sink_audits as sa
import vault as vault_mod
from agents import logs as logs_mod
from envelope import sha
from helpers import rid

AT = "@"
DOMAIN = sa.DOMAIN
pytestmark = pytest.mark.local_http                    # the fixture site is an in-process server on 127.0.0.1


def word(rng, n=10) -> str:
    return "".join(rng.choice(string.ascii_lowercase) for _ in range(n))


def planted(seed: int) -> dict:
    rng = random.Random(seed)
    return {"prose": f"quiet {word(rng)} river {word(rng)}", "email": word(rng, 8) + AT + word(rng, 7) + ".example",
            "digits": "".join(rng.choice("123456789") for _ in range(14)), "term": f"{word(rng)} {word(rng)}",
            "path": "/reset/" + word(rng, 12), "ctx_email": word(rng, 9) + AT + word(rng, 6) + ".example"}


def records(h, kind):
    return sa.records(h, kind)


def lose(vault, ref: str) -> None:
    vault._index.pop(vault_mod.parse_blob_ref(ref))


def entity_body(v: dict) -> dict:
    return {"request_id": rid(), "fields": [
        {"field": "name", "value": f"Acme {v['prose']}", "source": "founder_statement",
         "provenance": f"said by {v['email']}", "authority": "first_party"},
        {"field": "street_address", "value": f"{v['digits']} Main St", "source": "site_footer",
         "provenance": v["term"], "authority": "third_party"},
        {"field": "telephone", "value": "(562) 555-0142", "source": "founder_statement", "provenance": "stated",
         "authority": "first_party"}]}


def prompt_set_body(v: dict, name="core") -> dict:
    return {"request_id": rid(), "name": name, "brand_terms": ["Z Best Media", v["term"]],
            "brand_term_rules": [{"term": "Z Best Media", "context_terms": [v["ctx_email"], "revenue"],
                                  "negative_terms": [v["prose"]]}],
            "competitor_domains": ["competitor.test"], "prompts": [f"what about {v['prose']}", "who recovers revenue"],
            "engines": ["openai"]}


def client(h, v: dict) -> dict:
    h.tenant("acme", domains=(DOMAIN,))
    h.ok(h.post("/tenants/acme/finance-client", {"request_id": rid(), "finance_client_id": "acme-party-1"},
                andre=True))
    ent = h.ok(h.post("/tenants/acme/entity", entity_body(v), andre=True), 201)
    ps = h.ok(h.post("/tenants/acme/prompt-sets", prompt_set_body(v), caller="dashboard"), 201)
    return {"entity": ent, "prompt_set": ps}


# ================================================================================================ no legacy list

def test_the_guard_has_no_exceptions():
    assert not any(hasattr(sg, n) for n in ("LEGACY_PROSE_PATHS", "LEGACY_ROWS", "LEGACY_COUNTS", "_legacy"))
    text = "plain words here"
    for kind in ("entity_created", "prompt_set_version", "log_ingest_created", "log_ingest_finished", "log_chunk"):
        for data in ({"fields": {"name": {"value": text}}}, {"content": {"prompts": [text]}},
                     {"robots_text": text}, {"report": {"envelope": {"methodology": text}}},
                     {"delta": {"families": {"Googlebot": {"templates": {"/a b": 1}}}}}):
            assert sg.refused(kind, data) != [], kind


def test_every_strict_verdict_name_is_a_machine_value(tmp_path):
    """A ``flags`` entry holds a strict verdict name: each one is a code the guard takes (``d_token`` reads as a
    credential label, so the closed tuple ``secret_guard.CHECKS`` is in the guard's service words)."""
    from agents import secret_guard
    for name in secret_guard.CHECKS + (secret_guard.CREDENTIAL, "personal"):
        assert sg.classify(name) == "code", name
    # an entity value the strict check flags as a random token is accepted, vaulted and flagged in the line
    h = helpers.Harness(tmp_path)
    h.tenant("acme", "client", ("acme.test",))
    rng = random.Random(77)
    token = "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(44))
    assert secret_guard.check("ref " + token) == "d_token"
    h.ok(h.post("/tenants/acme/entity", {"request_id": rid(), "fields": [
        {"field": "name", "value": "ref " + token, "source": "founder_statement", "provenance": "stated",
         "authority": "first_party"}]}, andre=True), 201)
    (d,) = [r["data"] for r in records(h, "entity_created") if r["data"]["tenant_id"] == "acme"]
    assert d["flags"]["name"] == {"value": "d_token", "provenance": None} and token not in json.dumps(d)


# ================================================================================================ entity records

def test_entity_records_keep_names_flags_a_ref_and_a_keyed_hash(tmp_path, srv):
    v = planted(1)
    h = sa.harness(tmp_path, srv, own=False)
    got = client(h, v)
    eid = got["entity"]["entity_id"]
    (created,) = [r["data"] for r in records(h, "entity_created") if r["data"]["entity_id"] == eid]
    assert set(created) == {"entity_id", "tenant_id", "fields", "flags", "content_ref", "content_hmac", "actor",
                            "request_id", "request_sha", "_obj", "evidence"}
    assert created["fields"] == ["name", "street_address", "telephone"]
    assert set(created["fields"]) <= set(svc_entity.FIELDS)
    assert created["flags"]["street_address"]["value"] == "e_digits" and created["flags"]["telephone"]["value"] is None
    assert sg.violations("entity_created", created) == []
    doc = h.svc.optext_vault.get_blob("acme", created["content_ref"])
    assert doc["fields"]["name"] == {"value": f"Acme {v['prose']}", "source": "founder_statement",
                                     "provenance": f"said by {v['email']}", "authority": "first_party"}
    canon = svc_vaulted.canonical(doc)
    assert created["content_hmac"] == h.svc.optext_vault.field_hash("acme", "entity_content", canon)[1]
    assert created["content_hmac"] != hashlib.sha256(canon.encode()).hexdigest()
    ev = h.ledger.of_type("entity_created")[-1]["_payload"]
    assert set(ev) == {"entity_id", "tenant_id", "content_hmac", "rk", "seq"} and "fields_sha256" not in ev
    # an update: ONE blob for the new version; the line names the field and the version
    h.ok(h.post(f"/entities/{eid}/fields", {
        "request_id": rid(), "field": "locality", "value": v["term"], "source": "founder_statement",
        "provenance": v["prose"], "authority": "first_party", "expected_version": 1}, andre=True))
    (upd,) = [r["data"] for r in records(h, "entity_field_set")]
    assert {k: upd[k] for k in ("field", "version")} == {"field": "locality", "version": 2}
    assert not {"value", "provenance", "source", "authority"} & set(upd) and sg.violations("entity_field_set", upd) == []
    assert h.svc.optext_vault.get_blob("acme", upd["content_ref"])["fields"]["locality"]["value"] == v["term"]
    assert "value_sha256" not in h.ledger.of_type("entity_field_set")[-1]["_payload"]
    # the founder's own record is named, not copied
    (seed,) = [r["data"] for r in records(h, "entity_created") if r["data"]["entity_id"] == svc_entity.ZBM_ENTITY]
    assert seed["seed"] == svc_entity.SEED_NAME and "fields" not in seed and "content_ref" not in seed
    assert h.svc.entities[svc_entity.ZBM_ENTITY]["fields"]["name"]["value"] == svc_entity.SEED["name"]
    blob = sa.written(h)
    assert sa.found(blob, v) == [] and svc_entity.SEED["street_address"] not in blob \
        and svc_entity.SEED_PROVENANCE not in blob
    # a restart reads the blobs back; the owner's view is the same
    before = h.ok(h.get("/tenants/acme/entity"))
    h2 = h.restart()
    assert h2.ok(h2.get("/tenants/acme/entity")) == before and "vault_unavailable" not in before
    assert h2.svc.entities[eid]["fields"]["locality"]["value"] == v["term"]
    assert h2.svc.entities[eid]["fields"]["locality"]["history"] == []
    assert h2.svc.entities[eid]["version"] == 2


def test_a_lost_entity_blob_fails_closed_everywhere(tmp_path, srv):
    v = planted(2)
    h = sa.harness(tmp_path, srv, own=False)
    got = client(h, v)
    eid = got["entity"]["entity_id"]
    (created,) = [r["data"] for r in records(h, "entity_created") if r["data"]["entity_id"] == eid]
    lose(h.svc.optext_vault, created["content_ref"])
    os.remove(os.path.join(h.settings.data_dir, vault_mod.OPERATOR_VAULT_NAME))
    h2 = h.restart()
    view = h2.ok(h2.get("/tenants/acme/entity"))
    assert view["vault_unavailable"] is True
    assert {f["value"] for f in view["fields"].values()} == {None} and set(view["fields"]) == set(created["fields"])
    assert svc_entity.unreadable(h2.svc.entity_for_tenant("acme", raw=True))
    # the audit's entity check does not run against a record it cannot read (never a comparison with None)
    a = h2.ok(h2.post("/tenants/acme/audits", {"request_id": rid(), "domain": DOMAIN, "scheme": "http",
                                                "paths": ["/"], "invoice_id": helpers.invoice_id(3)},
                      caller="dashboard", andre=True), 201)
    (step,) = [e for e in a["report"]["agents"] if e["agent"] == "entity_check"]
    assert (step["outcome"], step["reason"], step["findings"]) == ("BLOCKED", "VAULT_UNAVAILABLE", [])
    # the operator repairs it by setting each field again
    for f in created["fields"]:
        h2.ok(h2.post(f"/entities/{eid}/fields", {
            "request_id": rid(), "field": f, "value": "(562) 555-0142" if f == "telephone" else "Acme again",
            "source": "founder_statement", "provenance": "restated", "authority": "first_party",
            "expected_version": h2.svc.entities[eid]["version"]}, andre=True))
    assert "vault_unavailable" not in h2.ok(h2.get("/tenants/acme/entity"))


def test_entity_lines_written_before_this_change_still_replay(tmp_path, srv):
    v = planted(3)
    h = sa.harness(tmp_path, srv, own=False)
    h.tenant("acme", domains=(DOMAIN,))
    stubs = h.svc._optext_seal("acme", "entity", [v["prose"], v["term"]], [None, None])
    h.svc._optext_committed()
    sa.raw_commit(h.svc, "entity_created", {"entity_id": "ent-acme", "tenant_id": "acme", "fields": {
        "name": {"value": stubs[0], "source": "founder_statement", "provenance": stubs[1],
                 "authority": "first_party"}}}, "andre")
    (s2,) = h.svc._optext_seal("acme", "entity", [v["digits"]], [None])
    h.svc._optext_committed()
    sa.raw_commit(h.svc, "entity_field_set", {"entity_id": "ent-acme", "field": "locality", "value": s2,
                                               "source": "site_footer", "provenance": s2, "authority": "inferred",
                                               "version": 2}, "andre")
    h2 = h.restart()
    e = h2.svc.entities["ent-acme"]
    assert e["fields"]["name"]["value"] == v["prose"] and e["fields"]["name"]["provenance"] == v["term"]
    assert e["fields"]["locality"]["value"] == v["digits"] and e["version"] == 2
    # and the old seed line (plain values) replays as it was recorded
    (tmp_path / "old-seed").mkdir()
    h3 = helpers.Harness(tmp_path / "old-seed", data_dir=str(tmp_path / "old-seed" / "data"))
    del h3.svc.entities[svc_entity.ZBM_ENTITY]
    sa.raw_commit(h3.svc, "entity_created", {"entity_id": svc_entity.ZBM_ENTITY, "tenant_id": "zbm", "fields": {
        k: dict(f) for k, f in svc_entity.SEEDS[svc_entity.SEED_NAME].items()}})
    h4 = h3.restart()
    assert h4.svc.entities[svc_entity.ZBM_ENTITY]["fields"]["telephone"]["value"] == svc_entity.SEED["telephone"]
    assert len([r for r in h4.svc.log.iter_records() if r["kind"] == "entity_created"]) == 2


# ================================================================================================ prompt sets

def test_a_prompt_set_version_is_one_blob_and_the_line_keeps_counts(tmp_path, srv, caplog):
    caplog.set_level(logging.DEBUG)
    v = planted(4)
    h = sa.harness(tmp_path, srv, own=False)
    ps = client(h, v)["prompt_set"]
    (d,) = [r["data"] for r in records(h, "prompt_set_version")]
    assert set(d) == {"prompt_set_id", "tenant_id", "version", "content_ref", "content_hmac", "facts", "actor",
                      "request_id", "request_sha", "_obj", "evidence"}
    assert d["facts"] == {"prompts": 2, "brand_terms": 2, "brand_term_rules": 1, "competitor_domains": 1,
                          "engines": ["openai"]}
    assert sg.violations("prompt_set_version", d) == []
    content = h.svc.optext_vault.get_blob("acme", d["content_ref"])
    assert content["name"] == "core" and content["brand_terms"] == ["Z Best Media", v["term"]]
    assert content["brand_term_rules"][0]["context_terms"] == [v["ctx_email"], "revenue"]
    canon = svc_vaulted.canonical(content)
    assert d["content_hmac"] == h.svc.optext_vault.field_hash("acme", "prompt_set_content", canon)[1]
    assert d["content_hmac"] not in (hashlib.sha256(canon.encode()).hexdigest(), sha(content))
    ev = h.ledger.of_type("prompt_set_version")[-1]["_payload"]
    assert set(ev) == {"prompt_set_id", "tenant_id", "version", "content_hmac", "rk", "seq"}
    # the fingerprint every later record names is that keyed HMAC
    cur = ps["versions"]["1"]
    assert cur["sha256"] == d["content_hmac"] and "plain_sha256" not in cur
    # the known gap is closed: the e-mail-shaped context term, the brand term and the rule's term are nowhere
    blob = sa.written(h, caplog)
    assert sa.found(blob, v) == []
    assert "Z Best Media" not in blob and "revenue" not in "".join(json.dumps(r) for r in records(h, "prompt_set_version"))
    # the same content again is a conflict (compared by the keyed HMAC), also after a restart
    h.refused(h.post("/tenants/acme/prompt-sets", {**prompt_set_body(v)}, caller="dashboard"), 409, "PROMPT_SET_EXISTS")
    h2 = h.restart()
    h2.refused(h2.post("/tenants/acme/prompt-sets", {**prompt_set_body(v)}, caller="dashboard"), 409,
               "PROMPT_SET_EXISTS")
    again = h2.ok(h2.get(f"/tenants/acme/prompt-sets/{ps['prompt_set_id']}"))
    assert again == h.ok(h.get(f"/tenants/acme/prompt-sets/{ps['prompt_set_id']}"))
    v2 = h2.ok(h2.post("/tenants/acme/prompt-sets", {**prompt_set_body(v), "prompts": ["another question here"]},
                       caller="dashboard"), 201)
    assert v2["current"] == 2


def test_a_lost_prompt_set_blob_makes_the_set_unusable(tmp_path, srv):
    v = planted(5)
    h = sa.harness(tmp_path, srv, own=False)
    ps = client(h, v)["prompt_set"]
    os.remove(os.path.join(h.settings.data_dir, vault_mod.OPERATOR_VAULT_NAME))
    h2 = h.restart()
    cur = h2.svc.prompt_sets[ps["prompt_set_id"]]["versions"]["1"]
    assert cur["vault_unavailable"] is True and cur["prompts"] == [] and cur["brand_terms"] == []
    view = h2.ok(h2.get(f"/tenants/acme/prompt-sets/{ps['prompt_set_id']}"))
    assert view["versions"]["1"]["vault_unavailable"] is True
    body = {"request_id": rid(), "domain": DOMAIN, "prompt_set_id": ps["prompt_set_id"]}
    h2.refused(h2.post("/tenants/acme/citation-runs", body, caller="dashboard", andre=True), 503, "VAULT_UNAVAILABLE")
    h2.refused(h2.post("/tenants/acme/audits", {**body, "scheme": "http", "paths": ["/"],
                                                "invoice_id": helpers.invoice_id(4)}, caller="dashboard",
                       andre=True), 503, "VAULT_UNAVAILABLE")


def test_a_prompt_set_line_written_before_this_change_still_replays(tmp_path, srv):
    v = planted(6)
    h = sa.harness(tmp_path, srv)
    stubs = h.svc._optext_seal("zbm", "prompt", [f"what about {v['prose']}"])
    h.svc._optext_committed()
    content = {"name": "old", "brand_terms": ["Z Best Media"], "competitor_domains": [], "prompts": stubs,
               "engines": ["openai"]}
    hashed = {**content, "prompts": [s["hmac"] for s in stubs]}
    psid = __import__("ledger").derived_id("pst", "zbm", "old")
    sa.raw_commit(h.svc, "prompt_set_version", {"prompt_set_id": psid, "tenant_id": "zbm", "version": 1,
                                                "content": content, "sha256": sha(hashed)}, "dashboard")
    h2 = h.restart()
    cur = h2.svc.prompt_sets[psid]["versions"]["1"]
    assert cur["prompts"] == [f"what about {v['prose']}"] and cur["sha256"] == sha(hashed) and cur["plain_sha256"]
    view = h2.ok(h2.get(f"/tenants/zbm/prompt-sets/{psid}"))
    assert "plain_sha256" not in view["versions"]["1"]
    # the same content posted now is still recognised as the existing version (the old hash rule)
    h2.refused(h2.post("/tenants/zbm/prompt-sets", {
        "request_id": rid(), "name": "old", "brand_terms": ["Z Best Media"], "competitor_domains": [],
        "prompts": [f"what about {v['prose']}"], "engines": ["openai"]}), 409, "PROMPT_SET_EXISTS")
    a = h2.ok(sa.audit(h2, prompt_set_id=psid), 201)
    assert a["status"] == "completed"


# ================================================================================================ log ingests

def log_lines(v: dict, n=30) -> list:
    return [tl.clf("66.249.66.1", f"{v['path']}/{i}?who={v['email']}", 200, tl.GOOGLE_UA + " " + v["prose"])
            for i in range(n)] + [tl.clf("1.1.1.1", "/private/x", 200, tl.GPT_UA), "not a log line " + v["term"]]


def ingest_world(tmp_path, srv, v):
    tmp_path.mkdir(parents=True, exist_ok=True)
    tl.install_site(srv, robots=f"User-agent: *\nDisallow: /private\n# {v['prose']}\n"
                    "Sitemap: http://site.test/sitemap.xml\n")
    h = tl.harness(tmp_path, srv, data_dir=str(tmp_path / "data"))
    tl.own(h)
    h.ok(tl.audit(h), 201)
    return h


def test_a_log_ingest_keeps_refs_keyed_hashes_counts_and_codes_only(tmp_path, srv, caplog):
    caplog.set_level(logging.DEBUG)
    v = planted(7)
    h = ingest_world(tmp_path, srv, v)
    iid = h.ok(tl.ingest(h), 201)["ingest_id"]
    h.ok(tl.send(h, iid, 1, tl.b64(log_lines(v))))
    h.ok(tl.send(h, iid, 2, tl.b64(log_lines(v, 5)), last=True))
    fin = h.ok(h.post(f"/tenants/zbm/log-ingests/{iid}/finish", {"request_id": rid()}))
    (c,) = [r["data"] for r in records(h, "log_ingest_created")]
    assert {"inputs_ref", "inputs_hmac", "robots_present", "sitemap_sample_count", "robots_status"} <= set(c)
    assert "robots_text" not in c and "sitemap_sample" not in c and c["robots_present"] is True
    assert c["sitemap_sample_count"] == 2
    inputs = h.svc.vault.get_blob("zbm", c["inputs_ref"])
    assert v["prose"] in inputs["robots_text"] and inputs["sitemap_sample"] == ["/", "/about"]
    chunks = [r["data"] for r in records(h, "log_chunk")]
    assert len(chunks) == 2
    for d in chunks:
        assert "delta" not in d and set(d["counts"]) == {"lines", "non_bot", "no_user_agent", "quarantined",
                                                         "families"}
        assert set(d["counts"]["families"]["Googlebot"]) == {"requests", "verified", "spoofed", "claimed"}
        delta = h.svc.vault.get_blob("zbm", d["delta_ref"])
        assert delta["families"]["Googlebot"]["templates"] and len(delta["families"]["Googlebot"]["hll"]) == 256
        assert d["delta_hmac"] == h.svc.vault.field_hash("zbm", "log_chunk_delta", svc_vaulted.canonical(delta))[1]
    assert chunks[0]["counts"]["lines"] == 32 and chunks[0]["counts"]["quarantined"] == {"MALFORMED": 1}
    (f,) = [r["data"] for r in records(h, "log_ingest_finished")]
    assert "report" not in f and "report_sha256" not in f
    assert f["facts"]["outcome"] == fin["report"]["outcome"] and f["facts"]["lines"] == 39
    report = h.svc.vault.get_blob("zbm", f["report_ref"])
    assert report["envelope"]["methodology"] and report["envelope"]["facts"]["families"]["Googlebot"][
        "top_path_templates"]
    for kind in ("log_ingest_created", "log_chunk", "log_ingest_finished"):
        for r in records(h, kind):
            assert sg.violations(kind, r["data"]) == [], kind
    for et in ("log_ingest_created", "log_chunk_ingested", "log_report_recorded"):
        for e in h.ledger.of_type(et):
            assert sg.violations("ledger:" + et, e["_payload"]) == [], et
            assert not any(k in e["_payload"] for k in ("robots_sha256", "sitemap_sample_sha256", "delta_sha256",
                                                        "report_sha256")), et
    # nothing of a log line, robots.txt, a path template or the report's prose in any file, the ledger or the logs
    blob = sa.written(h, caplog)
    assert sa.found(blob, v) == []
    log_only = "".join(json.dumps(r) for r in h.svc.log.iter_records())
    for needle in ("66.249.66", "Mozilla", "Googlebot/2.1", "/reset", "Disallow", "line by line", "{id}", "/private"):
        assert needle not in log_only, needle
    # the owner reads the report; the view carries the keyed hash
    view = h.ok(h.get(f"/tenants/zbm/log-ingests/{iid}"))
    assert view["report"]["outcome"] == f["facts"]["outcome"] and view["report_hmac"] == f["report_hmac"]
    assert view["report_sha256"] is None and "vault_unavailable" not in view
    (row,) = h.ok(h.get("/tenants/zbm/log-ingests"))
    assert row["report"] is None and row["totals"]["lines"] == 39               # the list reads no blob


def test_a_restart_between_chunks_rebuilds_the_totals_from_the_blobs(tmp_path, srv):
    v = planted(8)
    reports = []
    for restart in (False, True):
        h = ingest_world(tmp_path / str(restart), srv, v)
        iid = h.ok(tl.ingest(h), 201)["ingest_id"]
        h.ok(tl.send(h, iid, 1, tl.b64(log_lines(v))))
        if restart:
            h = h.restart()
            assert iid not in h.svc._log_full and iid not in h.svc._log_inputs
            assert set(h.svc.log_ingests[iid]["totals"]["families"]["Googlebot"]) == {"requests", "verified",
                                                                                     "spoofed", "claimed"}
        h.ok(tl.send(h, iid, 2, tl.b64(log_lines(v, 7)), last=True))
        fin = h.ok(h.post(f"/tenants/zbm/log-ingests/{iid}/finish", {"request_id": rid()}))
        rep = json.loads(json.dumps(fin["report"]))
        rep["ingest_id"] = None
        reports.append(rep)
        assert iid not in h.svc._log_full and iid not in h.svc._log_inputs      # dropped when the ingest finishes
    assert reports[0] == reports[1] and reports[0]["envelope"]["facts"]["lines"] == 41


@pytest.mark.parametrize("part", ["inputs", "delta", "report"])
def test_a_lost_ingest_blob_fails_closed(tmp_path, srv, part):
    v = planted(9)
    h = ingest_world(tmp_path, srv, v)
    iid = h.ok(tl.ingest(h), 201)["ingest_id"]
    h.ok(tl.send(h, iid, 1, tl.b64(log_lines(v))))
    if part == "report":
        h.ok(h.post(f"/tenants/zbm/log-ingests/{iid}/finish", {"request_id": rid()}))
        lose(h.svc.vault, h.svc.log_ingests[iid]["report_ref"])
        view = h.ok(h.get(f"/tenants/zbm/log-ingests/{iid}"))
        assert view["report"] is None and view["vault_unavailable"] is True
        assert h.ok(h.get("/department"))["agents"]["selene"]                    # the scorecard reads facts only
        return
    g = h.svc.log_ingests[iid]
    lose(h.svc.vault, g["inputs_ref"] if part == "inputs" else g["deltas"][0][0])
    h.svc._log_inputs.clear()
    h.svc._log_full.clear()                                                      # as after a restart
    before = len(h.svc.log)
    if part == "inputs":
        h.refused(tl.send(h, iid, 2, tl.b64(log_lines(v, 3))), 503, "VAULT_UNAVAILABLE")
    h.refused(h.post(f"/tenants/zbm/log-ingests/{iid}/finish", {"request_id": rid()}), 503, "VAULT_UNAVAILABLE")
    assert len(h.svc.log) == before and h.svc.log_ingests[iid]["status"] == "open"


def test_ingest_lines_written_before_this_change_still_replay(tmp_path, srv):
    v = planted(10)
    h = ingest_world(tmp_path, srv, v)
    iid = __import__("ledger").derived_id("lgi", "zbm", "seo_agent", "old")
    key_fp = hashlib.sha256(h.settings.log_hash_key).hexdigest()[:16]
    robots = "User-agent: *\nDisallow: /private\n"
    sa.raw_commit(h.svc, "log_ingest_created", {"ingest_id": iid, "tenant_id": "zbm", "domain": DOMAIN,
                                                "scheme": "http", "format": "combined", "key_fp": key_fp,
                                                "robots_status": "2xx", "robots_text": robots,
                                                "sitemap_sample": ["/", "/about"]}, "seo_agent")
    delta = logs_mod.process_chunk("combined", tl.chunk(log_lines(v)), h.settings.log_hash_key,
                                   h.ports.bot_verifier.verify, {}, {"left": 10}, None, {"/": 0, "/about": 1})
    sa.raw_commit(h.svc, "log_chunk", {"ingest_id": iid, "seq": 1, "last": False, "bytes": 100, "delta": delta},
                  "seo_agent")
    assert h.svc.log_ingests[iid]["legacy"] and "templates" in h.svc.log_ingests[iid]["totals"]["families"]["Googlebot"]
    # a chunk sent NOW to that ingest is a blob; the totals stay whole and the finish builds the report
    h.ok(tl.send(h, iid, 2, tl.b64(log_lines(v, 4)), last=True))
    h2 = h.restart()
    g = h2.svc.log_ingests[iid]
    assert g["totals"]["lines"] == 32 + 6 and g["totals"]["families"]["Googlebot"]["templates"]
    fin = h2.ok(h2.post(f"/tenants/zbm/log-ingests/{iid}/finish", {"request_id": rid()}))
    assert fin["report"]["envelope"]["facts"]["lines"] == 38 and fin["report_hmac"]
    # an old finished line (the report inline, a plain hash) replays and is served
    iid2 = __import__("ledger").derived_id("lgi", "zbm", "seo_agent", "old2")
    sa.raw_commit(h2.svc, "log_ingest_created", {"ingest_id": iid2, "tenant_id": "zbm", "domain": DOMAIN,
                                                 "scheme": "http", "format": "combined", "key_fp": key_fp,
                                                 "robots_status": "2xx", "robots_text": None,
                                                 "sitemap_sample": None}, "seo_agent")
    old = {"ingest_id": iid2, "outcome": "INSUFFICIENT_EVIDENCE",
           "envelope": {"agent": "selene", "outcome": "INSUFFICIENT_EVIDENCE", "findings": [], "facts": {}}}
    sa.raw_commit(h2.svc, "log_ingest_finished", {"ingest_id": iid2, "report": old, "report_sha256": sha(old)},
                  "seo_agent")
    h3 = h2.restart()
    view = h3.ok(h3.get(f"/tenants/zbm/log-ingests/{iid2}"))
    assert view["report"]["outcome"] == "INSUFFICIENT_EVIDENCE" and view["report_sha256"] == sha(old)
    assert h3.ok(h3.get("/department"))["agents"]["selene"]


# ================================================================================================ Search Console

def test_a_lost_property_record_keeps_the_host_bound(tmp_path):
    v = planted(11)
    h = helpers.Harness(tmp_path, data_dir=str(tmp_path / "data"))
    h.tenant("acme", "client", ("acme.test",))
    h.tenant("globex", "client", ("globex.test",))
    prop = "https://acme.test" + v["path"]
    t = h.ok(h.post("/tenants/acme/gsc-property", {"request_id": rid(), "property": prop}, andre=True))
    assert t["gsc_property"] == prop and "gsc_vault_unavailable" not in t and "gsc_property_host" not in t
    (d,) = [r["data"] for r in records(h, "tenant_gsc_property_set")]
    assert d["property_host"] == "acme.test" and len(d["property"]["hmac"]) == 64
    assert sg.violations("tenant_gsc_property_set", d) == [] and v["path"] not in json.dumps(d)
    ev = h.ledger.of_type("tenant_gsc_property_set")[-1]["_payload"]
    assert ev["property_hmac"] == d["property"]["hmac"] and "property_sha256" not in ev
    os.remove(os.path.join(h.settings.data_dir, vault_mod.OPERATOR_VAULT_NAME))
    h2 = h.restart()
    t = h2.ok(h2.get("/tenants/acme"))
    assert t["gsc_property"] is None and t["gsc_vault_unavailable"] is True
    assert h2.ok(h2.get("/status"))["search_console"]["tenants_bound"] == 1    # still bound: not "unbound"
    # the host is NOT free: another tenant that came to hold the domain cannot bind it (nor a parent / subdomain)
    h2.svc.tenants["globex"]["domains"] = ["acme.test", "shop.acme.test"]
    for other in ("sc-domain:acme.test", "https://acme.test/", "https://shop.acme.test/x"):
        h2.refused(h2.post("/tenants/globex/gsc-property", {"request_id": rid(), "property": other}, andre=True),
                   409, "GSC_PROPERTY_TAKEN")
    # Andre re-binds (or unbinds) the tenant: the mark goes
    t = h2.ok(h2.post("/tenants/acme/gsc-property", {"request_id": rid(), "property": "sc-domain:acme.test"},
                      andre=True))
    assert t["gsc_property"] == "sc-domain:acme.test" and "gsc_vault_unavailable" not in t
    (dom,) = [r["data"] for r in records(h2, "tenant_gsc_property_set")][-1:]
    assert dom["property_kind"] == "domain" and dom["property_host"] == "acme.test" and "property" not in dom
    h3 = h2.restart()
    assert h3.ok(h3.get("/tenants/acme"))["gsc_property"] == "sc-domain:acme.test"
    h3.ok(h3.post("/tenants/acme/gsc-property", {"request_id": rid(), "property": None}, andre=True))
    assert h3.ok(h3.get("/status"))["search_console"]["tenants_bound"] == 0


def test_a_long_domain_property_is_logged_as_its_host(tmp_path):
    h = helpers.Harness(tmp_path)
    host = "a" + "b" * 55 + ".example.test"                   # ``sc-domain:`` + this is longer than a code may be
    h.tenant("acme", "client", (host,))
    t = h.ok(h.post("/tenants/acme/gsc-property", {"request_id": rid(), "property": "sc-domain:" + host}, andre=True))
    assert t["gsc_property"] == "sc-domain:" + host


def test_an_identifier_the_sink_would_refuse_is_refused_by_its_route(tmp_path):
    """A tenant id, a domain or a Finance client id is logged: one that is not a machine value answers 422
    IDENTIFIER_WITHHELD and writes nothing (it used to be accepted and then refused at the sink, 503)."""
    h = helpers.Harness(tmp_path)
    h.tenant("acme", "client", ("acme.test",))
    n = len(h.svc.log)
    ssn_label = "-".join(("321", "54", "9876")) + ".acme-shop.test"
    for domains in ([ssn_label], ["acme.test", ssn_label]):
        h.refused(h.post("/tenants/acme/domains", {"request_id": rid(), "domains": domains}, andre=True), 422,
                  "IDENTIFIER_WITHHELD")
    for cid in ("c" * 65, "2026-10.7", "a" + "b" * 99):
        h.refused(h.post("/tenants/acme/finance-client", {"request_id": rid(), "finance_client_id": cid}, andre=True),
                  422, "IDENTIFIER_WITHHELD")
    assert len(h.svc.log) == n
    h.ok(h.post("/tenants/acme/finance-client", {"request_id": rid(), "finance_client_id": "Acme_Party.7-b"},
                andre=True))
    h.ok(h.post("/tenants/acme/domains", {"request_id": rid(), "domains": ["acme.test", "shop-2.acme.test"]},
                andre=True))


# ================================================================================================ idempotency hash

def test_the_request_hash_is_never_a_plain_hash_of_client_text(tmp_path):
    v = planted(12)
    h = helpers.Harness(tmp_path, data_dir=str(tmp_path / "data"))
    body = prompt_set_body(v)
    a = h.ok(h.post("/tenants/zbm/prompt-sets", body), 201)
    assert h.ok(h.post("/tenants/zbm/prompt-sets", body), 201) == a             # the same request: the same answer
    h.refused(h.post("/tenants/zbm/prompt-sets", {**body, "prompts": ["a different question"]}), 409,
              "REQUEST_ID_REUSED")
    (d,) = [r["data"] for r in records(h, "prompt_set_version")]
    from ledger import payload_sha256
    parsed = json.loads(json.dumps(body))
    assert d["request_sha"] != payload_sha256(parsed)
    # a body of machine values only hashes as it always did (lines written before this change stay idempotent)
    plain = {"request_id": rid(), "tenant_id": "plainco", "kind": "client"}
    h.ok(h.post("/tenants", plain, andre=True), 201)
    (t,) = [r["data"] for r in records(h, "tenant_created") if r["data"]["tenant_id"] == "plainco"]
    assert t["request_sha"] == payload_sha256(plain) and h.svc._machine_body(plain) is plain
    # every free-text leaf of a projected body is a keyed HMAC bound to the request field
    out = h.svc._machine_body({"a": [v["prose"], "ok_code"], "b": {"c": v["email"]}})
    want = h.svc.optext_vault.field_hash("seo", "request_text", v["prose"])[1]
    assert out == {"a": [want, "ok_code"], "b": {"c": h.svc.optext_vault.field_hash("seo", "request_text", v["email"])[1]}}
    # a log chunk's request hash is over the keyed HMAC of its base64 text, not the text
    (tmp_path / "nokey").mkdir()
    nokey = helpers.Harness(tmp_path / "nokey", data_dir=str(tmp_path / "nokey" / "data"), SEO_VAULT_KEY_FILE=None)
    with pytest.raises(Exception) as e:
        nokey.svc._machine_body({"data_b64": "x" * 400})
    assert getattr(e.value, "reason", None) == "VAULT_UNAVAILABLE"


# ================================================================================================ the logging sink

def test_the_logging_sink_keeps_literal_templates_and_machine_arguments_only(tmp_path, caplog):
    v = planted(13)
    helpers.Harness(tmp_path)
    assert log_sink.installed()
    caplog.set_level(logging.DEBUG)
    path = tmp_path / "process.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        log = logging.getLogger("seo.test")
        log.error("audit run failed: %s in %s at %s", "ValueError", "selene", "svc_audits:_run_audit:12")
        log.warning("counts %d and %s", 12, "seo-aud-" + "a" * 40)
        log.error("user said %s", v["prose"])
        log.error("contact %s id %s", v["email"], v["digits"])
        log.error("a number %d", int(v["digits"]))
        log.error(f"built at run time {v['prose']}")
        log.error("joined " + v["email"])
        log.error("object %s", {"k": v["prose"]})
        log.error("mapping %(who)s", {"who": v["email"]})
        logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5", "GET",
                                                 v["path"] + "?q=" + v["email"], "1.1", 200)
        logging.getLogger("httpx").info('HTTP Request: %s %s "%s %d %s"', "POST", "https://x.test" + v["path"],
                                        "HTTP/1.1", 200, "OK")
        try:
            raise ValueError(v["prose"] + " " + v["email"])
        except ValueError:
            log.exception("it failed for %s", v["email"])
            logging.getLogger("uvicorn.error").error("Exception in ASGI application\n", exc_info=True)
        logging.getLogger("asyncio").warning("Executing %s took %.3f seconds", v["prose"], 0.25)
        made = logging.makeLogRecord({"name": "x", "levelno": 40, "levelname": "ERROR", "msg": "raw " + v["prose"]})
        handler.handle(made)                               # (a record that never passed a logger)
    finally:
        root.removeHandler(handler)
        handler.close()
    text = path.read_text() + caplog.text + "\n".join(r.getMessage() for r in caplog.records)
    assert sa.found(text, v) == []
    for kept in ("audit run failed: ValueError in selene at svc_audits:_run_audit:12", "counts 12 and seo-aud-aaaa",
                 "user said WITHHELD", "contact WITHHELD id WITHHELD", "a number %d [arguments WITHHELD]",
                 "object {'k': 'WITHHELD'}", "mapping WITHHELD", 'WITHHELD - "GET WITHHELD HTTP/1.1" 200',
                 "it failed for WITHHELD [ValueError@", "Exception in ASGI application", "[ValueError@",
                 "Executing WITHHELD took 0.250 seconds"):
        assert kept in text, kept
    assert "Traceback" not in text and "raise ValueError" not in text
    assert text.count("ERROR seo.test WITHHELD") == 2                           # the two messages built at run time
    # the filter is on the root logger, the named loggers and the handler of last resort
    for name in log_sink.LOGGERS:
        assert log_sink.FILTER in logging.getLogger(name).filters, name
    assert log_sink.FILTER in logging.lastResort.filters


def test_the_logging_sink_is_load_bearing(tmp_path, caplog, monkeypatch):
    v = planted(14)
    helpers.Harness(tmp_path)
    caplog.set_level(logging.DEBUG)
    monkeypatch.setitem(log_sink._STATE, "enabled", False)
    logging.getLogger("seo.test").error("user said %s", v["prose"])
    assert sa.found(caplog.text, v) == ["prose"]


def test_the_services_own_log_lines_survive_the_sink(tmp_path, srv, caplog, monkeypatch):
    """What the service logs on a failed run is still there: type, step and where, as codes."""
    from service import SeoService
    caplog.set_level(logging.DEBUG)
    h = sa.harness(tmp_path, srv)

    def boom(self, *a, **k):
        raise RuntimeError("the page said: " + planted(15)["prose"])
    monkeypatch.setattr(SeoService, "_report", boom)
    a = h.ok(sa.audit(h), 201)
    assert a["status"] == "interrupted"
    assert any(r.getMessage().startswith("audit run failed: RuntimeError in report at ") for r in caplog.records)
    assert sa.found(caplog.text, planted(15)) == []
    with pytest.raises(Exception):
        h.svc._commit("job_ran", {"job": "two words"}, "scheduler")
    assert "commit refused by the sink guard: sink=job_ran paths=1 first=job" in caplog.text


# ================================================================================================ error bodies

def test_an_error_body_repeats_nothing_the_caller_sent(tmp_path):
    v = planted(16)
    h = helpers.Harness(tmp_path)
    bodies = []

    def take(r, code):
        assert r.status_code == code, r.status_code
        bodies.append(r.text)
        return r.json()
    # a key the caller invented, at the top and nested under invented keys
    e = take(h.post("/tenants", {"request_id": rid(), "tenant_id": "acme", "kind": "client", v["prose"]: 1},
                    andre=True), 422)
    assert e["detail"][0]["loc"] == ["body", "*"] and e["detail"][0]["type"] == "extra_forbidden"
    e = take(h.post("/tenants", {"request_id": rid(), v["email"]: {v["term"]: {"IP-Address": v["digits"]}}},
                    andre=True), 422)
    assert e["detail"] == [{"loc": ["body", "ip_address"], "msg": e["detail"][0]["msg"], "type": "forbidden_field"}]
    # a value that fails a pattern, a literal, a type, a length
    e = take(h.post("/tenants", {"request_id": v["prose"], "tenant_id": v["email"], "kind": v["term"]}, andre=True),
             422)
    assert [x["loc"] for x in e["detail"]] == [["body", "request_id"], ["body", "tenant_id"], ["body", "kind"]]
    assert all(set(x) == {"loc", "msg", "type"} for x in e["detail"])
    e = take(h.post("/tenants/zbm/entity", {"request_id": rid(), "fields": [{"field": "name", "value": v["prose"],
                                                                            v["term"]: v["email"]}]}, andre=True),
             422)
    assert ["body", "fields", 0, "*"] in [x["loc"] for x in e["detail"]]
    # query and path parameters
    take(h.get("/audit/export", params={"since": v["prose"], "limit": v["digits"]}), 422)
    take(h.get("/audit/evidence", params={"event_type": v["email"]}), 422)
    take(h.get("/tenants/zbm/audits/" + "seo-aud-" + "0" * 40 + "/drift", params={"against": v["path"]}), 422)
    take(h.get("/tenants/" + v["email"]), 422)
    take(h.get("/tenants/zbm/audits/" + v["term"].replace(" ", "%20")), 422)
    take(h.post("/jobs/" + v["term"].replace(" ", "%20") + "/run", {"request_id": rid()}, caller="scheduler"), 422)
    take(h.client.get("/seo/v1/" + v["term"].replace(" ", "%20")), 404)
    take(h.client.post("/seo/v1/tenants", content=b'{"request_id": "' + v["prose"].encode() + b'", ',
                       headers={**h.headers("dashboard", andre=True), "content-type": "application/json"}), 422)
    assert sa.found("\n".join(bodies), v) == []
    # a message is kept only for the error types whose text comes from the schema
    out = api._sanitize([{"loc": ("body", "x", 3, v["prose"]), "msg": "Value error, " + v["prose"],
                          "type": "value_error", "input": v["email"], "ctx": {"error": v["term"]}},
                         {"loc": ("query", "limit"), "msg": "Input should be a valid integer", "type": "int_parsing",
                          "input": v["digits"]},
                         {"loc": ("body", 10 ** 9), "msg": v["prose"], "type": v["email"]}])
    assert out == {"detail": [{"loc": ["body", "*", 3, "*"], "msg": "invalid value", "type": "value_error"},
                              {"loc": ["query", "limit"], "msg": "Input should be a valid integer",
                               "type": "int_parsing"},
                              {"loc": ["body", "*"], "msg": "invalid value", "type": "invalid"}], "errors_total": 3}


def test_finance_sees_billing_facts_and_no_audited_path(tmp_path, srv):
    v = planted(17)
    h = sa.harness(tmp_path, srv)
    a = h.ok(sa.audit(h, paths=("/", v["path"] + "?who=" + v["email"].replace(AT, "%40"))), 201)
    owner = h.ok(h.get(f"/tenants/zbm/audits/{a['audit_id']}"))
    assert len(owner["paths"]) == 2
    one = h.ok(h.get(f"/tenants/zbm/audits/{a['audit_id']}", caller="finance_31"))
    (row,) = h.ok(h.get("/tenants/zbm/audits", caller="finance_31"))
    for view in (one, row):
        assert "paths" not in view and view["paths_count"] == 2 and "report" not in view
        assert view["facts"]["outcome"] == owner["facts"]["outcome"]
        assert v["path"] not in json.dumps(view) and "who=" not in json.dumps(view)


# ================================================================================================ hash fields

# Every hash-like field the log or the ledger may carry, with its decision (ADR 0051, sink job D). ``keyed``: an
# HMAC-SHA256 under the vault key's log-field sub-key, bound to the tenant and a field name. ``plain``: a SHA-256
# whose whole input is itself in the log as machine values (or is a secret / a constant of the deployment, never
# client text). A hash field that is not listed fails ``test_every_hash_field_has_a_decision``.
HASH_FIELDS = {
    # keyed (client or operator content)
    "content_hmac": "keyed", "report_hmac": "keyed", "drift_hmac": "keyed", "result_hmac": "keyed",
    "section_hmac": "keyed", "inputs_hmac": "keyed", "delta_hmac": "keyed", "model_hmac": "keyed",
    "property_hmac": "keyed", "facts_hmac": "keyed", "hmac": "keyed", "audit_report_hmac": "keyed",
    # plain, over machine values that are themselves logged
    "request_sha": "plain: the body with every non-machine string replaced by its keyed HMAC",
    "sha256": "plain or keyed: a prompt-set version's fingerprint as recorded (keyed HMAC since job D)",
    "prompt_set_sha256": "the prompt-set fingerprint above, repeated",
    "domain_sha256": "plain: the domain is logged as a host name",
    "paths_sha256": "plain: over the keyed HMACs of the paths",
    "property_sha256": "plain: a Domain property, logged as its host",
    "scorecard_sha256": "plain: counts and outcome codes derived from logged facts",
    "document_sha256": "plain: the rendered document handed to the owner, who verifies the file with it",
    "audit_report_sha256": "plain: only for audits recorded before job B (null since)",
    "branding_sha256": "plain: the deployment's branding configuration",
    "key_fp": "plain: a 64-bit fingerprint of the log hash key (a secret of the deployment)",
    "line_sha256": "plain: the log chain", "payload_sha256": "plain: the log chain",
}


def hash_keys(x, out=None) -> set:
    out = set() if out is None else out
    if isinstance(x, dict):
        for k, val in x.items():
            if isinstance(k, str) and any(t in k for t in ("sha", "hmac", "hash", "digest", "_fp", "md5")):
                out.add(k)
            hash_keys(val, out)
    elif isinstance(x, list):
        for val in x:
            hash_keys(val, out)
    return out


def test_every_hash_field_has_a_decision(tmp_path, srv):
    v = planted(18)
    h = sa.harness(tmp_path, srv, own=False)
    got = client(h, v)
    a = h.ok(h.post("/tenants/acme/audits", {"request_id": rid(), "domain": DOMAIN, "scheme": "http",
                                             "paths": ["/", v["path"]], "invoice_id": helpers.invoice_id(9),
                                             "prompt_set_id": got["prompt_set"]["prompt_set_id"]},
                    caller="dashboard", andre=True), 201)
    h.ok(sa.client_report(h, "acme", a["audit_id"]))
    h.ok(h.post("/tenants/acme/citation-runs", {"request_id": rid(), "domain": DOMAIN,
                                                "prompt_set_id": got["prompt_set"]["prompt_set_id"]},
                caller="dashboard", andre=True), 201)
    h.ok(h.post("/tenants/acme/schedules", {"request_id": rid(), "domain": DOMAIN, "scheme": "http",
                                            "paths": ["/"], "every_days": 7, "invoice_id": helpers.invoice_id(10)},
                caller="dashboard", andre=True), 201)
    sa.tick(h)
    h.clock.advance(days=7)
    srv.html(DOMAIN, "/", sa.home_html(canonical=None))
    sa.tick(h)
    iid = h.ok(tl.ingest(h, tid="acme", caller="dashboard"), 201)["ingest_id"]
    h.ok(tl.send(h, iid, 1, tl.b64(log_lines(v)), tid="acme", caller="dashboard"))
    h.ok(h.post(f"/tenants/acme/log-ingests/{iid}/finish", {"request_id": rid()}, caller="dashboard"))
    h.ok(h.post("/tenants/acme/gsc-property", {"request_id": rid(), "property": f"http://{DOMAIN}{v['path']}"},
                andre=True))
    h.ok(h.post("/agents/naomi/lifecycle", {"request_id": rid(), "to": "watch", "expected_state": "active",
                                            "reason": "QUALITY_REVIEW"}, andre=True))
    seen = set()
    for r in h.svc.log.iter_records():
        seen |= hash_keys(r["data"])
    for e in h.ledger.events:
        seen |= hash_keys(e["_payload"])
    assert seen - set(HASH_FIELDS) == set(), sorted(seen - set(HASH_FIELDS))
    gone = {"fields_sha256", "value_sha256", "robots_sha256", "sitemap_sample_sha256", "delta_sha256",
            "report_sha256", "result_sha256", "drift_sha256", "facts_sha256"}
    assert not seen & gone, sorted(seen & gone)
    assert {"content_hmac", "inputs_hmac", "delta_hmac", "report_hmac", "drift_hmac", "result_hmac",
            "property_hmac", "request_sha"} <= seen
