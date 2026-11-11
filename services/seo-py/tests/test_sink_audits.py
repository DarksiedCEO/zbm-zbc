"""Sink job B: audit reports and drift detail are the client's content, kept whole in the tenant's vault; the log and
the ledger keep the blob ref, a keyed HMAC and machine facts only.

Covered: the log / ledger shape of ``audit_completed``, ``drift_recorded`` and ``audit_interrupted``; a real audit of
a tenant that HAS an entity record and a prompt set, with secret- and personal-data-shaped values planted in the entity
fields, the prompts, a page title, a canonical link and a redirect target (nothing of them in any data-directory file,
ledger payload or captured log, in raw / hex / base64 at three alignments / URL-encoded / JSON-escaped / digits-only
form; the owner's GET shows the report through the client-view masker); the write order and its failures (no key, a
failed blob write, a failed commit, a kill switch mid-run); readers failing closed on a lost or changed blob; a replay
of a line written BEFORE this change; and mutations showing each rule is load-bearing.

Every sensitive-looking value is GENERATED at run time; failures report names and counts only."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import random
import urllib.parse

import pytest

import helpers
import sink_guard as sg
import svc_audits
import svc_schedules
import svc_vaulted
import test_review_c0c09bd1 as rc
import vault as vault_mod
from clock import iso
from envelope import sha
from fixture_server import home_html, install_site
from helpers import invoice_id, rid
from ports import Ports, ProviderAnswer
from service import SeoService, sha256_hex

pytestmark = pytest.mark.local_http
SEED = 20261110
DOMAIN = "site.test"


# ================================================================================================ helpers

class Engine:
    connected = True

    def ask(self, prompt, model=None):
        return ProviderAnswer("ANSWER", "Z Best Media in Long Beach.", ["http://site.test/"], "m", "1")


def harness(tmp_path, srv, data=True, engine=True, own=True, **env) -> helpers.Harness:
    tmp_path.mkdir(parents=True, exist_ok=True)
    install_site(srv)
    ports = Ports.default()
    ports.fetcher = srv.site_fetcher()
    if engine:
        ports.engines["openai"] = Engine()
    h = helpers.Harness(tmp_path, data_dir=str(tmp_path / "data") if data else None, ports=ports,
                        SEO_PROBE_SAMPLES="3", **env)
    if own:
        h.ok(h.post("/tenants/zbm/domains", {"request_id": rid(), "domains": [DOMAIN]}, andre=True))
    return h


def audit(h, tid="zbm", paths=("/", "/about"), **body):
    b = {"request_id": rid(), "domain": DOMAIN, "scheme": "http", "paths": list(paths), **body}
    return h.post(f"/tenants/{tid}/audits", b, caller="dashboard", andre=tid != "zbm")


def schedule(h, tid="zbm"):
    return h.ok(h.post(f"/tenants/{tid}/schedules", {"request_id": rid(), "domain": DOMAIN, "scheme": "http",
                                                      "paths": ["/", "/about"], "every_days": 7}), 201)


def tick(h) -> dict:
    return h.ok(h.post("/jobs/schedule-tick/run", {"request_id": rid()}, caller="scheduler"))


def two_ticks(h, srv) -> dict:
    """A schedule with two completed slots whose second differs: one drift report recorded."""
    s = schedule(h)
    assert tick(h)["ran"] == 1
    h.clock.advance(days=7)
    srv.html(DOMAIN, "/", home_html(canonical=None))
    t = tick(h)
    assert t["ran"] == 1 and t["drift_recorded"] == 1
    return s


def records(h, kind: str) -> list:
    return [r for r in h.svc.log.iter_records() if r["kind"] == kind]


def ledger_payloads(h, event_type: str) -> list:
    return [e["_payload"] for e in h.ledger.of_type(event_type)]


def client_report(h, tid: str, aid: str):
    return h.post(f"/tenants/{tid}/audits/{aid}/client-report", {"request_id": rid(), "format": "html"},
                  caller="dashboard", andre=True)


def vault_lines(h) -> int:
    return h.svc.vault.status()["records"]


def forms(v: str) -> set:
    """Every form a value is searched in: raw, hex (both cases), base64 and base64url at the three byte alignments
    (padded and not; the unaligned ones without the characters that depend on the neighbours), URL-encoded (two
    ways), JSON-escaped, and the bare digits of a value holding nine or more."""
    b = v.encode("utf-8")
    out = {v, b.hex(), b.hex().upper(), urllib.parse.quote(v, safe=""), urllib.parse.quote_plus(v),
           json.dumps(v)[1:-1], json.dumps(v, ensure_ascii=False)[1:-1]}
    for enc in (base64.b64encode, base64.urlsafe_b64encode):
        whole = enc(b).decode()
        out |= {whole, whole.rstrip("=")}
        for pad in (1, 2):                                   # the value starting 1 or 2 bytes into a base64 group
            e = enc(b"\x00" * pad + b).decode().rstrip("=")
            out.add(e[4:len(e) - 4] if len(e) > 16 else e[4:])
    digits = "".join(c for c in v if c.isdigit())
    if len(digits) >= 9:
        out.add(digits)
    return {f for f in out if len(f) >= 6}


def written(h, caplog=None) -> str:
    """Everything the service wrote: every file under the data directory (raw, and each JSON line re-dumped both
    ways), the ledger events with their payloads, and the captured logs."""
    blob = json.dumps(h.ledger.events, ensure_ascii=False, default=str) + json.dumps(h.ledger.events, default=str)
    if h.settings.data_dir:
        for root, _d, files in os.walk(h.settings.data_dir):
            for f in files:
                with open(os.path.join(root, f), "rb") as fh:
                    raw = fh.read().decode("utf-8", "replace")
                blob += raw
                for line in raw.splitlines():
                    if line.startswith("{"):
                        blob += json.dumps(json.loads(line), ensure_ascii=False)
    if caplog is not None:
        blob += caplog.text
    return blob


def found(blob: str, values: dict) -> list:
    """Names (never values) of the planted values present in ``blob`` in any form."""
    return sorted(n for n, v in values.items() if any(f in blob for f in forms(v)))


def planted(rng) -> dict:
    a3, b3, c4 = rc.r6._nanp(rng)
    s1, s2, s3 = rc.r6._ssn(rng)
    loc, dom = rc._mail(rng)
    return {"entity_card": rc._card(rng), "entity_ssn": rc._ssn(rng), "entity_phone": rc._phone(rng),
            "entity_email": rc._email(rng), "prompt_label_value": rc._secret_word(rng),
            "prompt_ssn_words": f"{rc._words(s1)} {rc._words(s2)} {rc._words(s3)}",
            "prompt_phone_words": rc._words(a3 + b3 + c4), "prompt_local": loc, "prompt_dom": dom,
            "title_email": rc._email(rng), "title_label_value": rc._secret_word(rng),
            "canonical_card": rc._card(rng), "redirect_email": rc._email(rng),
            "redirect_digits": rc._r(rng, "123456789", 14)}


def plant_site(srv, v: dict) -> None:
    """The fixture site with planted values in the home page's title and canonical link, and a redirect whose
    target carries personal data."""
    q = urllib.parse.quote
    srv.html(DOMAIN, "/", home_html(title=f"Write {v['title_email']} password is {v['title_label_value']}",
                                    canonical=f"http://site.test/order/{v['canonical_card']}"))
    target = f"/landing?email={q(v['redirect_email'])}&variant={v['redirect_digits']}"
    srv.redirect(DOMAIN, "/moved", f"http://site.test{target}")
    srv.html(DOMAIN, target.split("?")[0], home_html(title="Landing"))


def plant_tenant(h, v: dict) -> dict:
    """A paying client with an entity record and a prompt set, both holding planted values."""
    h.tenant("acme", domains=(DOMAIN,))
    h.ok(h.post("/tenants/acme/finance-client", {"request_id": rid(), "finance_client_id": "acme-party-1"},
                andre=True))
    h.ok(h.post("/tenants/acme/entity", {"request_id": rid(), "fields": [
        {"field": "name", "value": f"Acme {v['entity_card']}", "source": "founder_statement",
         "provenance": "stated", "authority": "first_party"},
        {"field": "telephone", "value": v["entity_phone"], "source": "founder_statement", "provenance": "stated",
         "authority": "first_party"},
        {"field": "street_address", "value": f"{v['entity_ssn']} Main St, write {v['entity_email']}",
         "source": "founder_statement", "provenance": "stated", "authority": "first_party"}]}, andre=True), 201)
    prompts = [f"how to reset it, my password is {v['prompt_label_value']}",
               f"reach {v['prompt_local']} at {v['prompt_dom']}.com", f"verify {v['prompt_ssn_words']}",
               f"call {v['prompt_phone_words']}", "who recovers marketplace revenue"]
    return h.ok(h.post("/tenants/acme/prompt-sets", {
        "request_id": rid(), "name": "core", "brand_terms": ["Z Best Media"], "competitor_domains": [],
        "prompts": prompts, "engines": ["openai"]}, caller="dashboard"), 201)


def strings(x) -> list:
    if isinstance(x, str):
        return [x]
    if isinstance(x, dict):
        return [s for k, v in x.items() for s in strings(k) + strings(v)]
    if isinstance(x, list):
        return [s for v in x for s in strings(v)]
    return []


def raw_commit(svc, kind: str, data: dict, actor: str = "seo") -> None:
    """One log line written as a binary from BEFORE sink job B wrote it: anchored and appended, no sink guard."""
    with svc.lock:
        at = iso(svc.now())
        data = {**data, "actor": actor}
        rec, line = svc.log.prepare(kind, at, data)
        line_sha = sha256_hex(line)
        epoch = svc.log.epoch or line_sha[:16]
        eid, payload = svc._anchor_ids(epoch, rec["seq"], line_sha)
        svc._anchor(eid, actor, epoch, payload, kind, rec["seq"])
        svc.log.append_prepared(rec, line)
        svc._apply(kind, data, at)


class _Stop(BaseException):
    pass


def old_format_audit(h, monkeypatch) -> tuple:
    """An audit recorded in the OLD format: ``audit_requested`` as always, then ``audit_completed`` carrying the
    whole report and its plain SHA-256 (what the service wrote before sink job B). Returns (audit id, report)."""
    box = {}
    real = SeoService._run_audit

    def run_and_stop(self, aid, tid, body, ps):
        box["aid"], box["report"] = aid, json.loads(json.dumps(real(self, aid, tid, body, ps), default=str))
        raise _Stop()
    with monkeypatch.context() as m:
        m.setattr(SeoService, "_run_audit", run_and_stop)
        with pytest.raises(_Stop):
            h.svc.request_audit("dashboard", "zbm", {"request_id": rid(), "domain": DOMAIN, "scheme": "http",
                                                      "paths": ["/", "/about"]}, andre=False)
    raw_commit(h.svc, "audit_completed", {"audit_id": box["aid"], "report": box["report"],
                                          "report_sha256": sha(box["report"])})
    return box["aid"], box["report"]


# ================================================================================================ the log's shape

def test_audit_completed_keeps_ids_codes_counts_a_ref_and_a_keyed_hash(tmp_path, srv):
    h = harness(tmp_path, srv)
    a = h.ok(audit(h), 201)
    (rec,) = records(h, "audit_completed")
    d = rec["data"]
    assert set(d) == {"audit_id", "report_ref", "report_hmac", "facts", "actor", "evidence"}
    assert sg.violations("audit_completed", d) == []                 # machine values only, no legacy entry needed
    assert vault_mod.parse_blob_ref(d["report_ref"]) is not None
    facts = d["facts"]
    assert set(facts) == {"report_version", "outcome", "findings", "started_at", "finished_at", "agents"}
    assert all(set(x) == {"agent", "task", "outcome", "findings"} and type(x["findings"]) is int
               for x in facts["agents"])
    report = h.svc._audit_recorded("zbm", a["audit_id"])["report"]
    assert facts["outcome"] == report["outcome"] and facts["findings"] == report["summary"]["findings"]
    assert [(e["agent"], e["task"], e["outcome"], len(e["findings"])) for e in report["agents"]] == \
        [(x["agent"], x["task"], x["outcome"], x["findings"]) for x in facts["agents"]]
    # the hash is the vault's keyed HMAC of the canonical report, never a plain SHA-256 of it
    canon = svc_vaulted.canonical(report)
    assert d["report_hmac"] == h.svc.vault.field_hash("zbm", "audit_report", canon)[1]
    plain = {hashlib.sha256(canon.encode()).hexdigest(), sha(report)}
    line = json.dumps(rec)
    assert not any(p in line for p in plain) and "report_sha256" not in line
    # the ledger: the keyed hash, the outcome and the count
    (p,) = ledger_payloads(h, "audit_report_recorded")
    assert set(p) == {"audit_id", "report_hmac", "outcome", "findings", "rk", "seq"}
    assert p["report_hmac"] == d["report_hmac"] and not any(x in json.dumps(h.ledger.events) for x in plain)
    # memory holds no report; the owner's view is read from the vault
    assert h.svc.audits[a["audit_id"]]["report"] is None
    assert a["report"]["audit_id"] == a["audit_id"] and a["report_hmac"] == d["report_hmac"]
    assert "report_ref" not in a and a["report_sha256"] is None and a["facts"] == facts
    # the list view and a non-owner caller's view: machine facts only, no vault read
    (row,) = h.ok(h.get("/tenants/zbm/audits"))
    assert "report" not in row and row["facts"]["outcome"] == facts["outcome"]
    fin = h.ok(h.get(f"/tenants/zbm/audits/{a['audit_id']}", caller="finance_31"))
    assert "report" not in fin and fin["facts"] == facts


def test_no_legacy_entry_remains_for_the_audit_sinks():
    """Sink job D deleted the legacy list: there is no entry for any sink."""
    assert not hasattr(sg, "LEGACY_ROWS") and not hasattr(sg, "LEGACY_PROSE_PATHS") and not hasattr(sg, "LEGACY_COUNTS")


def test_the_report_is_not_readable_at_rest(tmp_path, srv):
    h = harness(tmp_path, srv)
    h.ok(audit(h), 201)
    blob = written(h)
    # words every report carries (the legend, the limitations, a methodology, a page title)
    for needle in ("credit is not causation", "untrusted", "Revenue recovery", "methodology", "limitations"):
        assert needle not in blob


# ================================================================================================ the planted values

def test_a_real_audit_with_an_entity_and_a_prompt_set_keeps_nothing_raw(tmp_path, srv, caplog):
    caplog.set_level(logging.DEBUG)
    rng = random.Random(SEED)
    v = planted(rng)
    h = harness(tmp_path, srv, own=False)
    plant_site(srv, v)
    ps = plant_tenant(h, v)
    a = h.ok(audit(h, "acme", paths=("/", "/about", "/moved"), invoice_id=invoice_id(),
                   prompt_set_id=ps["prompt_set_id"]), 201)
    assert a["status"] == "completed"
    aid = a["audit_id"]
    # the audit really compared the entity record and really asked the prompts (the two earlier leaks' paths)
    report = h.svc._audit_recorded("acme", aid)["report"]
    agents = {(e["agent"], e["task"]): e for e in report["agents"]}
    ent = agents[("entity_check", "on_site_entity_consistency")]
    canonicals = [f["detail"]["canonical"] for f in ent["findings"] if "canonical" in (f.get("detail") or {})]
    assert any(v["entity_card"] in c for c in canonicals) and any(v["entity_phone"] in c for c in canonicals)
    cal = agents[("callum", "citation_sources")]
    asked = [p["prompt"]["text"] for p in cal["facts"]["engines"]["openai"]["prompts"] if p.get("prompt")]
    assert any(v["prompt_label_value"] in t for t in asked) and len(asked) == 5
    everything = json.dumps(report)
    in_report = found(everything, v)
    assert {"entity_card", "entity_phone", "entity_ssn", "entity_email", "prompt_label_value", "prompt_ssn_words",
            "prompt_phone_words", "title_email", "title_label_value", "canonical_card", "redirect_email",
            "redirect_digits"} <= set(in_report)                     # the vaulted report holds them (it is the client's)
    # a drift report over two such audits, and a client report, are recorded too
    b = h.ok(audit(h, "acme", paths=("/", "/about", "/moved"), invoice_id=invoice_id(2),
                   prompt_set_id=ps["prompt_set_id"]), 201)
    h.ok(h.get(f"/tenants/acme/audits/{b['audit_id']}/drift", params={"against": aid}))
    h.ok(client_report(h, "acme", aid))
    # the scan: every data-directory file, every ledger payload, every captured log line: nothing, in any form
    assert found(written(h, caplog), v) == []
    # and every string of the audit's log records is a machine value
    for kind in ("audit_completed", "audit_requested"):
        for r in records(h, kind):
            assert sg.violations(kind, r["data"]) == []
    # the owner's GET: the report through the client-view masker
    shown = json.dumps(h.ok(h.get(f"/tenants/acme/audits/{aid}")))
    masked = {"entity_card", "entity_ssn", "entity_email", "entity_phone", "prompt_label_value", "prompt_ssn_words",
              "prompt_phone_words", "title_email", "title_label_value", "canonical_card", "redirect_email"}
    assert [n for n in sorted(masked) if v[n] in shown] == []
    assert "[hidden: looks like" in shown and "who recovers marketplace revenue" in shown
    # a restart replays the log: the same view, from the vault
    h2 = h.restart()
    again = h2.ok(h2.get(f"/tenants/acme/audits/{aid}"))
    assert again == json.loads(shown) and h2.svc.audits[aid]["report"] is None


def test_mutation_the_report_written_inline_again_is_refused_and_nothing_is_written(tmp_path, srv, monkeypatch):
    monkeypatch.setattr(sg, "OBSERVER", None)               # this test's refused records are not the suite's
    h = harness(tmp_path, srv)
    a = h.ok(audit(h), 201)
    report = h.svc._audit_recorded("zbm", a["audit_id"])["report"]
    before = (len(h.svc.log), len(h.ledger.events), vault_lines(h))
    for data in ({"audit_id": a["audit_id"], "report": report, "report_sha256": sha(report)},
                 {"audit_id": a["audit_id"], "report_ref": "x", "report_hmac": "0" * 64, "facts": {},
                  "note": report["limitations"][0]}):
        with pytest.raises(Exception) as ei:
            h.svc._commit("audit_completed", data, "seo")
        assert getattr(ei.value, "reason", None) == "SINK_REFUSED"
    assert (len(h.svc.log), len(h.ledger.events), vault_lines(h)) == before
    # the same through the real path: an audit whose completion record carries the report again is never recorded
    real = SeoService._commit

    def inline(self, kind, data, actor, evidence=None):
        if kind == "audit_completed":
            data = {**data, "report": report}
        return real(self, kind, data, actor, evidence=evidence)
    monkeypatch.setattr(SeoService, "_commit", inline)
    r = audit(h)
    assert r.status_code == 503 and r.json()["detail"] == "SINK_REFUSED"
    assert len(records(h, "audit_completed")) == 1


# ================================================================================================ write order

def test_no_vault_key_refuses_the_audit_up_front_and_nothing_is_written(tmp_path, srv):
    h = harness(tmp_path, srv)
    h2 = h.restart(SEO_VAULT_KEY_FILE=None)
    assert not h2.svc.vault.key_configured
    before = (len(h2.svc.log), len(h2.ledger.events), written(h2))
    seen = len(srv.seen)
    h2.refused(audit(h2), 503, "VAULT_UNAVAILABLE")
    assert (len(h2.svc.log), len(h2.ledger.events), written(h2)) == before and len(srv.seen) == seen
    assert h2.svc.audits == {} and not h2.svc.running_audits


def test_a_scheduled_slot_waits_while_there_is_no_vault_key(tmp_path, srv):
    h = harness(tmp_path, srv)
    s = schedule(h)
    h2 = h.restart(SEO_VAULT_KEY_FILE=None)
    t = tick(h2)
    assert t["ran"] == 0 and t["refused"] == 1
    assert h2.svc.schedules[s["schedule_id"]]["slots"] == {} and h2.svc.audits == {}


def _fail_write(*a, **k):
    raise vault_mod.VaultWriteError("vault write failed: test")


def test_a_failed_blob_write_closes_the_audit_failed_with_nothing_raw(tmp_path, srv, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    h = harness(tmp_path, srv)
    before = vault_lines(h)
    monkeypatch.setattr(h.svc.vault, "_append", _fail_write)        # the citation vault only (the report's)
    a = h.ok(audit(h), 201)
    assert a["status"] == "interrupted" and a["interrupted_reason"] == "RUN_FAILED" and a["report"] is None
    assert a["failure"] == {"agent": "report", "where": "vault", "error_type": "VaultWriteError"}
    assert vault_lines(h) == before and records(h, "audit_completed") == []
    assert h.svc.vault.status()["orphaned_records"] == 0 and not h.svc.running_audits
    (rec,) = records(h, "audit_interrupted")
    assert sg.violations("audit_interrupted", rec["data"]) == []
    blob = written(h, caplog)
    assert "credit is not causation" not in blob and "Revenue recovery" not in blob


def test_a_failed_seal_closes_the_audit_failed(tmp_path, srv, monkeypatch):
    h = harness(tmp_path, srv)

    def boom(self, tid, obj):
        raise vault_mod.VaultRecordInvalid("the blob is not encodable as strict JSON")
    monkeypatch.setattr(type(h.svc.vault), "put_blob", boom)
    a = h.ok(audit(h), 201)
    assert a["status"] == "interrupted" and a["failure"]["where"] == "vault"
    assert a["failure"]["error_type"] == "VaultRecordInvalid" and vault_lines(h) == 0


def test_a_failed_commit_after_the_blob_write_counts_an_orphan(tmp_path, srv):
    h = harness(tmp_path, srv)

    def home(handler):
        h.ledger.fail = True                                 # the ledger is lost while the run is under way
        handler._send(200, {"Content-Type": "text/html"}, home_html().encode())
    srv.routes[(DOMAIN, "/")] = home
    h.refused(audit(h), 503, "LEDGER_UNAVAILABLE")
    h.ledger.fail = False
    st = h.svc.vault.status()
    assert st["orphaned_records"] == st["records"] >= 2      # the blob's chunk and manifest lines: written, unreferenced
    assert records(h, "audit_completed") == []
    (a,) = h.ok(h.get("/tenants/zbm/audits"))
    assert a["status"] == "running_elsewhere_or_interrupted" and a["report_hmac"] is None
    assert h.ok(h.get("/status"))["citation_vault"]["orphaned_records"] == st["records"]


def test_a_kill_switch_engaged_mid_run_writes_no_blob(tmp_path, srv):
    h = harness(tmp_path, srv)

    def home(handler):
        h.svc.switches["capability"]["audit"] = True         # engaged while the run is under way (memory only)
        handler._send(200, {"Content-Type": "text/html"}, home_html().encode())
    srv.routes[(DOMAIN, "/")] = home
    a = h.ok(audit(h), 201)
    assert a["status"] == "interrupted" and a["interrupted_reason"].startswith("KILLED")
    assert vault_lines(h) == 0 and h.svc.vault.status()["orphaned_records"] == 0


def test_the_blob_is_encrypted_outside_the_service_lock_and_appended_once_under_it(tmp_path, srv, monkeypatch):
    h = harness(tmp_path, srv)
    vault = type(h.svc.vault)
    held = {"seal": [], "append": [], "open": []}
    real_put, real_append, real_get = vault.put_blob, vault._append, vault.get_blob

    def owned() -> bool:
        return h.svc.lock._is_owned()

    def put_blob(self, tid, obj):
        held["seal"].append(owned())
        return real_put(self, tid, obj)

    def append(self, line):
        if self is h.svc.vault:                              # the reports' vault (not the operator-text one)
            held["append"].append(owned())
        return real_append(self, line)

    def get_blob(self, tid, ref):
        held["open"].append(owned())
        return real_get(self, tid, ref)
    monkeypatch.setattr(vault, "put_blob", put_blob)
    monkeypatch.setattr(vault, "_append", append)
    monkeypatch.setattr(vault, "get_blob", get_blob)
    a = h.ok(audit(h), 201)
    h.ok(h.get(f"/tenants/zbm/audits/{a['audit_id']}"))
    h.ok(client_report(h, "zbm", a["audit_id"]))
    b = h.ok(audit(h), 201)
    h.ok(h.get(f"/tenants/zbm/audits/{b['audit_id']}/drift", params={"against": a["audit_id"]}))
    s = two_ticks(h, srv)
    h.ok(h.get(f"/tenants/zbm/schedules/{s['schedule_id']}"))
    h.ok(h.get("/tenants/zbm/schedules"))
    import test_logs as tl
    h.ok(tl.ingest(h), 201)                                  # reads the latest audit's sitemap sample
    assert held["seal"] and not any(held["seal"])            # encryption: never under the service lock
    assert held["open"] and not any(held["open"])            # every read of a blob: never under the service lock
    assert held["append"] and all(held["append"])            # the one append per document: under it, with the commit
    assert len(held["append"]) == len(held["seal"]) == len(records(h, "audit_completed")) + len(
        records(h, "drift_recorded")) + len(records(h, "log_ingest_created"))     # (sink job D: the ingest's inputs)


# ================================================================================================ readers fail closed

def _lose(h, ref: str) -> None:
    """The blob's manifest line is forgotten (as a vault file restored without it)."""
    h.svc.vault._index.pop(vault_mod.parse_blob_ref(ref))


def _tamper(h, ref: str) -> None:
    """The blob's manifest now points at ANOTHER blob's (same tenant): readable ciphertext, the wrong document."""
    idx = h.svc.vault._index
    others = [k for k in idx if k != vault_mod.parse_blob_ref(ref)]
    idx[vault_mod.parse_blob_ref(ref)] = idx[others[0]]


@pytest.mark.parametrize("damage", ["lost", "tampered", "no_key"])
def test_a_lost_or_changed_blob_shows_vault_unavailable_and_dependants_refuse(tmp_path, srv, damage):
    h = harness(tmp_path, srv)
    a = h.ok(audit(h), 201)
    b = h.ok(audit(h), 201)
    s = two_ticks(h, srv)
    sid = s["schedule_id"]
    ref = h.svc.audits[a["audit_id"]]["report_ref"]
    dref = h.svc.schedules[sid]["drifts"][-1]["drift_ref"]
    if damage == "lost":
        _lose(h, ref)
        _lose(h, dref)
    elif damage == "tampered":
        _tamper(h, ref)
        _tamper(h, dref)
    else:
        h = h.restart(SEO_VAULT_KEY_FILE=None)
    got = h.ok(h.get(f"/tenants/zbm/audits/{a['audit_id']}"))
    assert got["status"] == "completed" and got["report"] is None and got["vault_unavailable"] is True
    assert got["facts"]["outcome"] and got["report_hmac"]                    # the machine facts are still served
    (first, *_rest) = h.ok(h.get("/tenants/zbm/audits"))
    assert first["status"] == "completed" and "report" not in first          # the list reads no blob
    assert ("vault_unavailable" in first) == (damage == "no_key")           # (no key: its PATHS cannot be read)
    h.refused(client_report(h, "zbm", a["audit_id"]), 503, "VAULT_UNAVAILABLE")
    h.refused(h.get(f"/tenants/zbm/audits/{b['audit_id']}/drift", params={"against": a["audit_id"]}), 503,
              "VAULT_UNAVAILABLE")
    assert h.ledger.of_type("client_report_rendered") == []
    d = h.ok(h.get(f"/tenants/zbm/schedules/{sid}"))["drifts"][-1]
    assert d["drift"] is None and d["vault_unavailable"] is True and d["counts"]
    # the department scorecards use the machine facts only: unaffected
    card = h.ok(h.get("/department"))["agents"]
    assert card["roman"]["scorecard"]["runs"] == 4 and card["osei"]["scorecard"]["drift_reports_recorded"] == 1
    if damage != "no_key":
        ok = h.ok(h.get(f"/tenants/zbm/audits/{b['audit_id']}"))
        assert ok["report"] is not None and "vault_unavailable" not in ok


def test_a_scheduled_drift_is_not_recorded_when_a_report_cannot_be_read(tmp_path, srv, caplog):
    h = harness(tmp_path, srv)
    s = schedule(h)
    assert tick(h)["ran"] == 1
    (first,) = h.svc.audits
    _lose(h, h.svc.audits[first]["report_ref"])
    h.clock.advance(days=7)
    srv.html(DOMAIN, "/", home_html(canonical=None))
    with caplog.at_level(logging.WARNING):
        t = tick(h)
    assert t["ran"] == 1 and t["drift_recorded"] == 0 and records(h, "drift_recorded") == []
    assert h.svc.schedules[s["schedule_id"]]["drifts"] == []
    assert "drift not recorded: VAULT_UNAVAILABLE" in caplog.text


def test_a_log_ingest_reads_the_sitemap_sample_from_the_vault_or_refuses(tmp_path, srv):
    import test_logs as tl
    h = harness(tmp_path, srv)
    a = h.ok(audit(h), 201)
    g = h.ok(tl.ingest(h), 201)
    # sink job D: the sample is in the ingest's inputs blob (memory keeps its count)
    assert h.svc._ingest_inputs("zbm", g["ingest_id"])["sitemap_sample"] == ["/", "/about"]
    assert h.svc.log_ingests[g["ingest_id"]]["sitemap_sample_count"] == 2
    _lose(h, h.svc.audits[a["audit_id"]]["report_ref"])
    before = len(h.svc.log)
    h.refused(tl.ingest(h), 503, "VAULT_UNAVAILABLE")        # never created against a sample that could not be read
    assert len(h.svc.log) == before


def test_another_tenant_cannot_read_the_report_or_the_blob(tmp_path, srv):
    h = harness(tmp_path, srv)
    h.tenant("globex", domains=("globex.test",))
    a = h.ok(audit(h), 201)
    aid = a["audit_id"]
    rec = h.svc.audits[aid]
    for path in (f"/tenants/globex/audits/{aid}", f"/tenants/globex/audits/{aid}/client-reports"):
        h.refused(h.get(path), 404, "AUDIT_NOT_FOUND")
    h.refused(h.get(f"/tenants/globex/audits/{aid}/drift", params={"against": aid}), 404, "AUDIT_NOT_FOUND")
    h.refused(client_report(h, "globex", aid), 404, "AUDIT_NOT_FOUND")
    h.refused(h.get(f"/tenants/zbm/audits/{aid}", caller="hub", tenant="globex"), 404, "TENANT_NOT_FOUND")
    with pytest.raises(vault_mod.VaultNotFound):
        h.svc.vault.get_blob("globex", rec["report_ref"])
    assert h.svc._vaulted_open("globex", "audit_report", rec["report_ref"], rec["report_hmac"]) is None
    assert h.svc._vaulted_open("zbm", "audit_report", rec["report_ref"], rec["report_hmac"]) is not None
    # the keyed hash is bound to the tenant and to the field
    canon = svc_vaulted.canonical(h.svc._audit_recorded("zbm", aid)["report"])
    assert h.svc.vault.field_hash("globex", "audit_report", canon)[1] != rec["report_hmac"]
    assert h.svc.vault.field_hash("zbm", "drift_report", canon)[1] != rec["report_hmac"]


def test_the_report_reads_after_a_key_rotation(tmp_path, srv):
    h = harness(tmp_path, srv)
    a = h.ok(audit(h), 201)
    old_file = h.env["SEO_VAULT_KEY_FILE"]
    new_file = helpers.write_vault_key(tmp_path / "vault2.key")
    h2 = h.restart(SEO_VAULT_KEY_FILE=new_file, SEO_VAULT_KEY_ID="k-next", SEO_VAULT_KEY_PREVIOUS_FILE=old_file,
                   SEO_VAULT_KEY_PREVIOUS_ID="k-test")
    got = h2.ok(h2.get(f"/tenants/zbm/audits/{a['audit_id']}"))
    assert got["report"] == a["report"] and "vault_unavailable" not in got


# ================================================================================================ drift

def test_drift_recorded_keeps_counts_a_ref_and_a_keyed_hash(tmp_path, srv):
    h = harness(tmp_path, srv)
    s = two_ticks(h, srv)
    sid = s["schedule_id"]
    (rec,) = records(h, "drift_recorded")
    d = rec["data"]
    assert set(d) == {"schedule_id", "older", "newer", "drift_ref", "drift_hmac", "facts", "actor", "evidence"}
    assert sg.violations("drift_recorded", d) == []
    assert set(d["facts"]) == {"counts", "changes", "unchanged", "persisted_by_agent", "overturned_by_agent",
                               "rules_version", "search_performance"}
    (p,) = ledger_payloads(h, "drift_recorded")
    assert set(p) == {"schedule_id", "older", "newer", "drift_hmac", "counts", "rk", "seq"}
    assert p["drift_hmac"] == d["drift_hmac"] and "drift_sha256" not in json.dumps(rec)
    # the owner's view reads the detail from the vault; the keyed hash is the HMAC of its canonical JSON
    shown = h.ok(h.get(f"/tenants/zbm/schedules/{sid}"))["drifts"][-1]
    assert shown["drift_hmac"] == d["drift_hmac"] and "drift_ref" not in shown and shown["drift_sha256"] is None
    detail = h.svc.vault.get_blob("zbm", d["drift_ref"])
    assert d["drift_hmac"] == h.svc.vault.field_hash("zbm", "drift_report", svc_vaulted.canonical(detail))[1]
    assert hashlib.sha256(svc_vaulted.canonical(detail).encode()).hexdigest() not in written(h)
    assert shown["drift"]["changes"] and shown["drift"]["rules"].startswith("first matching rule")
    assert d["facts"]["changes"] == len(detail["changes"]) and d["facts"]["counts"] == detail["counts"]
    assert shown["counts"] == detail["counts"]
    # the rules text and the changes' URLs / keys are in no file, ledger payload or list view
    blob = written(h)
    assert "first matching rule" not in blob and "http://site.test/" not in blob
    (row,) = h.ok(h.get("/tenants/zbm/schedules"))
    assert "drift" not in row["drifts"][-1] and row["drifts"][-1]["counts"] == detail["counts"]
    assert h.svc.schedules[sid]["drifts"][-1]["drift"] is None
    # the same after a restart
    h2 = h.restart()
    assert h2.ok(h2.get(f"/tenants/zbm/schedules/{sid}"))["drifts"][-1] == shown
    assert h2.svc.latest_drift("zbm", sid) == shown


# ================================================================================================ where

def test_a_crashed_run_records_where_as_a_machine_code(tmp_path, srv, monkeypatch):
    from agents import roman
    h = harness(tmp_path, srv)

    def run(ctx, pages):
        return [(lambda: {}["missing"])() for _ in range(1)]          # fails inside <lambda> inside <listcomp>
    monkeypatch.setattr(roman, "run", run)
    a = h.ok(audit(h), 201)
    assert a["status"] == "interrupted" and a["interrupted_reason"] == "RUN_FAILED"
    where = a["failure"]["where"]
    assert where.startswith("test_sink_audits") is False and "<" not in where and ">" not in where
    assert sg.classify(where) == "code" and a["failure"]["agent"] == "roman" and a["failure"]["error_type"] == "KeyError"
    (rec,) = records(h, "audit_interrupted")
    assert sg.violations("audit_interrupted", rec["data"]) == []
    (p,) = ledger_payloads(h, "audit_interrupted")
    assert p["where"] == where and sg.violations("ledger:audit_interrupted", p) == []


def _raise_in(name: str):
    """An exception whose innermost frame inside src/ has the function name ``name``."""
    src = os.path.join(os.path.dirname(svc_audits.__file__), "svc_audits.py")
    code = compile(f"g = lambda: {{}}['k']\ng.__code__ = g.__code__.replace(co_name={name!r})\n", src, "exec")
    ns: dict = {}
    exec(code, ns)
    try:
        ns["g"]()
    except Exception as exc:  # noqa: BLE001
        return exc
    raise AssertionError("no exception")


@pytest.mark.parametrize("name", ["<lambda>", "<listcomp>", "<module>", "<genexpr>", "run", "_step"])
def test_where_code_is_a_code_for_every_frame_name(name):
    exc = _raise_in(name)
    assert svc_audits._where(exc).split(":")[1] == name
    code = svc_audits.where_code(exc)
    assert sg.classify(code) == "code" and "<" not in code and name.strip("<>") in code


def test_where_code_falls_back_when_the_value_is_not_a_code():
    long_name = "f" * 70
    code = svc_audits.where_code(_raise_in(long_name))
    assert sg.classify(code) == "code" and long_name not in code and code.startswith("svc_audits:")
    assert svc_audits.where_code(ValueError("never raised")) == "unknown"


# ================================================================================================ replay of old lines

def test_an_old_format_audit_line_replays_and_every_view_works(tmp_path, srv, monkeypatch):
    monkeypatch.setattr(sg, "OBSERVER", None)
    h = harness(tmp_path, srv)
    old_aid, old_report = old_format_audit(h, monkeypatch)
    new = h.ok(audit(h), 201)

    def check(hh):
        a = hh.svc.audits[old_aid]
        assert a["status"] == "completed" and a["report"] == old_report and a["report_sha256"] == sha(old_report)
        assert a["report_ref"] is None and a["report_hmac"] is None
        assert a["facts"] == svc_audits.report_facts(old_report)
        v = hh.ok(hh.get(f"/tenants/zbm/audits/{old_aid}"))
        assert v["report"] == svc_vaulted.owner_view(old_report) and v["report_sha256"] == sha(old_report)
        assert "vault_unavailable" not in v and v["report"]["audit_id"] == old_aid
        rows = hh.ok(hh.get("/tenants/zbm/audits"))
        assert {r["audit_id"] for r in rows} == {old_aid, new["audit_id"]} and all("report" not in r for r in rows)
        # drift between an old-format and a new-format audit
        d = hh.ok(hh.get(f"/tenants/zbm/audits/{new['audit_id']}/drift", params={"against": old_aid}))
        assert d["older"] == old_aid and d["newer"] == new["audit_id"]
        # the scorecards count both
        assert hh.ok(hh.get("/department"))["agents"]["roman"]["scorecard"]["runs"] == 2
        # the export serves the old line as it was recorded, and the new one without a report
        ev = {e["data"]["audit_id"]: e["data"] for e in hh.ok(hh.get("/audit/export", params={"limit": 1000}))["events"]
              if e["kind"] == "audit_completed"}
        assert ev[old_aid]["report"]["audit_id"] == old_aid and "report" not in ev[new["audit_id"]]
    check(h)
    # the client report of an old-format audit names its plain SHA-256; of a new one, its keyed hash
    r_old = h.ok(client_report(h, "zbm", old_aid))
    r_new = h.ok(client_report(h, "zbm", new["audit_id"]))
    assert r_old["audit_report_sha256"] == sha(old_report) and r_old["audit_report_hmac"] is None
    assert "report SHA-256" in r_old["content"] and sha(old_report) in r_old["content"]
    assert r_new["audit_report_hmac"] == new["report_hmac"] and r_new["audit_report_sha256"] is None
    assert "report keyed hash (HMAC-SHA256)" in r_new["content"] and new["report_hmac"] in r_new["content"]
    assert "report SHA-256" not in r_new["content"]
    h2 = h.restart()
    check(h2)
    assert h2.ok(h2.get("/status"))["integrity"]["ok"] is True
    assert len(h2.ok(h2.get(f"/tenants/zbm/audits/{old_aid}/client-reports"))) == 1


def test_an_old_format_drift_line_replays_and_the_views_work(tmp_path, srv, monkeypatch):
    monkeypatch.setattr(sg, "OBSERVER", None)
    from agents import drift as drift_mod
    h = harness(tmp_path, srv)
    s = two_ticks(h, srv)
    sid = s["schedule_id"]
    new = h.svc.schedules[sid]["drifts"][-1]
    reports = [h.svc._audit_recorded("zbm", x)["report"] for x in (new["older"], new["newer"])]
    old = json.loads(json.dumps(drift_mod.compare(*reports)))
    raw_commit(h.svc, "drift_recorded", {"schedule_id": sid, "older": new["older"], "newer": new["newer"],
                                         "drift": old, "drift_sha256": sha(old)}, actor="scheduler")

    def check(hh):
        a, b = hh.ok(hh.get(f"/tenants/zbm/schedules/{sid}"))["drifts"][-2:]
        assert b["drift"] == svc_vaulted.owner_view(old) and b["drift_sha256"] == sha(old) and b["drift_hmac"] is None
        assert a["drift"] == b["drift"] and a["drift_hmac"] and a["drift_sha256"] is None
        assert a["counts"] == b["counts"] == old["counts"]
        assert a["persisted_by_agent"] == b["persisted_by_agent"] == old["persisted_by_agent"]
        assert hh.ok(hh.get("/department"))["agents"]["osei"]["scorecard"]["drift_reports_recorded"] == 2
    check(h)
    check(h.restart())


# ================================================================================================ mutation-bite

def _scenario(tmp_path, srv) -> dict:
    """The properties the mutations below must break, each as a bool (never a value)."""
    h = harness(tmp_path, srv)
    h.tenant("globex", domains=("globex.test",))
    a = h.ok(audit(h), 201)
    aid = a["audit_id"]
    rec = h.svc.audits.get(aid) or {}
    (line,) = records(h, "audit_completed") or [None]
    out = {"owner_reads_the_report": isinstance(a.get("report"), dict) and "vault_unavailable" not in a}
    report = a.get("report") if isinstance(a.get("report"), dict) else {}
    raw = h.svc.vault.get_blob("zbm", rec["report_ref"]) if rec.get("report_ref") and vault_lines(h) else {}
    plain = hashlib.sha256(svc_vaulted.canonical(raw).encode()).hexdigest()
    out["hash_is_keyed"] = plain not in json.dumps(line) and plain not in json.dumps(h.ledger.events, default=str)
    out["other_tenant_reads_nothing"] = h.svc._vaulted_open("globex", "audit_report", rec.get("report_ref"),
                                                           rec.get("report_hmac")) is None
    out["report_not_on_disk"] = "credit is not causation" not in written(h)
    if rec.get("report_ref"):
        _lose(h, rec["report_ref"]) if vault_lines(h) else None
    r = client_report(h, "zbm", aid)
    out["client_report_refuses_a_lost_blob"] = r.status_code == 503 and r.json().get("detail") == "VAULT_UNAVAILABLE"
    b = h.ok(audit(h), 201)
    r = h.get(f"/tenants/zbm/audits/{b['audit_id']}/drift", params={"against": aid})
    out["drift_refuses_a_lost_blob"] = r.status_code == 503 and r.json().get("detail") == "VAULT_UNAVAILABLE"
    out["report_has_content"] = bool(report.get("agents"))
    return out


def test_the_scenario_holds_unmutated(tmp_path, srv):
    assert all(_scenario(tmp_path, srv).values())


def test_mutation_the_blob_write_skipped_is_caught(tmp_path, srv, monkeypatch):
    def commit_only(self, sealed, commit):
        commit()
    monkeypatch.setattr(svc_vaulted.VaultedMixin, "_vaulted_commit", commit_only)
    got = _scenario(tmp_path, srv)
    assert got["owner_reads_the_report"] is False


def test_mutation_readers_ignoring_vault_unavailable_is_caught(tmp_path, srv, monkeypatch):
    monkeypatch.setattr(svc_audits.AuditsMixin, "_audit_recorded",
                        lambda self, tid, aid: self.audit_view(tid, aid, masked=False))
    got = _scenario(tmp_path, srv)
    assert got["client_report_refuses_a_lost_blob"] is False and got["drift_refuses_a_lost_blob"] is False
    assert got["owner_reads_the_report"] is True


def test_mutation_a_plain_sha256_instead_of_the_keyed_hash_is_caught(tmp_path, srv, monkeypatch):
    monkeypatch.setattr(svc_vaulted.VaultedMixin, "_vaulted_hmac",
                        lambda self, tid, field, obj, vault=None: hashlib.sha256(
                            svc_vaulted.canonical(obj).encode()).hexdigest())
    got = _scenario(tmp_path, srv)
    assert got["hash_is_keyed"] is False
    assert got["owner_reads_the_report"] is False            # and a reader refuses a hash that is not the keyed one


def test_mutation_a_cross_tenant_blob_read_is_caught_twice(tmp_path, srv, monkeypatch):
    # layer 1 removed: the vault no longer binds a blob to its tenant
    monkeypatch.setattr(vault_mod, "_tenant_matches", lambda material, tenant_id, tag: True)
    monkeypatch.setattr(vault_mod, "_blob_manifest_aad", lambda tenant_id, blob_id, key_id: b"m" + blob_id.encode())
    monkeypatch.setattr(vault_mod, "_blob_chunk_aad",
                        lambda tenant_id, blob_id, index, count, key_id: f"c{blob_id}{index}{count}".encode())
    h = harness(tmp_path / "one", srv)
    h.tenant("globex", domains=("globex.test",))
    a = h.ok(audit(h), 201)
    rec = h.svc.audits[a["audit_id"]]
    assert h.svc.vault.get_blob("globex", rec["report_ref"])["audit_id"] == a["audit_id"]      # the mutation bites
    # layer 2 still holds: the keyed hash is bound to the tenant, so the service's reader refuses
    assert h.svc._vaulted_open("globex", "audit_report", rec["report_ref"], rec["report_hmac"]) is None
    # both removed: the read goes through, and the scenario's property fails
    monkeypatch.setattr(type(h.svc.vault), "field_hash_matches", lambda self, *a, **k: True)
    got = _scenario(tmp_path / "two", srv)
    assert got["other_tenant_reads_nothing"] is False


def test_mutation_no_owner_masking_is_caught(tmp_path, srv, monkeypatch):
    rng = random.Random(SEED + 1)
    v = planted(rng)

    def shown(mutated: bool) -> str:
        with monkeypatch.context() as m:
            if mutated:
                m.setattr(svc_audits, "owner_view", lambda obj, memo=None: obj)
            h = harness(tmp_path / ("m" if mutated else "u"), srv, data=False, own=False)
            plant_site(srv, v)
            ps = plant_tenant(h, v)
            a = h.ok(audit(h, "acme", invoice_id=invoice_id(), prompt_set_id=ps["prompt_set_id"]), 201)
            return json.dumps(a)
    assert v["entity_card"] not in shown(False) and v["title_email"] not in shown(False)
    assert v["entity_card"] in shown(True) and v["title_email"] in shown(True)


# ================================================================================================ size

def test_a_large_report_is_chunked_and_the_lock_holds_no_encryption(tmp_path, srv, monkeypatch):
    h = harness(tmp_path, srv)
    real = SeoService._run_audit

    def big(self, aid, tid, body, ps):
        report = real(self, aid, tid, body, ps)
        page = report["pages"]["/"]
        report["pages"] = {f"/p/{i}": page for i in range(4000)}
        return report
    monkeypatch.setattr(SeoService, "_run_audit", big)
    a = h.ok(audit(h), 201)
    assert a["status"] == "completed" and len(a["report"]["pages"]) == 4000
    assert vault_lines(h) >= 3                               # more than one chunk, plus the manifest
    (rec,) = records(h, "audit_completed")
    assert len(json.dumps(rec)) < 4096                       # the log line stays small whatever the report's size
    h2 = h.restart()
    assert h2.ok(h2.get(f"/tenants/zbm/audits/{a['audit_id']}"))["report"] == a["report"]


def test_the_helpers_in_this_file_find_every_form():
    v = "a planted value 1234567890"
    b = v.encode()
    for carrier in (v, b.hex(), base64.b64encode(b"x" + b + b"yz").decode(), base64.b64encode(b"xy" + b).decode(),
                    base64.urlsafe_b64encode(b).decode(), urllib.parse.quote(v), json.dumps({"k": v}),
                    "tel 1234567890"):
        assert found(carrier, {"v": v}) == ["v"]
    assert found("nothing here", {"v": v}) == []
    assert svc_schedules.DRIFT_VIEW_MAX == 10
