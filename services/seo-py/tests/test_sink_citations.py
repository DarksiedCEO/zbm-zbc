"""Sink job C: a citation run's result is the client's content, kept whole in the tenant's vault; the log and the
ledger keep the blob ref, a keyed HMAC and machine facts only. An engine's model is logged only when it is one of the
model ids the adapters declare.

Covered: the log / ledger shape of ``citation_run_recorded``, ``citation_run_interrupted``, ``engine_calls_reserved``
and ``engine_run_settled``; real citation runs whose answers carry secret- and personal-data-shaped values in the
answer text, titles, hints, cited URLs (path, query, fragment), redirect hop hosts and prompts (nothing of them in any
data-directory file, ledger payload, captured log or error response, in raw / hex / base64 at three alignments /
URL-encoded / JSON-escaped / digits-only form; the owner's GET shows the content through the client-view masker); the
write order and its failures (no key, a failed seal, a failed blob write, a failed commit, a kill switch mid-run);
readers failing closed on a lost or changed blob; replays of lines written BEFORE this change; a 2,000-row run; and
mutations showing each rule is load-bearing.

Every sensitive-looking value is GENERATED at run time; failures report names and counts only."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import random
import string
import time
import urllib.parse

import pytest

import helpers
import sink_guard as sg
import svc_citations
import svc_engines
import svc_vaulted
import test_callum as tc
import test_sink_audits as sa
import test_vault_wiring as vw
import vault as vault_mod
from agents import callum
from agents import secret_guard as guard_mod
from envelope import sha
from helpers import rid
from ledger import derived_id
from ports import ProviderAnswer
from primitives.fetch import FetchResult
from service import SeoService

SEED = 20261111
DOMAIN = vw.DOMAIN
FIELD = "citation_result"
HOP_MARK = "hopmarker"                                 # a legitimate host label: it would pass every filter
PROMPT_MARK = "promptmarker"                           # a legitimate prompt word
NEEDLES = ("Wilson", "pooled over", "visibility score", "orchid", "lantern", "heron", vw.MARK, HOP_MARK, PROMPT_MARK)
KINDS = ("citation_run_recorded", "citation_run_interrupted", "engine_calls_reserved", "engine_run_settled")


# ================================================================================================ helpers

def records(h, kind: str) -> list:
    return sa.records(h, kind)


def payloads(h, event_type: str) -> list:
    return sa.ledger_payloads(h, event_type)


def run_of(h, cid: str) -> dict:
    return h.svc.citation_runs[cid]


def document(h, cid: str, tid: str = "zbm") -> dict:
    """The vaulted document of a run, as its owning tenant reads it."""
    r = run_of(h, cid)
    return h.svc.vault.get_blob(tid, r["result_ref"])


def started(tmp_path, v=None, fetcher=None, **env) -> tuple:
    v = v or vw.synthetic()
    tmp_path.mkdir(parents=True, exist_ok=True)
    h = vw.harness(tmp_path, **env)
    port = vw.start(h, v, fetcher)
    return h, v, port


class _Stop(BaseException):
    pass


def planted(rng) -> dict:
    """Synthetic values for every place a citation run reads client or provider text from."""
    a = sa.planted(rng)
    out = {k: a[k] for k in ("prompt_label_value", "prompt_ssn_words", "prompt_phone_words", "prompt_local",
                             "prompt_dom")}
    out.update({"title_label_value": sa.rc._secret_word(rng), "title_email": sa.rc._email(rng),
                "hint_ssn": sa.rc._ssn(rng), "hint_card": sa.rc._card(rng),
                "text_phone": sa.rc._phone(rng), "text_email": sa.rc._email(rng),
                "path_digits": sa.rc._r(rng, "123456789", 14), "query_email": sa.rc._email(rng),
                "fragment_card": sa.rc._card(rng),
                "hop_digits": sa.rc._r(rng, "123456789", 14),
                "hop_token": "tok" + sa.rc._r(rng, string.ascii_lowercase + string.digits, 40)})
    return out


def hop_hosts(p: dict) -> list:
    return [f"acct-{p['hop_digits']}.competitor.test", f"{p['hop_token']}.competitor.test", f"{HOP_MARK}.test"]


class HopFetcher:
    """Every URL answers a redirect chain that stops: its hop hosts carry planted values."""

    def __init__(self, p: dict):
        self.hops, self.fetched = hop_hosts(p), 0

    def fetch(self, url, robots_cache=None, guard=None, **kw):
        self.fetched += 1
        guard(capability="fetch", provider="web")
        return FetchResult(url=url, state="TOO_MANY_REDIRECTS", final_url=url,
                           redirects=[{"url": f"https://{host}/r", "status": 302} for host in self.hops])


def leak_answers(v: dict, p: dict) -> list:
    """The vault-wiring answers plus one whose every field carries a planted value."""
    q = urllib.parse.quote
    path_url = f"https://competitor.test/order/{p['path_digits']}/view"
    query_url = f"https://competitor.test/p?contact={q(p['query_email'])}&v=2"
    frag_url = f"https://competitor.test/pay#card={p['fragment_card']}"
    d = ProviderAnswer("ANSWER", f"Z Best Media can be reached at {p['text_email']} or {p['text_phone']} any day.",
                       [path_url, query_url, frag_url], "m", "1", cite_from_text=False, provider="openai",
                       asked_at="2026-10-09T18:00:00Z")
    d.citation_titles = {path_url: f"Write {p['title_email']} password is {p['title_label_value']}"}
    d.citation_hints = {query_url: "ssn " + p["hint_ssn"], frag_url: "card " + p["hint_card"]}
    return vw.answers(v) + [d]


def leak_prompts(p: dict) -> list:
    return [f"how to reset it, my password is {p['prompt_label_value']}",
            f"reach {p['prompt_local']} at {p['prompt_dom']}.com", f"verify {p['prompt_ssn_words']}",
            f"call {p['prompt_phone_words']}", f"who recovers marketplace revenue {PROMPT_MARK}"]


def leak_run(tmp_path, seed: int = SEED):
    """A real run: scripted answers, a redirect chain per cited URL, prompts with planted values."""
    rng = random.Random(seed)
    v, p = vw.synthetic(seed), planted(rng)
    fetcher = HopFetcher(p)
    h = vw.harness(tmp_path, SEO_CALLUM_RESOLVE_URLS="1")
    h.ports.engines["openai"] = vw.Port(leak_answers(v, p))
    h.ports.fetcher = fetcher
    h.ok(h.post("/tenants/zbm/domains", {"request_id": rid(), "domains": [DOMAIN]}, andre=True))
    psid = h.ok(h.post("/tenants/zbm/prompt-sets", {
        "request_id": rid(), "name": "core", "brand_terms": ["Z Best Media"],
        "competitor_domains": ["competitor.test"], "prompts": leak_prompts(p), "engines": ["openai"]}),
        201)["prompt_set_id"]
    run = h.ok(h.post("/tenants/zbm/citation-runs", {"request_id": rid(), "domain": DOMAIN, "prompt_set_id": psid}),
               201)
    return h, v, p, run, fetcher


class MeteredPort(vw.Port):
    """A metered port that reports its own model name."""

    metered = True

    def __init__(self, script, model):
        super().__init__(script)
        self.model = model

    def ask(self, prompt, guard=None, meter=None):
        if meter is not None:
            meter.before()
        a = super().ask(prompt)
        if meter is not None:
            meter.after(None)
        return a


def metered(tmp_path, model):
    h = vw.harness(tmp_path, **tc.BUDGET_ON)
    port = MeteredPort(vw.answers(vw.synthetic()), model)
    h.ports.engines["openai"] = port
    h.ok(h.post("/tenants/zbm/domains", {"request_id": rid(), "domains": [DOMAIN]}, andre=True))
    return h, port


def unknown_model(seed: int = SEED) -> str:
    r = random.Random(seed)
    return "vendor" + vw._rand(r, string.ascii_lowercase, 8) + "/name" + vw._rand(r, string.ascii_lowercase, 8) \
        + "@2026-11?beta"


def old_format_result(h, doc: dict, tid: str = "zbm") -> dict:
    """``doc``'s result as a binary from BEFORE sink job C logged it: every row's evidence a separate vault record,
    the row naming its ``vault_id`` with keyed per-field hashes."""
    env = copy.deepcopy(doc["result"])
    refs: dict = {}
    kid = h.svc.vault.field_hash(tid, "hint", "")[0]

    def ref(eid):
        if eid is None:
            return None
        if eid not in refs:
            refs[eid] = vault_mod.ref(h.svc.vault.put(tid, doc["evidence"][eid]))
        return refs[eid]

    def stubs(eid, fields: dict) -> dict:
        rec = doc["evidence"][eid]
        return {name: {"hmac_sha256": h.svc.vault.field_hash(tid, name, rec[name])[1], "flag": f["flag"]}
                for name, f in fields.items()}
    for row in env["facts"]["rows"]:
        ev = row.get("evidence")
        if ev:
            eid = ev["evidence_id"]
            row["evidence"] = {"vault_id": ref(eid), "basis": ev["basis"], "hash_key_id": kid,
                               "fields": stubs(eid, ev["fields"])}
    for it in env["facts"]["mnc_priority"]["items"]:
        for x in it["cited_instead"]:
            x["vault_id"] = ref(x.pop("evidence_id", None))
        for e in it["evidence"]:
            eid = e.pop("evidence_id", None)
            e["vault_id"] = ref(eid)
            if e.get("excerpt"):
                e["excerpt"] = stubs(eid, {"excerpt": e["excerpt"]})["excerpt"]
    return env


def requested_only(h, monkeypatch, psid=None) -> str:
    """A citation run whose request is recorded and whose process then stops: nothing else is written."""
    box = {}

    def stop(self, env, sink):
        raise _Stop()
    real_begin = SeoService._begin_citation_run

    def begin(self, *a, **k):
        out = real_begin(self, *a, **k)
        box["cid"] = out[0]
        return out
    psid = psid or tc.prompt_set(h, engines=("openai",))
    with monkeypatch.context() as m:
        m.setattr(SeoService, "_prepare_citation_result", stop)
        m.setattr(SeoService, "_begin_citation_run", begin)
        with pytest.raises(_Stop):
            h.svc.request_citation_run("seo_agent", "zbm", {"request_id": rid(), "domain": DOMAIN,
                                                            "prompt_set_id": psid}, andre=False)
    return box["cid"]


# ================================================================================================ the log's shape

def test_citation_run_recorded_keeps_ids_codes_counts_a_ref_and_a_keyed_hash(tmp_path):
    h, v, _port = started(tmp_path)
    run = vw.citation_run(h)
    cid = run["citation_run_id"]
    (rec,) = records(h, "citation_run_recorded")
    d = rec["data"]
    assert set(d) == {"citation_run_id", "result_ref", "result_hmac", "facts", "actor", "evidence"}
    assert sg.violations("citation_run_recorded", d) == []          # machine values only, no legacy entry needed
    assert vault_mod.parse_blob_ref(d["result_ref"]) is not None
    facts = d["facts"]
    assert set(facts) == {"outcome", "rows", "findings", "usable_answers", "mentioned_not_cited", "strength_answers"}
    assert all(type(facts[k]) is int for k in facts if k != "outcome")
    doc = document(h, cid)
    assert set(doc) == {"v", "result", "evidence"} and doc["v"] == svc_citations.RESULT_VERSION
    totals = doc["result"]["facts"]["metrics"]["totals"]
    assert facts == {"outcome": "OK", "rows": len(doc["result"]["facts"]["rows"]),
                     "findings": len(doc["result"]["findings"]), "usable_answers": totals["usable"],
                     "mentioned_not_cited": totals["mnc"], "strength_answers": totals["strength"]}
    assert facts["rows"] == len(doc["evidence"]) > 0
    # the hash is the vault's keyed HMAC of the canonical document, never a plain SHA-256 of it or of the result
    canon = svc_vaulted.canonical(doc)
    assert d["result_hmac"] == h.svc.vault.field_hash("zbm", FIELD, canon)[1]
    plain = {hashlib.sha256(canon.encode()).hexdigest(), sha(doc), sha(doc["result"]),
             hashlib.sha256(svc_vaulted.canonical(doc["result"]).encode()).hexdigest()}
    line = json.dumps(d)
    assert not any(x in json.dumps(rec) for x in plain) and "result_sha256" not in line and "sha256" not in line
    assert "seo-vlt-" not in line                                   # no per-row stub, no per-row vault record
    # the ledger: the keyed hash, the outcome and counts
    (p,) = payloads(h, "citation_run_recorded")
    assert {"citation_run_id", "result_hmac", "outcome", "rows", "mentioned_not_cited", "strength_answers"} <= set(p)
    assert "result_sha256" not in p and p["result_hmac"] == d["result_hmac"]
    assert p["outcome"] == "OK" and p["rows"] == facts["rows"]
    assert sg.violations("ledger:citation_run_recorded", p) == []
    assert not any(x in json.dumps(h.ledger.events, default=str) for x in plain)
    # memory holds no result; the owner's view is read from the vault
    assert run_of(h, cid)["result"] is None and run_of(h, cid)["result_sha256"] is None
    assert run["result"]["outcome"] == "OK" and run["result_hmac"] == d["result_hmac"]
    assert "result_ref" not in run and run["result_sha256"] is None and run["facts"] == facts
    # the vault: ONE document (its chunk and manifest lines), no record per row
    assert sa.vault_lines(h) == 2 < facts["rows"]
    # the list view: machine facts only, no vault read
    (row,) = h.ok(h.get("/tenants/zbm/citation-runs"))
    assert "result" not in row and "result_ref" not in row and row["outcome"] == "OK" and row["facts"] == facts
    card = h.ok(h.get("/department"))["agents"]["callum"]["scorecard"]["citation_runs"]
    assert card == {"runs": 1, "completed": 1, "interrupted": 0, "outcomes": {"OK": 1}}


def test_the_stored_envelope_names_evidence_by_id_and_carries_no_hash(tmp_path):
    h, v, _port = started(tmp_path)
    cid = vw.citation_run(h)["citation_run_id"]
    doc = document(h, cid)
    for row in doc["result"]["facts"]["rows"]:
        assert "url" not in row and "engine_hint_unverified" not in row and "evidence_ref" not in row
        ev = row["evidence"]
        assert set(ev) == {"evidence_id", "basis", "fields"} and ev["evidence_id"] in doc["evidence"]
        assert all(set(f) == {"flag"} for f in ev["fields"].values())
        assert set(ev["fields"]) == {k for k in callum.EVIDENCE_FIELDS
                                     if isinstance(doc["evidence"][ev["evidence_id"]].get(k), str)}
    text = json.dumps(doc["result"])
    assert "hmac" not in text and "sha256" in text                 # (the prompt set's content hash is an id, kept)
    assert len({r["evidence"]["evidence_id"] for r in doc["result"]["facts"]["rows"]}) == len(doc["evidence"])


def test_no_legacy_entry_remains_for_the_citation_and_engine_sinks():
    """Sink job D deleted the legacy list: there is no entry for any sink."""
    assert not hasattr(sg, "LEGACY_ROWS") and not hasattr(sg, "LEGACY_PROSE_PATHS") and not hasattr(sg, "LEGACY_COUNTS")


def test_the_result_is_not_readable_at_rest(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    h, _v, p, run, _f = leak_run(tmp_path)
    assert run["status"] == "completed"
    blob = sa.written(h, caplog)
    for needle in NEEDLES:
        assert needle not in blob and needle.encode().hex() not in blob, NEEDLES.index(needle)
    shown = json.dumps(run)
    assert all(n in shown for n in ("Wilson", "pooled over", vw.MARK, HOP_MARK, PROMPT_MARK))


# ================================================================================================ the planted values

def test_real_runs_with_planted_values_keep_nothing_raw_and_the_owner_reads_them_masked(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    h, v, p, run, fetcher = leak_run(tmp_path)
    cid = run["citation_run_id"]
    assert run["status"] == "completed" and run["result"]["outcome"] == "OK" and fetcher.fetched > 0
    h.tenant("acme", domains=("other.test",))
    got = h.ok(h.get(f"/tenants/zbm/citation-runs/{cid}"))
    mp = h.ok(h.get(f"/tenants/zbm/citation-runs/{cid}/mnc-priority"))
    listing = h.ok(h.get("/tenants/zbm/citation-runs"))
    errors = [h.get(f"/tenants/acme/citation-runs/{cid}"), h.get(f"/tenants/acme/citation-runs/{cid}/mnc-priority"),
              h.get(f"/tenants/zbm/citation-runs/{'seo-cit-' + '0' * 40}"),
              h.post("/tenants/zbm/citation-runs", {"request_id": rid(), "domain": "nope.test",
                                                    "prompt_set_id": run["prompt_set"]["prompt_set_id"]}),
              h.get(f"/tenants/zbm/citation-runs/{cid}", caller="hub", tenant="acme")]
    assert [r.status_code for r in errors] == [404, 404, 404, 403, 404]
    sa._lose(h, run_of(h, cid)["result_ref"])
    lost = [h.get(f"/tenants/zbm/citation-runs/{cid}/mnc-priority"), h.get(f"/tenants/zbm/citation-runs/{cid}")]
    assert [r.status_code for r in lost] == [503, 200]
    everything = {**v, **p}
    blob = sa.written(h, caplog) + "".join(r.text for r in errors + lost) + json.dumps(listing)
    assert sa.found(blob, everything) == []
    for needle in NEEDLES:
        assert needle not in blob and needle.encode().hex() not in blob, NEEDLES.index(needle)
    for host in hop_hosts(p):
        assert host not in blob
    # every kind this job migrated passes the guard with no legacy entry
    for kind in KINDS:
        for rec in records(h, kind):
            assert sg.violations(kind, rec["data"]) == [], kind
    for e in h.ledger.events:
        if e["event_type"] in KINDS:
            assert sg.violations("ledger:" + e["event_type"], e["_payload"]) == [], e["event_type"]
    # the owner reads the content: legitimate fields in full, a high-precision finding masked with an alert
    out = json.dumps([got, mp], ensure_ascii=False)
    by = vw.rows_by(got)
    assert by[vw.LEGIT_URL]["url"] == vw.LEGIT_URL
    assert by[vw.LEGIT_URL]["evidence"]["title"]["value"] == vw.LEGIT_TITLE
    assert by[vw.LEGIT_URL]["evidence"]["excerpt"]["value"] == vw.LEGIT_TEXT
    assert f"{HOP_MARK}.test" in by[vw.LEGIT_URL]["chain"]["hop_hosts"]
    assert PROMPT_MARK in out and "Wilson" in out
    masked = dict(v)
    masked.update({k: p[k] for k in ("title_label_value", "title_email", "hint_ssn", "hint_card", "text_phone",
                                     "text_email", "query_email", "fragment_card", "hop_digits", "hop_token",
                                     "prompt_label_value")})
    assert sa.found(out, masked) == []
    rows = got["result"]["facts"]["rows"]
    alerts = [r["evidence"][k]["reason"] for r in rows for k in callum.EVIDENCE_FIELDS
              if r["evidence"].get(k) and r["evidence"][k]["alert"]]
    assert "LOOKS_LIKE_CREDENTIAL" in alerts and "LOOKS_LIKE_PERSONAL_DATA" in alerts
    assert all(r["evidence"]["available"] is True for r in rows)
    # a flagged hop host is withheld even from the stored envelope (the strict host check), never shown
    assert all(None in r["chain"]["hop_hosts"] for r in rows if r.get("chain") and r["chain"].get("hop_hosts"))


def test_the_legacy_leak_harness_reader_scrubs_the_keyed_hashes():
    import leak_harness_v2 as lh
    mac = "0123456789abcdef" * 4
    blob = json.dumps({"result_hmac": mac, "model_hmac": mac, "report_hmac": mac, "result_ref": "seo-blb-" + "1" * 40})
    out = lh._scrub(blob)
    assert mac not in out and "1" * 40 not in out


# ================================================================================================ write order

def test_no_vault_key_refuses_before_any_engine_and_nothing_is_written(tmp_path, caplog):
    h, v, port = started(tmp_path)
    psid = tc.prompt_set(h, engines=("openai",))
    h2 = h.restart(SEO_VAULT_KEY_FILE=None)
    before = (len(h2.svc.log), len(h2.ledger.events), sa.vault_lines(h2))
    r = h2.post("/tenants/zbm/citation-runs", {"request_id": rid(), "domain": DOMAIN, "prompt_set_id": psid})
    assert r.status_code == 503 and r.json()["detail"] == "VAULT_UNAVAILABLE"
    assert port.asked == 0 and (len(h2.svc.log), len(h2.ledger.events), sa.vault_lines(h2)) == before
    # and inside the run itself (a key lost after the request was recorded): closed, no engine asked
    h3 = h2.restart()
    h3.svc.vault._key = None
    run = h3.svc._execute_citation_run(requested_only_id(h3, psid), "zbm", DOMAIN, {})
    assert run["status"] == "interrupted" and run["failure"]["code"] == "VAULT_UNAVAILABLE" and port.asked == 0
    assert records(h3, "citation_run_recorded") == []


def requested_only_id(h, psid) -> str:
    """A request recorded under the lock, not yet executed."""
    with h.svc.lock:
        t = h.svc.tenants["zbm"]
        body = {"request_id": rid(), "domain": DOMAIN, "prompt_set_id": psid}
        rk = h.svc.rk("citation_run", "zbm", body)
        cid, _ps = h.svc._begin_citation_run(t, "zbm", "seo_agent", rk, body, False, None)
    return cid


def _unavailable(*a, **k):
    raise vault_mod.VaultUnavailable("forced")


def _write_error(*a, **k):
    raise vault_mod.VaultWriteError("forced")


@pytest.mark.parametrize("where", ["seal_unavailable", "seal_error", "blob_write", "hmac"])
def test_a_failure_while_storing_closes_the_run_failed_with_nothing_written(tmp_path, monkeypatch, caplog, where):
    caplog.set_level(logging.DEBUG)
    h, v, _port = started(tmp_path)
    vault = type(h.svc.vault)
    patch = {"seal_unavailable": (vault, "put_blob", _unavailable), "seal_error": (vault, "put_blob", vw._boom),
             "blob_write": (vault, "write_batch", _write_error),
             "hmac": (svc_vaulted.VaultedMixin, "_vaulted_hmac", _unavailable)}[where]
    psid = tc.prompt_set(h, engines=("openai",))
    lines = sa.vault_lines(h)
    with monkeypatch.context() as m:
        m.setattr(*patch)
        run = h.ok(tc.run(h, psid), 201)
    assert run["status"] == "interrupted" and run["interrupted_reason"] == "RUN_FAILED", where
    assert run["citation_run_id"] not in h.svc.running_citation_runs
    assert records(h, "citation_run_recorded") == [] and h.ledger.of_type("citation_run_recorded") == []
    assert sa.vault_lines(h) == lines and h.svc.vault.status()["orphaned_records"] == 0
    f = run["failure"]
    assert f["agent"] == "callum" and sg.classify(f["where"]) == "code" and sg.classify(f["error_type"]) == "code"
    assert (f.get("code") == "VAULT_UNAVAILABLE") == (where in ("seal_unavailable", "hmac"))
    (rec,) = records(h, "citation_run_interrupted")
    assert sg.violations("citation_run_interrupted", rec["data"]) == []
    blob = sa.written(h, caplog)
    assert sa.found(blob, v) == [] and vw.MARK not in blob
    assert h.ok(tc.run(h, psid), 201)["status"] == "completed"       # the vault and the service still work


def test_a_failed_commit_after_the_blob_write_counts_an_orphan(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    h, v, _port = started(tmp_path)
    psid = tc.prompt_set(h, engines=("openai",))
    real = SeoService._commit

    def commit(self, kind, *a, **k):
        if kind == "citation_run_recorded":
            raise RuntimeError("forced")
        return real(self, kind, *a, **k)
    SeoService._commit = commit
    try:
        run = h.ok(tc.run(h, psid), 201)
    finally:
        SeoService._commit = real
    assert run["status"] == "interrupted" and run["interrupted_reason"] == "RUN_FAILED"
    st = h.svc.vault.status()
    assert st["orphaned_records"] == st["records"] == 2       # the blob's chunk and manifest: written, unreferenced
    assert h.ok(h.get("/status"))["citation_vault"]["orphaned_records"] == 2
    assert run["result"] is None and run["result_hmac"] is None and "vault_unavailable" not in run
    blob = sa.written(h, caplog)
    assert sa.found(blob, v) == [] and vw.MARK not in blob


def test_a_kill_switch_engaged_mid_run_writes_no_blob(tmp_path, monkeypatch):
    h, v, port = started(tmp_path)
    psid = tc.prompt_set(h, engines=("openai",))
    real = SeoService._prepare_citation_result

    def prepare(self, env, sink):
        out = real(self, env, sink)
        self.switches["capability"]["audit"] = True          # engaged after the run, before the record (memory only)
        return out
    # review 2b26d980 SHOULD 8: patched with ``monkeypatch`` (which removes the attribute it added). Assigning
    # ``real`` back by hand left the mixin's method on ``SeoService`` itself, shadowing the mixin for every later
    # test that patches ``CitationsMixin`` (tests/test_review_adacb2ca.py failed when run after this file)
    assert "_prepare_citation_result" not in vars(SeoService)
    with monkeypatch.context() as m:
        m.setattr(SeoService, "_prepare_citation_result", prepare)
        run = h.ok(tc.run(h, psid), 201)
    assert "_prepare_citation_result" not in vars(SeoService)
    assert run["status"] == "interrupted" and run["interrupted_reason"].startswith("KILLED")
    assert sa.vault_lines(h) == 0 and h.svc.vault.status()["orphaned_records"] == 0
    assert records(h, "citation_run_recorded") == []


def test_the_blob_is_sealed_outside_the_service_lock_appended_once_under_it_and_read_outside_it(tmp_path, monkeypatch):
    h, v, _port = started(tmp_path)
    vault = type(h.svc.vault)
    held = {"seal": [], "append": [], "open": [], "sweep": [], "mask": []}
    real_put, real_append, real_get = vault.put_blob, vault._append, vault.get_blob
    real_sweep, real_display = guard_mod.guard_record, callum._display

    def owned() -> bool:
        return h.svc.lock._is_owned()

    def put_blob(self, tid, obj):
        held["seal"].append(owned())
        return real_put(self, tid, obj)

    def append(self, line):
        if self is h.svc.vault:
            held["append"].append(owned())
        return real_append(self, line)

    def get_blob(self, tid, ref):
        held["open"].append(owned())
        return real_get(self, tid, ref)

    def sweep(obj):
        held["sweep"].append(owned())
        return real_sweep(obj)

    def display(*a, **k):
        held["mask"].append(owned())
        return real_display(*a, **k)
    monkeypatch.setattr(vault, "put_blob", put_blob)
    monkeypatch.setattr(vault, "_append", append)
    monkeypatch.setattr(vault, "get_blob", get_blob)
    monkeypatch.setattr(guard_mod, "guard_record", sweep)
    monkeypatch.setattr(callum, "_display", display)
    psid = tc.prompt_set(h, engines=("openai",))
    assert held["seal"] == [False]                           # (sink job D: the prompt set's blob, outside the lock too)
    held["seal"].clear()
    held["mask"].clear()                                     # (a prompt set's own view masks its texts)
    run = h.ok(tc.run(h, psid), 201)
    cid = run["citation_run_id"]
    h.ok(h.get(f"/tenants/zbm/citation-runs/{cid}"))
    h.ok(h.get(f"/tenants/zbm/citation-runs/{cid}/mnc-priority"))
    opened = len(held["open"])
    h.ok(h.get("/tenants/zbm/citation-runs"))
    assert len(held["open"]) == opened == 3                  # the POST's answer, the GET, the export; never the list
    assert held["seal"] == [False] and held["sweep"] == [False]
    assert held["append"] == [True]                          # one append (one fsync), with the commit
    assert not any(held["open"]) and held["mask"] and not any(held["mask"])


def test_an_idempotent_replay_reads_the_same_view(tmp_path):
    h, v, port = started(tmp_path)
    psid = tc.prompt_set(h, engines=("openai",))
    body = {"request_id": rid(), "domain": DOMAIN, "prompt_set_id": psid}
    a = h.ok(h.post("/tenants/zbm/citation-runs", body), 201)
    asked = port.asked
    b = h.post("/tenants/zbm/citation-runs", body)
    assert b.status_code in (200, 201) and b.json() == a and port.asked == asked
    assert len(records(h, "citation_run_recorded")) == 1 and sa.vault_lines(h) == 2


# ================================================================================================ readers fail closed

@pytest.mark.parametrize("damage", ["lost", "tampered", "no_key", "wrong_hmac", "wrong_shape"])
def test_a_lost_or_changed_blob_shows_vault_unavailable_and_the_export_refuses(tmp_path, damage):
    h, v, _port = started(tmp_path)
    a = vw.citation_run(h)
    b = h.ok(tc.run(h, a["prompt_set"]["prompt_set_id"]), 201)
    cid = a["citation_run_id"]
    ref = run_of(h, cid)["result_ref"]
    if damage == "lost":
        sa._lose(h, ref)
    elif damage == "tampered":
        sa._tamper(h, ref)
    elif damage == "wrong_hmac":
        run_of(h, cid)["result_hmac"] = run_of(h, b["citation_run_id"])["result_hmac"]
    elif damage == "wrong_shape":
        other = h.svc._vaulted_seal("zbm", FIELD, {"v": 1, "result": "x", "evidence": {}})
        with h.svc.lock:
            h.svc.vault.write_batch(other.pending)
        run_of(h, cid).update(result_ref=other.ref, result_hmac=other.hmac)
    else:
        h = h.restart(SEO_VAULT_KEY_FILE=None)
    got = h.ok(h.get(f"/tenants/zbm/citation-runs/{cid}"))
    assert got["status"] == "completed" and got["result"] is None and got["vault_unavailable"] is True
    assert got["facts"]["outcome"] == "OK" and got["facts"]["rows"] > 0 and got["result_hmac"]
    h.refused(h.get(f"/tenants/zbm/citation-runs/{cid}/mnc-priority"), 503, "VAULT_UNAVAILABLE")
    rows = h.ok(h.get("/tenants/zbm/citation-runs"))
    assert [r["outcome"] for r in rows] == ["OK", "OK"] and not any("vault_unavailable" in r for r in rows)
    card = h.ok(h.get("/department"))["agents"]["callum"]["scorecard"]["citation_runs"]
    assert card["completed"] == 2 and card["outcomes"] == {"OK": 2}
    assert vw.MARK not in json.dumps(got)
    if damage != "no_key":
        ok = h.ok(h.get(f"/tenants/zbm/citation-runs/{b['citation_run_id']}"))
        assert ok["result"] == b["result"] and "vault_unavailable" not in ok


def test_another_tenant_cannot_read_the_run_or_the_blob(tmp_path):
    h, v, _port = started(tmp_path)
    cid = vw.citation_run(h)["citation_run_id"]
    h.tenant("acme", domains=("other.test",))
    missing = h.get(f"/tenants/acme/citation-runs/{'seo-cit-' + '0' * 40}")
    other = h.get(f"/tenants/acme/citation-runs/{cid}")
    assert other.status_code == missing.status_code == 404 and other.json() == missing.json()
    assert h.get(f"/tenants/acme/citation-runs/{cid}/mnc-priority").status_code == 404
    assert h.get(f"/tenants/zbm/citation-runs/{cid}", caller="hub", tenant="acme").status_code == 404
    assert h.ok(h.get("/tenants/acme/citation-runs")) == []
    rec = run_of(h, cid)
    with pytest.raises(vault_mod.VaultNotFound):
        h.svc.vault.get_blob("acme", rec["result_ref"])
    assert h.svc._vaulted_open("acme", FIELD, rec["result_ref"], rec["result_hmac"]) is None
    doc = h.svc._vaulted_open("zbm", FIELD, rec["result_ref"], rec["result_hmac"])
    assert doc is not None
    canon = svc_vaulted.canonical(doc)
    assert h.svc.vault.field_hash("acme", FIELD, canon)[1] != rec["result_hmac"]      # tenant-bound
    assert h.svc.vault.field_hash("zbm", "audit_report", canon)[1] != rec["result_hmac"]   # field-bound


def test_the_result_reads_after_a_restart_and_after_a_key_rotation(tmp_path):
    h, v, _port = started(tmp_path)
    a = vw.citation_run(h)
    h2 = h.restart()
    assert h2.ok(h2.get(f"/tenants/zbm/citation-runs/{a['citation_run_id']}")) == a
    old_file = h.env["SEO_VAULT_KEY_FILE"]
    new_file = helpers.write_vault_key(tmp_path / "vault2.key")
    h3 = h2.restart(SEO_VAULT_KEY_FILE=new_file, SEO_VAULT_KEY_ID="k-next", SEO_VAULT_KEY_PREVIOUS_FILE=old_file,
                    SEO_VAULT_KEY_PREVIOUS_ID="k-test")
    got = h3.ok(h3.get(f"/tenants/zbm/citation-runs/{a['citation_run_id']}"))
    assert got["result"] == a["result"] and "vault_unavailable" not in got
    h4 = h3.restart(SEO_VAULT_KEY_FILE=new_file, SEO_VAULT_KEY_ID="k-next")            # the old key retired
    gone = h4.ok(h4.get(f"/tenants/zbm/citation-runs/{a['citation_run_id']}"))
    assert gone["result"] is None and gone["vault_unavailable"] is True


def test_the_export_of_a_run_without_a_list_and_of_a_run_that_is_not_completed(tmp_path, monkeypatch):
    h = vw.harness(tmp_path)
    h.ok(h.post("/tenants/zbm/domains", {"request_id": rid(), "domains": [DOMAIN]}, andre=True))
    psid = tc.prompt_set(h, engines=("openai",))                    # no engine port connected
    run = h.ok(tc.run(h, psid), 201)
    assert run["status"] == "completed" and run["result"]["outcome"] == "NOT_CONNECTED"
    assert run["facts"] == {"outcome": "NOT_CONNECTED", "rows": 0, "findings": 0, "usable_answers": None,
                            "mentioned_not_cited": None, "strength_answers": None}
    mp = h.ok(h.get(f"/tenants/zbm/citation-runs/{run['citation_run_id']}/mnc-priority"))
    assert mp["items"] == [] and mp["outcome"] == "NOT_CONNECTED"
    (p,) = payloads(h, "citation_run_recorded")
    assert p["rows"] == 0 and p["mentioned_not_cited"] is None
    cid = requested_only(h, monkeypatch, psid)
    h.refused(h.get(f"/tenants/zbm/citation-runs/{cid}/mnc-priority"), 409, "CITATION_RUN_NOT_COMPLETED")


# ================================================================================================ where / WITHHELD

def test_a_crashed_run_records_where_as_a_machine_code_in_the_log_and_the_ledger(tmp_path, monkeypatch):
    h, v, _port = started(tmp_path)

    def run(*a, **k):
        return [(lambda: {}["missing"])() for _ in range(1)]          # fails inside <lambda> inside <listcomp>
    monkeypatch.setattr(callum, "run", run)
    got = vw.citation_run(h)
    assert got["status"] == "interrupted" and got["interrupted_reason"] == "RUN_FAILED"
    where = got["failure"]["where"]
    assert "<" not in where and ">" not in where and sg.classify(where) == "code"
    assert got["failure"]["agent"] == "callum" and got["failure"]["error_type"] == "KeyError"
    (rec,) = records(h, "citation_run_interrupted")
    assert sg.violations("citation_run_interrupted", rec["data"]) == []
    (p,) = payloads(h, "citation_run_interrupted")
    assert p["where"] == where and sg.violations("ledger:citation_run_interrupted", p) == []


def test_a_failure_while_splitting_records_where_as_a_machine_code(tmp_path, monkeypatch):
    h, v, _port = started(tmp_path)
    monkeypatch.setattr(callum, "split_evidence", lambda env, sink, put: (lambda: {}["missing"])())
    got = vw.citation_run(h)
    assert got["status"] == "interrupted" and sg.classify(got["failure"]["where"]) == "code"
    assert "<" not in got["failure"]["where"]
    (p,) = payloads(h, "citation_run_interrupted")
    assert sg.violations("ledger:citation_run_interrupted", p) == []


def test_a_withheld_marker_is_never_logged_it_is_the_code_withheld(tmp_path, monkeypatch):
    assert svc_citations.run_facts({"outcome": guard_mod.WITHHELD})["outcome"] == "WITHHELD"
    assert svc_citations.run_facts({"outcome": "has space"})["outcome"] == "WITHHELD"
    assert svc_citations.run_facts(guard_mod.WITHHELD) == svc_citations.run_facts({})
    assert sg.classify(svc_citations.WITHHELD) == "code" and sg.classify(guard_mod.WITHHELD) is None
    h, v, _port = started(tmp_path)
    real = guard_mod.guard_record

    def sweep(obj):
        out = real(obj)
        return {**out, "outcome": guard_mod.WITHHELD}       # the sweep withheld a value the log would have kept
    monkeypatch.setattr(guard_mod, "guard_record", sweep)
    run = vw.citation_run(h)
    assert run["status"] == "completed" and run["facts"]["outcome"] == "WITHHELD"
    assert run["result"]["outcome"] == guard_mod.WITHHELD        # the owner's stored document says what was swept
    blob = sa.written(h)
    assert guard_mod.WITHHELD not in blob
    (p,) = payloads(h, "citation_run_recorded")
    assert p["outcome"] == "WITHHELD"


# ================================================================================================ model ids

def test_model_id_is_a_closed_exact_class():
    import anthropic_engine
    import google_engine
    import openai_engine
    import perplexity
    declared = set(perplexity.MODELS) | set(perplexity.SONAR_MODELS) | set(openai_engine.MODELS) \
        | set(anthropic_engine.MODELS) | set(google_engine.MODELS)
    assert sg.MODEL_IDS == declared and "model_id" in sg.CLASSES
    assert {perplexity.DEFAULT_MODEL, openai_engine.DEFAULT_MODEL, anthropic_engine.DEFAULT_MODEL,
            google_engine.DEFAULT_MODEL} <= sg.MODEL_IDS
    for m in sorted(declared):
        assert sg.classify(m) == "model_id" and sg.violations("engine_calls_reserved", {"model": m}) == []
    slashed = sorted(m for m in declared if "/" in m)
    assert slashed                                           # the class is what admits a declared ``vendor/name``
    for m in slashed:
        for near in (m + "x", "x" + m, m.upper(), m + "/", m + "@1", m + "?", m.replace("/", "//"), " " + m,
                     m.replace("/", "/ ")):
            assert sg.classify(near) is None, len(near)
            assert sg.violations("engine_calls_reserved", {"model": near}) != []
    # the general classes still admit neither "@" nor a free "/"
    for bad in ("name@1", "vendor/name", "name@2026-11-08", "a/b/c", "m?", "name@version?x"):
        assert sg.classify(bad) is None
        assert sg.violations("engine_run_settled", {"model": bad}) != []
        assert sg.violations("ledger:engine_run_settled", {"model": bad}) != []
    assert sg.classify(sg.UNKNOWN_MODEL) == "code" and sg.UNKNOWN_MODEL not in sg.MODEL_IDS


def test_a_declared_model_is_logged_as_it_is(tmp_path):
    h, port = metered(tmp_path, "perplexity/sonar")
    run = h.ok(tc.run(h, tc.prompt_set(h, engines=("openai",))), 201)
    assert run["status"] == "completed" and port.asked == 3
    for kind in ("engine_calls_reserved", "engine_run_settled"):
        recs = records(h, kind)
        assert recs and all(r["data"]["model"] == "perplexity/sonar" and "model_hmac" not in r["data"] for r in recs)
        assert all(sg.violations(kind, r["data"]) == [] for r in recs)
        ps = payloads(h, kind)
        assert ps and all(p["model"] == "perplexity/sonar" and "model_hmac" not in p for p in ps)
        assert all(sg.violations("ledger:" + kind, p) == [] for p in ps)
    (er,) = h.svc.engine_runs.values()
    assert er["model"] == "perplexity/sonar" and er["settled"]["calls"] == 3


def test_an_unknown_model_is_logged_as_a_code_with_a_keyed_hash(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    raw = unknown_model()
    assert sg.classify(raw) is None and raw not in sg.MODEL_IDS
    h, port = metered(tmp_path, raw)
    run = h.ok(tc.run(h, tc.prompt_set(h, engines=("openai",))), 201)
    assert run["status"] == "completed" and port.asked == 3
    mac = h.svc.vault.field_hash("zbm", svc_engines.MODEL_FIELD, raw)[1]
    assert mac != hashlib.sha256(raw.encode()).hexdigest() and mac != sha(raw)
    assert h.svc.vault.field_hash("acme", svc_engines.MODEL_FIELD, raw)[1] != mac        # tenant-bound
    for kind in ("engine_calls_reserved", "engine_run_settled"):
        recs = records(h, kind)
        assert recs and all(r["data"]["model"] == "UNKNOWN_MODEL" and r["data"]["model_hmac"] == mac for r in recs)
        assert all(sg.violations(kind, r["data"]) == [] for r in recs)
        ps = payloads(h, kind)
        assert ps and all(p["model"] == "UNKNOWN_MODEL" and p["model_hmac"] == mac for p in ps)
        assert all(sg.violations("ledger:" + kind, p) == [] for p in ps)
    blob = sa.written(h, caplog)
    assert sa.found(blob, {"model": raw, "vendor": raw.split("/")[0], "name": raw.split("/")[1].split("@")[0]}) == []
    assert hashlib.sha256(raw.encode()).hexdigest() not in blob
    (er,) = h.svc.engine_runs.values()
    assert er["model"] == "UNKNOWN_MODEL"
    # an in-memory code-shaped name that no adapter declares is unknown too (nothing is admitted by shape)
    assert h.svc._engine_model("zbm", "m") == {"model": "UNKNOWN_MODEL",
                                               "model_hmac": h.svc.vault.field_hash("zbm", "engine_model", "m")[1]}
    assert h.svc._engine_model("zbm", None) == {"model": None}
    assert h.svc._engine_model("zbm", 7)["model"] == "UNKNOWN_MODEL"


def test_an_unknown_model_without_a_vault_key_is_the_code_alone(tmp_path):
    h, port = metered(tmp_path, "x")
    h.svc.vault._key = None
    assert h.svc._engine_model("zbm", unknown_model()) == {"model": "UNKNOWN_MODEL", "model_hmac": None}
    assert sg.violations("engine_calls_reserved", h.svc._engine_model("zbm", unknown_model())) == []


def test_mutation_an_unknown_model_written_raw_is_refused_and_nothing_is_asked(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(sg, "OBSERVER", None)               # this test's refused records are not the suite's
    raw = unknown_model()
    monkeypatch.setattr(svc_engines.EnginesMixin, "_engine_model", lambda self, tid, model: {"model": model})
    h, port = metered(tmp_path, raw)
    run = h.ok(tc.run(h, tc.prompt_set(h, engines=("openai",))), 201)
    assert port.asked == 0                                   # the reservation was refused: no request is made
    assert run["result"]["facts"]["engines"]["openai"]["reason_detail"] == "RESERVATION_NOT_RECORDED"
    assert records(h, "engine_calls_reserved") == [] and payloads(h, "engine_calls_reserved") == []
    assert sa.found(sa.written(h, caplog), {"model": raw}) == []
    before = (len(h.svc.log), len(h.ledger.events))
    for kind in ("engine_calls_reserved", "engine_run_settled"):
        with pytest.raises(Exception) as ei:
            h.svc._commit(kind, {"run_id": derived_id("egr", "x", "openai"), "model": raw}, "seo")
        assert getattr(ei.value, "reason", None) == "SINK_REFUSED"
        with pytest.raises(Exception) as ei:
            h.svc._commit(kind, {"run_id": derived_id("egr", "x", "openai"), "model": None}, "seo",
                          evidence=(kind, "citation_run:x", {"model": raw}, ("m", kind)))
        assert getattr(ei.value, "reason", None) == "SINK_REFUSED"
    assert (len(h.svc.log), len(h.ledger.events)) == before


# ================================================================================================ replay

def test_an_old_format_citation_line_replays_and_every_view_works(tmp_path, monkeypatch):
    h, v, _port = started(tmp_path)
    a = vw.citation_run(h)
    doc = document(h, a["citation_run_id"])
    cid = requested_only(h, monkeypatch, a["prompt_set"]["prompt_set_id"])
    old = old_format_result(h, doc)
    assert sg.violations("citation_run_recorded", {"result": old}) != []     # today's guard would refuse this line
    sa.raw_commit(h.svc, "citation_run_recorded", {"citation_run_id": cid, "result": old, "result_sha256": sha(old)})
    for restart in (False, True):
        hh = h.restart() if restart else h
        r = run_of(hh, cid)
        assert r["status"] == "completed" and r["result"] == old and r["result_sha256"] == sha(old)
        assert r["result_ref"] is None and r["result_hmac"] is None
        assert r["facts"] == svc_citations.run_facts(old) == {**a["facts"]}
        got = hh.ok(hh.get(f"/tenants/zbm/citation-runs/{cid}"))
        assert got["status"] == "completed" and "vault_unavailable" not in got
        assert got["result_sha256"] == sha(old) and got["result_hmac"] is None
        rows, new_rows = got["result"]["facts"]["rows"], a["result"]["facts"]["rows"]
        assert len(rows) == len(new_rows) > 0
        for x, y in zip(rows, new_rows):                    # the same view, the evidence named by its vault record
            assert vault_mod.parse_ref(x["evidence"]["vault_id"]) is not None and "evidence_id" not in x["evidence"]
            assert {k: w for k, w in x["evidence"].items() if k != "vault_id"} == \
                {k: w for k, w in y["evidence"].items() if k != "evidence_id"}
            assert x["url"] == y["url"] and x["engine_hint_unverified"] == y["engine_hint_unverified"]
        mp = hh.ok(hh.get(f"/tenants/zbm/citation-runs/{cid}/mnc-priority"))
        new_mp = hh.ok(hh.get(f"/tenants/zbm/citation-runs/{a['citation_run_id']}/mnc-priority"))
        assert [[e["excerpt"] for e in it["evidence"]] for it in mp["items"]] == \
            [[e["excerpt"] for e in it["evidence"]] for it in new_mp["items"]]
        assert [[x["url"] for x in it["cited_instead"]] for it in mp["items"]] == \
            [[x["url"] for x in it["cited_instead"]] for it in new_mp["items"]]
        listing = hh.ok(hh.get("/tenants/zbm/citation-runs"))
        assert [x["outcome"] for x in listing] == ["OK", "OK"]
        card = hh.ok(hh.get("/department"))["agents"]["callum"]["scorecard"]["citation_runs"]
        assert card["completed"] == 2 and card["outcomes"] == {"OK": 2}
        assert vw.found(json.dumps([got, mp], ensure_ascii=False), v) == []


def test_old_format_interrupted_and_engine_lines_replay(tmp_path, monkeypatch):
    h, v, _port = started(tmp_path)
    psid = tc.prompt_set(h, engines=("openai",))
    cid = requested_only(h, monkeypatch, psid)
    raw_model, where = unknown_model(), "callum:<lambda>:12"
    assert sg.classify(where) is None
    erid = derived_id("egr", cid, "openai")
    day = h.svc.today()
    old = [("engine_calls_reserved", {"run_id": erid, "audit_id": cid, "tenant_id": "zbm", "engine": "openai",
                                      "model": raw_model, "prompt_index": 0, "calls": 3, "spend_usd": "0.030000",
                                      "day": day, "prompt_set_sha256": None, "run_kind": "citation_run"}),
           ("engine_run_settled", {"run_id": erid, "audit_id": cid, "tenant_id": "zbm", "engine": "openai",
                                   "model": raw_model, "calls": 2, "calls_cost_unknown": 2, "spend_usd": "0.020000",
                                   "reserved_calls": 3, "reserved_usd": "0.030000", "answers": 2,
                                   "stop_reason": None, "by_day": {day: {"calls": 2, "spend_usd": "0.020000"}},
                                   "run_kind": "citation_run"}),
           ("citation_run_interrupted", {"citation_run_id": cid, "reason": "RUN_FAILED",
                                         "failure": {"agent": "callum", "where": where, "error_type": "KeyError"}})]
    for kind, data in old:
        assert sg.violations(kind, data) != [], kind         # today's guard would refuse each of these lines
        sa.raw_commit(h.svc, kind, data)
    for restart in (False, True):
        hh = h = h.restart() if restart else h
        er = hh.svc.engine_runs[erid]
        assert er["model"] == raw_model and er["settled"]["calls"] == 2 and er["reservations"]["0"]["calls"] == 3
        calls, spend = hh.svc.engine_usage("openai", day)
        assert calls == 2 and str(spend) == "0.020000"
        r = hh.ok(hh.get(f"/tenants/zbm/citation-runs/{cid}"))
        assert r["status"] == "interrupted" and r["interrupted_reason"] == "RUN_FAILED"
        assert r["failure"] == {"agent": "callum", "where": where, "error_type": "KeyError"}
        card = hh.ok(hh.get("/department"))["agents"]["callum"]["scorecard"]
        assert card["citation_runs"]["interrupted"] == 1 and card["engine_runs"]["openai"]["settled"] == 1
    # a new line of a run without the model field at all (never written, tolerated on replay)
    sa.raw_commit(h.svc, "engine_calls_reserved", {"run_id": derived_id("egr", cid, "google"), "audit_id": cid,
                                                   "tenant_id": "zbm", "engine": "google", "prompt_index": 0,
                                                   "calls": 1, "spend_usd": "0.010000", "day": day,
                                                   "prompt_set_sha256": None})
    assert h.restart().svc.engine_runs[derived_id("egr", cid, "google")]["model"] is None


# ================================================================================================ mutations

def test_mutation_the_result_written_inline_again_is_refused_and_nothing_is_written(tmp_path, monkeypatch):
    monkeypatch.setattr(sg, "OBSERVER", None)               # this test's refused records are not the suite's
    h, v, _port = started(tmp_path)
    a = vw.citation_run(h)
    cid = a["citation_run_id"]
    doc = document(h, cid)
    before = (len(h.svc.log), len(h.ledger.events), sa.vault_lines(h))
    for data in ({"citation_run_id": cid, "result": doc["result"], "result_sha256": sha(doc["result"])},
                 {"citation_run_id": cid, "result_ref": "x", "result_hmac": "0" * 64, "facts": {},
                  "note": doc["result"]["limitations"][0]},
                 {"citation_run_id": cid, "result_ref": "x", "result_hmac": "0" * 64,
                  "facts": {"outcome": guard_mod.WITHHELD}},
                 {"citation_run_id": cid, "result_ref": "x", "result_hmac": "0" * 64, "facts": {},
                  "evidence_records": doc["evidence"]}):
        with pytest.raises(Exception) as ei:
            h.svc._commit("citation_run_recorded", data, "seo")
        assert getattr(ei.value, "reason", None) == "SINK_REFUSED"
    assert (len(h.svc.log), len(h.ledger.events), sa.vault_lines(h)) == before
    # the same through the real path: a run whose completion record carries the result again is never recorded
    real = SeoService._commit

    def inline(self, kind, data, actor, evidence=None):
        if kind == "citation_run_recorded":
            data = {**data, "result": doc["result"]}
        return real(self, kind, data, actor, evidence=evidence)
    monkeypatch.setattr(SeoService, "_commit", inline)
    b = h.ok(tc.run(h, a["prompt_set"]["prompt_set_id"]), 201)
    assert b["status"] == "interrupted" and b["interrupted_reason"] == "RUN_FAILED"
    assert len(records(h, "citation_run_recorded")) == 1 and len(h.ledger.of_type("citation_run_recorded")) == 1
    blob = sa.written(h)
    assert vw.MARK not in blob and sa.found(blob, v) == []


def _scenario(tmp_path) -> dict:
    """The properties the mutations below must break, each as a bool (never a value)."""
    h, v, _port = started(tmp_path)
    h.tenant("acme", domains=("other.test",))
    a = vw.citation_run(h)
    cid = a["citation_run_id"]
    rec = h.svc.citation_runs.get(cid) or {}
    (line,) = records(h, "citation_run_recorded") or [None]
    out = {"owner_reads_the_result": isinstance(a.get("result"), dict) and "vault_unavailable" not in a}
    shown = json.dumps(a, ensure_ascii=False)
    out["owner_reads_full_evidence"] = vw.LEGIT_URL in shown and vw.LEGIT_TITLE in shown
    out["owner_view_is_masked"] = vw.found(shown, v) == []
    raw = h.svc.vault.get_blob("zbm", rec["result_ref"]) if rec.get("result_ref") and sa.vault_lines(h) else {}
    plain = hashlib.sha256(svc_vaulted.canonical(raw).encode()).hexdigest()
    out["hash_is_keyed"] = plain not in json.dumps(line) and plain not in json.dumps(h.ledger.events, default=str)
    out["other_tenant_reads_nothing"] = h.svc._vaulted_open("acme", FIELD, rec.get("result_ref"),
                                                           rec.get("result_hmac")) is None
    out["cross_tenant_is_404"] = h.get(f"/tenants/acme/citation-runs/{cid}").status_code == 404
    blob = sa.written(h)
    out["result_not_on_disk"] = vw.MARK not in blob and "Wilson" not in blob and sa.found(blob, v) == []
    if rec.get("result_ref") and sa.vault_lines(h):
        sa._lose(h, rec["result_ref"])
    r = h.get(f"/tenants/zbm/citation-runs/{cid}/mnc-priority")
    out["export_refuses_a_lost_blob"] = r.status_code == 503 and r.json().get("detail") == "VAULT_UNAVAILABLE"
    g = h.get(f"/tenants/zbm/citation-runs/{cid}").json()
    out["view_says_vault_unavailable"] = g.get("result") is None and g.get("vault_unavailable") is True
    return out


def test_the_scenario_holds_unmutated(tmp_path):
    got = _scenario(tmp_path)
    assert all(got.values()), sorted(k for k, ok in got.items() if not ok)


def test_mutation_the_blob_write_skipped_is_caught(tmp_path, monkeypatch):
    def commit_only(self, sealed, commit):
        commit()
    monkeypatch.setattr(svc_vaulted.VaultedMixin, "_vaulted_commit", commit_only)
    got = _scenario(tmp_path)
    assert got["owner_reads_the_result"] is False and got["owner_reads_full_evidence"] is False


def test_mutation_readers_ignoring_vault_unavailable_is_caught(tmp_path, monkeypatch):
    real = svc_citations.CitationsMixin.citation_run_view

    def view(self, tid, cid, full=True):
        out = real(self, tid, cid, full)
        if full and out.get("result") is None and out["status"] == "completed":
            out.pop("vault_unavailable", None)
            out["result"] = {"outcome": out["facts"]["outcome"], "facts": {}}     # an empty result served instead
        return out
    monkeypatch.setattr(svc_citations.CitationsMixin, "citation_run_view", view)
    got = _scenario(tmp_path)
    assert got["export_refuses_a_lost_blob"] is False and got["view_says_vault_unavailable"] is False
    assert got["owner_reads_the_result"] is True


def test_mutation_a_plain_sha256_instead_of_the_keyed_hash_is_caught(tmp_path, monkeypatch):
    monkeypatch.setattr(svc_vaulted.VaultedMixin, "_vaulted_hmac",
                        lambda self, tid, field, obj, vault=None: hashlib.sha256(
                            svc_vaulted.canonical(obj).encode()).hexdigest())
    got = _scenario(tmp_path)
    assert got["hash_is_keyed"] is False
    assert got["owner_reads_the_result"] is False            # and a reader refuses a hash that is not the keyed one


def test_mutation_a_cross_tenant_blob_read_is_caught_twice(tmp_path, monkeypatch):
    # layer 1 removed: the vault no longer binds a blob to its tenant
    monkeypatch.setattr(vault_mod, "_tenant_matches", lambda material, tenant_id, tag: True)
    monkeypatch.setattr(vault_mod, "_blob_manifest_aad", lambda tenant_id, blob_id, key_id: b"m" + blob_id.encode())
    monkeypatch.setattr(vault_mod, "_blob_chunk_aad",
                        lambda tenant_id, blob_id, index, count, key_id: f"c{blob_id}{index}{count}".encode())
    h, v, _port = started(tmp_path / "one")
    h.tenant("acme", domains=("other.test",))
    cid = vw.citation_run(h)["citation_run_id"]
    rec = run_of(h, cid)
    assert h.svc.vault.get_blob("acme", rec["result_ref"])["v"] == 1                 # the mutation bites
    # layer 2 still holds: the keyed hash is bound to the tenant, so the service's reader refuses
    assert h.svc._vaulted_open("acme", FIELD, rec["result_ref"], rec["result_hmac"]) is None
    # layer 3 (the API): another tenant's run is the same 404 whatever the vault would answer
    assert h.get(f"/tenants/acme/citation-runs/{cid}").status_code == 404
    # layers 1 and 2 removed: the read goes through, and the scenario's property fails
    monkeypatch.setattr(type(h.svc.vault), "field_hash_matches", lambda self, *a, **k: True)
    got = _scenario(tmp_path / "two")
    assert got["other_tenant_reads_nothing"] is False and got["cross_tenant_is_404"] is True
    # and with the API's tenant check removed as well, the run is served to the other tenant
    monkeypatch.setattr(svc_citations.CitationsMixin, "_owns", lambda self, r, tid: r is not None)
    assert _scenario(tmp_path / "three")["cross_tenant_is_404"] is False


def test_mutation_no_owner_masking_is_caught(tmp_path, monkeypatch):
    def unmasked(name, value, flag=None, recorded=None):
        return None if value is None else {"value": value, "alert": False, "reason": None}
    monkeypatch.setattr(callum, "_display", unmasked)
    got = _scenario(tmp_path)
    assert got["owner_view_is_masked"] is False and got["result_not_on_disk"] is True


def test_mutation_evidence_left_in_the_logged_facts_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(sg, "OBSERVER", None)
    real = svc_citations.run_facts

    def leaky(result):
        rows = (result.get("facts") or {}).get("rows") or []
        return {**real(result), "hosts": [r.get("host") for r in rows], "note": result.get("methodology")}
    monkeypatch.setattr(svc_citations, "run_facts", leaky)
    h, v, _port = started(tmp_path)
    run = vw.citation_run(h)
    assert run["status"] == "interrupted" and run["interrupted_reason"] == "RUN_FAILED"
    assert records(h, "citation_run_recorded") == []
    assert "Wilson" not in sa.written(h)


# ================================================================================================ size

ROWS = 2000


class BigPort:
    """An unmetered port whose every answer cites fourteen never-seen hosts."""

    connected, metered, accepts_guard, name, attempts = True, False, True, "openai", 1

    def __init__(self, tag: str = "north"):
        self.asked, self.tag = 0, tag

    def ask(self, prompt, guard=None, meter=None):
        self.asked += 1
        n = self.asked
        urls = [f"https://{self.tag}{n}.competitor{k}.test/page/{n}/{k}?ref=a{n}" for k in range(14)]
        a = ProviderAnswer("ANSWER", f"Z Best Media is named in answer {n} with several sources about recovery.",
                           urls, "m", "1", cite_from_text=False, provider="openai", asked_at="2026-10-09T18:00:00Z")
        a.citation_titles = {u: f"Title of page {n} {i}" for i, u in enumerate(urls)}
        a.citation_hints = {u: f"{self.tag}{n}.competitor{i}.test" for i, u in enumerate(urls)}
        return a


def test_a_two_thousand_row_run_keeps_the_log_line_small_and_the_lock_light(tmp_path, monkeypatch):
    h = vw.harness(tmp_path)
    h.ports.engines["openai"] = BigPort()
    h.ok(h.post("/tenants/zbm/domains", {"request_id": rid(), "domains": [DOMAIN]}, andre=True))
    psid = tc.prompt_set(h, engines=("openai",), prompts=tuple(f"who recovers revenue case {i}" for i in range(50)))
    vault = type(h.svc.vault)
    held = {"seal": [], "append": [], "open": []}
    timing = {"record": 0.0, "guard": 0.0}
    real_put, real_append, real_get = vault.put_blob, vault._append, vault.get_blob
    real_record, real_enforce = SeoService._record_citation_result, sg.enforce

    def put_blob(self, tid, obj):
        held["seal"].append(h.svc.lock._is_owned())
        return real_put(self, tid, obj)

    def append(self, line):
        if self is h.svc.vault:
            held["append"].append(h.svc.lock._is_owned())
        return real_append(self, line)

    def get_blob(self, tid, ref):
        held["open"].append(h.svc.lock._is_owned())
        return real_get(self, tid, ref)

    def record(self, *a, **k):
        t = time.perf_counter()
        try:
            return real_record(self, *a, **k)
        finally:
            timing["record"] += time.perf_counter() - t

    def enforce(kind, data):
        t = time.perf_counter()
        try:
            return real_enforce(kind, data)
        finally:
            if kind.endswith("citation_run_recorded"):
                timing["guard"] += time.perf_counter() - t
    monkeypatch.setattr(vault, "put_blob", put_blob)
    monkeypatch.setattr(vault, "_append", append)
    monkeypatch.setattr(vault, "get_blob", get_blob)
    monkeypatch.setattr(SeoService, "_record_citation_result", record)
    monkeypatch.setattr(sg, "enforce", enforce)
    run = h.ok(tc.run(h, psid), 201)
    rows = run["result"]["facts"]["rows"]
    assert run["status"] == "completed" and len(rows) == ROWS == callum.MAX_ROWS
    hosts = {r["host"] for r in rows if r["host"]}
    assert len(hosts) > 1800                                 # hosts no guard cache has ever seen (rows without a
    #                                                          cited URL, and a host the strict check withholds, aside)
    assert held["seal"] == [False] and held["append"] == [True] and not any(held["open"])
    (rec,) = records(h, "citation_run_recorded")
    assert len(json.dumps(rec)) < 2048 and rec["data"]["facts"]["rows"] == ROWS
    assert sa.vault_lines(h) >= 3                            # more than one chunk, plus the manifest: ONE document
    blob = sa.written(h)
    assert "competitor3.test" not in blob and "north1." not in blob     # no host of any row in any file
    # generous bounds (the measured numbers are in the job's report): no per-row work under the lock or in the guard
    assert timing["guard"] < 0.05 and timing["record"] < 1.0
    got = h.ok(h.get(f"/tenants/zbm/citation-runs/{run['citation_run_id']}"))
    assert got == run
    mp = h.ok(h.get(f"/tenants/zbm/citation-runs/{run['citation_run_id']}/mnc-priority"))
    assert mp["outcome"] == "OK" and len(mp["items"]) > 0
    h2 = h.restart()
    assert h2.ok(h2.get(f"/tenants/zbm/citation-runs/{run['citation_run_id']}"))["result"] == run["result"]


def test_the_helpers_in_this_file_plant_what_they_say():
    rng = random.Random(SEED)
    p = planted(rng)
    answers = leak_answers(vw.synthetic(SEED), p)
    text = json.dumps([[a.text, a.citations, getattr(a, "citation_titles", {}), getattr(a, "citation_hints", {})]
                       for a in answers]) \
        + json.dumps(leak_prompts(p)) + json.dumps(hop_hosts(p))
    assert sa.found(text, p) == sorted(p)
    assert all(guard_mod.check(h) is not None for h in hop_hosts(p)[:2]) and guard_mod.check(hop_hosts(p)[2]) is None
