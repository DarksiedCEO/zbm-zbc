"""Review round 20 (search_performance 2026-11-08.20): the findings of review rv-priya19 against priya-spec/1 and its
v1.1 clarifications (C-1 the closed at-word / dot-word lists; C-2 fullwidth and ideographic punctuation and CJK
context glued without spaces; C-3 every ordered composed pair <= 5%; C-4 a number word inside an ordinary word is not
a list disguise). Every personal-data value is synthetic and generated at run time; no value is written here.

- items 1, 4: every at-word x every dot-word in every reading (spelled, bracketed, '_' / '-', rot13, reversed,
  chunked), any case;
- items 2, 3: e-mails and addresses glued to ideographic / fullwidth punctuation and CJK context, chunked base64url
  and bracketed e-mails in context;
- item 5: composed gaps counted one character for one character (other-script letters the lookalike fold makes two
  letters, symbols NFKC makes two characters, a thin space), URL-encoded e-mails reversed;
- item 8: number words inside ordinary words read only as a gap BETWEEN two units;
- item 7: the two behavioural survivors of the review (reversed-window search of encoded e-mails; the GTIN check
  digit) pinned by cases that change verdict under them."""

from __future__ import annotations

import base64
import codecs
import random

import privacy_r18_corpus as rc
import pytest
from conformance.oracle import priya_list

from agents import priya
from agents import search_performance as sp

BRANDS = ["acme"]
AT_WORDS = ("at", "arroba", "arobase", "chiocciola", "klammeraffe", "at-zeichen", "malpa", "kukac", "snabel-a",
            "apenstaartje", "sobaka")
DOT_WORDS = ("dot", "punto", "point", "ponto", "punkt", "kropka", "pont", "prikk", "piste", "nokta", "titik")
CJK_GLUE = ("\u3002", "\u3001", "\uff1a", "\uff0c", "\uff01")
CJK = ("\u8bf7\u8054\u7cfb", "\u9023\u7d61", "\uc804\ud654", "\u0928\u0902\u092c\u0930", "\u0631\u0642\u0645")


def _drop(q: str) -> bool:
    return sp.classify_query(q, BRANDS) == "pii_suspect" or sp.classify_query(priya.normalize(q), BRANDS) == \
        "pii_suspect"


def _rng(tag: str) -> random.Random:
    return random.Random("r20:" + tag)


def _mailbox(rng, tld_len: int) -> tuple:
    local, dom = rc.email(rng).split("@")
    tld = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(tld_len))
    return local, dom.split(".")[0], tld


# ------------------------------------------------------------------ items 1 / 4: C-1 at-words x dot-words, any case
def test_the_lists_are_the_closed_lists_of_the_spec():
    assert set(sp.AT_WORDS) == set(AT_WORDS) and set(sp.DOT_WORDS) == set(DOT_WORDS)


@pytest.mark.parametrize("at", AT_WORDS)
def test_every_at_word_with_every_dot_word_in_every_spelling_and_case(at):
    rng = _rng(at)
    for dot in DOT_WORDS:
        for tld_len in (2, 3, 7, 24):
            local, label, tld = _mailbox(rng, tld_len)
            for case in (str.lower, str.upper, str.title):
                a, d = case(at), case(dot)
                forms = (f"{local} {a} {label} {d} {tld}", f"{local} [{a}] {label} ({d}) {tld}",
                         f"{local}{{{a}}}{label}<{d}>{tld}", f"{local}_{a}_{label}_{d}_{tld}",
                         f"{local} -{a}- {label} -{d}- {tld}")
                for q in forms:
                    assert _drop(q), (at, dot, tld_len)
                    assert _drop("please send to " + q + " thanks"), (at, dot)


@pytest.mark.parametrize("at", ("at-zeichen", "snabel-a"))
def test_hyphenated_at_words_spaced_and_run_together(at):
    rng = _rng("hy" + at)
    for _ in range(20):
        local, label, tld = _mailbox(rng, 3)
        for a in (at, at.replace("-", " "), at.replace("-", ""), at.replace("-", "_")):
            assert _drop(f"{local} {a} {label} punkt {tld}"), a


def test_every_pair_rot13_reversed_and_word_reversed_and_chunked():
    rng = _rng("views")
    for at in AT_WORDS:
        for dot in DOT_WORDS:
            local, label, tld = _mailbox(rng, rng.randint(2, 9))
            q = f"{local} {at.upper()} {label} {dot.title()} {tld}"
            for v in (codecs.encode(q, "rot13"), q[::-1], " ".join(w[::-1] for w in q.split(" ")),
                      " ".join(w[:2] + " " + w[2:] if len(w) > 3 else w for w in q.split(" "))):
                assert _drop(v), (at, dot)


def test_uppercase_french_spelled_emails_round18_dropped_by_accident_now_by_rule():
    """Item 4: 644b4901 dropped "<name> AROBASE <label> Point <tld>" only through an accidental base32 reading of
    the upper-case word; the round-20 lists read it."""
    rng = _rng("arobase")
    for _ in range(50):
        local, label, tld = _mailbox(rng, rng.choice((2, 3, 4)))
        q = f"{local} AROBASE {label} Point {tld}"
        assert sp.email_views_hit(sp._LOOKALIKE.fold_cased(q)), "rule"
        assert _drop(q) and _drop(q + "\u3001contatto")


# ------------------------------------------------------------------ items 2 / 3: C-2 glue punctuation and CJK context
def test_spelled_emails_ended_by_ideographic_or_fullwidth_punctuation_before_cjk_context():
    rng = _rng("glue")
    for i in range(300):
        local, label, tld = _mailbox(rng, rng.randint(2, 15))
        at, dot = AT_WORDS[i % 11], DOT_WORDS[(i * 7) % 11]
        v = rng.choice((f"{local} -{at}- {label} _{dot}_ {tld}", f"{local}_{at}_{label}-{dot}-{tld}",
                        f"{local} {at} {label} {dot} {tld}", f"{local}@{label}.{tld}"))
        g, c = CJK_GLUE[i % 5], CJK[i % 5]
        for q in (v + g + c, c + g + v, c + g + v + g + c):
            assert _drop(q), i


def test_addresses_glued_to_fullwidth_or_ideographic_punctuation_and_context():
    rng = _rng("addr")
    for i in range(300):
        a = rc.address(rng)
        g, c = CJK_GLUE[i % 5], CJK[i % 5]
        for q in (a + g + c, a + g + "directions to", a + "\uff1a\u0430\u0434\u0440\u0435\u0441"):
            assert _drop(q), i


def test_encoded_email_chunks_glued_to_fullwidth_punctuation_in_context():
    rng = _rng("enc")
    for i in range(200):
        v = rc.email(rng)
        e = base64.urlsafe_b64encode(v.encode()).decode().rstrip("=")
        cut = sorted(rng.sample(range(2, len(e) - 1), 2))
        chunked = " ".join((e[:cut[0]], e[cut[0]:cut[1]], e[cut[1]:]))
        for q in ("call\uff1a" + chunked, "meine Nummer\u3002" + chunked + "\U0001f4deanrufen"):
            assert _drop(q), i


def test_bracketed_email_chunked_inside_its_brackets_in_context():
    rng = _rng("brk")
    for i in range(200):
        local, label, tld = _mailbox(rng, 3)
        k = rng.randint(1, len(label) - 1)
        q = f"{local} {{ at}} {label[:k]} {label[k:]} {{dot}} {tld}\u3002\u5e2e\u52a9"
        assert _drop(q), i


# ------------------------------------------------------------------ item 5: composed gaps counted per character
WIDE_LETTERS = ("\u044e", "\u044b", "\u042e", "\u042b", "\u04d4")       # one character the lookalike fold makes two
WIDE_SYMBOLS = ("\u2026", "\u2033", "\u2036", "\u203c", "\u2057")       # one character NFKC makes two or more
SPACES = ("\u2009", "\u00a0", "\u202f", "\u2007")                        # separators of the list (D06), not word ends


def _digit_values(rng):
    return ((rc.ssn(rng), "ssn"), (rc.nanp(rng), "nanp"), (rc.card(rng), "card"))


def _spell_with_gaps(rng, d: str, pool) -> str:
    words = rc.EN if rng.random() < 0.5 else rc.ES
    out = []
    for i, ch in enumerate(d):
        out.append(words[int(ch)] if rng.random() < 0.6 else ch)
        if i + 1 < len(d):
            out.append("".join(rng.choice(pool) for _ in range(rng.randint(1, 3))))
    return "".join(out)


@pytest.mark.parametrize("pool", (WIDE_LETTERS, WIDE_SYMBOLS), ids=("fold_two_letters", "nfkc_two_chars"))
def test_gap_of_three_characters_that_normalisation_widens(pool):
    rng = _rng("wide" + pool[0])
    for _ in range(150):
        for d, cls in _digit_values(rng):
            q = _spell_with_gaps(rng, d, pool)
            assert _drop(q), cls
            assert _drop("info " + q + " \U0001f600"), cls


def test_gap_of_a_separator_space_and_a_letter_touching_a_number_word():
    """CP-3: the gap replaces the word joiner, so a number word may touch gap letters; a space character other than
    the typed ones (U+2009, U+00A0 ...) is a separator of the list inside such a gap, not the end of a word."""
    rng = _rng("thin")
    for _ in range(200):
        for d, cls in _digit_values(rng):
            words = rc.EN if rng.random() < 0.5 else rc.ES
            q = "".join(words[int(c)] + (rng.choice(SPACES) + rng.choice("mqxz") if i + 1 < len(d) else "")
                        for i, c in enumerate(d))
            assert _drop(q), cls


def test_number_word_touching_letters_and_symbols_of_one_gap():
    rng = _rng("touch")
    for _ in range(200):
        for d, cls in _digit_values(rng):
            words = rc.ES if rng.random() < 0.5 else rc.EN
            q = "".join(words[int(c)] + (rng.choice("pqxk") + rng.choice("\u21d2\u2994\u2746") if i + 1 < len(d)
                                         else "") for i, c in enumerate(d))
            assert _drop(q), cls
            assert sp._number_words_read(q).count("\u21d2") + sp._number_words_read(q).count("\u2994") + \
                sp._number_words_read(q).count("\u2746") == len(d) - 1


def test_vanity_with_other_script_letters_the_fold_widens():
    rng = _rng("vanw")
    for _ in range(200):
        code = rng.choice(("800", "833", "844", "855", "866", "877", "888"))
        word = rng.choice(("flowers", "carpets", "callnow", "getcash"))
        gap = "".join(rng.choice(WIDE_LETTERS) for _ in range(2))          # two letters: four after the fold
        gapped = gap.join(code)
        assert _drop(gapped + "-" + word) and _drop("1" + gap + gapped + " " + word.upper())
        assert sp._vanity_any(sp.script_view(gapped + "-" + word))


def test_digit_groups_with_widened_gaps_and_no_number_word():
    rng = _rng("widedigits")
    for _ in range(200):
        for d, cls in _digit_values(rng):
            q = "".join(c + ("".join(rng.choice(WIDE_LETTERS + WIDE_SYMBOLS) for _ in range(rng.randint(1, 3)))
                             if i + 1 < len(d) else "") for i, c in enumerate(d))
            assert _drop(q), cls
            assert sp.digit_stream_hit(sp.script_view(q)), cls


def test_spelled_email_glued_to_a_long_word_of_another_script():
    """C-2 (context of X7-X12 glued without a space): after the lookalike fold a Cyrillic word glued to the
    top-level domain would make it longer than 24 letters; the per-character view sets it apart."""
    rng = _rng("cyrglue")
    word = "\u043f\u043e\u0436\u0430\u043b\u0443\u0439\u0441\u0442\u0430" * 3
    for i in range(100):
        local, label, tld = _mailbox(rng, rng.randint(2, 6))
        q = f"{local} {AT_WORDS[i % 11]} {label} {DOT_WORDS[i % 11]} {tld}{word}"
        assert _drop(q), i
        assert sp.email_views_hit(sp.script_view(q)), i


def test_address_glued_to_a_word_of_another_script_with_no_punctuation():
    rng = _rng("addrcyr")
    for i in range(200):
        a = rc.address(rng)
        q = a + "\u0430\u0434\u0440\u0435\u0441" if i % 2 else "\u0430\u0434\u0440\u0435\u0441" + a
        assert _drop(q), i


def test_url_encoded_email_reversed_whole_or_word_by_word():
    rng = _rng("pct")
    for i in range(300):
        v = rc.email(rng)
        enc = v.replace("@", "%40") if i % 2 else "".join("%%%02X" % b if chr(b) in "@." else chr(b) for b in v.encode())
        for q in (enc[::-1], "\u9023\u7d61\u3001" + enc[::-1] + ": \u0928\u0902\u092c\u0930"):
            assert _drop(q), i
        wr = " ".join(w[::-1] for w in ("contact " + enc).split(" "))
        assert _drop(wr), i


def test_script_view_counts_one_character_for_one():
    assert sp.script_view("ascii only") == ""
    v = sp.script_view("a\u044eb\u2036c\u2009d\uff11\u0663\u00e9\u3002\u3000x\uff1a\U0001d41a\uff41")
    assert v == "a#b\u2036c\u2009d13e. x:aa"


# ------------------------------------------------------------------ item 8: C-4, number words inside ordinary words
C4_KEEP = ["iphone 2023 2024", "best phone 2025 2026", "2024 2025 phone deals", "someone like you 2011 2012",
           "money heist 2017 2021", "oneplus 12 2024 2025", "phone case 2023 2024", "2023 2024 oneplus",
           "2011 2012 twosome", "2023 2024 dosis", "2024 2025 eighty", "2023 2024 ones"]


@pytest.mark.parametrize("q", C4_KEEP)
def test_number_word_inside_an_ordinary_word_is_not_a_disguise(q):
    assert not _drop(q), q


def test_ordinary_words_holding_number_words_next_to_digit_groups_are_kept():
    rng = _rng("c4")
    for _ in range(300):
        q = rng.choice(("mattress %05d %d", "part number %02d-%04d-%02d phone case", "iphone %d %d", "phone %d %d"))
        args = tuple(rng.randint(0, 99999) for _ in range(q.count("%")))
        t = q % args
        stripped = " ".join(w for w in t.split() if not any(n in w for n in ("one", "tres", "dos")))
        if not _drop(stripped):                      # only when the digits alone are not a value of the list
            assert not _drop(t), t


def test_in_word_reading_needs_units_on_both_sides_of_every_gap():
    assert sp._number_word_token("phone") == ("phone", None)
    assert sp._number_word_token("phone", 2, 0) == ("ph1", 0)          # after a unit across a no-space gap
    assert sp._number_word_token("fivexzero") == ("5x0", 0)
    assert sp._number_word_token("ninefdosixexzthree") == ("9fdo6exz3", 0)
    assert sp._number_word_token("dosp") == ("dosp", None) and sp._number_word_token("dosp", 0, 1) == ("2p", 1)
    assert sp._number_word_token("iphone15")[0] == "iphone15" and sp._number_word_token("abcone5") == ("abcone5", 0)
    assert sp._number_words_read("2025 phone") == "2025 phone"
    assert sp._number_words_read("2025\u21d2phone") == "2025\u21d2ph1"
    assert sp._number_words_read("dosa\u21d2bocho") == "2a\u21d2b8"
    assert sp._number_words_read("dosa\u21d2\u21d2bocho") == "dosa\u21d2\u21d2bocho"    # a gap of four: not read


def test_polka_dot_dress_stays_dropped_for_the_composed_pair_bar():
    """Item 8: not one list rule but the ordered pair reversed -> spelled (CP-2 / CP-4 per pair <= 5%), see
    test_query_privacy_r18.test_still_kept_moved_list_mandated."""
    assert _drop("polka dot dress at target")


# ------------------------------------------------------------------ item 7: the review's two behavioural survivors
def test_encoded_email_reversed_is_read_by_the_reversed_window_search():
    """rv-priya19 mutant e_no_reversed_windows (the reversed-window search of ``encoded_email`` removed) changed 6
    verdicts: base64 / upper-case base32 of an e-mail, the whole string reversed, alone or in context."""
    rng = _rng("encrev")
    for i in range(200):
        v = rc.email(rng).encode()
        e = (base64.b64encode(v).decode().rstrip("=") if i % 2 else base64.b32encode(v).decode().rstrip("="))[::-1]
        for q in (e, "help - " + e + " thanks"):
            assert sp.encoded_email(q), i
            assert _drop(q), i


def _gtin(rng, n: int, valid: bool) -> str:
    body = "".join(str(rng.randint(0, 9)) for _ in range(n - 1))
    total = sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(reversed(body)))
    check = (10 - total % 10) % 10
    return body + str(check if valid else (check + rng.randint(1, 9)) % 10)


def test_gtin_exemption_needs_a_valid_check_digit():
    """rv-priya19 mutant g_gtin_any (any 12-13 digits after a code word exempted) changed 199 verdicts of the review's
    ordinary corpus: a UPC / ISBN with an INVALID check digit stays a 12-13 digit run for the generic rule (dropped,
    a disclosed over-drop of ADR 0047); with a valid check digit the GENERIC rule exempts it (kept when the digits are
    no value of the list: the detectors are never exempted)."""
    rng = _rng("gtin")
    kept_valid = dropped_invalid = 0
    for i in range(300):
        word, n = ("upc", 12) if i % 2 else ("isbn", 13)
        good, bad = _gtin(rng, n, True), _gtin(rng, n, False)
        if word == "isbn":
            good, bad = "978" + good[3:-1], "978" + bad[3:-1]
            good, bad = good + _gtin_tail(good, True), bad + _gtin_tail(bad, False, rng)
        if not priya_list.mandated(good):             # the digits are no value of the list (the oracle)
            assert not _drop(f"{word} {good}"), i
            kept_valid += 1
        assert _drop(f"{word} {bad}"), i
        dropped_invalid += 1
    assert kept_valid >= 100 and dropped_invalid == 300


def _gtin_tail(body12: str, valid: bool, rng=None) -> str:
    total = sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(reversed(body12)))
    check = (10 - total % 10) % 10
    return str(check if valid else (check + rng.randint(1, 9)) % 10)


def test_short_url_encoded_email_chunked_at_any_two_positions():
    """D21 of D19: a short URL-encoded e-mail ("<a>%40<b>.<tld>") split by spaces at any two positions, the escape
    itself split too ("%4 0"): the escape is joined again before decoding (``_percent_chunked_email``); the encoded
    e-mail windows are too short to decode and the pieces hold no host name."""
    import string
    rng = _rng("pctshort")
    for i in range(300):
        v = (rng.choice(string.ascii_lowercase) + "%40" + rng.choice(string.ascii_lowercase) + "."
             + "".join(rng.choice(string.ascii_lowercase) for _ in range(2)))
        a, b = sorted(rng.sample(range(1, len(v)), 2))
        q = v[:a] + " " + v[a:b] + " " + v[b:]
        assert _drop(q), i
        assert priya_list.mandated(q) == "email", i


def test_short_encoded_email_followed_by_a_word():
    """D19 in context: an e-mail of six or seven characters ("ab@c.de"), base64 / base32 / hex / twice base64,
    followed (or surrounded) by a word: its window decodes to the e-mail and then the word's bytes, so the printable
    prefix is short; a 6-7 byte prefix holding an '@' is read (``_printable_prefix``)."""
    import string
    rng = _rng("shortenc")
    low = string.ascii_lowercase
    for i in range(400):
        v = ("".join(rng.choice(low) for _ in range(rng.randint(1, 2))) + "@" + rng.choice(low) + "."
             + "".join(rng.choice(low) for _ in range(2)))
        raw = v.encode()
        e = (base64.b64encode(raw).decode().rstrip("="), base64.b32encode(raw).decode().rstrip("="), raw.hex(),
             base64.b64encode(base64.b64encode(raw)).decode().rstrip("="))[i % 4]
        w = ("Zx9", "QQ", "please", "info", "tonight")[i % 5]
        for q in (e + " " + w, w + " " + e + " " + w, codecs.encode(e, "rot13") + " " + w):
            assert _drop(q), i
            assert sp.encoded_email(sp.decode_query(q)), i


def test_gap_of_letters_nfkc_writes_as_two_or_three_letters():
    """D04 / D07: a gap of three characters, two of them a letter or symbol NFKC writes as two or three ASCII letters
    (U+FB00 ff, U+33C4 cc, U+33BE kW, U+213B FAX, U+3372 da, U+3373 AU) and a separator between them: up to seven
    characters after NFKC; the per-character view counts three (``script_view``)."""
    rng = _rng("nfkcwide")
    pool = ("\ufb00", "\u33c4", "\u33be", "\u213b", "\u3372", "\u3373")
    for _ in range(300):
        d = rng.choice((rc.ssn, rc.nanp))(rng)
        q = "".join(c + (rng.choice(pool) + rng.choice(" -.") + rng.choice(pool) if i + 1 < len(d) else "")
                    for i, c in enumerate(d))
        assert _drop(q)
        assert sp.digit_stream_hit(sp.script_view(q))


def test_rot13_or_word_reversed_spelled_email_ended_by_an_ellipsis_before_cjk():
    """Context glued by U+2026 / U+2025 (NFKC writes three / two full stops): the detection form ends the top-level
    domain with "...", which no mailbox reading accepts; the per-character view keeps the ellipsis one punctuation
    character, a marker (``script_view``, ``email_views_hit``)."""
    rng = _rng("ellipsis")
    for i in range(300):
        local, label, tld = _mailbox(rng, rng.randint(2, 6))
        v = f"{local} {AT_WORDS[i % 11]} {label} {DOT_WORDS[(i * 3) % 11]} {tld}"
        g = ("\u2026", "\u2025")[i % 2]
        for q in (codecs.encode(v, "rot13") + g + "\u8bf7\u8054\u7cfb", " ".join(w[::-1] for w in v.split()) + g + "\u8c22"):
            assert _drop(q), i
            assert sp.email_views_hit(sp.script_view(q)), i


def test_short_url_encoded_email_reversed_whole_or_word_by_word():
    """A short URL-encoded e-mail ("a%40b.cd") reversed whole or word by word, alone or after a word: too short for the
    encoded-e-mail windows, so ``_percent_chunked_email`` reads the reversed views and decodes them."""
    import string
    rng = _rng("pctrev")
    low = string.ascii_lowercase
    for i in range(300):
        v = ("".join(rng.choice(low) for _ in range(rng.randint(1, 2))) + "%40" + rng.choice(low) + "."
             + "".join(rng.choice(low) for _ in range(2)))
        for q in (v[::-1], "mail " + v[::-1], " ".join(w[::-1] for w in ("mail " + v).split())):
            assert _drop(q), i
