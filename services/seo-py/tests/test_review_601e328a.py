"""Review of 601e328a (shared redaction core, both lanes): e-mails inside <...> with markdown in them, the filler-word
bridge for 2-6 digit groups (SSN 3-2-4, Amex 4-6-5), letter-spaced / mid-word-split spelled e-mails, the client view's
retailer-keyed " at " exemption, the client view run on the strict lane's bridged / no-space / decoded views, the
linear excerpt path, the replay view outside the lock, the orphan counter and the torn-tail vault repair.

Every sensitive-looking value here is GENERATED at run time from fragments (nothing credential-shaped or e-mail-shaped
is written in this file); failures report counts and case names only."""

from __future__ import annotations

import base64
import json
import random
import re
import string
import time
from urllib.parse import quote

import leak_corpus_v2 as lc
import pytest
import test_vault_wiring as vw
import utility_corpus_v2 as uc

import svc_citations
import vault
from agents import callum
from agents import prompt_guard
from agents import secret_guard as sg
from ports import ProviderAnswer

SEED = 6012026
LOW = string.ascii_lowercase
ALNUM = string.ascii_letters + string.digits
AT = chr(64)
FILLERS = ("then", "next", "plus", "dash", "space", "followed by")
CLIENT_FILLERS = FILLERS + ("and",)


def _r(rng, alphabet, n):
    return "".join(rng.choice(alphabet) for _ in range(n))


def _card(rng, prefix: str, n: int) -> str:
    while True:
        body = prefix + _r(rng, string.digits, n - len(prefix) - 1)
        for c in string.digits:
            if sg._luhn(body + c) and sg._cv_card(body + c):
                return body + c


def _ssn(rng) -> tuple:
    while True:
        a, g, s = f"{rng.randint(100, 665):03d}", f"{rng.randint(10, 99):02d}", f"{rng.randint(1000, 9999):04d}"
        if sg._cv_ssn_ok(a + g + s):
            return a, g, s


def _nanp(rng) -> tuple:
    return f"{rng.randint(2, 9)}{_r(rng, string.digits, 2)}", f"{rng.randint(2, 9)}{_r(rng, string.digits, 2)}", \
        _r(rng, string.digits, 4)


def _mail(rng) -> tuple:
    return _r(rng, LOW, 7), _r(rng, LOW, 8)


def _gh(rng) -> str:
    return "gh" + "p_" + _r(rng, ALNUM, 36)


def _keys(rng) -> dict:
    return {"github": _gh(rng), "stripe": "sk" + "_live_" + _r(rng, ALNUM, 24),
            "slack": "xo" + "xb-" + _r(rng, string.digits, 12) + "-" + _r(rng, ALNUM, 24),
            "openai": "sk-" + "proj-" + _r(rng, ALNUM, 40), "google": "AI" + "za" + _r(rng, ALNUM, 35)}


# review c13529af (owner decision, ADR 0051 decision 22): the client view shows the PLAIN spelled form "<word> at
# <domain>" (a letters-only local part, " at ", a domain with literal dots, nothing spelled / bracketed / defanged
# after it); every client-view assertion below that this decision changed excludes exactly these samples (the strict
# lane still withholds every one of them)
_PLAIN_AT = re.compile(r"(?<![A-Za-z0-9._%+\-])([A-Za-z]{2,}) {1,3}(?:at|AT|At) {1,3}([A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+)(?![A-Za-z0-9\-])")


def plain_spelled(p: str) -> bool:
    t = re.sub(r"[*`\\]", "", p)
    if AT in t or any(b in t for b in "[({") or re.search(r"(?i)(?<![a-z])(?:dot|period|punto|point)(?![a-z])", t):
        return False
    return _PLAIN_AT.search(t) is not None


def _md_escape(s: str) -> str:
    return "".join("\\" + c if c in "_.-*" else c for c in s)


# ------------------------------------------------------------------------------------------------ generated cases

def strict_cases() -> dict:
    """name -> texts the STRICT lane must withhold through every entry point (MUST 1, 2, 3)."""
    rng = random.Random(SEED)
    out: dict = {}
    md = []
    for _ in range(4):
        loc, dom = _mail(rng)
        x = _r(rng, LOW, 4)
        md += [f"<{loc}{AT}{dom}\\.com>", f"<{loc}\\.{x}{AT}{dom}.com>", f"<**{loc}**{AT}{dom}.com>",
               f"<{loc}{AT}**{dom}**.com>", f"<`{loc}`{AT}{dom}.com>", f"<{loc}{AT}`{dom}`.com>",
               f"<{loc}{AT}{dom}`.`com>", f"< {loc} {AT} {dom} \\. com >", f"<{loc} {AT} **{dom}** . com>",
               f"<**{loc} at {dom}.com**>", f"<{loc} **at** {dom}.com>", f"<{loc} **at** {dom} **dot** com>",
               f"<{loc}\\_{x}{AT}{dom}.com>", f"<{loc}{AT}{dom}\\.co\\.uk>", f"<`{loc} at {dom} dot com`>"]
    out["md_tag_email"] = [f"write to {p} today" for p in md]
    ssn, amex = [], []
    for f in FILLERS:
        for _ in range(2):
            a, g, s = _ssn(rng)
            ssn.append(f"ref {a} {f} {g} {f} {s} ok")
            d = _card(rng, rng.choice(("34", "37")), 15)
            amex.append(f"card {d[:4]} {f} {d[4:10]} {f} {d[10:]} ok")
    out["ssn_324_words"] = ssn
    out["amex_465_words"] = amex
    spaced = []
    for _ in range(4):
        loc, dom = _mail(rng)
        sp = " ".join
        spaced += [f"{sp(loc)}  a t  {sp(dom)} . c o m", f"{sp(loc)} a t {sp(dom)} . c o m",
                   f"{sp(loc)}  a t  {sp(dom)}  d o t  c o m", f"{loc} at {dom[:3]} {dom[3:]} DOT com",
                   f"{loc[:3]} {loc[3:]} at {dom[:4]} {dom[4:]} dot com", f"{sp(loc)} at {sp(dom)} dot com",
                   f"{loc} at {sp(dom)} . com", f"{loc} at {dom[:3]} {dom[3:]} . com"]
    out["spaced_spelled_email"] = [f"mail {p} now" for p in spaced]
    return out


def client_cases() -> dict:
    """name -> texts the CLIENT view must flag (MUST 1, 2, 5)."""
    rng = random.Random(SEED + 1)
    out: dict = {}
    out["md_escaped_key"] = ["<" + _md_escape(k) + ">" for _ in range(3) for k in _keys(rng).values()]
    nums = []
    for f in CLIENT_FILLERS:
        a, g, s = _ssn(rng)
        nums.append(f"ref {a} {f} {g} {f} {s} ok")
        d = _card(rng, "37", 15)
        nums.append(f"card {d[:4]} {f} {d[4:10]} {f} {d[10:]} ok")
        d = _card(rng, "4", 13)
        nums.append(f"card {d[:4]} {f} {d[4:8]} {f} {d[8:]} ok")
        p = _nanp(rng)
        nums.append(f"call {p[0]} {f} {p[1]} {f} {p[2]} now")
    out["word_bridged_numbers"] = nums
    dd = []
    for _ in range(4):
        a, g, s = _ssn(rng)
        p = _nanp(rng)
        dd += [f"ssn {a}--{g}--{s}", f"id {a} -- {g} -- {s}", f"call {p[0]}--{p[1]}--{p[2]}",
               f"call ({p[0]})--{p[1]}--{p[2]}"]
    out["double_dash"] = dd
    lab = []
    for label in ("api_key", "password", "secret", "token", "passwd"):
        v = _r(rng, LOW, 6) + str(rng.randint(10, 99))
        lab += [" ".join(label) + " = " + v, " ".join(label) + ": " + v]
        enc = base64.b64encode((label + "=" + v).encode()).decode()
        lab += ["x" + enc, "ab" + enc, "ref-" + enc]
    out["spaced_or_b64_label"] = lab
    mails = []
    for _ in range(4):
        loc, dom = _mail(rng)
        e = loc + AT + dom + ".com"
        mails += ["<" + base64.b64encode(quote(e, safe="").encode()).decode() + ">",
                  "<" + base64.b64encode(quote(quote(e, safe=""), safe="").encode()).decode() + ">"]
    out["b64_pct_email_tag"] = mails
    return out


ROLE_WORDS = ("sales", "support", "info", "help", "contact", "admin", "hello", "billing", "orders", "order", "team",
              "press", "jobs", "careers", "service", "news", "mail", "office", "marketing", "media", "returns",
              "privacy", "legal", "security", "feedback", "partners", "shop", "store", "deals", "offers", "shipping",
              "sale", "us", "me", "you", "our", "we", "my", "your", "them", "hr", "accounts", "care", "events",
              "booking", "studio", "hi", "it", "they", "i")
NON_RETAILERS = ("acmepets", "brightpaws", "zbestmedia", "localshop", "kittyhaven", "smallbiz", "pawsome", "catcorner",
                 "shopnorth", "petsupply")
RETAILER_PROSE_WORDS = ("available", "sold", "found", "listed", "buy one", "find one", "purchased", "priced",
                        "stocked", "featured", "selling", "cheaper", "reviewed", "launched", "Available", "Sold")


def role_mailboxes() -> list:
    out = []
    for w in ROLE_WORDS:
        for d in NON_RETAILERS:
            out += [f"write {w} at {d}.com today", f"write {w} at {d} dot com today"]
    return out


def retailer_prose() -> list:
    hosts = ("amazon.com", "walmart.com", "target.com", "bestbuy.com", "etsy.com", "ebay.com", "chewy.com",
             "petco.com", "amazon.co.uk", "homedepot.com", "sephora.com", "costco.com")
    out = []
    for w in RETAILER_PROSE_WORDS:
        for h in hosts:
            out += [f"It is {w} at {h} for 12 dollars.", f"It is {w} at {h.replace('.', ' dot ')} now."]
    return out


def role_at_retailers() -> list:
    return [f"write {w} at {h}" for w in ROLE_WORDS for h in ("amazon.com", "walmart dot com", "target.com")]


# ------------------------------------------------------------------------------------------------ entry points

def _strict_everywhere(p: str) -> tuple:
    rec_v = sg.guard_record({"v": p})
    rec_k = sg.guard_record({p: "x"})
    ex = callum.excerpt("Answer: " + p + " end.", [])
    cl = callum.classify_citation("https://competitor.test/a", vw.DOMAIN, ["competitor.test"],
                                  {"state": "FAILED", "detail": "TOO_MANY_REDIRECTS", "redirects": 1,
                                   "hop_hosts": [p], "redirect_refused": False})
    logged = callum.log_row({"class": cl["class"], "chain": cl["chain"]}, None, None)
    return (sg.check(p) is not None,
            sg.redact_text(p) == (sg.WITHHELD, 1),
            isinstance(rec_v, dict) and rec_v.get("v") == sg.WITHHELD,
            isinstance(rec_k, dict) and p not in rec_k and all(str(k).startswith(sg.WITHHELD) for k in rec_k),
            ex.get("withheld") is not None and p not in json.dumps(ex),
            callum.field_check(p) is not None,
            p not in json.dumps(logged),
            callum.url_flag("https://site.test/u/" + quote(p, safe="")) is not None,
            quote(p, safe="") not in sg.safe_url("https://site.test/u/" + quote(p, safe="")),
            quote(p, safe="") not in json.dumps(callum.stored_url("https://site.test/u/" + quote(p, safe=""))))


STRICT_ENTRIES = ("check", "redact_text", "guard_record_value", "guard_record_key", "callum_excerpt",
                  "callum_field_check", "callum_classify_log", "url_flag", "safe_url", "stored_url")


def _client_everywhere(p: str) -> tuple:
    shown = callum._display("excerpt", p)
    u = "https://site.test/u/" + quote(p, safe="")
    cv_url, cv_kind = sg.client_view_url(u)
    return (bool(sg.client_view_findings(p)), bool(shown and shown["alert"]),
            cv_kind is not None and quote(p, safe="") not in (cv_url or ""))


CLIENT_ENTRIES = ("client_view_findings", "owner_display", "client_view_url")


def _misses(samples, judge, names) -> dict:
    out: dict = {}
    for p in samples:
        for name, ok in zip(names, judge(p)):
            if not ok:
                out[name] = out.get(name, 0) + 1
    return out


# ------------------------------------------------------------------------------------------------ recall corpus (client)

CV_RESIDUAL = frozenset(("aws_secret", "hex32_unlabelled", "hex64_unlabelled", "b64_unlabelled"))   # ADR 0051 d13
NUMBER_CLASSES = {"card_visa": (4, 4, 4, 4), "card_visa13": (4, 4, 5), "card_amex": (4, 6, 5), "card_mc": (4, 4, 4, 4),
                  "ssn": (3, 2, 4), "phone_us": (3, 3, 4)}


def _number_values(rng) -> dict:
    a, g, s = _ssn(rng)
    p = _nanp(rng)
    return {"card_visa": _card(rng, "4", 16), "card_visa13": _card(rng, "4", 13), "card_amex": _card(rng, "37", 15),
            "card_mc": _card(rng, "5" + str(rng.randint(1, 5)), 16), "ssn": a + g + s, "phone_us": "".join(p)}


def _groups(d: str, shape: tuple) -> list:
    out, i = [], 0
    for n in shape:
        out.append(d[i:i + n])
        i += n
    return out


def number_disguises(cls: str, d: str) -> dict:
    g = _groups(d, NUMBER_CLASSES[cls])
    out = {f"word_{f.replace(' ', '_')}": f" {f} ".join(g) for f in CLIENT_FILLERS}
    out["double_dash"] = "--".join(g)
    out["spaced_double_dash"] = " -- ".join(g)
    out["dashes"] = "-".join(g)
    out["dots"] = ".".join(g)
    if cls == "phone_us":
        out["paren"] = f"({g[0]}) {g[1]}-{g[2]}"
        out["paren_double_dash"] = f"({g[0]})--{g[1]}--{g[2]}"
    return out


def email_disguises(loc: str, dom: str) -> dict:
    sp = " ".join
    e = loc + AT + dom + ".com"
    return {"plain": e, "spelled_dot": f"{loc} at {dom} dot com", "spelled_literal": f"{loc} at {dom}.com",
            "bracket": f"{loc} [at] {dom} [dot] com", "letter_spaced": f"{sp(loc)}  a t  {sp(dom)} . c o m",
            "letter_spaced_dot": f"{sp(loc)} a t {sp(dom)} d o t c o m", "split_words": f"{loc} at {dom[:3]} {dom[3:]} DOT com",
            "md_tag": f"<**{loc}**{AT}{dom}\\.com>", "md_tag_spelled": f"<{loc} **at** {dom} **dot** com>",
            "b64_pct_tag": "<" + base64.b64encode(quote(e, safe="").encode()).decode() + ">",
            "misaligned_b64": "x" + base64.b64encode(e.encode()).decode(),
            "ref_b64": "ref-" + base64.b64encode(e.encode()).decode()}


def label_disguises(label: str, value: str) -> dict:
    enc = base64.b64encode((label + "=" + value).encode()).decode()
    out = {f"form_{k}": t for k, t in lc.label_forms(label, value).items()}
    if label.isascii():
        out.update({"letter_spaced": " ".join(label) + " = " + value, "letter_spaced_colon": " ".join(label) + ": " + value,
                    "b64": enc, "b64_x": "x" + enc, "b64_ab": "ab" + enc, "b64_ref": "ref-" + enc,
                    "md_escaped": _md_escape(label) + ": " + value, "md_tag": f"<**{label}**>: {value}"})
    return out


def recall_corpus(seed: int = SEED + 7) -> list:
    """(class, disguise, carrier) for every required client-view class x the strict lane's disguise set
    (``leak_corpus_v2``: every character disguise, every pair, every label form) and the disguises of this review."""
    rng = random.Random(seed)
    out = []
    for fam, (value, kind) in lc.values(rng).items():
        if kind == "labelled" or fam in CV_RESIDUAL:
            continue
        cls = "email" if fam == "email" else ("phone" if fam.startswith("phone") else fam)
        for dname, dv in lc.char_disguises(value).items():
            out.append((cls, dname, f"Z Best Media note: {dv} end."))
        for a, b in lc.PAIRS:
            out.append((cls, f"{a}+{b}", f"Z Best Media: {lc.char_disguises(lc.char_disguises(value)[a])[b]} ok"))
        if value.isascii() and "\n" not in value:
            out.append((cls, "md_escaped_tag", "see <" + _md_escape(value) + "> ok"))
    for cls, d in _number_values(rng).items():
        for dname, dv in number_disguises(cls, d).items():
            out.append((cls, dname, f"ref {dv} ok"))
    for _ in range(3):
        loc, dom = _mail(rng)
        for dname, dv in email_disguises(loc, dom).items():
            out.append(("email", dname, f"write to {dv} today"))
    for label in lc.LABELS:
        value = _r(rng, LOW, 6) + str(rng.randint(10, 99))
        for dname, dv in label_disguises(label, value).items():
            out.append(("label_value", dname, "Z Best Media. " + dv))
    return out


# ------------------------------------------------------------------------------------------------ MUST 1-3: strict lane

@pytest.mark.parametrize("cls", sorted(strict_cases()))
def test_strict_lane_withholds_every_new_shape_through_every_entry_point(cls):
    samples = strict_cases()[cls]
    assert len(samples) >= 12
    assert _misses(samples, _strict_everywhere, STRICT_ENTRIES) == {}, cls


@pytest.mark.parametrize("cls", sorted(client_cases()))
def test_strict_lane_also_withholds_the_client_view_cases(cls):
    assert _misses(client_cases()[cls], _strict_everywhere, STRICT_ENTRIES) == {}, cls


@pytest.mark.parametrize("cls", sorted({**strict_cases(), **client_cases()}))
def test_client_view_masks_every_new_shape_through_every_entry_point(cls):
    samples = {**strict_cases(), **client_cases()}[cls]
    shown = [p for p in samples if plain_spelled(p)]                  # decision 22 (c13529af): shown to the owner
    assert _misses([p for p in samples if not plain_spelled(p)], _client_everywhere, CLIENT_ENTRIES) == {}, cls
    assert sum(1 for p in shown if sg.client_view_findings(p)) == 0, cls


def test_only_real_tags_without_an_address_are_stripped():
    rng = random.Random(SEED + 2)
    loc, dom = _mail(rng)
    for kept in (f"<{loc}{AT}{dom}.com>", f"<{loc} at {dom} dot com>", f"<{loc}[at]{dom}.com>", "< 5 and > 3"):
        assert kept.replace(" ", "") in sg._strip_markup(kept).replace(" ", ""), len(kept)
    assert sg._strip_markup("<b>bold</b> <a href='x'>y</a> <!-- c --> <br/>") == "bold y  "
    assert sg._strip_md("**a** `b` ~~c~~ d\\_e") == "a b c d_e"


def test_the_markdown_only_view_is_a_view():
    rng = random.Random(SEED + 3)
    loc, dom = _mail(rng)
    t = f"<**{loc}**{AT}{dom}\\.com>"
    assert f"<{loc}{AT}{dom}.com>" in sg.views(t)


# ------------------------------------------------------------------------------------------------ MUST 4: retailer key

def test_role_mailboxes_on_other_domains_are_all_masked():
    # c13529af (decision 22): the plain "write sales at shop.com" form is shown; the spelled " dot " form stays masked
    texts = role_mailboxes()
    assert len(ROLE_WORDS) >= 40 and len(NON_RETAILERS) >= 10 and len(texts) >= 800
    dotted = [t for t in texts if " dot " in t]
    assert len(dotted) == len(texts) // 2
    assert sum(1 for t in dotted if "email" in sg.client_view_findings(t)) == len(dotted)
    assert sum(1 for t in texts if " dot " not in t and sg.client_view_findings(t)) == 0
    assert all(sg.check(t) is not None for t in texts)


def test_prose_words_before_another_domain_are_shown_by_owner_decision_08eea6e1():
    # review 08eea6e1 SHOULD 5 (owner decision): a prose-list word before the spelled " at " is prose for ANY domain
    texts = [f"It is {w} at {d}.com now." for w in RETAILER_PROSE_WORDS for d in NON_RETAILERS]
    assert sum(1 for t in texts if "email" in sg.client_view_findings(t)) == 0
    assert all(sg.check(t) is not None for t in texts)                 # the strict lane withholds them all


def test_shop_prose_with_retailer_domains_is_shown():
    # c13529af (decision 22): the plain form is shown; the spelled " dot " form is masked (it was shown until c13529af)
    texts = retailer_prose()
    assert len(texts) >= 300
    assert sum(1 for t in texts if " dot " not in t and sg.client_view_findings(t)) == 0
    assert sum(1 for t in texts if " dot " in t and "email" not in sg.client_view_findings(t)) == 0
    assert all(sg.check(t) is not None for t in texts)                 # the strict lane withholds them all


def test_role_words_before_retailer_domains_are_masked():
    # c13529af (decision 22): only the spelled " dot " form is masked now; the plain form is shown for every word
    texts = role_at_retailers()
    dotted = [t for t in texts if " dot " in t]
    assert dotted and sum(1 for t in dotted if "email" in sg.client_view_findings(t)) == len(dotted)
    assert sum(1 for t in texts if " dot " not in t and sg.client_view_findings(t)) == 0


def test_the_lists_are_closed_disjoint_and_documented():
    assert len(sg.CV_RETAILER_HOSTS) >= 100
    assert not sg.CV_PROSE_BEFORE_AT & sg.CV_ROLE_WORDS
    assert set(ROLE_WORDS) <= sg.CV_ROLE_WORDS
    for h in ("amazon.com", "amazon.co.uk", "walmart.com", "target.com", "etsy.com", "chewy.com", "bestbuy.com"):
        assert h in sg.CV_RETAILER_HOSTS, h


def test_mutation_role_words_added_back_is_caught(monkeypatch):
    monkeypatch.setattr(sg, "CV_PROSE_BEFORE_AT", sg.CV_PROSE_BEFORE_AT | sg.CV_ROLE_WORDS)
    texts = role_at_retailers()
    assert sum(1 for t in texts if "email" not in sg.client_view_findings(t)) > 0


def test_mutation_domain_check_removed_is_caught(monkeypatch):
    # c13529af (decision 22): the retailer check is INERT (no rule calls it); the plain form is shown for any domain
    # and the " dot " form masked for any domain, with or without it (a mutation bite until c13529af)
    texts = [f"Buy {w} at {d}.com now." for w in sorted(sg.CV_PRONOUNS_BEFORE_RETAILER) for d in NON_RETAILERS]
    dotted = [f"Buy {w} at {d} dot com now." for w in sorted(sg.CV_PRONOUNS_BEFORE_RETAILER) for d in NON_RETAILERS]
    for patch in (False, True):
        if patch:
            monkeypatch.setattr(sg, "_cv_retailer_after", lambda v, i: True)
        assert sum(1 for t in texts if sg.client_view_findings(t)) == 0
        assert sum(1 for t in dotted if "email" not in sg.client_view_findings(t)) == 0


def test_mutation_retailer_list_emptied_is_caught(monkeypatch):
    # c13529af (decision 22): the retailer list is INERT (a mutation bite until c13529af): emptying it changes nothing
    hosts = sorted({t.split(" at ")[1].split(" ")[0] for t in retailer_prose() if " dot " not in t})
    texts = [f"Buy {p} at {h} for 12 dollars." for p in ("it", "them", "It", "Them") for h in hosts]
    assert len(texts) >= 40 and sum(1 for t in texts if sg.client_view_findings(t)) == 0
    monkeypatch.setattr(sg, "CV_RETAILER_HOSTS", frozenset())
    assert sum(1 for t in texts if sg.client_view_findings(t)) == 0


# ------------------------------------------------------------------------------------------------ MUST 5: recall

def test_client_view_recall_is_complete_on_every_required_class_and_disguise():
    corpus = recall_corpus()
    assert len(corpus) >= 3000
    classes = {c for c, _d, _p in corpus}
    assert {"card_visa", "card_visa13", "card_amex", "ssn", "phone", "phone_us", "email", "label_value",
            "url_userinfo", "github_classic", "pem"} <= classes
    # c13529af (decision 22): the plain spelled form ("spelled_literal": "name at domain.com") is shown to the owner
    missed = [(c, d) for c, d, p in corpus if not sg.client_view_findings(p) and not plain_spelled(p)]
    assert missed == [], sorted(set(missed))[:20]
    assert all(not sg.client_view_findings(p) for _c, _d, p in corpus if plain_spelled(p))
    strict_missed = [(c, d) for c, d, p in corpus if sg.check(p) is None]
    assert strict_missed == [], sorted(set(strict_missed))[:20]


# ------------------------------------------------------------------------------------------------ mutation bite

def _old_bridge(monkeypatch):
    monkeypatch.setattr(sg, "WORD_BRIDGE_GROUP", (3, 4))
    monkeypatch.setattr(sg, "_WORD_BRIDGE", sg._bridge_rx(r"[^\W\d_]{1,%d}" % sg.WORD_BRIDGE_LETTERS))
    monkeypatch.setattr(sg, "_CV_WORD_BRIDGE", sg._bridge_rx(r"(?i:" + "|".join(sg.CV_FILLER_WORDS) + r")(?![^\W\d_])"))


BITES = {
    # name: (patch, probes, lane): removing the rule must make at least one probe leak in that lane
    "md_only_view": (lambda mp: mp.setattr(sg, "_strip_md", lambda t: t), ("strict", "md_tag_email"), "both"),
    "bridge_2_6": (_old_bridge, ("strict", "ssn_324_words"), "both"),
    "bridge_2_6_amex": (_old_bridge, ("strict", "amex_465_words"), "both"),
    "spaced_spelled": (lambda mp: mp.setattr(sg, "_spaced_spelled_email", lambda v: False),
                       ("strict", "spaced_spelled_email"), "both"),
    "cv_filler_bridge": (lambda mp: mp.setattr(sg, "_unbridged_fillers", lambda v, sep: v),
                         ("client", "word_bridged_numbers"), "client"),
    # c0c09bd1: the labelled-phone rule (MUST 8) also reads these probes; it is disabled with the rule under test
    "cv_double_dash": (lambda mp: (mp.setattr(sg, "_cv_labelled_phone", lambda m: False), mp.setattr(sg, "_CV_NANP", re.compile(
        r"(?<![0-9])(?:\+?1[ .\-]?)?(?:\(\d{3}\) ?\d{3}-\d{4}|\d{3}-\d{3}-\d{4}|\d{3}\.\d{3}\.\d{4})(?![0-9])"))),
        ("client", "double_dash"), "client"),
    "cv_nospace_label": (lambda mp: mp.setattr(sg, "_cv_label_nospace", lambda v: False),
                         ("client", "spaced_or_b64_label"), "client"),
    "decoded_pct_rounds": (lambda mp: mp.setattr(sg, "PERCENT_ROUNDS", 1), ("client", "b64_pct_email_tag"), "client"),
}


def _probes(where):
    lane, cls = where
    return (strict_cases() if lane == "strict" else client_cases())[cls]


@pytest.mark.parametrize("name", sorted(BITES))
def test_each_new_rule_is_load_bearing(monkeypatch, name):
    patch, where, lane = BITES[name]
    probes = _probes(where)
    cv_probes = [p for p in probes if not plain_spelled(p)]           # c13529af decision 22: plain form shown
    assert all(sg.check(p) is not None for p in probes) and all(sg.client_view_findings(p) for p in cv_probes), name
    patch(monkeypatch)
    strict_leaks = sum(1 for p in probes if sg.check(p) is None)
    client_leaks = sum(1 for p in cv_probes if not sg.client_view_findings(p))
    if lane in ("both", "strict"):
        assert strict_leaks > 0, (name, "strict: removing this rule broke nothing")
    if lane in ("both", "client"):
        assert client_leaks > 0, (name, "client: removing this rule broke nothing")


def test_the_spaced_label_rule_is_load_bearing_in_the_strict_lane(monkeypatch):
    rng = random.Random(SEED + 4)
    probes = [" ".join("DB_" + "PASS") + sep + _r(rng, LOW, 6) + "12" for sep in (" = ", ": ")]
    assert all(sg.check(p) is not None for p in probes)
    monkeypatch.setattr(sg, "_spaced_label", lambda v: False)
    assert any(sg.check(p) is None for p in probes)


def test_the_tag_keep_rule_is_load_bearing(monkeypatch):
    rng = random.Random(SEED + 5)
    loc, dom = _mail(rng)
    t = f"<{loc}{AT}{dom}.com>"
    assert loc in sg._strip_markup(t)
    monkeypatch.setattr(sg, "_TAG_KEEP", re.compile(r"(?!x)x"))
    assert loc not in sg._strip_markup(t)


# ------------------------------------------------------------------------------------------------ MUST 6: linear

_EMAIL_SPEC = re.compile(r"[a-z0-9._%+-]+\s*(?:@|\(at\)|\[at\])\s*[a-z0-9-]+"
                         r"(?:\s*(?:\.|\(dot\)|\[dot\])\s*[a-z0-9-]+)+", re.I)                                  # the pre-review (unanchored) prompt-guard pattern
_YEARS_SPEC = re.compile(r"(?<!\d)(?:19|20)\d\d\s*(?:-|/|\s)\s*(?:19|20)\d\d(?!\d)")


def _fuzz(rng, alphabet, n):
    return "".join(rng.choice(alphabet) for _ in range(rng.randint(0, n)))


def test_the_linear_prompt_guard_forms_decide_exactly_what_the_specification_decides():
    rng = random.Random(SEED + 6)
    pieces = ["a", "b", "K", ".", "-", "%", "+", "_", " ", "  ", "\t", AT, "(at)", "[at]", "(dot)", "[dot]", " at ",
              " dot ", "at", "dot", "listed", "x.", "1", "2020", "2021", "/", " - ", "\u212a", "\u00e9"]
    for _ in range(6000):
        t = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 14)))
        assert (prompt_guard._EMAIL.search(t) is not None) == (_EMAIL_SPEC.search(t) is not None), repr(t)
        spec = any(m.group("local").lower() not in prompt_guard._PROSE_BEFORE_AT
                   for m in prompt_guard._EMAIL_WORDS.finditer(t))
        assert prompt_guard._email_words(t) == spec, repr(t)
        assert [m.span() for m in prompt_guard._YEARS.finditer(t)] == [m.span() for m in _YEARS_SPEC.finditer(t)]
    for _ in range(2000):
        t = _fuzz(rng, "ab.-%+ @()[]tdo19 20", 40)
        assert (prompt_guard._EMAIL.search(t) is not None) == (_EMAIL_SPEC.search(t) is not None), repr(t)
        spec = any(m.group("local").lower() not in prompt_guard._PROSE_BEFORE_AT
                   for m in prompt_guard._EMAIL_WORDS.finditer(t))
        assert prompt_guard._email_words(t) == spec, repr(t)


def _timed(fn, *a):
    best = None
    for _ in range(2):
        t0 = time.perf_counter()
        fn(*a)
        d = time.perf_counter() - t0
        best = d if best is None else min(best, d)
    return best


EXCERPT_FAMILIES = {
    "letters": lambda n: ("Z" + LOW * (n // 26 + 1))[:n],
    "local_chars": lambda n: ("a.b%c+d-e_" * (n // 10 + 1))[:n],
    "dots": lambda n: ("a." * (n // 2 + 1))[:n],
    "brand_then_run": lambda n: ("Z Best Media " + "x" * n)[:n],
    "words_tail": lambda n: ("a.b at c dot d " * (n // 15 + 1))[:n],
    "year_spaces": lambda n: "2020" + " " * (n - 5) + "x",
}


@pytest.mark.parametrize("name", sorted(EXCERPT_FAMILIES))
def test_the_excerpt_path_is_linear_on_answers_without_whitespace(name):
    gen = EXCERPT_FAMILIES[name]
    brand = ["Z Best Media"]
    for fn in (lambda t: callum.excerpt(t, brand), lambda t: callum._excerpt_view_findings(t, brand),
               callum.field_check, prompt_guard.personal_data):
        t16, t64 = _timed(fn, gen(16 * 1024)), _timed(fn, gen(64 * 1024))
        assert t64 < 1.0, (name, t64)                                 # 200 ms measured bar; 1 s here for slow CI
        assert t64 < 8 * t16 + 0.05, (name, t16, t64)


def test_the_excerpt_window_is_capped(monkeypatch):
    t = "Z" + "x" * 64 * 1024
    _b, _s, w = callum._excerpt_window(t, [])
    margins = callum.EXCERPT_MARGIN + callum.EXCERPT_MARGIN_AFTER
    assert len(w) <= callum.EXCERPT_MAX + margins + 2 * callum.EXCERPT_WORD_MAX
    monkeypatch.setattr(callum, "EXCERPT_WORD_MAX", 10 ** 9)                   # mutation: no cap
    assert len(callum._excerpt_window(t, [])[2]) == len(t)


def test_a_key_cut_by_the_capped_window_edge_is_still_withheld():
    rng = random.Random(SEED + 8)
    key = _gh(rng)
    for pad in (0, 100, callum.EXCERPT_WORD_MAX - 10, 4 * callum.EXCERPT_WORD_MAX):
        t = "Z Best Media " + "x" * 150 + key + "y" * pad
        ex = callum.excerpt(t, ["Z Best Media"])
        assert ex["withheld"] is not None and key[:12] not in json.dumps(ex), pad


# ------------------------------------------------------------------------------------------------ the real service

class _Port:
    connected, metered, accepts_guard, name, attempts = True, False, True, "openai", 1

    def __init__(self, script):
        self.script, self.asked = list(script), 0

    def ask(self, prompt, guard=None, meter=None):
        self.asked += 1
        return self.script[(self.asked - 1) % len(self.script)]


def _service_values() -> dict:
    rng = random.Random(SEED + 9)
    loc, dom = _mail(rng)
    sl, sd = _mail(rng)
    a, g, s = _ssn(rng)
    d = _card(rng, "37", 15)
    p = _nanp(rng)
    k = _gh(rng)
    pw = _r(rng, LOW, 6) + "73"
    return {"md_email": f"<**{loc}**{AT}{dom}\\.com>", "md_email_local": loc,
            "spaced_email": " ".join(sl) + "  a t  " + " ".join(sd) + " . c o m", "spaced_local": sl,
            "ssn_words": f"{a} then {g} then {s}", "amex_words": f"{d[:4]} plus {d[4:10]} plus {d[10:]}",
            "phone_words": f"{p[0]} then {p[1]} then {p[2]}", "md_key": "<" + _md_escape(k) + ">", "key": k,
            "spaced_label": " ".join("api_key") + " = " + pw, "label_value": pw}


def _service_answers(v: dict) -> list:
    cited = "https://competitor.test/blog/guide"
    a = ProviderAnswer("ANSWER", f"Z Best Media support: write to {v['md_email']} or {v['spaced_email']}, ref "
                                 f"{v['ssn_words']}, card {v['amex_words']}, call {v['phone_words']} today.",
                       [cited], "m", "1", cite_from_text=False, provider="openai", asked_at="2026-10-09T18:00:00Z")
    a.citation_titles = {cited: "Z Best Media key " + v["md_key"]}
    a.citation_hints = {cited: "Z Best Media " + v["spaced_label"]}
    return [a]


def _secret_values(v: dict) -> dict:
    """The values that must never be written, in the forms ``test_vault_wiring.found`` searches."""
    return {k: x for k, x in v.items() if k in ("md_email_local", "spaced_local", "key", "label_value", "ssn_words",
                                               "amex_words", "phone_words")}


def test_a_real_citation_run_writes_none_of_the_new_shapes_and_the_owner_view_masks_them(tmp_path, caplog):
    v = _service_values()
    h = vw.harness(tmp_path)
    h.ports.engines["openai"] = _Port(_service_answers(v))
    h.ok(h.post("/tenants/zbm/domains", {"request_id": vw.rid(), "domains": [vw.DOMAIN]}, andre=True))
    run = vw.citation_run(h)
    assert run["status"] == "completed" and run["result"]["outcome"] == "OK"
    cid = run["citation_run_id"]
    leaks = vw.found(vw.written(h, caplog), _secret_values(v))
    assert leaks == [], leaks                                          # names only
    assert len(h.svc.vault) >= 1                                       # the full evidence went to the vault
    got = h.ok(h.get(f"/tenants/zbm/citation-runs/{cid}"))
    shown = vw.found(json.dumps(got, ensure_ascii=False), _secret_values(v))
    assert shown == [], shown
    ev = [r["evidence"] for r in got["result"]["facts"]["rows"] if r.get("evidence")]
    assert ev and all(e[k]["alert"] for e in ev for k in ("excerpt", "title", "hint") if e.get(k))


def _md_only_run(tmp_path, caplog) -> tuple:
    v = _service_values()
    cited = "https://competitor.test/blog/guide"
    a = ProviderAnswer("ANSWER", f"Z Best Media support: write to {v['md_email']} today.", [cited], "m", "1",
                       cite_from_text=False, provider="openai", asked_at="2026-10-09T18:00:00Z")
    h = vw.harness(tmp_path)
    h.ports.engines["openai"] = _Port([a])
    h.ok(h.post("/tenants/zbm/domains", {"request_id": vw.rid(), "domains": [vw.DOMAIN]}, andre=True))
    run = vw.citation_run(h)
    got = json.dumps(h.ok(h.get(f"/tenants/zbm/citation-runs/{run['citation_run_id']}")), ensure_ascii=False)
    vals = {"md_email_local": v["md_email_local"]}
    return vw.found(vw.written(h, caplog), vals), vw.found(got, vals)


def test_the_real_run_mutation_on_the_markdown_view_is_caught(tmp_path, caplog, monkeypatch):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    assert _md_only_run(tmp_path / "a", caplog) == ([], [])
    monkeypatch.setattr(sg, "_strip_md", lambda t: t)
    _written, shown = _md_only_run(tmp_path / "b", caplog)
    assert shown == ["md_email_local"]                       # the owner view shows it without the markdown view


# ------------------------------------------------------------------------------------------------ SHOULD 1-3, NIT

def test_an_idempotent_replay_builds_its_view_outside_the_service_lock(tmp_path, monkeypatch):
    v = vw.synthetic()
    h = vw.harness(tmp_path)
    vw.start(h, v)
    psid = h.ok(h.post("/tenants/zbm/prompt-sets", {"request_id": vw.rid(), "name": "core",
                                                    "brand_terms": ["Z Best Media"], "competitor_domains": [],
                                                    "prompts": ["who recovers marketplace revenue"],
                                                    "engines": ["openai"]}), 201)["prompt_set_id"]
    body = {"request_id": vw.rid(), "domain": vw.DOMAIN, "prompt_set_id": psid}
    first = h.ok(h.post("/tenants/zbm/citation-runs", body), 201)
    held = []
    real = svc_citations.CitationsMixin.citation_run_view

    def view(self, tid, cid, full=True):
        held.append(self.lock._is_owned())
        return real(self, tid, cid, full)
    monkeypatch.setattr(svc_citations.CitationsMixin, "citation_run_view", view)
    again = h.post("/tenants/zbm/citation-runs", body)
    assert again.status_code in (200, 201)
    assert again.json()["citation_run_id"] == first["citation_run_id"]
    assert held and not any(held)


def test_orphaned_vault_records_after_a_failed_commit_are_counted_in_health(tmp_path, monkeypatch):
    v = vw.synthetic()
    h = vw.harness(tmp_path)
    vw.start(h, v)
    real = type(h.svc)._commit

    def commit(self, kind, *a, **k):
        if kind == "citation_run_recorded":
            raise RuntimeError("forced")
        return real(self, kind, *a, **k)
    monkeypatch.setattr(type(h.svc), "_commit", commit)
    run = vw.citation_run(h)
    assert run["status"] == "interrupted" and run["interrupted_reason"] == "RUN_FAILED"
    n = len(h.svc.vault)
    assert n >= 1
    st = h.svc.health()["citation_vault"]
    assert st["orphaned_records"] == n and st["records"] == n and st["tail_repair"] is None


def _torn(h) -> str:
    p = h.svc.vault.path
    with open(p, "ab") as fh:
        fh.write(b'{"v":1,"id":"' + b"A" * 11)
    return p


def test_a_torn_final_vault_line_is_cut_at_start_and_recorded_with_counts_only(tmp_path):
    v = vw.synthetic()
    h = vw.harness(tmp_path)
    vw.start(h, v)
    a = vw.citation_run(h)
    n = len(h.svc.vault)
    p = _torn(h)
    h2 = h.restart()
    st = h2.svc.health()["citation_vault"]
    rep = dict(st["tail_repair"])
    assert len(rep.pop("repair_id")) == 32                             # review 08eea6e1: a hash id, no content
    assert rep == {"torn_lines": 1, "bytes_removed": 24, "records_kept": n, "recorded": True, "records_removed": 1,
                   "newline_added": 0, "quarantined": 1}
    assert st["records"] == n and open(p, "rb").read().endswith(b"\n")
    evs = h2.ledger.of_type("citation_vault_tail_repaired")
    assert len(evs) == 1 and set(evs[0]["_payload"]) >= {"torn_lines", "bytes_removed", "records_kept"}
    assert "AAAAAAAAAAA" not in json.dumps(evs)
    b = h2.ok(h2.get(f"/tenants/zbm/citation-runs/{a['citation_run_id']}"))
    assert b["result"] == a["result"]                                  # every record still reads


def test_any_other_vault_corruption_still_refuses_to_open(tmp_path):
    key = vault.ephemeral_vault_key()
    d = str(tmp_path / "data")
    cv = vault.CitationVault(d, key)
    for i in range(3):
        cv.put("zbm", {"i": i})
    with open(cv.path, "rb") as fh:
        lines = fh.read().split(b"\n")
    lines[1] = lines[1][:-5] + b"!!!!!"                             # a bad middle line
    with open(cv.path, "wb") as fh:
        fh.write(b"\n".join(lines[:-1]) + b"\n" + b'{"v":1')           # and a torn tail
    with pytest.raises(vault.VaultCorrupt):
        vault.CitationVault(d, key)
    with open(cv.path, "rb") as fh:
        assert fh.read().endswith(b'{"v":1')                          # nothing was cut


def test_a_batch_id_collision_writes_nothing(tmp_path, monkeypatch):
    key = vault.ephemeral_vault_key()
    cv = vault.CitationVault(str(tmp_path / "data"), key)
    monkeypatch.setattr(vault.secrets, "token_bytes", lambda n: b"\x07" * n)
    batch = vault.VaultBatch()
    with cv.collecting(batch):
        cv.put("zbm", {"a": 1})
        cv.put("zbm", {"a": 2})
    with pytest.raises(vault.VaultWriteError):
        cv.write_batch(batch)
    assert len(cv) == 0 and len(batch) == 0 and not __import__("os").path.exists(cv.path)


def test_a_batch_id_collision_closes_the_run_and_writes_nothing(tmp_path, monkeypatch, caplog):
    v = vw.synthetic()
    h = vw.harness(tmp_path)
    vw.start(h, v)
    psid = vw.prompt_set(h)                # (sink job D: the prompt set is a blob too; made before ids collide)
    monkeypatch.setattr(vault.secrets, "token_bytes", lambda n: b"\x07" * n)
    run = vw.citation_run(h, psid)
    assert run["status"] == "interrupted" and run["interrupted_reason"] == "RUN_FAILED"
    assert len(h.svc.vault) == 0 and h.ledger.of_type("citation_run_recorded") == []
    assert vw.found(vw.written(h, caplog), v) == []


# ------------------------------------------------------------------------------------------------ utility

def _shop_numbers(seed: int = SEED + 10) -> list:
    rng = random.Random(seed)
    t = ["Buy {a} for {p} dollars then {b} for {q}.", "Sales grew from {y1} to {y2} and {y3}.",
         "Sizes {a} {b} and {c} are in stock, {n} units left.", "Rated {s} by {n} buyers and {m} reviews in {y1}.",
         "Order {a} plus {b} more, then {c} next week.", "Between {y1} and {y2} we shipped {n} boxes.",
         "Pack of {a} at {p} each, or {b} for {q}.", "Models {n} and {m} launched in {y2} then {y3}."]
    return [t[i % len(t)].format(a=rng.randint(1, 99), b=rng.randint(10, 999), c=rng.randint(2, 60),
                                 p=rng.randint(5, 999), q=rng.randint(10, 9999), y1=rng.randint(1990, 2026),
                                 y2=rng.randint(1990, 2026), y3=rng.randint(1990, 2026), n=rng.randint(100, 99999),
                                 m=rng.randint(10, 9999), s=f"{rng.randint(1, 4)}.{rng.randint(0, 9)}")
            for i in range(400)]


# measured at this commit (the strict lane withholds number-dense prose more: 2-digit groups are now bridged)
STRICT_SHOP_NUMBERS_WITHHELD = 0.75


def test_utility_of_both_lanes_on_prices_years_and_quantities():
    texts = _shop_numbers()
    assert sum(1 for t in texts if sg.client_view_findings(t)) == 0              # client view: nothing masked
    withheld = sum(1 for t in texts if sg.check(t) is not None) / len(texts)
    assert withheld <= STRICT_SHOP_NUMBERS_WITHHELD, withheld


def test_utility_of_the_client_view_on_the_repo_corpora_stays_under_the_bars():
    ex = uc.excerpts()
    masked = 0
    for i, b, e in ex:
        raw = callum._raw_excerpt(uc.ANSWERS[i], [b] if b else [])
        found = callum._excerpt_view_findings(uc.ANSWERS[i], [b] if b else [])
        d = callum._display("excerpt", raw, e["withheld"], found)
        masked += bool(d and d["alert"])
    assert masked / len(ex) <= 0.05, masked
    assert sum(1 for _i, _b, e in ex if e["withheld"]) == 22                    # the strict lane: unchanged
    urls = [u for _, u in uc.urls()]
    assert sum(1 for u in urls if sg.client_view_url(u)[1]) / len(urls) <= 0.025
