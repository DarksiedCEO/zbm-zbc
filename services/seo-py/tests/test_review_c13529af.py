"""Review of c13529af (shared redaction core, both lanes; the citation vault's torn-tail repair; operator text in the
log): e-mail variants wrapped at a line break, words split by one line break (number words, filler words, the Spanish /
French spellings, bracketed "[a t]"), a JSON key wrapped inside its quotes, Unicode dashes folded to "-", a phone
number never read as an EAN / ISBN / date safe shape, base64 read from offsets 0-8 with both ends trimmed and format
characters removed before the readable test, prompt-set text refused when it fails the strict check (and the other
operator-text paths into the log), a faster client view, the two untested torn-tail branches, and the owner decision
(ADR 0051 decision 22): the client view shows the PLAIN spelled form "<word> at <domain>" and never reads a price as
an e-mail's local part.

Every sensitive-looking value here is GENERATED at run time from fragments (nothing credential-shaped or e-mail-shaped
is written in this file); failures report counts and case names only."""

from __future__ import annotations

import base64
import json
import os
import random
import re
import string
import time

import helpers
import pytest
import test_review_08eea6e1 as t8
import test_review_601e328a as r6
import test_vault_wiring as vw

import reasons
import serve
import vault
from agents import callum
from agents import secret_guard as sg
from helpers import rid
from ports import ProviderAnswer

SEED = 13529
AT = chr(64)
LOW = string.ascii_lowercase
ALNUM = string.ascii_letters + string.digits
NEVER = re.compile(r"(?!x)x")
NEVER1 = re.compile(r"(?!x)(x)")                                       # a never-matching pattern with one group
DIGIT_WORDS = t8.DIGIT_WORDS
DASHES = ("\u2010", "\u2011", "\u2012", "\u2013", "\u2014", "\u2015", "\u2212", "\ufe58", "\ufe63", "\uff0d")


def _r(rng, alphabet, n):
    return "".join(rng.choice(alphabet) for _ in range(n))


def _key(rng) -> str:
    return "gh" + "p_" + _r(rng, ALNUM, 36)


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode().rstrip("=")


def _split_word(w: str, k: int | None = None) -> str:
    k = len(w) // 2 if k is None else k
    return w[:k] + "\n" + w[k:]


def _split_words(d: str, at: tuple) -> str:
    """``d``'s digits as number words, the words at positions ``at`` split by one line break."""
    ws = [DIGIT_WORDS[c] for c in d]
    return " ".join(_split_word(w) if i in at else w for i, w in enumerate(ws))


def _ean(rng, first: str) -> str:
    """A 13-digit EAN-valid number that is not a card (no network prefix + Luhn)."""
    while True:
        body = first + _r(rng, string.digits, 11)
        for c in string.digits:
            n = body + c
            if sg._ean_ok(n) and not sg._cv_card(n):
                return n


# ------------------------------------------------------------------------------------------------ generated cases

def wrapped_cases(seed: int = SEED) -> dict:
    """MUST 1: name -> texts that BOTH lanes must withhold / mask (the required class in ``CLASS_OF``)."""
    rng = random.Random(seed)
    out: dict = {}
    for _ in range(3):
        loc, dom = r6._mail(rng)
        out.setdefault("wrap_hyphen", []).extend([
            f"{loc}-\nat-\n{dom}-\ndot-\ncom", f"{loc}\n-at-\n{dom}\n-dot-\ncom", f"{loc}-at\n-{dom}-dot\n-com",
            f"{loc}-\nat-\n{dom}.com", f"{loc} -at- {dom} -dot- com"])
        out.setdefault("wrap_underscore", []).extend([
            f"{loc}_\nat_\n{dom}_\ndot_\ncom", f"{loc}\n_at_\n{dom}\n_dot_\ncom", f"{loc}_\nat_\n{dom}.co.uk"])
        out.setdefault("wrap_at_sign", []).extend([f"{loc} at-\nsign {dom}.com", f"{loc} at-\nsign {dom} dot com"])
        out.setdefault("wrap_es_fr", []).extend([
            f"{loc} arro\nba {dom} pun\nto com", f"{loc} aro\nbase {dom} po\nint fr", f"{loc} arroba {dom} pun\nto com",
            f"{loc} ar\nroba {dom}.es"])
        out.setdefault("wrap_bracket_at", []).extend([
            f"{loc} [a\nt] {dom} [dot] com", f"{loc} (a\nt) {dom} (dot) com", f"{loc} [a t] {dom}.com",
            f"{loc}{{a\nt}}{dom}{{d\not}}com"])
    out = {k: [f"write to {x} today" for x in v] for k, v in out.items()}
    for _ in range(3):
        v16 = r6._card(rng, "4", 16)
        a15 = r6._card(rng, "37", 15)
        out.setdefault("wrap_number_words_card", []).extend([
            f"card {_split_words(v16, (5, 11))} ok", f"card {_split_words(a15, (4, 9))} ok",
            f"card {_split_words(v16, (2, 7, 12))} ok"])
        a, g, s = r6._ssn(rng)
        out.setdefault("wrap_number_words_ssn", []).extend([
            f"ssn {_split_words(a + g + s, (4,))} ok", f"SSN: {_split_words(a + g + s, (2, 6))} ok"])
        out.setdefault("wrap_ssn_filler", []).extend([
            f"ref {a} fol\nlowed by {g} fol\nlowed by {s} ok", f"ref {a} hyp\nhen {g} hyp\nhen {s} ok",
            f"ref {a} fol\nlowed by {g} th\nen {s} ok"])
        v = _r(rng, LOW, 6) + str(rng.randint(10, 99))
        out.setdefault("wrap_json_key", []).extend([
            '{"password\n": "%s"}' % v, '{"api_key\n": "%s"}' % v, '{"pass\nword": "%s"}' % v,
            '{"\ntoken": "%s"}' % v, "{'secret\n': '%s'}" % v])
        out.setdefault("wrap_spaced_label", []).extend([
            "p a s s\nw o r d = " + v, "t o k\ne n : " + v, "s e c r e t\n= " + v, "p a s s w o r d :\n" + v[:4]])
    return out


def dash_cases(seed: int = SEED + 1) -> dict:
    """MUST 2: Unicode dashes (typed, and the ``&hyphen;`` / ``&dash;`` entities) where a rule reads "-"."""
    rng = random.Random(seed)
    out: dict = {}
    for d in DASHES:
        loc, dom = r6._mail(rng)
        out.setdefault("dash_variant_email", []).append(f"write to {loc}{d}at{d}{dom}{d}dot{d}com today")
        p = r6._nanp(rng)
        out.setdefault("dash_phone", []).append(f"call {p[0]}{d}{p[1]}{d}{p[2]} now")
        a, g, s = r6._ssn(rng)
        out.setdefault("dash_ssn", []).append(f"ssn {a}{d}{g}{d}{s} ok")
        out.setdefault("dash_key", []).append("key sk" + d + "proj" + d + _r(rng, ALNUM, 40) + " end")
    for ent in ("&hyphen;", "&dash;", "&#8208;", "&#x2011;", "&minus;"):
        loc, dom = r6._mail(rng)
        out.setdefault("entity_dash_email", []).append(f"write to {loc}{ent}at{ent}{dom}{ent}dot{ent}com today")
        p = r6._nanp(rng)
        out.setdefault("entity_dash_phone", []).append(f"call {p[0]}{ent}{p[1]}{ent}{p[2]} now")
    return out


PHONE_WORDS = ("+", "+ ", "tel:", "tel: +", "phone ", "Phone: ", "call ", "Call us on ", "text ", "WhatsApp ",
               "Mobile: ", "phone number ")


def phone_ean_cases(seed: int = SEED + 2) -> dict:
    """MUST 3: a 13-digit EAN-valid number written as a phone number. Both lanes withhold the "+" forms as a phone;
    the strict lane withholds every form (9+ digits)."""
    rng = random.Random(seed)
    out: dict = {}
    for w in PHONE_WORDS:
        for first in "1479":
            n = _ean(rng, first)
            key = "ean_plus_phone" if w.strip().endswith("+") else "ean_word_phone"
            out.setdefault(key, []).append(f"Reach us: {w}{n} anytime")
    return out


def b64_cases(seed: int = SEED + 3) -> dict:
    """MUST 4: base64 payloads that need (a) no offset skip, (b) offset 8, (c) both-end trims of junk that DECODES,
    (d) format characters removed before the readable test."""
    rng = random.Random(seed)
    out: dict = {}
    junk = (_b64("Qzx"), _b64("zQx"), _b64("Kwz"))                    # aligned quanta decoding to 3 ASCII letters
    digit_junk = (_b64("123"), _b64("907"), _b64("555"))               # ... to 3 digits
    for i in range(6):
        v = _r(rng, LOW, 6) + str(rng.randint(10, 99))
        lab = base64.b64encode(("password=" + v).encode()).decode()
        out.setdefault("b64_glued_letters_prefix", []).append(f"see {junk[i % 3]}{lab} ok")
        a, g, s = r6._ssn(rng)
        ssn = f"SSN {a}-{g}-{s}"                                       # 15 bytes: aligned, so a suffix is its own quanta
        out.setdefault("b64_digit_suffix", []).append(f"see {base64.b64encode(ssn.encode()).decode()}{digit_junk[i % 3]} ok")
        card = r6._card(rng, "4", 16)
        pay = f"card {card} ok."                                        # 24 bytes: aligned
        out.setdefault("b64_digit_suffix", []).append(f"see {base64.b64encode(pay.encode()).decode()}{digit_junk[i % 3]} ok")
        loc, dom = r6._mail(rng)
        zw = "\u200b".join(loc + AT + dom + ".com")
        out.setdefault("b64_format_chars", []).append(f"see {_b64(zw)} ok")
        out.setdefault("b64_format_chars", []).append(f"see {_b64(chr(0x2060).join(_key(rng)))} ok")
    return out


def all_cases() -> dict:
    return {**wrapped_cases(), **dash_cases(), **phone_ean_cases(), **b64_cases()}


# the client view's REQUIRED class per case family (None: any finding)
CLASS_OF = {"wrap_hyphen": "email", "wrap_underscore": "email", "wrap_at_sign": "email", "wrap_es_fr": "email",
            "wrap_bracket_at": "email", "wrap_number_words_card": "card", "wrap_number_words_ssn": "ssn",
            "wrap_ssn_filler": "ssn", "wrap_json_key": "label_value", "wrap_spaced_label": "label_value",
            "dash_variant_email": "email", "dash_phone": "phone", "dash_ssn": "ssn", "dash_key": "strong_key",
            "entity_dash_email": "email", "entity_dash_phone": "phone", "ean_plus_phone": "phone",
            "ean_word_phone": None, "b64_glued_letters_prefix": "label_value", "b64_digit_suffix": None,
            "b64_format_chars": None}
STRICT_ONLY = frozenset(("ean_word_phone",))                           # no client-view class: a bare 13-digit run


# ------------------------------------------------------------------------------------------------ every entry point

@pytest.mark.parametrize("cls", sorted(all_cases()))
def test_strict_lane_withholds_every_case_through_every_entry_point(cls):
    samples = all_cases()[cls]
    assert len(samples) >= 5, cls
    assert r6._misses(samples, r6._strict_everywhere, r6.STRICT_ENTRIES) == {}, cls


@pytest.mark.parametrize("cls", sorted(set(all_cases()) - STRICT_ONLY))
def test_client_view_masks_every_case_through_every_entry_point(cls):
    samples = all_cases()[cls]
    assert r6._misses(samples, r6._client_everywhere, r6.CLIENT_ENTRIES) == {}, cls
    want = CLASS_OF[cls]
    if want is not None:
        assert sum(1 for p in samples if want not in sg.client_view_findings(p)) == 0, cls


def test_b64_digit_suffix_cases_carry_the_required_class():
    for p in b64_cases()["b64_digit_suffix"]:
        f = set(sg.client_view_findings(p))
        assert f & {"ssn", "card"}, "b64_digit_suffix"


# ------------------------------------------------------------------------------------------------ mutation bite

def _no_wrap(mp):
    mp.setattr(sg, "_WRAP_SEP", NEVER1)


def _no_split(mp):
    mp.setattr(sg, "_SPLIT_NUMBER", NEVER)
    mp.setattr(sg, "_SPLIT_FILLER", NEVER)
    mp.setattr(sg, "_SPLIT_VARIANT", NEVER)


def _old_bracket(mp):
    mp.setattr(sg, "_EMAIL_BRACKET", re.compile(r"(?i)[a-z0-9._%+\-] ?[\[\(\{<] ?(?:at|@) ?[\]\)\}>] ?[a-z0-9\-]{1,63}"))
    mp.setattr(sg, "_CV_BRACKET_AT", re.compile(r"(?i)[a-z0-9._%+\-] ?[\[\(\{<] ?(?:at|@) ?[\]\)\}>] ?"))


def _old_after(mp):
    mp.setattr(sg, "_CV_AFTER", re.compile(sg._CV_AFTER.pattern[2:]))


def _no_dashes(mp):
    mp.setattr(sg, "_DASHES", frozenset())
    mp.setattr(sg, "_CMAP", {})
    mp.setattr(sg, "_TABLE", sg._CharTable())


def _no_phone_context(mp):
    mp.setattr(sg, "_phone_context", lambda v, a: False)


def _old_b64(mp):
    """The 08eea6e1 decoder: offsets 0-7, offset k skipped when k-4 read, trims only on a decode error, no format
    character removal."""
    mp.setattr(sg, "B64_OFFSETS", 8)

    def printable(b):
        for lead in range(sg.DECODE_TRIM + 1):
            seg = b[lead:] if lead else b
            try:
                s = seg.decode("utf-8")
            except UnicodeDecodeError as e:
                if e.start < len(seg) - sg.DECODE_TRIM:
                    if e.start >= sg.DECODE_TRIM + 1 - lead:
                        return None
                    continue
                try:
                    s = seg[:e.start].decode("utf-8")
                except UnicodeDecodeError:
                    continue
            ok = sum(1 for c in s if c.isprintable() or c in "\n\t\r")
            if s and ok >= 0.85 * len(s):
                return s
        return None

    def runs(t):
        out, n = [], 0
        for rx in (sg._HEX_RUN, sg._B64_RUN):
            for m in rx.finditer(t):
                n += 1
                if n > sg.DECODE_RUNS_MAX:
                    return out
                run = m.group(0)[:sg.DECODE_RUN_MAX]
                if rx is sg._HEX_RUN:
                    continue
                done = set()
                for k in sg._b64_starts(run):
                    if 4 <= k < 8 and k - 4 in done:
                        continue
                    try:
                        b = sg._b64_decode(run[k:])
                    except ValueError:
                        continue
                    s = printable(b)
                    if s:
                        done.add(k)
                        out.append(sg._clean(s))
        return out
    mp.setattr(sg, "_decoded_runs", runs)


BITES = {
    # name: (patch, case class, lanes that must leak once the rule is removed)
    "wrap_hyphen": (_no_wrap, "wrap_hyphen", ("strict", "client")),
    "wrap_underscore": (_no_wrap, "wrap_underscore", ("strict", "client")),
    "wrap_at_sign": (_no_wrap, "wrap_at_sign", ("strict", "client")),
    "split_es_fr": (_no_split, "wrap_es_fr", ("strict", "client")),
    "split_number_words_card": (_no_split, "wrap_number_words_card", ("strict", "client")),
    "split_number_words_ssn": (_no_split, "wrap_number_words_ssn", ("strict", "client")),
    "split_filler_ssn": (_no_split, "wrap_ssn_filler", ("strict", "client")),
    "bracket_a_t": (_old_bracket, "wrap_bracket_at", ("strict", "client")),
    "json_key_quote": (_old_after, "wrap_json_key", ("client",)),
    "dashes_email": (_no_dashes, "dash_variant_email", ("strict", "client")),
    "dashes_phone": (_no_dashes, "dash_phone", ("client",)),
    "dashes_key": (_no_dashes, "dash_key", ("client",)),
    "dashes_entity_email": (_no_dashes, "entity_dash_email", ("strict", "client")),
    "dashes_entity_phone": (_no_dashes, "entity_dash_phone", ("client",)),
    "phone_not_ean_plus": (_no_phone_context, "ean_plus_phone", ("strict", "client")),
    "phone_not_ean_word": (_no_phone_context, "ean_word_phone", ("strict",)),
    "b64_glued_prefix": (_old_b64, "b64_glued_letters_prefix", ("client",)),
    "b64_digit_suffix": (_old_b64, "b64_digit_suffix", ("client",)),
    "b64_format_chars": (_old_b64, "b64_format_chars", ("client",)),   # strict: the run is a random token anyway
}


@pytest.mark.parametrize("name", sorted(BITES))
def test_each_new_rule_is_load_bearing(monkeypatch, name):
    patch, cls, lanes = BITES[name]
    probes = all_cases()[cls]
    want = CLASS_OF.get(cls)
    assert all(sg.check(p) is not None for p in probes), name

    def leaks():
        return sum(1 for p in probes if (not sg.client_view_findings(p) if want is None
                                         else want not in sg.client_view_findings(p)))
    if "client" in lanes:
        assert leaks() == 0, name
    patch(monkeypatch)
    if "strict" in lanes:
        assert sum(1 for p in probes if sg.check(p) is None) > 0, (name, "strict: removing this rule broke nothing")
    if "client" in lanes:
        assert leaks() > 0, (name, "client: removing this rule broke nothing")


def test_each_b64_part_is_load_bearing(monkeypatch):
    """(a) the offset skip removed, (b) offset 8, (c) trims always tried, (d) format characters removed first: each
    part, removed alone, lets a case through."""
    c = b64_cases()
    assert all(sg.client_view_findings(p) for k in c for p in c[k])
    # (c) only: no trims at all (the windows are the untrimmed decode)
    monkeypatch.setattr(sg, "DECODE_TRIM", 0)
    assert sum(1 for p in c["b64_digit_suffix"] if not {"ssn", "card"} & set(sg.client_view_findings(p))) > 0
    monkeypatch.undo()
    # (d) only: format characters kept (counted as unreadable)
    monkeypatch.setattr(sg, "_visible", lambda s: s)
    assert sum(1 for p in c["b64_format_chars"] if not sg.client_view_findings(p)) > 0
    monkeypatch.undo()
    # (b) only, read without trims: an 8-character aligned prefix is read from offset 8 alone
    monkeypatch.setattr(sg, "DECODE_TRIM", 0)
    rng = random.Random(SEED + 9)
    probes = [f"see {_b64('Qzx')}{_b64('zQx')}{base64.b64encode(('password=' + _r(rng, LOW, 6) + '12').encode()).decode()}"
              for _ in range(3)]
    assert all("label_value" in sg.client_view_findings(p) for p in probes)
    monkeypatch.setattr(sg, "B64_OFFSETS", 8)
    assert sum(1 for p in probes if "label_value" not in sg.client_view_findings(p)) > 0


def test_b64_decoding_stays_bounded():
    run = "A" * 64
    assert len(sg._b64_starts(run)) == sg.B64_OFFSETS == 9
    assert sg._b64_starts("-".join(["AAAA"] * 40))[:9] == list(range(9))
    assert len(sg._b64_starts("-".join(["AAAA"] * 40))) <= sg.B64_OFFSETS + sg.DECODE_BOUNDARIES_MAX
    # one edge view per run, at most DECODE_EDGES_MAX pieces; a long readable payload is read once in full
    payload = base64.b64encode(("the quick brown fox jumps over the lazy dog " * 60).encode()).decode()
    out = sg._decoded_runs("see " + payload + " ok")
    assert sum(1 for v in out if len(v) > 1000 and sg._EDGE_JOIN not in v) == 1
    edges = [v for v in out if sg._EDGE_JOIN in v]
    assert len(edges) <= 1 and all(v.count(sg._EDGE_JOIN) < sg.DECODE_EDGES_MAX for v in edges)
    assert sg._printable(b"\xff\xfe\xfd" + b"x" * 40 + b"\xff\xfe\xfd") == "x" * 40
    assert sg._printable(b"\xff" * (sg.DECODE_TRIM + 1) + b"x" * 40) is None     # c0c09bd1 MUST 4: TRIM is 6
    assert sg._printable(b"x" * 20 + b"\xff" + b"x" * 20) is None


def test_the_window_split_reads_exactly_what_decoding_every_window_reads():
    rng = random.Random(SEED + 10)
    pool = [b"\xff", b"\xc3\xa9", b"\xe2\x80\x8b", b"\xf0\x9f\x98\x80", b"\x01", b"a", b"Z", b" ", b"\x80"]
    for _ in range(3000):
        b = b"".join(rng.choice(pool) for _ in range(rng.randint(0, 40)))
        want = []
        tr = sg.DECODE_TRIM
        for lead in range(tr + 1):
            for tail in range(tr + 1):
                seg = b[lead:len(b) - tail] if len(b) - tail - lead > 0 else b""
                try:
                    s = sg._visible(seg.decode("utf-8"))
                except UnicodeDecodeError:
                    continue
                if s and sg._unreadable(s) <= 0.15 * len(s):
                    want.append((lead, tail, s))
        assert sg._windows(b) == want


# ------------------------------------------------------------------------------------------------ MUST 3 details

def test_a_safe_shape_after_a_phone_word_is_read_as_digits_and_otherwise_kept():
    rng = random.Random(SEED + 11)
    n = _ean(rng, "4")
    assert sg.check(f"Barcode {n} on the box") is None                   # the EAN shape still applies
    assert sg.check(f"phone {n}") == "e_digits" and sg.check(f"+{n}") == "e_digits"
    isbn = "978" + _r(rng, string.digits, 9)
    isbn += next(c for c in string.digits if sg._ean_ok(isbn + c))
    assert sg.check(f"ISBN {isbn}") is None and sg.check(f"text {isbn}") == "e_digits"
    far = f"phone {'x' * 20} {n}"                                       # the phone word beyond the window
    assert sg.check(far) is None
    assert "phone" in sg.client_view_findings(f"+{n}") and "phone" not in sg.client_view_findings(f"Barcode {n}")


# ------------------------------------------------------------------------------------------------ UTILITY (decision 22)

def _plain_forms(rng) -> list:
    out = []
    for _ in range(20):
        loc, dom = r6._mail(rng)
        out += [f"Visit us at {dom}.com today.", f"Get yours at {dom}.co.uk now.", f"Our team at {dom}.com can help.",
                f"Write {loc} at {dom}.com today.", f"{loc.capitalize()} at {dom}.io replied."]
    return out


def _still_masked(rng) -> list:
    out = []
    for _ in range(10):
        loc, dom = r6._mail(rng)
        out += [f"write {loc} at {dom} dot com", f"write {loc}{AT}{dom}.com", f"write {loc} {AT} {dom}.com",
                f"write {loc} [at] {dom}.com", f"write {loc} (at) {dom} (dot) com", f"write {loc} at {dom}[.]com",
                f"write {loc}-at-{dom}.com", f"write {loc}_at_{dom}.com", f"write {loc}AT{dom}DOTcom",
                f"write {loc} at sign {dom}.com", f"write {loc} arroba {dom}.com", f"write {loc} arobase {dom}.fr",
                f"write {' '.join(loc)} at {dom}.com", f"write {loc}{rng.randint(1, 99)} at {dom}.com",
                f"write {loc}.{loc[:3]} at {dom}.com", f"write {loc}_{loc[:3]} at {dom}.com",
                f"write {loc}+x at {dom}.com", f"write {loc}-{loc[:3]} at {dom}.com", f"write {loc} at {dom} . com",
                f"write {loc} at {dom}.com[.]uk"]
    return out


def _prices(rng) -> list:
    out = []
    for _ in range(20):
        dom = r6._mail(rng)[1]
        p = f"{rng.randint(5, 999)}.{rng.randint(10, 99)}"
        out += [f"It costs ${p} at {dom}.com.", f"The bundle is {p} at {dom} dot com.", f"Grab 2 at {dom}.com.",
                f"Now {p}{AT}{dom}.com only.", f"Price: $1,{rng.randint(100, 999)}.99 at {dom}.com."]
    return out


def test_the_plain_spelled_form_is_shown_and_every_other_form_stays_masked():
    rng = random.Random(SEED + 12)
    plain, masked = _plain_forms(rng), _still_masked(rng)
    assert sum(1 for t in plain if sg.client_view_findings(t)) == 0
    assert sum(1 for t in masked if "email" not in sg.client_view_findings(t)) == 0
    assert all(sg.check(t) == "f_email" for t in plain + masked)         # the strict lane withholds every one


def test_a_price_is_never_an_e_mail_local_part_in_the_client_view():
    texts = _prices(random.Random(SEED + 13))
    # c0c09bd1 MUST 5 (tightened): only a REAL price (currency sign and / or two decimals) before the PLAIN spelled
    # " at " is exempt; a price before a spelled "dot" domain, a bare count ("2 at") and "@" are masked
    shown = [t for t in texts if "$" in t and " dot " not in t]
    masked = [t for t in texts if t not in shown]
    assert sum(1 for t in shown if sg.client_view_findings(t)) == 0
    assert sum(1 for t in masked if "email" not in sg.client_view_findings(t)) == 0
    assert all(sg.check(t) is not None for t in texts)                   # the strict lane may keep withholding


def test_the_plain_form_rule_is_load_bearing(monkeypatch):
    rng = random.Random(SEED + 14)
    plain, prices, masked = _plain_forms(rng), _prices(rng), _still_masked(rng)
    monkeypatch.setattr(sg, "_cv_plain_at", lambda v: v)                # mutation: the decision undone
    assert sum(1 for t in plain if sg.client_view_findings(t)) == len(plain)
    assert sum(1 for t in prices if sg.client_view_findings(t)) > 0
    monkeypatch.undo()
    monkeypatch.setattr(sg, "_CV_WORD", re.compile(r"[A-Za-z0-9._+\-]+"))   # mutation: any local part read as a word
    assert sum(1 for t in masked if "email" not in sg.client_view_findings(t)) > 0
    monkeypatch.undo()
    monkeypatch.setattr(sg, "_CV_HOST_GOES_ON", NEVER)                  # mutation: "x at y.com[.]uk" read as plain
    assert sum(1 for t in masked if "email" not in sg.client_view_findings(t)) > 0


def test_the_retailer_prose_and_role_lists_are_inert(monkeypatch):
    rng = random.Random(SEED + 15)
    texts = _plain_forms(rng) + _still_masked(rng) + _prices(rng)
    before = [sg.client_view_findings(t) for t in texts]
    for attr in ("CV_RETAILER_HOSTS", "CV_PROSE_BEFORE_AT", "CV_ROLE_WORDS", "CV_PRONOUNS_BEFORE_RETAILER"):
        monkeypatch.setattr(sg, attr, frozenset())
    monkeypatch.setattr(sg, "_cv_unprose_at", lambda v: (_ for _ in ()).throw(AssertionError("called")))
    assert [sg.client_view_findings(t) for t in texts] == before


# the e-commerce utility corpus (generated; business phones counted apart)
BRANDS = ("brightpaws", "acmepets", "kittyhaven", "pawsome", "catcorner", "furfriends", "sphynxwear", "purrfectfit",
          "tailwag", "zbestmedia", "glowkit", "maplehome", "rivergear", "sunnyside")
RETAILERS = ("amazon.com", "walmart.com", "target.com", "chewy.com", "petco.com", "etsy.com", "bestbuy.com",
             "amazon.co.uk", "costco.com", "homedepot.com", "ebay.com", "wayfair.com")
PRODUCTS = ("cat tree", "harness kit", "heated bed", "water fountain", "litter mat", "travel carrier", "lint roller",
            "scratching post", "feeder bowl", "grooming glove")
TEMPLATES = (
    "Visit us at {b}.com for the full {p} lineup.", "Get yours at {b}.com before {day}.",
    "Our team at {b}.com ships orders within {n} days.", "The {p} costs ${pr} at {r}.",
    "The {p} bundle is ${pr} at {b}.co.", "You can buy it at {r} or {r2}.",
    "Customers at {b}.com rate it {rt} out of 5.", "It is available at {r} in {color}.",
    "Delivery takes {n} days at {b}.com; returns are free for {m} weeks.", "Prices start at ${pr} for the {size} size.",
    "SKU {sku} is back in stock at {b}.com.", "Model {model} costs ${pr} and is the best value.",
    "Order model {model} today; it ships in {n}-{m} business days.", "Save {pct}% with code {code} at checkout.",
    "The {p} weighs {w} lbs and measures {a} x {c} inches.", "Rated {rt} stars by {k} reviewers on {r}.",
    "Free shipping on orders over ${pr} at {b}.com.", "Use code {code} at {where} for {pct}% off.",
    "In {year}, {B} sold {k} units at {r}.", "Restocks arrive every {n} weeks at {r}.",
    "Questions? Our support team at {b}.com answers within {n} hours.", "Shoppers at {r} love the {p}.",
    "The {p} is {pct}% off at {r} this week.", "Buy two at {b}.com and save ${n}.",
    "Members at {b}.com get early access every {day}.", "It sold out in {n} days at {r} last {month}.",
    "Pick it up at {r} or order at {b}.com.", "The {p} (SKU {sku}) ships from {b}.com in {n} days.",
    "Compare the {model} at {store} with the {model2}.", "Fans at {b}.com posted {k} photos of the {p}.",
)
STORE_NAMES = ("Best Buy", "Target", "Walmart", "Costco", "Petco", "Home Depot")
PHONE_TEMPLATES = ("Call {B} at ({a}) {b3}-{c4} for wholesale orders.", "Questions? Phone us at {a}-{b3}-{c4}.")


def shop_corpus(seed: int = SEED + 16) -> tuple:
    """(answers, phone answers): generated e-commerce answer texts, each naming a brand (so the excerpt is the one a
    row keeps); the business-phone answers are apart (the client view masks a phone: a required class)."""
    r = random.Random(seed)
    out = []
    for i in range(480):
        b = r.choice(BRANDS)
        # a code / model number directly before " at <domain>" is masked by decision 22 (a local part with a digit):
        # those two templates alternate between a domain and the ordinary wording ("at checkout", "at Best Buy")
        alt = (i // len(TEMPLATES)) % 2 == 0
        t = TEMPLATES[i % len(TEMPLATES)].format(
            b=b, B=b.capitalize(), p=r.choice(PRODUCTS), day=r.choice(("Friday", "Monday", "the holidays")),
            n=r.randint(2, 9), m=r.randint(2, 6), pr=f"{r.randint(5, 399)}.{r.randint(10, 99)}", r=r.choice(RETAILERS),
            r2=r.choice(RETAILERS), rt=r.choice(("4.5", "4.8", "4.2")), color=r.choice(("grey", "sand", "black")),
            size=r.choice(("small", "large")), sku=f"{r.choice(('PA', 'KT', 'GH'))}-{r.randint(1000, 9999)}",
            model=f"{r.choice(('X', 'K', 'Z'))}{r.randint(100, 990)}", model2=f"{r.choice(('X', 'K'))}{r.randint(100, 990)}",
            pct=r.choice((10, 15, 20, 25)), code=r.choice(("SAVE10", "PURR15", "WELCOME20")), w=r.randint(2, 30),
            a=r.randint(10, 40), c=r.randint(10, 40), k=r.randint(20, 900), year=r.randint(2021, 2026),
            month=r.choice(("spring", "summer", "winter")),
            where=f"{b}.com" if alt else "checkout", store=r.choice(RETAILERS) if alt else r.choice(STORE_NAMES))
        out.append(f"{b.capitalize()} answer: {t} Many buyers agree.")
    phones = []
    for i in range(40):
        b = r.choice(BRANDS)
        p = r6._nanp(r)
        phones.append(f"{b.capitalize()} answer: " + PHONE_TEMPLATES[i % 2].format(B=b.capitalize(), a=p[0], b3=p[1],
                                                                                   c4=p[2]))
    return out, phones


def _excerpt_masked(answer: str) -> bool:
    term = answer.split(" ", 1)[0]
    e = callum.excerpt(answer, [term])
    d = callum._display("excerpt", callum._raw_excerpt(answer, [term]), e["withheld"],
                        callum._excerpt_view_findings(answer, [term]))
    return bool(d and d["alert"])


CV_UTILITY_BAR = 0.05


def test_client_view_utility_on_the_generated_shop_corpus_is_under_the_bar():
    answers, phones = shop_corpus()
    assert len(answers) >= 460
    masked = sum(1 for a in answers if _excerpt_masked(a))
    assert masked / len(answers) <= CV_UTILITY_BAR, masked
    assert sum(1 for a in phones if _excerpt_masked(a)) == len(phones)   # business phones: masked (a required class)


# ------------------------------------------------------------------------------------------------ MUST 6: timing

ADVERSARIAL = {
    "pct_b_arroba": "%20</b>arroba ", "pct_b_at": "%20</b>at ", "pctAB_b_at": "%AB</b>at ", "nbsp_b_at": "&nbsp;</b>at x ",
    "pct20_at_dom": "%20</b>us at shop.com ", "pct20_arroba_dom": "%20</b>x arroba shop punto com ",
    "wrap_hyphen": "x-\nat-\ny ", "split_words": "fo ur on e ", "dashes": "word\u2014word\u2010x\u2212 ",
    "filler_split": "12 fol lowed by 34 ", "price_at": "$12.99 at ", "word_at": "us at ",
}


def _timed(fn, arg) -> float:
    best = None
    for _ in range(2):
        t = time.perf_counter()
        fn(arg)
        d = time.perf_counter() - t
        best = d if best is None else min(best, d)
    return best


@pytest.mark.parametrize("name", sorted(ADVERSARIAL))
def test_both_lanes_stay_linear_on_the_adversarial_repeats(name):
    unit = ADVERSARIAL[name]
    for fn in (sg.check, sg.client_view_findings):
        t16 = _timed(fn, t8._fill(unit, 16 * 1024))
        t64 = _timed(fn, t8._fill(unit, 64 * 1024))
        assert t64 < 1.0, (name, fn.__name__)                        # 200 / 150 ms measured bars; 1 s here for CI
        assert t64 < 8 * t16 + 0.05, (name, fn.__name__, t16, t64)


def test_the_client_view_memo_is_per_call(monkeypatch):
    calls = []
    real = sg._prefix_found
    monkeypatch.setattr(sg, "_prefix_found", lambda s, nospace=False: calls.append(s) or real(s, nospace))
    text = t8._fill("%20</b>at ", 8 * 1024)
    sg.client_view_findings(text)
    assert len(calls) == len(set(calls))                              # every distinct string scanned once
    n = len(calls)
    sg.client_view_findings(text)
    assert len(calls) == 2 * n and getattr(sg._CV_MEMO, "prefix", None) is None   # nothing kept between calls


# ------------------------------------------------------------------------------------------------ MUST 5: operator text

def _secret_forms(rng) -> dict:
    return {"key": _key(rng), "stripe": "sk" + "_live_" + _r(rng, ALNUM, 24),
            "aws": "AK" + "IA" + _r(rng, string.ascii_uppercase + "234567", 16),
            "hex": "".join(rng.choice("0123456789abcdef") for _ in range(40))}


def _ps_body(**over) -> dict:
    body = {"request_id": rid(), "name": "core", "brand_terms": ["Z Best Media"], "competitor_domains": ["competitor.test"],
            "prompts": ["who recovers marketplace revenue"], "engines": ["openai"]}
    body.update(over)
    return body


def _no_trace(h, values: dict) -> list:
    blob = vw.written(h)
    return vw.found(blob, values)


def test_prompt_set_text_failing_the_strict_check_is_refused_and_nothing_is_written(tmp_path):
    rng = random.Random(SEED + 20)
    vals = _secret_forms(rng)
    h = vw.harness(tmp_path)
    vw.start(h, vw.synthetic())
    n_log = len(h.svc.log)
    k = vals["key"]
    bodies = [
        _ps_body(prompts=[f"best tools for sellers {k}"]),
        _ps_body(prompts=["who recovers revenue", f"token {vals['stripe']} please"]),
        _ps_body(brand_terms=["Z Best Media"], brand_term_rules=[{"term": "Z Best Media", "context_terms": [k[:40]]}]),
        _ps_body(brand_term_rules=[{"term": "Z Best Media", "negative_terms": [vals["aws"]]}]),
        _ps_body(brand_terms=["Z Best Media", vals["aws"]]),
        _ps_body(name="core-" + vals["hex"]),
    ]
    for b in bodies:
        h.refused(h.post("/tenants/zbm/prompt-sets", b), 422, "PROMPT_SECRET")
    assert len(h.svc.log) == n_log and h.svc.prompt_sets == {}
    # c0c09bd1 (design change, Option B): personal data in a context term is no longer refused: it is vaulted
    loc, dom = r6._mail(rng)
    h.ok(h.post("/tenants/zbm/prompt-sets", _ps_body(brand_term_rules=[
        {"term": "Z Best Media", "context_terms": [f"{loc} at {dom} dot com"]}])), 201)
    assert _no_trace(h, {**vals, "loc": loc}) == []
    h.ok(h.post("/tenants/zbm/prompt-sets", _ps_body(name="core2", brand_term_rules=[
        {"term": "Z Best Media", "context_terms": ["marketplace"]}])), 201)    # an ordinary set is created


def test_the_prompt_set_strict_check_is_load_bearing(tmp_path, monkeypatch):
    import svc_audits
    rng = random.Random(SEED + 21)
    k = _key(rng)
    h = vw.harness(tmp_path)
    vw.start(h, vw.synthetic())
    body = _ps_body(brand_term_rules=[{"term": "Z Best Media", "context_terms": [k[:40]]}])
    h.refused(h.post("/tenants/zbm/prompt-sets", body), 422, "PROMPT_SECRET")
    monkeypatch.setattr(svc_audits, "_strict_prompt_set", lambda body: None)   # mutation: the check removed
    # c0c09bd1: the key is then accepted (and would be sent to an engine); the log still holds only its stub
    h.ok(h.post("/tenants/zbm/prompt-sets", {**body, "request_id": rid()}), 201)
    assert _no_trace(h, {"key": k[:40]}) == []


def test_the_mnc_priority_prompt_goes_through_the_strict_check(monkeypatch):
    rng = random.Random(SEED + 22)
    k = _key(rng)
    ps = {"prompt_set_id": "seo-pst-" + "0" * 40, "version": 1, "sha256": "0" * 64,
          "prompts": [f"compare tools {k}", "who recovers marketplace revenue"]}
    per = {("openai", i): {"usable": 3, "mnc": 2, "cited_instead": {}, "evidence": [], "versions": {"m"}} for i in (0, 1)}
    items = {it["prompt_index"]: it for it in callum.mnc_priority(per, ps, "run", "site.test", "t")["items"]}
    assert items[0]["prompt"] is None and items[1]["prompt"] is not None
    monkeypatch.setattr(callum.secret_guard, "check", lambda t, waive=frozenset(): None)   # mutation
    items = {it["prompt_index"]: it for it in callum.mnc_priority(per, ps, "run", "site.test", "t")["items"]}
    assert items[0]["prompt"] is not None


def test_other_operator_text_paths_refuse_text_failing_the_strict_check(tmp_path):
    rng = random.Random(SEED + 23)
    vals = _secret_forms(rng)
    h = vw.harness(tmp_path)
    vw.start(h, vw.synthetic())
    n_log = len(h.svc.log)
    k = vals["key"]
    # an audit / schedule path (written to the log in plaintext)
    h.refused(h.post("/tenants/zbm/audits", {"request_id": rid(), "domain": vw.DOMAIN, "paths": [f"/reset/{k}"]}),
              422, "PATH_WITHHELD")
    h.refused(h.post("/tenants/zbm/audits", {"request_id": rid(), "domain": vw.DOMAIN,
                                             "paths": [f"/p?token={vals['stripe']}"]}), 422, "PATH_WITHHELD")
    h.refused(h.post("/tenants/zbm/schedules", {"request_id": rid(), "domain": vw.DOMAIN, "paths": [f"/u/{k}"],
                                                "every_days": 7}), 422, "PATH_WITHHELD")
    # an entity field and its provenance
    body = {"request_id": rid(), "field": "name", "value": "Z Best Media", "source": "founder_statement",
            "provenance": f"from the dashboard {k}", "authority": "first_party", "expected_version": 1}
    h.refused(h.post("/entities/ent-zbm/fields", body, andre=True), 422, "ENTITY_VALUE_WITHHELD")
    h.refused(h.post("/entities/ent-zbm/fields", {**body, "request_id": rid(), "provenance": "dashboard",
                                                  "value": f"Z Best {vals['aws']}"}, andre=True), 422,
              "ENTITY_VALUE_WITHHELD")
    # a URL-prefix Search Console property
    with pytest.raises(Exception) as ei:
        h.svc.set_gsc_property("zbm", {"request_id": rid(), "property": f"https://{vw.DOMAIN}/x/{k}/"})
    assert getattr(ei.value, "reason", str(ei.value)) == "GSC_PROPERTY_INVALID"
    assert len(h.svc.log) == n_log
    assert _no_trace(h, vals) == []
    # ordinary values still pass
    h.ok(h.post("/entities/ent-zbm/fields", {**body, "request_id": rid(), "provenance": "re-confirmed on the dashboard"},
                andre=True))
    h.ok(h.post("/entities/ent-zbm/fields", {**body, "request_id": rid(), "field": "telephone", "value": "(562) 248-6617",
                                             "provenance": "re-confirmed", "expected_version": 2}, andre=True))


def test_the_new_reasons_are_in_the_closed_catalogue():
    for code in ("PROMPT_SECRET", "PATH_WITHHELD", "ENTITY_VALUE_WITHHELD"):
        assert reasons.R(code) == code


# ------------------------------------------------------------------------------------------------ a real service run

def _case_answers(cases: list) -> list:
    out = []
    for i, p in enumerate(cases):
        url = f"https://competitor.test/blog/case-{i}"
        a = ProviderAnswer("ANSWER", f"Z Best Media note: {p} end.", [url], "m", "1", cite_from_text=False,
                           provider="openai", asked_at="2026-10-09T18:00:00Z")
        out.append(a)
    return out


COMMON = frozenset(("anytime", "password", "arobase", "passwor"))


def _distinct(p: str) -> list:
    """The generated, distinctive parts of a case, searched for in every form in everything the service wrote: runs
    of 6+ letters / digits holding a digit, lower-case runs of 7+ letters that are not template words, and the case's
    digits when it holds 9 or more."""
    out = [x for x in re.findall(r"[A-Za-z0-9]{6,}", p) if x.lower() not in COMMON
           and (any(c.isdigit() for c in x) or (x.islower() and len(x) >= 7))]
    d = "".join(c for c in p if c.isdigit())
    return out + ([d] if len(d) >= 9 else [])


def test_a_real_service_run_writes_nothing_raw_and_the_owner_view_masks_every_required_class(tmp_path, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    cases = [p for k, v in all_cases().items() for p in v]
    h = helpers.Harness(tmp_path, data_dir=str(tmp_path / "data"), SEO_PROBE_SAMPLES="5")
    port = vw.Port(_case_answers(cases))
    h.ports.engines["openai"] = port
    h.ok(h.post("/tenants/zbm/domains", {"request_id": rid(), "domains": [vw.DOMAIN]}, andre=True))
    prompts = [f"who recovers marketplace revenue variant {i}" for i in range((len(cases) + 4) // 5)]
    assert len(prompts) <= 50
    psid = h.ok(h.post("/tenants/zbm/prompt-sets", _ps_body(prompts=prompts)), 201)["prompt_set_id"]
    # prompt-set creation with secrets: refused, nothing written
    rng = random.Random(SEED + 30)
    sec = _secret_forms(rng)
    h.refused(h.post("/tenants/zbm/prompt-sets", _ps_body(name="other", prompts=[f"tools {sec['key']}"])), 422,
              "PROMPT_SECRET")
    run = h.ok(h.post("/tenants/zbm/citation-runs", {"request_id": rid(), "domain": vw.DOMAIN, "prompt_set_id": psid}),
               201)
    assert run["status"] == "completed" and port.asked >= len(cases)
    blob = vw.written(h, caplog)
    parts = {f"{i}:{j}": x for i, p in enumerate(cases) for j, x in enumerate(_distinct(p))}
    assert len(parts) >= len(cases)
    assert vw.found(blob, {**parts, **sec}) == []
    # the owner's view: every case row's excerpt masked (a strict-only case may show; it has no client class)
    got = h.ok(h.get(f"/tenants/zbm/citation-runs/{run['citation_run_id']}"))
    by_url = {}
    for r in got["result"]["facts"]["rows"]:
        cu = (r.get("evidence") or {}).get("cited_url") or {}
        if cu.get("value"):
            by_url[cu["value"]] = r
    strict_only = {p for k in STRICT_ONLY for p in all_cases()[k]}
    missed = []
    for i, p in enumerate(cases):
        r = by_url.get(f"https://competitor.test/blog/case-{i}")
        assert r is not None, i
        ex = r["evidence"].get("excerpt")
        if p not in strict_only and not (ex and ex["alert"]):
            missed.append(i)
    assert missed == []


# ------------------------------------------------------------------------------------------------ MUST 7: torn tail

def _torn(tmp_path) -> tuple:
    key, d, p, _ids = t8._vault(tmp_path)
    frag = b'{"v":1,"id":"' + _r(random.Random(SEED + 40), "ABCDEFGH", 11).encode()
    with open(p, "ab") as fh:
        fh.write(frag)
    return key, d, p, frag


def test_a_quarantine_name_clash_with_other_content_refuses_and_nothing_is_cut(tmp_path):
    key, d, p, frag = _torn(tmp_path)
    rid_ = vault.hashlib.sha256(frag).hexdigest()[:32]
    q = p + vault.QUARANTINE_SUFFIX + rid_
    with open(q, "wb") as fh:
        fh.write(b"other bytes")
    os.chmod(q, 0o600)
    before = open(p, "rb").read()
    with pytest.raises(vault.VaultCorrupt):
        vault.CitationVault(d, key)
    assert open(p, "rb").read() == before                               # nothing cut
    assert open(q, "rb").read() == b"other bytes"                       # the clashing file untouched
    assert not os.path.exists(p + vault.REPAIR_MARKER_SUFFIX)
    # the same content already there (an earlier start copied it, then stopped) is accepted
    with open(q, "wb") as fh:
        fh.write(frag)
    cv = vault.CitationVault(d, key)
    assert cv.tail_repair["quarantined"] == 1 and not open(p, "rb").read().endswith(frag)


def test_the_quarantine_clash_rule_is_load_bearing(tmp_path, monkeypatch):
    key, d, p, frag = _torn(tmp_path)
    q = p + vault.QUARANTINE_SUFFIX + vault.hashlib.sha256(frag).hexdigest()[:32]
    with open(q, "wb") as fh:
        fh.write(b"other bytes")
    real_open = open

    def lying_open(path, mode="r", *a, **k):                          # mutation: the content comparison always agrees
        fh = real_open(path, mode, *a, **k)
        if path == q and "b" in mode and "r" in mode:
            class F:
                def __enter__(self):
                    return self

                def __exit__(self, *e):
                    fh.close()

                def read(self, n=-1):
                    return frag
            return F()
        return fh
    monkeypatch.setattr("builtins.open", lying_open)
    cv = vault.CitationVault(d, key)                                   # the repair would proceed over a clash
    monkeypatch.undo()
    assert cv.tail_repair is not None and open(q, "rb").read() == b"other bytes"


@pytest.mark.parametrize("bad", [
    lambda rep: "not json",
    lambda rep: json.dumps({k: v for k, v in rep.items() if k != "torn_lines"}),
    lambda rep: json.dumps({**rep, "extra": 1}),
    lambda rep: json.dumps({**rep, "bytes_removed": -1}),
    lambda rep: json.dumps({**rep, "records_kept": "3"}),
    lambda rep: json.dumps({**rep, "repair_id": "x" * 31}),
    lambda rep: json.dumps([rep]),
])
def test_a_malformed_repair_marker_refuses(tmp_path, bad):
    key, d, p, _ids = t8._vault(tmp_path)
    rep = {k: 1 for k in vault.REPAIR_FIELDS}
    rep["repair_id"] = "a" * 32
    with open(p + vault.REPAIR_MARKER_SUFFIX, "w") as fh:
        fh.write(bad(rep))
    with pytest.raises(vault.VaultCorrupt):
        vault.CitationVault(d, key)
    good = dict(rep)
    with open(p + vault.REPAIR_MARKER_SUFFIX, "w") as fh:
        fh.write(json.dumps(good))
    assert vault.CitationVault(d, key).tail_repair["from_marker"] is True


def test_the_marker_validation_is_load_bearing(tmp_path, monkeypatch):
    key, d, p, _ids = t8._vault(tmp_path)
    with open(p + vault.REPAIR_MARKER_SUFFIX, "w") as fh:
        fh.write(json.dumps({"torn_lines": -1}))
    monkeypatch.setattr(vault, "REPAIR_FIELDS", ("torn_lines",))      # mutation: the field set check loosened
    monkeypatch.setattr(vault, "REPAIR_ID", re.compile(r".*"))
    real = vault.CitationVault._read_marker

    def loose(self):                                                   # mutation: only JSON-object-ness checked
        try:
            return real(self)
        except vault.VaultCorrupt:
            return {"from_marker": True}
    monkeypatch.setattr(vault.CitationVault, "_read_marker", loose)
    assert vault.CitationVault(d, key).tail_repair == {"from_marker": True}   # a malformed marker would be accepted


# ------------------------------------------------------------------------------------------------ S4 / S5

def test_the_launcher_writes_no_access_log_line():
    assert serve.uvicorn_kwargs()["access_log"] is False


def test_the_access_log_setting_reaches_uvicorn(monkeypatch):
    seen = {}

    class FakeUvicorn:
        class Config:
            def __init__(self, app, **kw):
                seen.update(kw)

        @staticmethod
        def run(app, **kw):
            seen.update(kw)
    import dual_stack
    try:
        dual_stack.serve(FakeUvicorn, object(), "127.0.0.1", 0, "seo-py", **serve.uvicorn_kwargs())
    except Exception:  # noqa: BLE001 - only the configuration handed over is checked
        pass
    assert seen.get("access_log") is False


def test_non_latin_labels_joined_by_underscore_and_confusable_is_are_masked(monkeypatch):
    rng = random.Random(SEED + 50)
    v = _r(rng, LOW, 6) + str(rng.randint(10, 99))
    ar = "\u0643\u0644\u0645\u0629_\u0627\u0644\u0645\u0631\u0648\u0631"
    texts = [f"<{ar}>{v}</{ar}>", f"{ar}={v}", f"\u5bc6_\u7801: {v}", f"password \u0456s {v}",
             f"token \u03b9s {v}", f"password wa\u0455 {v.capitalize()}"]
    assert all("label_value" in sg.client_view_findings(t) for t in texts)
    assert all(sg.check(t) is not None for t in texts)
    # mutation: no "_" joiner (review cbdf5e78: the one script pattern of both lanes, ``_SCRIPT_SEQ``)
    monkeypatch.setattr(sg, "_SCRIPT_SEQ", re.compile("|".join(" ?".join(re.escape(c) for c in s.replace(" ", ""))
                                                               for s in sg._SCRIPT)))
    assert sum(1 for t in texts[:3] if "label_value" not in sg.client_view_findings(t)) > 0


# ------------------------------------------------------------------------------------------------ equivalence of the speedups

def test_the_label_prefilter_and_token_memo_give_the_same_answers(monkeypatch):
    rng = random.Random(SEED + 60)
    texts = []
    for _ in range(400):
        words = [rng.choice(("the", "pin", "Pass", "to", "ken", "sec", "ret", "x1", "AKIA", "auth", "\u00e9t\u00e9",
                             "p", "a", "s", "w", "o", "r", "d", "-", "_", "id", "sig", "cle", "Elum", "\u0440\u0430ss"))
                 for _ in range(rng.randint(1, 12))]
        texts.append(rng.choice((" ", "", "_", "-")).join(words))
    got = [sg._label_hit(t) for t in texts] + [sg._token_hit(t) for t in texts]
    monkeypatch.setattr(sg, "_PIECES_IN", re.compile(r"(?s).?"))         # the prefilter always passes
    want = [sg._label_hit(t) for t in texts] + [sg._token_hit(t) for t in texts]
    assert got == want


def test_a_log_ingest_path_template_segment_failing_the_strict_check_is_a_placeholder(monkeypatch):
    from agents import logs
    rng = random.Random(SEED + 24)
    segs = ["xo" + "xb-" + _r(rng, LOW, 12), "gl" + "pat-" + _r(rng, string.ascii_letters, 22), r6._mail(rng)[0] + "-at-" + r6._mail(rng)[1]
            + "-dot-com"]
    assert all(logs._kind(logs._LOOKALIKE.fold_cased(s)) is None for s in segs[:1])    # the template rules keep it
    # wave1-spec/1: the stored template codes every segment off the allowlist (a, b, and the value)
    stored = [logs.template_path("/a/" + s + "/b") for s in segs]
    assert all(not any(x in o for x in segs) for o in stored)
    # the display shape (shape_template_path, Search Console / client report) keeps the strict check
    outs = [logs.shape_template_path("/a/" + s + "/b") for s in segs]
    assert all(o in ("/a/{token}/b", "/a/{email}/b") for o in outs), sum(1 for o in outs if "{" not in o)
    assert logs.shape_template_path("/shop/blue-widget/reviews") == "/shop/blue-widget/reviews"
    monkeypatch.setattr(logs.secret_guard, "check", lambda t, waive=frozenset(): None)   # mutation
    assert logs.shape_template_path("/a/" + segs[0] + "/b") != "/a/{token}/b"


def test_each_operator_text_check_is_load_bearing(monkeypatch):
    import svc_audits
    import svc_entity
    from errors import Invalid
    rng = random.Random(SEED + 25)
    k = _key(rng)
    with pytest.raises(Invalid) as ei:
        svc_audits._check_paths([f"/reset/{k}"], 10, "https", vw.DOMAIN)
    assert ei.value.reason == "PATH_WITHHELD"
    data = {"value": "Z Best Media", "source": "founder_statement", "provenance": f"dashboard {k}",
            "authority": "first_party"}
    with pytest.raises(Invalid) as ei:
        svc_entity.validate_field("name", data)
    assert ei.value.reason == "ENTITY_VALUE_WITHHELD"
    # c0c09bd1: the refusal is the operator rule (a strong key / PEM / user info), not the strict check
    monkeypatch.setattr(svc_audits, "path_refused", lambda scheme, domain, path: False)     # mutation
    svc_audits._check_paths([f"/reset/{k}"], 10, "https", vw.DOMAIN)                       # would be fetched
    monkeypatch.setattr(svc_entity.secret_guard, "operator_refusal", lambda t: None)       # mutation
    svc_entity.validate_field("name", data)


def test_the_gsc_property_check_is_load_bearing(tmp_path, monkeypatch):
    rng = random.Random(SEED + 26)
    k = _key(rng)
    h = vw.harness(tmp_path)
    vw.start(h, vw.synthetic())
    body = {"request_id": rid(), "property": f"https://{vw.DOMAIN}/x/{k}/"}
    with pytest.raises(Exception) as ei:
        h.svc.set_gsc_property("zbm", body)
    assert getattr(ei.value, "reason", None) == "GSC_PROPERTY_INVALID"
    monkeypatch.setattr(sg, "operator_url_refusal", lambda u: None)                        # mutation (c0c09bd1)
    h.svc.set_gsc_property("zbm", {**body, "request_id": rid()})                           # accepted: it would be
    assert vw.found(vw.written(h), {"key": k}) == []                                       # queried; vaulted, not logged


def test_the_per_family_prefix_scan_never_finds_less_than_the_combined_one():
    import leak_corpus_v2 as lc
    rng = random.Random(SEED + 27)
    vals = [v for v, _kind in lc.values(rng).values()]
    texts = [t for v in vals for t in (v, "x " + v + " y", v.replace("-", ""), v[:12])]
    for t in texts:
        if len(t) <= sg._LONG:
            old = any(m.lastgroup != "sk_generic" or sg._sk_ok(m) for m in sg._PREFIX_RX.finditer(t))
            assert sg._prefix_found(t) or not old


def test_the_ascii_transition_count_is_the_per_character_count():
    rng = random.Random(SEED + 28)

    def old(r):
        ld = cs = 0
        prev = None
        for c in r:
            k = "d" if c.isdigit() else ("u" if c.isupper() else ("l" if c.islower() else "o"))
            if prev is not None and k != prev:
                if "d" in (k, prev) and "o" not in (k, prev):
                    ld += 1
                elif {k, prev} == {"u", "l"}:
                    cs += 1
            prev = k
        return ld, cs
    for _ in range(5000):
        r = "".join(rng.choice(string.printable[:94]) for _ in range(rng.randint(0, 48)))
        assert sg._transitions(r) == old(r)
