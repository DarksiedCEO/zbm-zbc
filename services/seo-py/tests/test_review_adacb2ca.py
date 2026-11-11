"""Review of adacb2ca (shared redaction core, both lanes): spelled e-mails with a literal dot, word-padded card numbers,
the EAN-13 shape hiding a 13-digit Visa, base64 glued to a short prefix, URL fragments, the service lock's scope, the
client-view gaps and utility keys. Every sensitive-looking value here is GENERATED at run time from fragments (nothing
credential-shaped is written in this file); failures report counts and case names only."""

from __future__ import annotations

import base64
import json
import random
import re
import string
import time

import helpers
import pytest
import test_review_601e328a as r6
import test_vault_wiring as vw

import svc_citations
import vault
from agents import callum
from agents import secret_guard as sg
from ports import ProviderAnswer
from primitives.fetch import FetchResult

SEED = 9112026
LOW = string.ascii_lowercase
ALNUM = string.ascii_letters + string.digits


def _r(rng, alphabet, n):
    return "".join(rng.choice(alphabet) for _ in range(n))


def _card(rng, prefix: str, n: int, ean: bool = False) -> str:
    """A Luhn-valid number of ``n`` digits starting with ``prefix`` (``ean``: also a valid EAN-13 check digit)."""
    while True:
        body = prefix + _r(rng, string.digits, n - len(prefix) - 1)
        for c in string.digits:
            d = body + c
            if sg._luhn(d) and (not ean or sg._ean_ok(d)):
                return d


def _not_luhn(rng, prefix: str, n: int) -> str:
    while True:
        d = prefix + _r(rng, string.digits, n - len(prefix))
        if not sg._luhn(d):
            return d


def _email_parts(rng):
    return _r(rng, LOW, 7), _r(rng, LOW, 8)


def _gh(rng) -> str:
    return "gh" + "p_" + _r(rng, ALNUM, 36)


# ------------------------------------------------------------------------------------------------ generated cases

def cases() -> dict:
    """name -> list of texts each lane must flag (strict: withheld; client view: a finding)."""
    rng = random.Random(SEED)
    out: dict = {}
    mails = []
    for _ in range(6):
        loc, dom = _email_parts(rng)
        mails += [f"write to {loc} at {dom}.com today", f"{loc} at {dom}.co.uk", f"{loc} at {dom} . com",
                  f"{loc} AT {dom} .com", f"{loc} at {dom}. com", f"{loc}[@]{dom}[.]com", f"{loc} at mail.{dom}.io"]
    out["spelled_email_dot"] = mails
    cards = []
    for _ in range(6):
        d = _card(rng, "4", 16)
        g = [d[i:i + 4] for i in range(0, 16, 4)]
        cards += [" then ".join(g), " followed by ".join(g), "then".join(g), f"card {g[0]} then {g[1]} followed by "
                  f"{g[2]} then {g[3]} ok"]
    out["word_padded_card"] = cards
    client_words = []
    for _ in range(6):
        d = _card(rng, "4", 16)
        g = [d[i:i + 4] for i in range(0, 16, 4)]
        client_words += [" and ".join(g), " plus ".join(g), " next ".join(g)]
    out["word_padded_card_client"] = client_words
    out["ean_visa13"] = [f"code {_card(rng, '4', 13, ean=True)} on file" for _ in range(8)]
    b64 = []
    for _ in range(4):
        payload = base64.b64encode(("token " + _gh(rng)).encode()).decode()
        url_payload = base64.urlsafe_b64encode(_gh(rng).encode()).decode().rstrip("=")
        b64 += ["x" + payload, "ref-" + payload, "x_" + payload, "ab" + url_payload, "abc" + url_payload,
                "ref-" + url_payload]
    out["misaligned_b64"] = b64
    return out


def client_cases() -> dict:
    """Client-view gaps (review SHOULD 3): name -> texts the client view must flag."""
    rng = random.Random(SEED + 1)
    out = {}
    out["phone_spaced"] = [f"call {rng.randint(2, 9)}{_r(rng, string.digits, 2)} {rng.randint(2, 9)}"
                           f"{_r(rng, string.digits, 2)} {_r(rng, string.digits, 4)} now" for _ in range(8)]
    out["ssn_label"] = [f"SSN {rng.randint(100, 665)}{rng.randint(10, 99)}{rng.randint(1000, 9999)}" for _ in range(8)]
    out["bearer_space"] = ["Authorization: Bearer " + _r(rng, ALNUM, 24) + "." + _r(rng, ALNUM, 12) for _ in range(4)] \
        + ["bearer " + _r(rng, ALNUM, 30) + "1" for _ in range(4)]
    return out


def client_urls() -> dict:
    rng = random.Random(SEED + 2)
    out = {}
    out["query_pw_pin"] = [f"https://shop.test/login?pw={_r(rng, ALNUM, 10)}" for _ in range(4)] \
        + [f"https://bank.test/card?pin={_r(rng, string.digits, 4)}" for _ in range(4)]
    out["jsessionid"] = [f"https://shop.test/cart;jsessionid={_r(rng, string.hexdigits, 24)}/view" for _ in range(4)]
    loc, dom = _email_parts(rng)
    out["bracketed_email_path"] = [f"https://site.test/u/{loc}[@]{dom}[.]com/x"]
    out["fragment_b64"] = ["https://site.test/p#x" + base64.b64encode(_gh(rng).encode()).decode()
                           for _ in range(4)]
    return out


# ------------------------------------------------------------------------------------------------ entry points

def _strict_everywhere(p: str) -> tuple:
    """(check, redact_text, guard_record value, guard_record key, callum excerpt): each True when withheld."""
    rec_v = sg.guard_record({"v": p})
    rec_k = sg.guard_record({p: "x"})
    ex = callum.excerpt("Answer: " + p + " end.", [])
    return (sg.check(p) is not None,
            sg.redact_text(p) == (sg.WITHHELD, 1),
            isinstance(rec_v, dict) and rec_v.get("v") == sg.WITHHELD,
            isinstance(rec_k, dict) and p not in rec_k and all(str(k).startswith(sg.WITHHELD) for k in rec_k),
            ex.get("withheld") is not None and p not in json.dumps(ex))


STRICT_ENTRIES = ("check", "redact_text", "guard_record_value", "guard_record_key", "callum_excerpt")


def _misses(samples, judge) -> dict:
    out: dict = {}
    for p in samples:
        for name, ok in judge(p):
            if not ok:
                out[name] = out.get(name, 0) + 1
    return out


def _strict_judge(p):
    return zip(STRICT_ENTRIES, _strict_everywhere(p))


def _client_judge(p):
    shown = callum._display("excerpt", p)
    return [("client_view_findings", bool(sg.client_view_findings(p))), ("owner_display", shown["alert"])]


STRICT_CLASSES = ("spelled_email_dot", "word_padded_card", "ean_visa13", "misaligned_b64")


@pytest.mark.parametrize("cls", STRICT_CLASSES)
def test_strict_lane_withholds_every_new_shape_through_every_entry_point(cls):
    samples = cases()[cls]
    assert len(samples) >= 8
    assert _misses(samples, _strict_judge) == {}, cls


@pytest.mark.parametrize("cls", sorted(cases()))
def test_client_view_masks_every_new_shape(cls):
    # c13529af (decision 22): the plain "name at domain.tld" samples are shown to the owner (masked until c13529af)
    samples = [p for p in cases()[cls] if not r6.plain_spelled(p)]
    assert samples and _misses(samples, _client_judge) == {}, cls
    assert all(not sg.client_view_findings(p) for p in cases()[cls] if r6.plain_spelled(p)), cls


@pytest.mark.parametrize("cls", sorted(client_cases()))
def test_client_view_gaps_are_closed(cls):
    samples = client_cases()[cls]
    assert _misses(samples, _client_judge) == {}, cls


@pytest.mark.parametrize("cls", sorted(client_urls()))
def test_client_view_url_gaps_are_closed_and_strict_urls_withhold_them(cls):
    for u in client_urls()[cls]:
        shown, kind = sg.client_view_url(u)
        assert kind is not None, cls
        assert shown is None or sg.CLIENT_HIDDEN in shown, cls
        assert callum.url_flag(u) is not None, cls                     # the strict lane flags it too
        assert sg.safe_url(u) != u, cls


def test_a_spelled_email_in_a_url_is_withheld_by_safe_url_and_stored_url():
    rng = random.Random(SEED + 3)
    for _ in range(4):
        loc, dom = _email_parts(rng)
        u = f"https://site.test/contact/{loc}%20at%20{dom}.com/x"
        assert callum.url_flag(u) is not None
        assert f"{loc}%20at%20{dom}" not in sg.safe_url(u)
        st = callum.stored_url(u)
        assert st["withheld"] and loc not in json.dumps(st)
        # c0c09bd1 (decision 22 is for PROSE only): the plain form in a path segment is masked again (tightened back)
        assert sg.client_view_url(u)[1] == sg.PERSONAL
        assert sg.client_view_url(u.replace(".com/x", "%20dot%20com/x"))[1] == sg.PERSONAL


def test_url_fragments_are_inspected_by_both_lanes():
    for u in client_urls()["fragment_b64"]:
        frag = u.split("#", 1)[1]
        assert sg.inspect_url(u)["fragment_kind"] is not None
        assert frag not in sg.safe_url(u)
        shown, kind = sg.client_view_url(u)
        assert kind is not None and frag not in (shown or "")


def test_decoded_views_cover_a_short_prefix_and_a_boundary():
    rng = random.Random(SEED + 4)
    tok = _gh(rng)
    std = base64.b64encode(tok.encode()).decode()
    url = base64.urlsafe_b64encode(tok.encode()).decode().rstrip("=")
    for glued in ("x" + std, "ab" + std, "abc" + url, "ref-" + std, "x_" + url, "pre-fix-" + url):
        assert any(tok in v for v in sg.views(glued)), len(glued)


def test_hop_hosts_with_a_spelled_email_are_withheld_from_the_log_row():
    rng = random.Random(SEED + 5)
    loc, dom = _email_parts(rng)
    host = f"{loc} at {dom}.com"
    assert callum.checked_host(host) == {"host": None, "host_withheld": "PERSONAL_DATA"}
    res = {"state": "FAILED", "detail": "TOO_MANY_REDIRECTS", "redirects": 2,
           "hop_hosts": ["competitor.test", host, "next.test"], "redirect_refused": False}
    c = callum.classify_citation("https://competitor.test/a", vw.DOMAIN, ["competitor.test"], res)
    logged = callum.log_row({"class": c["class"], "chain": c["chain"]}, None, None)
    assert logged["chain"]["hop_hosts"][0] == "competitor.test"
    assert logged["chain"]["hop_hosts"][1] is None
    assert loc not in json.dumps(logged)


# ------------------------------------------------------------------------------------------------ precision (client)

def test_client_view_shows_a_non_luhn_card_prefixed_number():
    """Review SHOULD 6: the card rule needs Luhn (kills the 'Luhn ignored' mutation)."""
    rng = random.Random(SEED + 6)
    for p in ("4", "51", "37", "6011"):
        n = 15 if p == "37" else 16
        d = _not_luhn(rng, p, n)
        assert sg.client_view_findings(f"ref {d} ok") == [], p


def test_mutation_luhn_ignored_is_caught(monkeypatch):
    rng = random.Random(SEED + 6)
    d = _not_luhn(rng, "4", 16)
    monkeypatch.setattr(sg, "_luhn", lambda s: True)
    assert "card" in sg.client_view_findings(f"ref {d} ok")


def test_utility_keys_and_shapes_are_kept_in_the_client_view():
    rng = random.Random(SEED + 7)
    hi = _r(rng, string.ascii_uppercase, 12) + _r(rng, string.ascii_lowercase, 12) + _r(rng, string.digits, 12)
    for k in ("ved", "ei", "sca_esv", "gs_lcrp", "oq", "aqs"):
        u = f"https://www.google.com/search?q=cat+toys&{k}={hi}"
        assert sg.client_view_url(u) == (u, None), k
    shop = f"https://pets.myshopify.com/search?q=cat&_sid={_r(rng, string.hexdigits.lower(), 10)}"
    assert sg.client_view_url(shop) == (shop, None)
    for k in ("sid", "SID", "sessionid", "session_id", "jsessionid", "phpsessid", "PHPSESSID"):
        assert sg.client_view_url(f"https://x.test/p?{k}=abc123")[1] == sg.CREDENTIAL, k
    assert sg._cv_query_key("_sid") is False and sg._cv_query_key("sid") is True


def _coords(rng, n):
    out = []
    while len(out) < n:
        lat = f"{rng.choice('+-')}{rng.randint(0, 89)}.{_r(rng, string.digits, 7)}".lstrip("+")
        lng = f"{rng.choice('+-')}{rng.randint(0, 179)}.{_r(rng, string.digits, 7)}".lstrip("+")
        if not sg._cv_card(re.sub(r"\D", "", lat + lng)):
            out.append(f"@{lat},{lng},15z")
    return out


def test_map_coordinates_are_not_read_as_a_card_in_either_lane():
    rng = random.Random(SEED + 8)
    for c in _coords(rng, 20):
        u = f"https://www.google.com/maps/place/Pet+Shop/{c}"
        assert sg.client_view_url(u) == (u, None)
        assert sg.check(c) is None and sg.check("see " + c + " ok") is None


def test_a_card_written_as_coordinates_is_still_a_card():
    rng = random.Random(SEED + 9)
    d = _card(rng, "4", 16)
    c = f"@{d[:2]}.{d[2:8]},{d[8:10]}.{d[10:]}"
    assert sg.check(c) is not None and "card" in sg.client_view_findings(c)


def test_ordinary_prose_with_at_and_sentence_ends_is_kept():
    for p in ("We met at noon. It was fun", "buy it at Target. In stores now", "look at page 3. Then go",
              "rated 4.5 at 3.2 GHz", "meet at 10.30 in room 4"):
        assert sg.check(p) is None and sg.client_view_findings(p) == [], p


# ------------------------------------------------------------------------------------------------ mutation bite

def _bites() -> dict:
    c = cases()
    return {
        # c13529af (decision 22): the probes are the forms BOTH lanes still flag (the plain form is shown)
        "at_word_email": ([("_at_word_email", lambda v: False)],
                          [p for p in c["spelled_email_dot"] if not r6.plain_spelled(p)][:5]),
        "word_bridge": ([("_WORD_BRIDGE", re.compile(r"(?!x)x"))], c["word_padded_card"]),
        "ean_not_visa": ([("_cv_card_in_shape", None)], c["ean_visa13"]),
        "b64_offsets": ([("_b64_starts", lambda run: [0])], c["misaligned_b64"]),
    }


@pytest.mark.parametrize("name", sorted(_bites()))
def test_each_new_rule_is_load_bearing(monkeypatch, name):
    patches, probes = _bites()[name]
    before = [(sg.check(p) is not None, bool(sg.client_view_findings(p)), tuple(sg.views(p))) for p in probes]
    assert all(s and c for s, c, _ in before), name
    for attr, value in patches:
        if attr == "_cv_card_in_shape":
            real = sg._shape_ok
            monkeypatch.setattr(sg, "_shape_ok", lambda m: _ean_old(m) if m.lastgroup == "ean" else real(m))
        else:
            monkeypatch.setattr(sg, attr, value)
    after = [(sg.check(p) is not None, bool(sg.client_view_findings(p)), tuple(sg.views(p))) for p in probes]
    assert any(a != b for a, b in zip(after, before)), (name, "removing this rule broke nothing")


def _ean_old(m):
    return sg._ean_ok(m.group(m.lastgroup))


@pytest.mark.parametrize("name,attr,value,cls", [
    ("cv_bridge", "_unbridged", lambda v, sep: v, "word_padded_card_client"),
    ("cv_at_word", "_at_word_email", lambda v: False, "spelled_email_dot"),
])
def test_each_new_client_rule_is_load_bearing(monkeypatch, name, attr, value, cls):
    probes = [p for p in cases()[cls] if not r6.plain_spelled(p)]       # c13529af decision 22: plain form shown
    assert all(sg.client_view_findings(p) for p in probes)
    monkeypatch.setattr(sg, attr, value)
    assert sum(1 for p in probes if not sg.client_view_findings(p)) > 0, name


@pytest.mark.parametrize("attr,value,cls", [
    ("_CV_NANP_SPACED", re.compile(r"(?!x)x"), "phone_spaced"),
    ("_CV_SSN_LABEL", re.compile(r"(?!x)x"), "ssn_label"),
    ("_CV_BEARER", re.compile(r"(?!x)x"), "bearer_space"),
])
def test_each_client_gap_rule_is_load_bearing(monkeypatch, attr, value, cls):
    probes = [p for p in client_cases()[cls] if "Authorization" not in p]      # that label is its own rule
    assert probes and all(sg.client_view_findings(p) for p in probes), attr
    monkeypatch.setattr(sg, attr, value)
    monkeypatch.setattr(sg, "_cv_labelled_phone", lambda m: False)     # c0c09bd1: MUST 8 also reads labelled phones
    assert all(not sg.client_view_findings(p) for p in probes), attr


def test_client_url_rules_are_load_bearing(monkeypatch):
    urls = client_urls()
    monkeypatch.setattr(sg, "CV_QUERY_KEYS", sg.CV_QUERY_KEYS - {"pw", "pin"})
    assert all(sg.client_view_url(u)[1] is None for u in urls["query_pw_pin"])
    monkeypatch.undo()
    monkeypatch.setattr(sg, "_cv_seg_findings", sg.client_view_findings)
    assert all(sg.client_view_url(u)[1] is None for u in urls["jsessionid"])
    monkeypatch.undo()
    monkeypatch.setattr(sg, "_CV_DOT", re.compile(r"(?i) ?[\[\(\{<] ?dot ?[\]\)\}>] ?| dot |\s?\.\s?"))
    assert sg.client_view_findings(urls["bracketed_email_path"][0].split("/u/")[1].split("/")[0]) == []


def test_utility_rules_are_load_bearing(monkeypatch):
    rng = random.Random(SEED + 10)
    hi = _r(rng, string.ascii_uppercase, 12) + _r(rng, string.ascii_lowercase, 12) + _r(rng, string.digits, 12)
    monkeypatch.setattr(sg, "CV_ANALYTICS_KEYS", sg.CV_ANALYTICS_KEYS - {"ved"})
    assert sg.client_view_url(f"https://www.google.com/search?q=x&ved={hi}")[1] is not None
    monkeypatch.undo()
    monkeypatch.setattr(sg, "CV_SID_KEYS", sg.CV_SID_KEYS | {"_sid"})
    assert sg.client_view_url("https://pets.myshopify.com/search?q=cat&_sid=ab12cd34ef")[1] is not None
    monkeypatch.undo()
    real = sg._shape_ok
    monkeypatch.setattr(sg, "_shape_ok", lambda m: False if m.lastgroup == "geo" else real(m))
    hits = [c for c in _coords(rng, 20) if sg.check(c) is not None]
    assert hits                                                  # without the shape the digits are withheld


# ------------------------------------------------------------------------------------------------ the real service

class _Port:
    connected, metered, accepts_guard, name, attempts = True, False, True, "openai", 1

    def __init__(self, script):
        self.script, self.asked = list(script), 0

    def ask(self, prompt, guard=None, meter=None):
        self.asked += 1
        return self.script[(self.asked - 1) % len(self.script)]


class _RedirectFetcher:
    """The cited competitor URL redirects through a hop whose host is a spelled e-mail, then stops."""

    def __init__(self, hop_host: str):
        self.hop_host = hop_host

    def fetch(self, url, robots_cache=None, guard=None, **kw):
        guard(capability="fetch", provider="web")
        hop = f"http://{self.hop_host}/next"
        return FetchResult(url=url, state="TOO_MANY_REDIRECTS", final_url=hop,
                           redirects=[{"url": url, "status": 302}, {"url": hop, "status": 302}])


def _service_values() -> dict:
    rng = random.Random(SEED + 11)
    loc, dom = _email_parts(rng)
    hl, hd = _email_parts(rng)
    d = _card(rng, "4", 16)
    g = [d[i:i + 4] for i in range(0, 16, 4)]
    return {"email": f"{loc} at {dom}.com", "hop": f"{hl} at {hd}.com",
            "card": f"{g[0]} then {g[1]} followed by {g[2]} then {g[3]}",
            "visa13": _card(rng, "4", 13, ean=True),
            "b64": "ref-" + base64.b64encode(("token " + _gh(rng)).encode()).decode()}


def _service_answers(v: dict) -> list:
    cited = "https://competitor.test/blog/guide"
    a = ProviderAnswer("ANSWER", f"Z Best Media support: write to {v['email']} or see card {v['card']} today.",
                       [cited], "m", "1", cite_from_text=False, provider="openai", asked_at="2026-10-09T18:00:00Z")
    a.citation_titles = {cited: "Z Best Media code " + v["visa13"]}
    a.citation_hints = {cited: "Z Best Media ref " + v["b64"]}
    return [a]


def _service_blob_values(v: dict) -> dict:
    return {**v, "email_local": v["email"].split(" at ")[0], "hop_local": v["hop"].split(" at ")[0]}


def test_a_real_citation_run_writes_none_of_the_new_shapes_and_the_owner_view_masks_them(tmp_path, caplog):
    v = _service_values()
    h = vw.harness(tmp_path, SEO_CALLUM_RESOLVE_URLS="1")
    h.ports.engines["openai"] = _Port(_service_answers(v))
    h.ports.fetcher = _RedirectFetcher(v["hop"])
    h.ok(h.post("/tenants/zbm/domains", {"request_id": vw.rid(), "domains": [vw.DOMAIN]}, andre=True))
    run = vw.citation_run(h)
    assert run["status"] == "completed" and run["result"]["outcome"] == "OK"
    cid = run["citation_run_id"]
    # sink job C: the split envelope is in the tenant's vault (no host is logged any more); the hop-host rule is read
    # on the stored document
    logged = helpers.citation_document(h, cid)["result"]["facts"]["rows"]
    assert any((r.get("chain") or {}).get("hop_hosts") for r in logged)            # the hop chain was kept
    assert all(h is None or " at " not in h for r in logged for h in (r.get("chain") or {}).get("hop_hosts") or [])
    blob = vw.written(h, caplog)
    assert vw.found(blob, _service_blob_values(v)) == []
    got = h.ok(h.get(f"/tenants/zbm/citation-runs/{cid}"))
    mp = h.ok(h.get(f"/tenants/zbm/citation-runs/{cid}/mnc-priority"))
    shown = json.dumps([got, mp], ensure_ascii=False)
    assert vw.found(shown, v) == []
    ev = [r["evidence"] for r in got["result"]["facts"]["rows"] if r.get("evidence")]
    assert ev and all(e[k]["alert"] for e in ev for k in ("excerpt", "title", "hint") if e.get(k))


def test_the_real_run_mutation_on_the_hop_host_rule_is_caught(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(sg, "_at_word_email", lambda s: False)
    v = _service_values()
    h = vw.harness(tmp_path, SEO_CALLUM_RESOLVE_URLS="1")
    h.ports.engines["openai"] = _Port(_service_answers(v))
    h.ports.fetcher = _RedirectFetcher(v["hop"])
    h.ok(h.post("/tenants/zbm/domains", {"request_id": vw.rid(), "domains": [vw.DOMAIN]}, andre=True))
    cid = vw.citation_run(h)["citation_run_id"]
    # sink job C: the mutated rule lets the hop host into the STORED envelope (the vaulted document, hence the
    # owner's view); nothing of it reaches a file, the ledger or a log line any more
    stored = json.dumps(helpers.citation_document(h, cid)["result"], ensure_ascii=False)
    assert "hop" in vw.found(stored, {"hop": v["hop"]})
    assert vw.found(vw.written(h, caplog), {"hop": v["hop"]}) == []


# ------------------------------------------------------------------------------------------------ lock scope

class _TimedLock:
    def __init__(self, inner):
        self.inner, self.depth, self.t0, self.holds = inner, 0, 0.0, []

    def acquire(self, *a, **k):
        got = self.inner.acquire(*a, **k)
        if got:
            if self.depth == 0:
                self.t0 = time.perf_counter()
            self.depth += 1
        return got

    def release(self):
        self.depth -= 1
        if self.depth == 0:
            self.holds.append(time.perf_counter() - self.t0)
        self.inner.release()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *a):
        self.release()


class _ManyPort(_Port):
    def __init__(self, n):
        super().__init__([])
        self.n = n

    def ask(self, prompt, guard=None, meter=None):
        self.asked += 1
        urls = [f"https://competitor.test/blog/post-{self.asked}-{i}" for i in range(self.n)]
        return ProviderAnswer("ANSWER", "Z Best Media recovers marketplace revenue for sellers.", urls, "m", "1",
                              cite_from_text=False, provider="openai", asked_at="2026-10-09T18:00:00Z")


def test_the_service_lock_is_not_held_while_rows_are_encrypted_or_decrypted(tmp_path, monkeypatch):
    """Review SHOULD 1: the split and the owner view's decrypt + mask run outside the service lock; the vault lines
    of a run are ONE append (one fsync). Sink job C: the run's evidence is one blob (``put_blob``), not a record per
    row."""
    h = vw.harness(tmp_path)
    h.ports.engines["openai"] = _ManyPort(40)
    h.ok(h.post("/tenants/zbm/domains", {"request_id": vw.rid(), "domains": [vw.DOMAIN]}, andre=True))
    appends = []
    real_append = vault.CitationVault._append
    # c0c09bd1: only the CITATION vault is counted (operator text has its own vault file, written with the prompt set)
    monkeypatch.setattr(vault.CitationVault, "_append", lambda self, line: (
        appends.append(1) if self is h.svc.vault else None, real_append(self, line))[1])
    held_during_put = []
    real_put = vault.CitationVault.put_blob

    def put(self, tid, record):
        if self is h.svc.vault:
            held_during_put.append(h.svc.lock._is_owned() if hasattr(h.svc.lock, "_is_owned") else None)
        return real_put(self, tid, record)
    monkeypatch.setattr(vault.CitationVault, "put_blob", put)
    run = vw.citation_run(h)
    rows = len(run["result"]["facts"]["rows"])
    assert run["status"] == "completed" and rows >= 120
    assert len(helpers.citation_document(h, run["citation_run_id"])["evidence"]) == rows
    assert len(h.svc.vault) == 2                                     # one chunk and its manifest: one document
    assert len(appends) == 1                                         # one append for the whole run
    assert held_during_put and not any(held_during_put)              # no put under the service lock
    decrypting_locked = []
    real_cv = callum.client_view

    def cv(env, got):
        decrypting_locked.append(h.svc.lock._is_owned())
        return real_cv(env, got)
    monkeypatch.setattr(callum, "client_view", cv)
    h.ok(h.get(f"/tenants/zbm/citation-runs/{run['citation_run_id']}"))
    h.ok(h.get(f"/tenants/zbm/citation-runs/{run['citation_run_id']}/mnc-priority"))
    assert decrypting_locked == [False, False]


def test_a_failed_batch_write_leaves_nothing_and_closes_run_failed(tmp_path, monkeypatch, caplog):
    v = vw.synthetic()
    h = vw.harness(tmp_path)
    vw.start(h, v)

    real_append = vault.CitationVault._append

    def boom(self, line):
        if self is not h.svc.vault:                  # c0c09bd1: the operator-text vault (the prompt set) writes
            return real_append(self, line)
        raise vault.VaultWriteError("forced")
    monkeypatch.setattr(vault.CitationVault, "_append", boom)
    run = vw.citation_run(h)
    assert run["status"] == "interrupted" and run["interrupted_reason"] == "RUN_FAILED"
    assert len(h.svc.vault) == 0 and h.ledger.of_type("citation_run_recorded") == []
    assert run["citation_run_id"] not in h.svc.running_citation_runs
    assert vw.found(vw.written(h, caplog), v) == []


def test_a_kill_switch_after_the_split_writes_nothing_to_the_vault(tmp_path, monkeypatch):
    v = vw.synthetic()
    h = vw.harness(tmp_path)
    vw.start(h, v)
    real = svc_citations.CitationsMixin._prepare_citation_result

    def prepare(self, *a, **k):
        out = real(self, *a, **k)
        monkeypatch.setattr(type(self), "kill_code", lambda self, **kw: "KILLED_TEST")
        return out
    monkeypatch.setattr(svc_citations.CitationsMixin, "_prepare_citation_result", prepare)
    run = vw.citation_run(h)
    assert run["status"] == "interrupted" and len(h.svc.vault) == 0
    assert h.ledger.of_type("citation_run_recorded") == []
