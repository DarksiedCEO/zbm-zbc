"""Idris war-room fixes (ADR 0048 "War room fixes"; devtools/warroom findings WR-F030..WR-F032), fixed structurally:

  WR-F030  the client's own brand in lookalike letters (IDN / punycode, homoglyphs, mixed script, digit / letter swaps,
           hyphens, another TLD) is CLIENT_LOOKALIKE — never a candidate, WATCH, CONFIRMED or in a share; confirm
           refuses it (409 CANDIDATE_IS_CLIENT_LOOKALIKE)
  WR-F031  keys, tokens and personal data are redacted before any excerpt is stored or emitted
  WR-F032  a visibility request id in flight answers 409 and writes nothing; only the original's result is stored

Property tests are seeded (``random.Random``), so every case is reproducible. No network, no key, no elapsed-time
assertion; the concurrency test interleaves real threads with events."""

# ruff: noqa: RUF001 - the homoglyphs in this file are the test data
from __future__ import annotations

import codecs
import hashlib
import json
import random
import threading

import pytest
import test_idris as ti
from helpers import rid

from agents import idris, text_redact

BRANDS = [("Z Best Media", "site.test"), ("Refund Rangers", "refundrangers.test"), ("Acme Recovery", "acme.test"),
          ("Quillbrook Labs", "quillbrook.test"), ("Seller Snap", "sellersnap.test")]
HOMOGLYPHS = {"a": "аα", "e": "еε", "o": "оο", "i": "іι", "s": "ѕ", "c": "с", "p": "рρ", "x": "х", "y": "у",
              "b": "Ь", "t": "τт", "m": "м", "z": "ζ", "d": "ԁ", "u": "υ", "k": "к", "h": "һ", "n": "п", "g": "ɡ",
              "l": "ӏ", "r": "г", "w": "ԝ"}
SWAPS = {"o": "0", "l": "1", "i": "1", "e": "3", "s": "5", "a": "4", "t": "7", "b": "8"}
TLDS = ["com", "net", "org", "io", "co", "ai", "app", "biz", "info", "co.uk", "github.io", "myshopify.com"]


def _label(brand: str) -> str:
    return "".join(ch for ch in brand.lower() if ch.isalnum())


def _punycode(host: str) -> str:
    out = []
    for lab in host.split("."):
        out.append(lab if lab.isascii() else "xn--" + codecs.encode(lab, "punycode").decode("ascii"))
    return ".".join(out)


def lookalike_label(rnd: random.Random, brand: str) -> tuple:
    """A generated lookalike of ``brand``'s label and the transforms that made it (seeded)."""
    lab = _label(brand)
    # any number of VISUAL transforms (they keep the skeleton), at most ONE edit (the documented one-edit rule)
    names = rnd.sample(["homoglyph", "homoglyph_many", "swap", "hyphen", "rn", "suffix", "case"], rnd.randint(1, 3))
    if rnd.random() < 0.3:
        names.append(rnd.choice(["double", "drop"]))
    for t in names:
        if t in ("homoglyph", "homoglyph_many"):
            idx = [i for i, ch in enumerate(lab) if ch in HOMOGLYPHS]
            for i in rnd.sample(idx, 1 if t == "homoglyph" else max(1, len(idx) // 2)):
                lab = lab[:i] + rnd.choice(HOMOGLYPHS[lab[i]]) + lab[i + 1:]
        elif t == "swap":
            idx = [i for i, ch in enumerate(lab) if ch in SWAPS]
            if idx:
                i = rnd.choice(idx)
                lab = lab[:i] + SWAPS[lab[i]] + lab[i + 1:]
        elif t == "hyphen":
            i = rnd.randint(1, len(lab) - 1)
            lab = lab[:i] + "-" + lab[i:]
        elif t == "rn" and "m" in lab:
            lab = lab.replace("m", "rn", 1)
        elif t == "suffix":
            lab += rnd.choice(["-llc", "inc", "-group", "labs", "-co"])
        elif t == "double":
            i = rnd.randrange(len(lab))
            lab = lab[:i] + lab[i] + lab[i:]
        elif t == "drop" and len(lab) >= 8:
            i = rnd.randrange(1, len(lab) - 1)
            lab = lab[:i] + lab[i + 1:]
        elif t == "case":
            lab = "".join(ch.upper() if rnd.random() < 0.5 else ch for ch in lab)
    return lab, names


def generated(n=240, seed=48030):
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        brand, domain = BRANDS[i % len(BRANDS)]
        lab, how = lookalike_label(rnd, brand)
        host = f"{lab}.{rnd.choice(TLDS)}".lower()
        out.append((brand, domain, host, _punycode(host) if rnd.random() < 0.6 else host, how))
    return out


@pytest.mark.parametrize("brand,domain,host,cited,how", generated())
def test_property_a_generated_lookalike_of_the_clients_brand_is_the_clients_lookalike(brand, domain, host, cited, how):
    label = idris.client_domain_label(domain)
    assert idris.client_like_host(cited, [brand], label), (cited, host, how)
    assert idris.client_like(idris.unidn(cited.split(".")[0]), [brand], label), (cited, how)


def _ps(brand):
    return {**ti.PS, "brand_terms": [brand]}


def _cs_without(brand):
    return {**ti.CS, "competitors": [c for c in ti.CS["competitors"]
                                     if not idris.client_like(c["name"], [brand], None)]}


@pytest.mark.parametrize("brand,domain,host,cited,how", generated(n=60, seed=48031))
def test_property_a_lookalike_is_never_a_competitor_end_to_end(brand, domain, host, cited, how, monkeypatch):
    """Answers name a fresh brand AND cite the lookalike across two engines and three runs; the lookalike is listed
    apart as CLIENT_LOOKALIKE, never EMERGING / WATCH / CONFIRMED, and a confirmation of its key (an old record) puts
    nothing into a share."""
    ps, cs = _ps(brand), _cs_without(brand)
    text = f"Options:\n1. **{host.split('.')[0]}** - a reimbursement agency.\n2. {brand} also helps."
    cites = [f"https://{cited}/claims"]
    e = ti.engines(openai=[(0, ti.ans(text, cites))] * 3, anthropic=[(0, ti.ans(text, cites, provider="anthropic"))] * 3)
    obs = [idris.observe(e, ti.tc.off(), cs, ps, domain, f"seo-cit-{i:040d}", f"2026-10-0{i + 1}T18:00:00Z",
                         "2026-10-09T19:00:00Z") for i in range(3)]
    # an observation recorded BEFORE the fix (lookalike hosts not decoded) is still in the history
    monkeypatch.setattr(idris, "client_like_host", lambda *a, **k: None)
    old = idris.observe(e, ti.tc.off(), cs, ps, domain, "seo-cit-" + "9" * 40, "2026-10-01T17:00:00Z",
                        "2026-10-09T19:00:00Z")
    monkeypatch.undo()
    keys = {c["key"] for o in obs for c in o["candidates"]}
    old_keys = {c["key"] for c in old["candidates"]} - keys
    conf = {k: {"by": "dashboard", "at": "2026-10-01T18:00:00Z", "name": k} for k in old_keys}
    env = idris.compute(obs[-1], [*obs[:-1], old], {}, None, {"outcome": "OK"}, "seo-vis-" + "d" * 40,
                        "2026-10-09T18:00:00Z", "fresh", conf)
    em = env["facts"]["emerging"]
    for c in em["items"] + em["watch"] + em["confirmed"]:
        assert not idris.client_lookalike_candidate(c["key"], c["names"], c["domains"], [brand],
                                                    idris.client_domain_label(domain)), (c, how)
        assert cited not in c["domains"]
    shown = {b["brand_id"] for b in env["facts"]["brands"]}
    assert not any(b.startswith("cnf:") and b[4:] in old_keys for b in shown), (shown, old_keys)
    for s in env["facts"]["benchmark"]["by_engine_prompt"]:
        assert not set(s["brands"]) & {f"cnf:{k}" for k in old_keys}
    if old_keys:
        assert {x["key"] for x in em["client_lookalikes"]} >= old_keys
        assert all(x["state"] == "CLIENT_LOOKALIKE" and x["signal"] == "impersonation" for x in em["client_lookalikes"])


@pytest.mark.parametrize("host", ["xn--zbstmedia-h0h.com", "zb3stmedia.com", "z-best-media.net", "zbestmedia.co.uk",
                                  "zbestrnedia.io", "ｚｂｅｓｔｍｅｄｉａ.com", "zbestmedia.github.io",
                                  "xn--zestmediaroup-xrf080e.com", "2bestmedia.co", "zbe5tmed1a.org"])
def test_the_war_room_spellings_are_the_clients_lookalike(host):
    assert idris.client_like_host(host, ["Z Best Media"], "site") is not None


@pytest.mark.parametrize("host", ["refundrangers.com", "acme.test", "getida.com", "sellersnap.io", "quillbrook.co",
                                  "xn--mnchen-3ya.de", "zoom.us", "mediabest.com"])
def test_other_brands_are_not_the_clients_lookalike(host):
    assert idris.client_like_host(host, ["Z Best Media"], "site") is None


def test_the_skeleton_uses_the_pinned_uts39_file_and_decodes_idn():
    assert idris.unidn("xn--zbstmedia-h0h.com") == "zbеstmedia.com"
    assert idris.unidn("xn--zzzzzzzzzzzzzzzzzzzz.com") == "xn--zzzzzzzzzzzzzzzzzzzz.com"   # invalid: read as written
    assert idris.brand_skeleton("ZBESTMEDIA") == idris.brand_skeleton("zbеstmed1a") \
        == idris.brand_skeleton("Z-Best rnedia") == idris.brand_skeleton("xn--zbstmedia-h0h")
    assert idris._skeleton_map()["0"] == "O" and idris._skeleton_map()["m"] == "rn"


def test_a_confirmed_lookalike_from_an_old_observation_is_refused_and_never_in_a_share(tmp_path, monkeypatch):
    """WR-F030 end to end: a lookalike recorded as a candidate (before the fix) cannot be confirmed, and the next
    run lists it as an impersonation signal, never a competitor."""
    lookalike = ti.ans("Options:\n1. **Refund Rangers** - a claims agency.\n2. Z Best Media helps.",
                       ["https://site.test/x", "https://xn--zbstmedia-h0h.com/", "https://www.refundrangers.com/"])
    h, _fakes, psid = ti.svc(tmp_path, openai=[lookalike], anthropic=[lookalike])
    h.ok(ti.comp_set(h), 201)
    monkeypatch.setattr(idris, "client_like_host", lambda *a, **k: None)          # the old, undecoded reading
    h.ok(ti.vis(h, psid), 201)
    monkeypatch.undo()
    key = next(c["key"] for o in h.svc.competitor_observations.values() for c in o["observations"]["candidates"]
               if "xn--zbstmedia-h0h.com" in c["domain_hints"])
    for caller, tenant in (("dashboard", None), ("hub", "zbm"), ("seo_agent", None)):
        h.refused(h.post("/tenants/zbm/competitor-set/confirmations", {"request_id": rid(), "key": key},
                         caller=caller, tenant=tenant), 409, "CANDIDATE_IS_CLIENT_LOOKALIKE")
    assert h.ok(h.get("/tenants/zbm/competitor-set"))["confirmations"] == []
    h.ok(h.post("/tenants/zbm/competitor-set/confirmations", {"request_id": rid(), "key": "refundrangers"},
                caller="dashboard"), 201)                                         # a real brand still can be
    # even a confirmation already on the ledger (recorded before the fix) never reaches a share
    h.svc.emerging_confirmations["zbm"][key] = {"by": "dashboard", "at": "2026-10-09T18:00:00Z", "name": key}
    f = h.ok(ti.vis(h, psid, max_age_minutes=0), 201)["result"]["facts"]
    em = f["emerging"]
    assert key not in {c["key"] for c in em["items"] + em["watch"] + em["confirmed"]}
    assert {c["key"] for c in em["confirmed"]} == {"refundrangers"}
    assert f"cnf:{key}" not in {b["brand_id"] for b in f["brands"]}
    assert key in {x["key"] for x in em["client_lookalikes"]}
    obs = [o["observations"] for o in h.svc.competitor_observations.values()]
    assert not any("xn--zbstmedia-h0h.com" in c["domain_hints"] for c in obs[-1]["candidates"])


def test_a_tracked_competitor_on_the_clients_label_is_refused_or_excluded(tmp_path):
    h, _fakes, _psid = ti.svc(tmp_path)
    for d in ("zbestmedia.com", "xn--zbstmedia-h0h.com", "z-best-media.net"):
        h.refused(ti.comp_set(h, competitors=[{"name": "Brightline", "domains": [d]}]), 422, "COMPETITOR_IS_CLIENT")
    bl, excluded = idris.brands({"competitors": [{"competitor_id": "cmp-x", "name": "Brightline",
                                                  "terms": ["Brightline"], "domains": ["zbestmedia.net"]}]},
                                ti.PS, ti.DOMAIN)
    assert [b["brand_id"] for b in bl] == ["client"] and excluded[0]["excluded"] == "CLIENT_LOOKALIKE"


# ---------------------------------------------------------------------------------------------- WR-F031 redaction

def _tok(label: str, n: int, alphabet: str = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789") -> str:
    """A key-shaped test value DERIVED at run time (no secret-shaped literal in the source)."""
    out, i = "", 0
    while len(out) < n:
        for byte in hashlib.sha256(f"seo-py test-only {label} {i}".encode()).digest():
            out += alphabet[byte % len(alphabet)]
        i += 1
    return out[:n]


UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
KEYS = {"aws_id": "AKIA" + _tok("aws id", 16, UPPER),
        "aws_secret": _tok("aws secret", 19) + "/" + _tok("aws secret b", 19) + "+",
        "openai": "sk-" + _tok("openai", 32),
        "openai_proj": "sk-proj-" + _tok("openai proj", 40),
        "anthropic": "sk-ant-api03-" + _tok("anthropic", 40),
        "github": "ghp_" + _tok("github", 36),
        "github_oauth": "gho_" + _tok("github oauth", 36),
        "github_pat": "github_pat_" + _tok("github pat", 40),
        "stripe_live": "sk_live_" + _tok("stripe live", 24),
        "stripe_test": "sk_test_" + _tok("stripe test", 24),
        "stripe_restricted": "rk_live_" + _tok("stripe rk", 24),
        "slack": "xoxb-" + _tok("slack", 12, "0123456789") + "-" + _tok("slack b", 24),
        "google": "AIza" + _tok("google", 35),
        "perplexity": "pplx-" + _tok("perplexity", 48),
        "jwt": "eyJ" + _tok("jwt h", 17) + ".eyJ" + _tok("jwt p", 30) + "." + _tok("jwt s", 43),
        "hex_token": _tok("hex", 64, "0123456789abcdef"),
        "test_key": "test-only-" + _tok("test key", 64, "0123456789abcdef")}
LABELLED = {"key=": "key={v}", "token=": "token={v}", "secret=": "secret={v}", "api_key:": "api_key: {v}",
            "password=": "password={v}", "Authorization": "Authorization: Bearer {v}", "Bearer": "bearer {v}",
            "client_secret": '"client_secret": "{v}"'}
POSITIONS = {
    "start": "{s} " + ti.ANSWER.text,
    "before_name": "Options:\n1. {s} **Refund Rangers** - a claims agency.",
    "after_name": "Options:\n1. **Refund Rangers** {s} - a claims agency.",
    "in_bold": "Options:\n1. **Refund Rangers {s}** - a claims agency.",
    "parenthesis": "Options:\n1. **Refund Rangers** (key: {s}) - a claims agency.",
    "end": "Options:\n1. **Refund Rangers** - a claims agency. Z Best Media helps.\n{s}",
    "window_edge": "Options:\n1. **Refund Rangers** - " + "a claims agency " * 6 + "{s} more.",
}


def _all_text(obs, env) -> str:
    return json.dumps(obs, ensure_ascii=False) + json.dumps(env, ensure_ascii=False)


@pytest.mark.parametrize("kind", sorted(KEYS))
@pytest.mark.parametrize("where", sorted(POSITIONS))
def test_property_no_key_or_token_survives_in_any_idris_output(kind, where):
    secret = KEYS[kind]
    text = POSITIONS[where].format(s=secret)
    cites = ["https://www.refundrangers.com/"]
    e = ti.engines(openai=[(0, ti.ans(text, cites))] * 3, anthropic=[(0, ti.ans(text, cites, provider="anthropic"))])
    obs = ti.observe(e)
    env = ti.compute(obs)
    out = _all_text(obs, env)
    assert secret not in out and secret[-16:] not in out, (kind, where)
    exs = [ev["excerpt"]["text"] for c in obs["candidates"] for ev in c["evidence"] if ev.get("excerpt")]
    assert all(secret[:12] not in x for x in exs)


@pytest.mark.parametrize("label", sorted(LABELLED))
def test_labelled_values_are_always_redacted(label):
    v = "s3cr3t-" + "Value-0xA9"
    t = text_redact.secrets("Refund Rangers " + LABELLED[label].format(v=v) + " helps.")
    assert v not in t and text_redact.SECRET in t and "Refund Rangers" in t


def test_property_random_positions_and_neighbours():
    rnd = random.Random(48031)
    for _ in range(400):
        secret = KEYS[rnd.choice(sorted(KEYS))]
        left = rnd.choice(["", " ", "(", "\n", "**", ": ", "=", "'", '"', "<", "Authorization: Bearer "])
        right = rnd.choice(["", " ", ")", "\n", "**", ".", ",", ";", "'", '"', ">"])
        words = ["Refund", "Rangers", "is", "an", "agency", "Z", "Best", "Media", "helps", "sellers"]
        i = rnd.randrange(len(words) + 1)
        # a token is delimited from the words around it (a space or punctuation), as written text has it
        text = " ".join(words[:i]) + " " + left + secret + right + " " + " ".join(words[i:])
        assert secret not in text_redact.secrets(text), text
        assert secret not in text_redact.redact(text)
        assert text_redact.secrets(text_redact.secrets(text)) == text_redact.secrets(text)   # idempotent


@pytest.mark.parametrize("text,gone,placeholder", [
    ("Write to jane.doe@example.org now", "jane.doe@example.org", text_redact.EMAIL),
    ("Card 4111 1111 1111 1111 on file", "4111 1111 1111 1111", text_redact.CARD),
    ("Card 4012-8888-8888-1881 on file", "4012-8888-8888-1881", text_redact.CARD),
    ("SSN 123-45-6789 was shown", "123-45-6789", text_redact.SSN),
    ("Call (555) 123-4567 today", "(555) 123-4567", text_redact.PHONE),
    ("Call +1 555.123.4567 today", "555.123.4567", text_redact.PHONE),
])
def test_personal_data_is_redacted(text, gone, placeholder):
    t = text_redact.redact(text)
    assert gone not in t and placeholder in t


# review of 0110a226 M1: a credential label followed by any 6+ character value is redacted with no "looks like a
# token" gate, so "Key: Refund Rangers" / "Token economy" lose the word after the label (test_idris_redaction.py)
@pytest.mark.parametrize("text", ["Refund Rangers leads the list.", "Agencies such as Getida.",
                                  "Pricing is $49 per month since 2021.", "Card 4111 1111 1111 1112 is not Luhn",
                                  "Refund Rangers (refundrangers.com) helps Amazon sellers."])
def test_prose_is_left_as_it_is(text):
    assert text_redact.secrets(text) == text
    if "Luhn" in text:
        assert text_redact.redact(text) == text


# ---------------------------------------------------------------------------------------------- WR-F032 idempotency

class Gate(ti.Scripted):
    """Answers from the script; the first ask blocks until released (a run held in flight)."""

    def __init__(self, *script):
        super().__init__(*script)
        self.entered, self.release = threading.Event(), threading.Event()

    def ask(self, prompt, guard=None, meter=None):
        if not self.entered.is_set():
            self.entered.set()
            assert self.release.wait(30), "the test never released the engine"
        return super().ask(prompt, guard=guard, meter=meter)


def test_a_request_id_in_flight_is_409_and_only_the_original_result_is_stored(tmp_path):
    h, fakes, psid = ti.svc(tmp_path)
    gate = h.ports.engines["openai"] = fakes["openai"] = Gate(ti.ANSWER)
    h.ok(ti.comp_set(h), 201)
    body = {"request_id": rid(), "domain": ti.DOMAIN, "prompt_set_id": psid, "max_age_minutes": 0}
    got = {}
    t = threading.Thread(target=lambda: got.setdefault("first", h.post("/tenants/zbm/competitor-visibility-runs",
                                                                       body)))
    t.start()
    try:
        assert gate.entered.wait(30)                       # the original is in flight, inside its citation run
        before = len(h.ledger.events)
        h.refused(h.post("/tenants/zbm/competitor-visibility-runs", body), 409, "VISIBILITY_RUN_IN_PROGRESS")
        h.refused(h.post("/tenants/zbm/competitor-visibility-runs", {**body, "max_age_minutes": 5}), 409,
                  "REQUEST_ID_REUSED")                     # the same id with another body
        assert len(h.ledger.events) == before and h.svc.visibility_runs == {}     # nothing was written
    finally:
        gate.release.set()
        t.join(60)
    first = h.ok(got["first"], 201)
    assert first["result"]["outcome"] == "OK" and first["source"] == "fresh"
    again = h.ok(h.post("/tenants/zbm/competitor-visibility-runs", body), 201)   # completed: the stored result
    assert again == first
    h.refused(h.post("/tenants/zbm/competitor-visibility-runs", {**body, "max_age_minutes": 5}), 409,
              "REQUEST_ID_REUSED")
    recs = [e for e in h.ledger.events if e.get("_payload", {}).get("event_type") == "competitor_visibility_recorded"
            or e.get("event_type") == "competitor_visibility_recorded"]
    assert len(h.svc.visibility_runs) == 1 and len(recs) <= 1
    assert h.svc._vis_in_flight() == {}


def test_many_concurrent_replays_of_one_request_id_store_one_result(tmp_path):
    h, fakes, psid = ti.svc(tmp_path)
    gate = h.ports.engines["openai"] = fakes["openai"] = Gate(ti.ANSWER)
    h.ok(ti.comp_set(h), 201)
    body = {"request_id": rid(), "domain": ti.DOMAIN, "prompt_set_id": psid, "max_age_minutes": 0}
    results, start = [], threading.Barrier(6)

    def replay():
        start.wait(30)
        results.append(h.post("/tenants/zbm/competitor-visibility-runs", body))
    first = []
    t0 = threading.Thread(target=lambda: first.append(h.post("/tenants/zbm/competitor-visibility-runs", body)))
    t0.start()
    assert gate.entered.wait(30)
    ts = [threading.Thread(target=replay) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    gate.release.set()
    t0.join(60)
    assert sorted(r.status_code for r in results) == [409] * 6
    assert {r.json()["detail"] for r in results} == {"VISIBILITY_RUN_IN_PROGRESS"}
    v = h.ok(first[0], 201)
    assert v["result"]["outcome"] == "OK" and list(h.svc.visibility_runs) == [v["visibility_run_id"]]
    assert h.ok(h.post("/tenants/zbm/competitor-visibility-runs", body), 201) == v
