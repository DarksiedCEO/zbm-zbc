"""Review round 21 (search_performance 2026-11-08.21): the findings of review rv-priya20 against priya-spec/1 and its
v1.1 clarifications. Every personal-data value is synthetic and generated at run time; no value is written here.

- item 1 (C-2, the FULL glue set): all 24 glue characters (fullwidth U+FF01-U+FF0F and U+FF1A-U+FF20, the
  ideographic comma and full stop) glued to an address with a Latin context word, to an e-mail's top-level domain
  before CJK / Arabic / Devanagari context, and to an encoded e-mail (the characters NFKC folds into an encoding's
  alphabet: '-', '%', '='); the whole set in every position is the conformance corpus ``c2glue``;
- item 2 (C-4 with D11 / D12 / D14): a spelled code digit glued to the keypad letters ("... zero" + "flowers");
- item 3: the review's two behavioural survivors (brackets as address glue; the script view's lower-casing) pinned by
  cases that change verdict under them;
- item 6 (outside the spec): ordinary words that decode by chance to a short text holding 'x@y' are kept."""

from __future__ import annotations

import base64
import random
import string

import privacy_r18_corpus as rc
import pytest
from conformance.oracle import priya_list

from agents import priya
from agents import search_performance as sp

BRANDS = ["acme"]
GLUE = tuple(chr(c) for c in (*range(0xFF01, 0xFF01 + 15), *range(0xFF1A, 0xFF1A + 7), 0x3001, 0x3002))
NON_LATIN = ("\u8c22\u8c22", "\u0634\u0643\u0631\u0627", "\u0927\u0928\u094d\u092f\u0935\u093e\u0926")


def _drop(q: str) -> bool:
    return sp.classify_query(q, BRANDS) == "pii_suspect" or sp.classify_query(priya.normalize(q), BRANDS) == \
        "pii_suspect"


def _rng(tag: str) -> random.Random:
    return random.Random("r21:" + tag)


def test_the_glue_set_is_the_24_characters_of_c2():
    assert len(GLUE) == 24 and set(sp._C2_GLUE) == {ord(c) for c in GLUE}
    assert set(priya_list.C2_GLUE) == set(GLUE)


# ------------------------------------------------------------------ item 1 (a): addresses, Latin context
@pytest.mark.parametrize("g", GLUE, ids=lambda c: "U+%04X" % ord(c))
def test_address_glued_by_every_glue_character_to_a_latin_word(g):
    rng = _rng("addr%04x" % ord(g))
    for i in range(12):
        a = rc.address(rng)
        w = ("thanks", "gracias", "call", "merci")[i % 4]
        for q in (w + g + a, a + g + w, w + g + a + g + w, g.join(a.split())):
            assert sp.classify_query(q, BRANDS) == "pii_suspect", (ord(g), i)
            assert priya_list.mandated(q) == "address", (ord(g), i)


# ------------------------------------------------------------------ item 1 (b): the TLD glued before other scripts
@pytest.mark.parametrize("g", GLUE, ids=lambda c: "U+%04X" % ord(c))
def test_email_tld_glued_by_every_glue_character_before_cjk_arabic_devanagari(g):
    rng = _rng("tld%04x" % ord(g))
    for i in range(9):
        local, dom = rc.email(rng).split("@")
        at, dot = ("at", "arroba", "kukac")[i % 3], ("dot", "punto", "titik")[i // 3]
        sep = ("_", "-", " ")[i % 3]
        v = sep.join([local, at] + [x for lab in dom.split(".") for x in (lab, dot)][:-1])
        c = NON_LATIN[i % 3]
        for q in (c + g + v + g + c, v + g + c):
            assert _drop(q), (ord(g), i)
            assert priya_list.mandated(q) == "email", (ord(g), i)


# ------------------------------------------------------------------ item 1 (c): encodings and glue they could absorb
@pytest.mark.parametrize("g", GLUE, ids=lambda c: "U+%04X" % ord(c))
def test_encoded_email_glued_or_chunked_by_every_glue_character(g):
    rng = _rng("enc%04x" % ord(g))
    for i in range(8):
        raw = rc.email(rng).encode()
        e = (base64.b64encode(raw).decode(), base64.urlsafe_b64encode(raw).decode().rstrip("="),
             base64.b32encode(raw).decode(), raw.hex(), "".join(f"%{b:02x}" for b in raw))[i % 5]
        a, b = sorted(rng.sample(range(2, len(e) - 1), 2))
        c = NON_LATIN[i % 3]
        for q in (c + g + e, e + g + c, c + g + e[:a] + g + e[a:b] + g + e[b:]):
            assert _drop(q), (ord(g), i)
            assert priya_list.mandated(q) is not None, (ord(g), i)      # hex digits may also read as a stream value


# ------------------------------------------------------------------ item 2: glued spelled vanity
@pytest.mark.parametrize("lang", ("en", "es"))
def test_spelled_code_digit_glued_to_the_keypad_letters(lang):
    words = rc.EN if lang == "en" else rc.ES
    rng = _rng("vglue" + lang)
    for code in ("800", "833", "844", "855", "866", "877", "888"):
        for one in ("", "1"):
            for _ in range(4):
                seven = rc.vanity(rng)[3:]
                d = one + code
                last = words[int(d[-1])]
                for head in (" ".join(words[int(x)] for x in d[:-1]) + " ", " ".join(d[:-1]) + " ", d[:-1],
                             "".join(words[int(x)] for x in d[:-1])):
                    for case in (str.lower, str.upper):
                        q = case(head + last + seven)
                        assert sp.classify_query(q, BRANDS) == "pii_suspect", (code, q[:2])
                        assert priya_list.mandated(q) == "vanity", (code, q[:2])


@pytest.mark.parametrize("q", ("zeroflowers", "phone zeroflowers", "800 onefootball", "one eight hundred flowers",
                               "800 one flowers", "eightfolds", "the 8 0 zerozone"))
def test_a_number_word_glued_to_an_ordinary_word_without_a_code_and_seven_is_kept(q):
    assert sp.classify_query(q, BRANDS) != "pii_suspect"
    assert priya_list.mandated(q) is None


# ------------------------------------------------------------------ item 3: the review's behavioural survivors
@pytest.mark.parametrize("brackets", ("[]", "{}", "<>"))
def test_address_inside_ascii_brackets_glued_to_words(brackets):
    """r01 (brackets removed from the address glue): an address glued inside square, curly or angle brackets."""
    o, c = brackets
    rng = _rng("br" + brackets)
    for i in range(10):
        a = rc.address(rng)
        w = ("info", "see", "x", "ship to")[i % 4]
        for q in (w + o + a + c, o + a + c + w, w + o + a + c + w):
            assert sp.classify_query(q, BRANDS) == "pii_suspect", (brackets, i)


def test_upper_case_address_glued_to_a_word_of_another_script_raw_text():
    """r08 (no lower-casing in ``script_view``): an upper-case address, or an upper-case glued spelled vanity,
    glued to letters of another script that the lookalike fold does not make Latin; the raw text alone is dropped
    (the stored key is lower case, so only the raw text's own reading is tested here)."""
    rng = _rng("upper")
    for i in range(20):
        a = rc.address(rng).upper()
        tail = ("\u0414\u041e\u041c", "\u0416\u0418\u041b", "\u4f4f\u6240", "\u0628\u064a\u062a")[i % 4]
        assert sp.classify_query(a + tail, BRANDS) == "pii_suspect", i
        seven = rc.vanity(rng)[3:]
        assert sp.classify_query(("EIGHT ZERO ZERO" + seven + tail).upper(), BRANDS) == "pii_suspect", i
        assert sp.classify_query(("EIGHT EIGHT EIGHT" + seven + tail).upper(), BRANDS) == "pii_suspect", i


# ------------------------------------------------------------------ item 6: short decodes holding 'x@y' by chance
def _short_at_decode(q: str) -> bool:
    """Some window of ``q`` (as written, rot13'd or reversed) decodes to a text under eight characters with an
    'x@y' shape and no mailbox's '.' + two TLD letters: what the encoded-e-mail rule of round 20 dropped."""
    toks = sp._ENC_TOKEN.findall(q)
    ok = [sp._ENC_TOKEN.fullmatch(t) is not None for t in toks]
    plain = [ok[i] and sp._PLAIN_TOKEN.fullmatch(toks[i]) is not None for i in range(len(toks))]
    wins = [v for s in sp._enc_windows(toks, ok, plain) for v in (s, sp.rot13(s))]
    wins += list(sp._enc_windows([t[::-1] for t in reversed(toks)], ok[::-1], plain[::-1]))
    for s in wins:
        for u in sp._decodings(s):
            low = u.lower()
            if len(low) < 8 and (sp._EMAIL_CORE.search(low) or sp._EMAIL_CORE.search(sp.rot13(low))) \
                    and not sp._SHORT_MAILBOX.search(low) and not sp._SHORT_MAILBOX.search(sp.rot13(low)):
                return True
    return False


def test_sizes_and_product_codes_that_decode_to_a_short_x_at_y_are_kept():
    """ADR 0047 round 21, outside the spec: "tent <n>mm" and two UPCs of rv-priya20's ordinary corpus were dropped
    because a window decodes (rot13 base64, reversed) to five or six characters holding 'x@y'; a text that short
    holds an e-mail only with its '.' and two TLD letters. Every such shape found from a seeded search is kept."""
    rng = _rng("short")
    found = []
    for n in range(10, 1000):
        q = "tent %dmm" % n
        if _short_at_decode(q):
            found.append(q)
    for _ in range(4000):
        b = "".join(rng.choice(string.digits) for _ in range(11))
        d = b + str(-sum(int(x) * (3 if k % 2 == 0 else 1) for k, x in enumerate(b)) % 10)   # a valid UPC-A
        q = "upc %s %s" % (d, rng.choice(("tent", "rain jacket", "yoga mat")))
        if _short_at_decode(q):
            found.append(q)
    assert len(found) >= 5 and any(q.startswith("upc") for q in found), len(found)
    for q in found:
        assert sp.classify_query(q, BRANDS) != "pii_suspect", q
        assert priya_list.mandated(q) is None, q


def test_shortest_encoded_mailbox_is_still_dropped():
    """The six-character mailbox ("a@b.cc") base64 / base32 / hex encoded, alone and before a word, stays dropped."""
    low = string.ascii_lowercase
    rng = _rng("six")
    for i in range(200):
        v = rng.choice(low) + "@" + rng.choice(low) + "." + "".join(rng.choice(low) for _ in range(2))
        raw = v.encode()
        e = (base64.b64encode(raw).decode(), base64.b32encode(raw).decode().rstrip("="), raw.hex())[i % 3]
        for q in (e, e + " " + ("tonight", "info", "Zx9")[i % 3]):
            assert _drop(q), i


def test_encoded_email_chunked_one_character_a_word_is_still_dropped():
    """D21 with chunks of one or two characters: an e-mail base32 (lower case) encoded and written one character a
    word, in other-script context, is longer than the twelve words a window joins (rv-priya18's boundary family
    ``chunk1_b32_nopad_lower``; a394a985 kept 24 of 300 such probes, found by this round's tests): a run of such
    words is also read as one word (``_short_chunks_merged``); a window cut by the span limits keeps the bare 'x@y'
    reading (the short-text rule above applies only to whole runs)."""
    rng = _rng("chunk1")
    before = ("\u092e\u0947\u0930\u093e \u0928\u0902\u092c\u0930", "\u8054\u7cfb", "\u0631\u0642\u0645\u064a")
    after = ("\u0939\u0948", "\u8c22\u8c22", "\u0634\u0643\u0631\u0627")
    for i in range(90):
        raw = rc.email(rng).encode()
        e = base64.b32encode(raw).decode().rstrip("=").lower()
        for w in (1, 2):
            q = before[i % 3] + " " + " ".join(e[k:k + w] for k in range(0, len(e), w)) + " " + after[i % 3]
            assert _drop(q), (i, w)
            assert priya_list.mandated(q) == "email", (i, w)


def test_sep_spelled_email_ended_by_an_ascii_full_stop_glued_to_cjk():
    """n05 (round 20's TLD followed by a full stop): with every C-2 glue character now also read as a space, the
    round-20 pins of U+3002 no longer need the trailing-dot reading; an ASCII full stop after the top-level domain of
    a '_' / '-' spelled e-mail, glued to CJK context, still does."""
    rng = _rng("dotcjk")
    for i in range(40):
        local, dom = rc.email(rng).split("@")
        sep = ("_", "-")[i % 2]
        v = sep.join([local, ("at", "arroba")[i % 2]] + [x for lab in dom.split(".") for x in (lab, "dot")][:-1])
        q = v + "." + ("\u8c22\u8c22", "\u3067\u3059")[i % 2]
        assert _drop(q), i
        assert priya_list.mandated(q) == "email", i


def test_address_with_an_ascii_full_stop_glued_to_the_next_word():
    """n08 (round 20's full stop glued to the next word as address glue): the ideographic and fullwidth stops of
    round 20's pins are now read by the C-2 view too; an ASCII full stop after a strong street suffix glued to the
    next word ("... st.gracias") is still read by the address core alone."""
    rng = _rng("adrdot")
    for i in range(40):
        a = "%d %s %s" % (rng.randint(1, 99999), rng.choice(rc._STREETS),
                          rng.choice(("st", "ave", "blvd", "rd", "dr", "ln", "ct", "pl", "hwy", "pkwy")))
        for q in (a + "." + ("gracias", "thanks", "merci")[i % 3], "info " + a + ".today"):
            assert sp.classify_query(q, BRANDS) == "pii_suspect", i


def test_sep_spelled_email_whose_local_part_ends_in_an_at_word():
    """rv-priya20's matrix (D18, the one case left after the glue fix): the local part itself ends in an at-word
    ("...-at") before the spelled "_AT_", glued to Arabic / CJK context; each at-word is tried in turn as the one '@'."""
    rng = _rng("localat")
    for i in range(40):
        local, dom = rc.email(rng).split("@")
        label, tld = dom.split(".", 1)
        at = ("at", "AT", "At")[i % 3]
        v = local + "-at_" + at + "_" + label + ("-dot-", "_dot_")[i % 2] + tld.replace(".", "-dot-")
        q = v + ("\u0634\u0643\u0631\u0627", "\u8c22\u8c22")[i % 2]
        assert sp.classify_query(q, BRANDS) == "pii_suspect", i
        assert priya_list.mandated(q) == "email", i
