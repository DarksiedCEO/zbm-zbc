"""ADR 0051 job 2: the client citation vault wired into Callum's citation runs (svc_citations.py, agents/callum.py).

Every synthetic value here (credentials, card / SSN / phone numbers, e-mail addresses) is BUILT AT RUN TIME from
fragments and random draws; nothing credential-shaped is written in this file, and no assertion message prints one
(counts and names only). The service runs for real (the ASGI stack, the fake ledger, a data directory) with a
scripted answer-engine port that returns ProviderAnswers carrying excerpts, titles, hints and cited URLs."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import pickle
import random
import string
import urllib.parse

import pytest
import utility_corpus_v2 as uc
from helpers import Harness, rid, write_vault_key

import config as config_mod
import svc_citations
import vault
from agents import callum
from agents import secret_guard as sg
from ports import ProviderAnswer
from primitives.fetch import FetchResult

DOMAIN = "site.test"
SEED = 51020261109
MARK = "vaultmarker"                                   # a legitimate word: passes the strict filter, so it would survive
#                                                        the sweep if the evidence were ever logged


# ------------------------------------------------------------------------------------------------ synthetic values

def _rand(r: random.Random, alphabet: str, n: int) -> str:
    return "".join(r.choice(alphabet) for _ in range(n))


def _luhn(r: random.Random) -> str:
    body = "4" + _rand(r, string.digits, 14)
    for c in string.digits:
        if sg._luhn(body + c):
            return body + c
    raise AssertionError("no check digit")


def synthetic(seed: int = SEED) -> dict:
    """name -> value, all generated now. Each one is checked below to be flagged by the strict filter."""
    r = random.Random(seed)
    up = string.ascii_uppercase + "234567"
    card = _luhn(r)
    area = r.randint(100, 665)
    return {
        "cred_aws": "AK" + "IA" + _rand(r, up, 16),
        "cred_gh": "gh" + "p_" + _rand(r, string.ascii_letters + string.digits, 36),
        "cred_sk": "sk" + "-proj-" + _rand(r, string.ascii_letters + string.digits, 40),
        "card": card,
        "card_spaced": " ".join(card[i:i + 4] for i in range(0, 16, 4)),
        "ssn": f"{area}-{r.randint(10, 99)}-{r.randint(1000, 9999)}",
        "phone": f"(555) 01{r.randint(0, 9)}-{r.randint(1000, 9999)}",
        "phone_path": f"555-01{r.randint(0, 9)}-{r.randint(1000, 9999)}",
        "email": _rand(r, string.ascii_lowercase, 9) + "@" + _rand(r, string.ascii_lowercase, 7) + ".com",
    }


def forms(v: str) -> set:
    """The plaintext forms searched for: raw, hex (both cases), base64 and base64url (padded and not), URL-quoted,
    and the bare digits of a number with 9 or more digits."""
    b = v.encode("utf-8")
    out = {v, b.hex(), b.hex().upper(), urllib.parse.quote(v, safe=""), urllib.parse.quote_plus(v)}
    for enc in (base64.b64encode(b).decode(), base64.urlsafe_b64encode(b).decode()):
        out |= {enc, enc.rstrip("=")}
    digits = "".join(c for c in v if c.isdigit())
    if len(digits) >= 9:
        out.add(digits)
    return out


def found(blob: str, values: dict) -> list:
    """Names of the values found in ``blob`` in any form (names only, never the values)."""
    return sorted(n for n, v in values.items() if any(f in blob for f in forms(v)))


# ------------------------------------------------------------------------------------------------ the scripted engine

LEGIT_URL = f"https://site.test/guides/{MARK}-orchid-lantern"
LEGIT_COMP = f"https://competitor.test/blog/{MARK}-heron-notes"
LEGIT_TITLE = f"The {MARK} orchid lantern guide"
LEGIT_TEXT = f"Z Best Media recovers marketplace revenue; the {MARK} orchid guide explains how sellers file claims."


def answers(v: dict) -> list:
    cred_url = f"https://competitor.test/u/{v['cred_aws']}/x"
    pers_url = f"https://competitor.test/c/{v['phone_path']}/x"
    mail_url = "https://competitor.test/p?mail=" + urllib.parse.quote(v["email"])
    card_url = f"https://competitor.test/order/{v['card']}"
    a = ProviderAnswer("ANSWER", LEGIT_TEXT, [LEGIT_URL, LEGIT_COMP], "m", "1", cite_from_text=False,
                       provider="openai", asked_at="2026-10-09T18:00:00Z")
    a.citation_titles = {LEGIT_URL: LEGIT_TITLE}
    a.citation_hints = {LEGIT_COMP: "competitor.test"}
    b = ProviderAnswer("ANSWER", f"Z Best Media support: write to {v['email']} or call {v['phone']} today.",
                       [cred_url, pers_url, mail_url], "m", "1", cite_from_text=False, provider="openai",
                       asked_at="2026-10-09T18:00:00Z")
    b.citation_titles = {cred_url: "Account of " + v["email"]}
    b.citation_hints = {pers_url: "key " + v["cred_gh"], mail_url: "ssn " + v["ssn"]}
    c = ProviderAnswer("ANSWER", f"Z Best Media billing: card {v['card_spaced']}, ssn {v['ssn']}, api key "
                                 f"{v['cred_sk']} on file.", [card_url], "m", "1", cite_from_text=False,
                       provider="openai", asked_at="2026-10-09T18:00:00Z")
    return [a, b, c]


class Port:
    """An unmetered, connected answer-engine port answering from a script."""

    connected, metered, accepts_guard, name, attempts = True, False, True, "openai", 1

    def __init__(self, script):
        self.script, self.asked = list(script), 0

    def ask(self, prompt, guard=None, meter=None):
        self.asked += 1
        return self.script[(self.asked - 1) % len(self.script)]


class Fetcher:
    """Records every URL fetched; answers DNS_FAILED (nothing is classified as resolved)."""

    def __init__(self):
        self.fetched = []

    def fetch(self, url, robots_cache=None, guard=None, **kw):
        self.fetched.append(url)
        guard(capability="fetch", provider="web")
        return FetchResult(url=url, state="DNS_FAILED", final_url=url)


def harness(tmp_path, data_dir=True, **env) -> Harness:
    d = str(tmp_path / "data") if data_dir else None
    return Harness(tmp_path, data_dir=d, SEO_PROBE_SAMPLES="3", **env)


def start(h: Harness, v: dict, fetcher=None) -> Port:
    port = Port(answers(v))
    h.ports.engines["openai"] = port
    if fetcher is not None:
        h.ports.fetcher = fetcher
    h.ok(h.post("/tenants/zbm/domains", {"request_id": rid(), "domains": [DOMAIN]}, andre=True))
    return port


def citation_run(h: Harness):
    psid = h.ok(h.post("/tenants/zbm/prompt-sets", {"request_id": rid(), "name": "core",
                                                    "brand_terms": ["Z Best Media"],
                                                    "competitor_domains": ["competitor.test"],
                                                    "prompts": ["who recovers marketplace revenue"],
                                                    "engines": ["openai"]}), 201)["prompt_set_id"]
    return h.ok(h.post("/tenants/zbm/citation-runs", {"request_id": rid(), "domain": DOMAIN, "prompt_set_id": psid}),
                201)


def written(h: Harness, caplog=None) -> str:
    """Everything the service wrote: every file under the data directory (raw bytes as text, and each JSON line
    re-dumped), the ledger events with their payloads, and the captured log records."""
    blob = json.dumps(h.ledger.events, ensure_ascii=False, default=str)
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


def rows_by(v: dict) -> dict:
    return {r["evidence"]["cited_url"]["value"] if r.get("evidence") and r["evidence"].get("cited_url") else
            f"{r['class']}:{r['sample']}": r for r in v["result"]["facts"]["rows"]}


# ------------------------------------------------------------------------------------------------ the fixtures hold

def test_every_synthetic_value_is_flagged_by_the_strict_filter():
    v = synthetic()
    assert all(sg.check(x) is not None for x in v.values()), [n for n, x in v.items() if sg.check(x) is None]
    assert sg.check(LEGIT_TEXT) is None and sg.check(LEGIT_TITLE) is None
    assert callum.url_flag(LEGIT_URL) is None and callum.url_flag(LEGIT_COMP) is None


# ------------------------------------------------------------------------------------------------ the full run

def test_a_real_run_logs_ids_and_hashes_only_and_the_owner_sees_full_evidence(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    v = synthetic()
    h = harness(tmp_path)
    start(h, v)
    run = citation_run(h)
    assert run["status"] == "completed" and run["result"]["outcome"] == "OK"
    cid = run["citation_run_id"]

    # nothing raw anywhere the service wrote: not a synthetic value in any form, not even the legitimate marker
    blob = written(h, caplog)
    assert found(blob, v) == []
    assert MARK not in blob and MARK.encode().hex() not in blob
    assert os.path.exists(os.path.join(h.settings.data_dir, vault.VAULT_NAME))
    assert len(h.svc.vault) == len(run["result"]["facts"]["rows"])

    # the logged row: vault reference, keyed hashes (job 3: HMAC-SHA256 under the vault's log-field sub-key, was a
    # plain SHA-256), host only, verdict flags
    logged = h.svc.citation_runs[cid]["result"]["facts"]["rows"]
    for row in logged:
        assert "url" not in row and "engine_hint_unverified" not in row and "evidence_ref" not in row
        ev = row["evidence"]
        assert vault.parse_ref(ev["vault_id"]) is not None
        assert set(ev) == {"vault_id", "basis", "hash_key_id", "fields"} and ev["hash_key_id"] == h.svc.vault._key.key_id
        assert all(set(f) == {"hmac_sha256", "flag"} for f in ev["fields"].values())
        record = h.svc.vault.get("zbm", vault.parse_ref(ev["vault_id"]))
        for name, f in ev["fields"].items():
            assert f["hmac_sha256"] == h.svc.vault.field_hash("zbm", name, record[name])[1], name
            assert f["hmac_sha256"] != hashlib.sha256(record[name].encode()).hexdigest(), name
    assert {r["host"] for r in logged} == {"site.test", "competitor.test", None}
    assert sum(1 for r in logged if r["evidence"]["fields"].get("cited_url", {}).get("flag")) == 4

    # the owner's view: legitimate fields in full, flagged fields masked with an alert
    got = h.ok(h.get(f"/tenants/zbm/citation-runs/{cid}"))
    assert got["result_sha256"] == run["result_sha256"]
    by = rows_by(got)
    legit = by[LEGIT_URL]
    assert legit["url"] == LEGIT_URL and legit["class"] == "STRENGTH_URL"
    assert legit["evidence"]["cited_url"] == {"value": LEGIT_URL, "alert": False, "reason": None}
    assert legit["evidence"]["title"]["value"] == LEGIT_TITLE and not legit["evidence"]["title"]["alert"]
    assert legit["evidence"]["excerpt"]["value"] == LEGIT_TEXT and not legit["evidence"]["excerpt"]["alert"]
    comp = by[LEGIT_COMP]
    assert comp["url"] == LEGIT_COMP and comp["engine_hint_unverified"] == "competitor.test"
    masked = [r for r in got["result"]["facts"]["rows"] if r["evidence"]["cited_url"] is not None
              and r["evidence"]["cited_url"]["alert"]]
    assert len(masked) == 4 and all(r["url"] is None for r in masked)
    reasons = sorted(r["evidence"]["cited_url"]["reason"] for r in masked)
    assert reasons == ["LOOKS_LIKE_CREDENTIAL"] + ["LOOKS_LIKE_PERSONAL_DATA"] * 3
    for r in masked:
        # job 3: a URL keeps every part but the offending one (a path segment, a query value), shown as {hidden}
        f = r["evidence"]["cited_url"]
        assert f["check"] == "url_part" and f["value"].startswith("https://competitor.test/"), f["check"]
        assert f["value"].count(sg.CLIENT_HIDDEN) == 1
    alerts = [(r["evidence"][k]["reason"]) for r in got["result"]["facts"]["rows"]
              for k in ("title", "hint", "excerpt") if r["evidence"].get(k) and r["evidence"][k]["alert"]]
    assert "LOOKS_LIKE_CREDENTIAL" in alerts and "LOOKS_LIKE_PERSONAL_DATA" in alerts
    # the owner's view never carries a flagged raw value (run, list and the MNC export)
    mp = h.ok(h.get(f"/tenants/zbm/citation-runs/{cid}/mnc-priority"))
    listing = h.ok(h.get("/tenants/zbm/citation-runs"))
    out = json.dumps([got, mp, listing], ensure_ascii=False)
    assert found(out, v) == []
    assert mp["items"] and all(e["excerpt"]["alert"] for e in mp["items"][0]["evidence"])
    shown = [x["url"] for x in mp["items"][0]["cited_instead"]]
    assert LEGIT_COMP in shown and all(u is None or MARK in u for u in shown)


def test_the_cross_tenant_read_is_the_same_404_as_a_missing_run(tmp_path):
    v = synthetic()
    h = harness(tmp_path)
    start(h, v)
    cid = citation_run(h)["citation_run_id"]
    h.tenant("acme", domains=("other.test",))
    missing = h.get(f"/tenants/acme/citation-runs/{'seo-cit-' + '0' * 40}")
    other = h.get(f"/tenants/acme/citation-runs/{cid}")
    assert other.status_code == missing.status_code == 404 and other.json() == missing.json()
    assert h.get(f"/tenants/acme/citation-runs/{cid}/mnc-priority").status_code == 404
    assert h.get(f"/tenants/zbm/citation-runs/{cid}", caller="hub", tenant="acme").status_code == 404
    # and the vault itself answers another tenant "not found" for every record of the run
    for row in h.svc.citation_runs[cid]["result"]["facts"]["rows"]:
        with pytest.raises(vault.VaultNotFound):
            h.svc.vault.get("acme", vault.parse_ref(row["evidence"]["vault_id"]))


def test_a_restart_with_the_key_reads_the_same_view(tmp_path):
    v = synthetic()
    h = harness(tmp_path)
    start(h, v)
    a = citation_run(h)
    h2 = h.restart()
    b = h2.ok(h2.get(f"/tenants/zbm/citation-runs/{a['citation_run_id']}"))
    assert b["result"] == a["result"]


def test_a_restart_without_the_key_shows_no_evidence_and_never_fails(tmp_path):
    v = synthetic()
    h = harness(tmp_path)
    start(h, v)
    a = citation_run(h)
    h2 = h.restart(SEO_VAULT_KEY_FILE=None)
    b = h2.ok(h2.get(f"/tenants/zbm/citation-runs/{a['citation_run_id']}"))
    evs = [r["evidence"] for r in b["result"]["facts"]["rows"]]
    assert evs and all(e["available"] is False and e["reason"] == "VAULT_UNAVAILABLE" for e in evs)
    assert all(r["url"] is None for r in b["result"]["facts"]["rows"])


# ------------------------------------------------------------------------------------------------ fail closed

def test_a_data_directory_without_a_key_fails_the_run_closed_and_writes_nothing(tmp_path, caplog):
    v = synthetic()
    h = harness(tmp_path, SEO_VAULT_KEY_FILE=None)
    assert h.settings.vault_key is None and not h.svc.vault.key_configured
    port = start(h, v)
    run = citation_run(h)
    assert run["status"] == "interrupted" and run["interrupted_reason"] == "RUN_FAILED"
    assert run["failure"]["code"] == "VAULT_UNAVAILABLE" and run["result"] is None
    assert port.asked == 0                                           # no engine asked, nothing spent
    assert not os.path.exists(os.path.join(h.settings.data_dir, vault.VAULT_NAME))
    assert h.ledger.of_type("citation_run_recorded") == []
    assert h.ledger.of_type("citation_run_interrupted")[0]["_payload"]["code"] == "VAULT_UNAVAILABLE"
    blob = written(h, caplog)
    assert found(blob, v) == [] and MARK not in blob


def _boom(*a, **k):
    raise RuntimeError("forced")


@pytest.mark.parametrize("where", ["vault_put_first", "vault_put_later", "vault_unavailable", "sweep",
                                   "sweep_withheld", "commit"])
def test_any_failure_while_storing_closes_the_run_failed_with_nothing_raw_written(tmp_path, monkeypatch, caplog,
                                                                                    where):
    caplog.set_level(logging.DEBUG)
    v = synthetic()
    h = harness(tmp_path)
    start(h, v)
    real_put = h.svc.vault.put
    calls = []

    def put_later(tid, record):
        calls.append(1)
        if len(calls) > 2:
            raise RuntimeError("forced")
        return real_put(tid, record)

    def unavailable(tid, record):
        raise vault.VaultUnavailable("forced")
    real_commit = h.svc._commit

    def commit(kind, *a, **k):
        if kind == "citation_run_recorded":
            raise RuntimeError("forced")
        return real_commit(kind, *a, **k)
    patch = {"vault_put_first": (h.svc.vault, "put", _boom), "vault_put_later": (h.svc.vault, "put", put_later),
             "vault_unavailable": (h.svc.vault, "put", unavailable),
             "sweep": (callum.secret_guard, "guard_record", _boom),
             "sweep_withheld": (callum.secret_guard, "guard_record", lambda obj: sg.WITHHELD),
             "commit": (h.svc, "_commit", commit)}[where]
    monkeypatch.setattr(*patch)
    run = citation_run(h)
    assert run["status"] == "interrupted" and run["interrupted_reason"] == "RUN_FAILED", where
    assert run["citation_run_id"] not in h.svc.running_citation_runs
    assert h.ledger.of_type("citation_run_recorded") == []
    if where == "vault_unavailable":
        assert run["failure"]["code"] == "VAULT_UNAVAILABLE"
    blob = written(h, caplog)
    assert found(blob, v) == [] and MARK not in blob, where


# ------------------------------------------------------------------------------------------------ fetch policy

def test_credential_urls_are_never_fetched_and_personal_data_urls_are(tmp_path):
    v = synthetic()
    h = harness(tmp_path, SEO_CALLUM_RESOLVE_URLS="1")
    fetcher = Fetcher()
    start(h, v, fetcher)
    citation_run(h)
    fetched = " ".join(fetcher.fetched)
    for name in ("cred_aws", "cred_gh", "cred_sk"):
        assert v[name] not in fetched, name
    assert any(v["phone_path"] in u for u in fetcher.fetched)                       # personal data only: fetched
    assert any(v["card"] in u for u in fetcher.fetched)
    assert any(urllib.parse.quote(v["email"]) in u for u in fetcher.fetched)
    assert LEGIT_URL in fetcher.fetched and LEGIT_COMP in fetcher.fetched
    assert found(written(h), v) == []


# ------------------------------------------------------------------------------------------------ exports

def test_the_client_report_has_no_citation_run_path():
    """The client report (client_report.py, svc_reports.py) renders audit envelopes only: no citation-run row, so no
    vault evidence, reaches it. If that changes, it must go through ``callum.client_view`` (the masked view)."""
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "src"
    for name in ("client_report.py", "svc_reports.py"):
        text = (src / name).read_text(encoding="utf-8")
        assert "citation_run" not in text and "citation_vault" not in text and "evidence_ref" not in text, name


# ------------------------------------------------------------------------------------------------ mutation bite

def _scenario(tmp_path) -> dict:
    v = synthetic()
    h = harness(tmp_path)
    start(h, v)
    cid = citation_run(h)["citation_run_id"]
    h.tenant("acme", domains=("other.test",))
    got = h.get(f"/tenants/zbm/citation-runs/{cid}").json()
    return {"log_clean": MARK not in written(h) and found(written(h), v) == [],
            "owner_masked": found(json.dumps(got, ensure_ascii=False), v) == [],
            "owner_full": LEGIT_URL in json.dumps(got),
            "cross_404": h.get(f"/tenants/acme/citation-runs/{cid}").status_code == 404}


def test_the_scenario_holds_unmutated(tmp_path):
    assert _scenario(tmp_path) == {"log_clean": True, "owner_masked": True, "owner_full": True, "cross_404": True}


def test_mutation_no_vault_split_is_caught(tmp_path, monkeypatch):
    real = callum.split_evidence

    def evidence_in_the_envelope(env, sink, put, **kw):
        out = real(env, sink, put, **kw)
        return {**out, "facts": {**out["facts"], "evidence": [e["record"] for e in sink]}}
    monkeypatch.setattr(callum, "split_evidence", evidence_in_the_envelope)
    assert _scenario(tmp_path)["log_clean"] is False


def test_mutation_no_display_mask_is_caught(tmp_path, monkeypatch):
    def unmasked(name, value, flag=None, recorded=None):
        return None if value is None else {"value": value, "alert": False, "reason": None}
    monkeypatch.setattr(callum, "_display", unmasked)
    assert _scenario(tmp_path)["owner_masked"] is False


def test_mutation_no_tenant_check_is_caught(tmp_path, monkeypatch):
    monkeypatch.setattr(svc_citations.CitationsMixin, "_owns", lambda self, r, tid: r is not None)
    assert _scenario(tmp_path)["cross_404"] is False


# ------------------------------------------------------------------------------------------------ utility (client)

# ADR 0051 job 3: the client view masks on HIGH-PRECISION findings only (secret_guard.client_view_findings /
# client_view_url), never on the strict flag. Bars: excerpts <= 5%, URLs <= 2.5% (job 2 measured 22 / 312 = 7.05% and
# 73 / 306 = 23.9% with the strict filter as the display rule; job 3 measures 0 / 312 and 0 / 306)
EXCERPT_BAR = 0.05
URL_BAR = 0.025


def test_utility_client_view_excerpts_meet_the_bar():
    ex = uc.excerpts()
    assert len(ex) == 312
    masked = 0
    for i, b, e in ex:
        raw = callum._raw_excerpt(uc.ANSWERS[i], [b] if b else [])
        d = callum._display("excerpt", raw, e["withheld"],
                            callum._excerpt_view_findings(uc.ANSWERS[i], [b] if b else []))       # span + margins
        masked += bool(d and d["alert"])
    assert masked / len(ex) <= EXCERPT_BAR, masked
    # the strict lane is unchanged: it still withholds the same 22 excerpts from the log (recorded flag)
    assert sum(1 for _i, _b, e in ex if e["withheld"]) == 22


def test_utility_client_view_urls_meet_the_bar():
    urls = [u for _, u in uc.urls()]
    shown = [callum._display("cited_url", u, callum.url_flag(u)) for u in urls]
    masked = sum(1 for d in shown if d["alert"])
    assert len(urls) == 306 and masked / len(urls) <= URL_BAR, masked
    assert all(d["value"] == u for d, u in zip(shown, urls) if not d["alert"])          # unmasked: in full
    assert sum(1 for u in urls if callum.url_flag(u)) == 73                             # the strict flag, unchanged


# ------------------------------------------------------------------------------------------------ in-memory backend

def _key(kid: str = "k-test") -> vault.VaultKey:
    raw = os.urandom(32)
    while len(set(raw)) < 16:
        raw = os.urandom(32)
    return vault.parse_vault_key(base64.urlsafe_b64encode(raw).decode(), kid)


def test_the_in_memory_vault_round_trips_and_isolates_tenants():
    m = vault.MemoryCitationVault(_key())
    vid = m.put("zbm", {"excerpt": f"{MARK} text"})
    assert m.get("zbm", vid) == {"excerpt": f"{MARK} text"} and len(m) == 1
    for bad in (lambda: m.get("acme", vid), lambda: m.get("zbm", "0" * 32), lambda: m.get("zbm", "x")):
        with pytest.raises(vault.VaultNotFound):
            bad()
    lines = m.lines()
    assert len(lines) == 1 and MARK.encode() not in lines[0] and vault._parse_line(lines[0]) is not None
    assert "redacted" not in repr(m) and "records=1" in repr(m)
    with pytest.raises(TypeError):
        pickle.dumps(m)


def test_log_field_hashes_are_keyed_and_bound_to_tenant_and_field():
    """Job 3: a logged field hash is HMAC-SHA256 under an HKDF sub-key of the vault key (its own info label), bound to
    the tenant and the field name: a short guessable value cannot be confirmed by hashing guesses."""
    k = _key()
    m = vault.MemoryCitationVault(k)
    r = random.Random(SEED + 3)
    guess = _rand(r, string.ascii_lowercase, 6) + "@" + _rand(r, string.ascii_lowercase, 5) + ".com"
    kid, mac = m.field_hash("zbm", "hint", guess)
    assert kid == "k-test" and len(mac) == 64 and mac != hashlib.sha256(guess.encode()).hexdigest()
    assert m.field_hash("zbm", "hint", guess)[1] == mac                                   # deterministic per key
    assert m.field_hash("acme", "hint", guess)[1] != mac                                  # tenant-bound
    assert m.field_hash("zbm", "title", guess)[1] != mac                                  # field-bound
    assert vault.MemoryCitationVault(_key()).field_hash("zbm", "hint", guess)[1] != mac   # key-bound
    assert vault.FIELD_HASH_INFO not in (b"seo-citation-vault/v1/aes-256-gcm", b"seo-citation-vault/v1/tenant-tag")
    for bad in (("Bad Tenant", "hint", guess), ("zbm", "Hint!", guess), ("zbm", "hint", 7)):
        with pytest.raises(vault.VaultRecordInvalid):
            m.field_hash(*bad)
    with pytest.raises(vault.VaultUnavailable):
        vault.MemoryCitationVault(None).field_hash("zbm", "hint", guess)


def test_a_run_without_a_keyed_hasher_fails_instead_of_logging_plain_hashes():
    with pytest.raises(ValueError):
        callum.split_evidence({"facts": {"rows": [{"evidence_ref": 0}]}},
                              [{"record": {"excerpt": MARK}, "flags": {"excerpt": None}}], lambda rec: "0" * 32)


def test_the_in_memory_vault_without_a_key_stores_nothing():
    m = vault.MemoryCitationVault(None)
    with pytest.raises(vault.VaultUnavailable):
        m.put("zbm", {"excerpt": "x"})
    with pytest.raises(vault.VaultUnavailable):
        m.get("zbm", "0" * 32)
    assert m.lines() == [] and not m.key_configured


def test_the_in_memory_lines_are_the_file_vaults_lines(tmp_path):
    k = _key()
    m = vault.MemoryCitationVault(k)
    ids = [m.put("zbm", {"n": i, "t": MARK}) for i in range(3)]
    d = tmp_path / "v"
    d.mkdir(mode=0o700)
    p = d / vault.VAULT_NAME
    p.write_bytes(b"".join(x + b"\n" for x in m.lines()))
    os.chmod(p, 0o600)
    f = vault.CitationVault(str(d), k)
    assert [f.get("zbm", i)["n"] for i in ids] == [0, 1, 2]


def test_refs_round_trip_and_a_tampered_ref_names_nothing():
    vid = os.urandom(16).hex()
    r = vault.ref(vid)
    assert r.startswith("seo-vlt-") and len(r) == 48 and vault.parse_ref(r) == vid
    assert sg.guard_record({"vault_id": r}) == {"vault_id": r}                 # the strict sweep keeps a ref
    flip = r[:-1] + ("0" if r[-1] != "0" else "1")
    for bad in (flip, r[:-1], vid, None, 7, r.upper()):
        assert vault.parse_ref(bad) is None
    with pytest.raises(vault.VaultRecordInvalid):
        vault.ref("not-an-id")


def test_the_ephemeral_key_is_redacted_and_never_pickled():
    k = vault.ephemeral_vault_key()
    assert k.key_id == vault.EPHEMERAL_KEY_ID and "redacted" in repr(k)
    with pytest.raises(TypeError):
        pickle.dumps(k)


def test_the_in_memory_service_uses_the_in_memory_vault(tmp_path):
    v = synthetic()
    h = harness(tmp_path, data_dir=False)
    assert type(h.svc.vault) is vault.MemoryCitationVault and h.svc.vault.key_configured
    start(h, v)
    run = citation_run(h)
    assert run["status"] == "completed" and rows_by(run)[LEGIT_URL]["url"] == LEGIT_URL
    assert all(MARK.encode() not in line for line in h.svc.vault.lines())
    assert found(json.dumps(run, ensure_ascii=False), v) == []


# ------------------------------------------------------------------------------------------------ configuration

def _env(tmp_path, **over):
    from helpers import base_env
    return base_env(SEO_DATA_DIR=str(tmp_path / "data"), SEO_LOG_HASH_KEY_FILE=_logkey(tmp_path), **over)


def _logkey(tmp_path):
    from helpers import write_key
    return write_key(tmp_path / "log.key")


def test_the_key_file_is_read_under_the_file_rules(tmp_path):
    path = write_vault_key(tmp_path / "vault.key")
    s = config_mod.load(_env(tmp_path, SEO_VAULT_KEY_FILE=path, SEO_VAULT_KEY_ID="k-test"))
    assert type(s.vault_key) is vault.VaultKey and s.vault_key.key_id == "k-test"
    secret = (tmp_path / "vault.key").read_text()
    assert secret not in repr(s) and "redacted" in repr(s.vault_key)


@pytest.mark.parametrize("case", ["both_set", "open_mode", "malformed", "no_id"])
def test_bad_key_settings_refuse_start_without_quoting_the_value(tmp_path, case):
    path = write_vault_key(tmp_path / "vault.key")
    value = (tmp_path / "vault.key").read_text()
    over = {"SEO_VAULT_KEY_FILE": path, "SEO_VAULT_KEY_ID": "k-test"}
    if case == "both_set":
        over["SEO_VAULT_KEY"] = value
    elif case == "open_mode":
        os.chmod(path, 0o644)
    elif case == "malformed":
        value = base64.urlsafe_b64encode(os.urandom(16)).decode()
        (tmp_path / "vault.key").write_text(value)
    elif case == "no_id":
        over.pop("SEO_VAULT_KEY_ID")
    with pytest.raises(RuntimeError) as e:
        config_mod.load(_env(tmp_path, **over))
    assert value not in str(e.value) and value.rstrip("=") not in str(e.value)
    assert "SEO_VAULT_KEY" in str(e.value)
