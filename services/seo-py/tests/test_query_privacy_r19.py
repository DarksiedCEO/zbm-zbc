"""Review round 19 (search_performance 2026-11-08.19): the shapes the independent review of round 18 measured, as
classes of the closed threat list (ADR 0047), and the over-drops it found. Every personal-data value is synthetic and
generated at run time (``privacy_r18_corpus``, ``privacy_r19_corpus``); no value is written here.

- MUST 1 vanity toll-free numbers spelled (number words for the code, English and Spanish), keypad letters spaced,
  letter gaps inside the digits of 1 + 8xx, separators inside the code;
- MUST 2 spelled / bracketed e-mails with every at-word and dot-word and a top-level domain outside the word list;
  '_at_' / '-dot-' spellings (SHOULD);
- MUST 3 addresses with an ordinary-word suffix followed by any amount of text;
- MUST 4 one number word among a value's digits;
- MUST 5 composed pairs built the reviewer's way (no gap longer than three, none inside a word): residual pinned;
- MUST 6 the round-18 glued trim only for unit / model words;
- MUST 7 tire sizes, UPC / EAN / ISBN codes, part numbers, flight times kept by the generic rules (the detectors are
  never exempted); the ordinary corpus (55% digit-bearing) pinned by class;
- SHOULD the E.164 stream rule pinned (8 digits, a '+' up to three characters before the first digit); the encoded
  e-mail search is linear; Priya classifies each text once."""

from __future__ import annotations

import base64
import collections
import random

import privacy_r18_corpus as rc
import privacy_r19_corpus as r19
import pytest
from test_priya import SITE, build, gsc

from agents import priya
from agents import search_performance as sp

BRANDS = ["acme"]


def _drop(q: str) -> bool:
    return sp.classify_query(q, BRANDS) == "pii_suspect"


def _rng():
    return random.Random(1919)


# ------------------------------------------------------------------ M1, the round-19 shapes (2,000 per class offline)
def test_round19_shapes_every_class_alone_and_in_context_drop():
    kept, total = collections.Counter(), collections.Counter()
    for cls, name, place, t, _v in r19.closed_list_r19(per_class=150):
        total[(cls, name, place)] += 1
        kept[(cls, name, place)] += not _drop(t)
    assert len(total) == 2 * (len(r19.VANITY_R19) + 6 + 1 + 4)
    assert sum(kept.values()) == 0, {k: v for k, v in kept.items() if v}


def test_round19_normalised_keys_drop_too():
    leaks = [(c, n) for c, n, _p, t, _v in r19.closed_list_r19(per_class=20) if not _drop(priya.normalize(t))]
    assert leaks == [], leaks[:10]


# ------------------------------------------------------------------ M2, built the reviewer's way
COMPOSED_N = 42000
COMPOSED_RESIDUAL_MAX = 0       # measured at 2026-11-08.19 (final code): 0 of 42,000


def test_composed_pairs_reviewer_construction_residual_is_pinned():
    cases = list(r19.composed_r19(COMPOSED_N))
    assert len(cases) == COMPOSED_N
    assert {c for c, *_ in cases} == {"card", "phone", "phone_intl", "ssn", "email", "vanity"}
    kept = [(c, n) for c, n, t, _v in cases if not _drop(t)]
    assert len(kept) <= COMPOSED_RESIDUAL_MAX, kept[:20]
    assert kept == [], kept
    assert len(kept) <= 0.01 * COMPOSED_N


# ------------------------------------------------------------------ M3, ordinary queries (55% digit-bearing)
# measured at 2026-11-08.19 on ordinary_r19(24000); every class is disclosed in ADR 0047 (round 19) with its rule:
# almost all are made personal data by the list itself (the digit stream reads a valid SSN / NANP phone across gaps
# of at most three characters: a date followed by a time, ZIP + year, a tire size with its load index, a hyphenated
# ISBN, a version number); the few others are older rules outside the list (ADR 0047 lists them)
def _over_drop():
    return collections.Counter(c for c, t in r19.ordinary_r19(24000) if _drop(t))


def test_ordinary_r19_corpus_over_drop_is_pinned_by_class():
    dropped = _over_drop()
    assert dict(dropped) == MEASURED_R19
    for cls in r19.TEXT_CLASSES + ("price", "quantity", "year", "zip", "time", "sku"):
        assert dropped[cls] <= MEASURED_R19.get(cls, 0), cls


def test_ordinary_r19_corpus_is_at_least_half_digit_bearing():
    classes = collections.Counter(c for c, _t in r19.ordinary_r19(24000))
    assert sum(v for k, v in classes.items() if k not in r19.TEXT_CLASSES) >= 12000


# ------------------------------------------------------------------ MUST 1: vanity toll-free numbers
@pytest.mark.parametrize("name", sorted(r19.VANITY_R19))
def test_vanity_spelled_and_gapped_forms(name):
    rng = _rng()
    for _ in range(150):
        t = r19.VANITY_R19[name](rng)
        assert _drop(t), name
        assert _drop(rc.in_context(rng, t)), name


def test_vanity_rule_reads_gaps_inside_the_code():
    for t in ("1x8yz00 flowers", "8-0-0 callnow", "8;88getcash", "1 8 0 0 f l o w e r s"):
        assert sp._vanity_any(t), t
    assert not sp._vanity_any("1abcd800flowers")              # four letters: not a gap of the list


# ------------------------------------------------------------------ MUST 2: at-words, any TLD, '_at_' spellings
def test_every_at_word_and_dot_word_with_unlisted_tlds():
    rng = _rng()
    for at in r19.AT_WORDS:
        for dot in r19.DOT_WORDS:
            for _ in range(10):
                v = r19.email_unlisted(rng)
                local, dom = v.split("@")
                q = f"{local} {at} {dom.replace('.', f' {dot} ')}"
                assert _drop(q), (at, dot)
                assert _drop(f"rug cleaner {q} thanks"), (at, dot)
                assert _drop(f"{local} [{at}] {dom.replace('.', f'({dot})')}"), (at, dot)


def test_every_at_word_before_a_mail_provider():
    """The older "<name> at <mail provider>" rule (no dot, no TLD) reads every at-word too."""
    rng = _rng()
    for at in r19.AT_WORDS:
        for provider in ("gmail", "yahoo", "outlook", "proton"):
            local = rc.email(rng).split("@")[0]
            assert _drop(f"{local} {at} {provider}"), (at, provider)


def test_separator_spelled_emails():
    rng = _rng()
    for _ in range(100):
        v = r19.email_unlisted(rng) if rng.random() < 0.5 else rc.email(rng)
        q = r19.sep_spelled(rng, v)
        assert _drop(q)
        assert _drop(rc.chunk(rng, q))


def test_spelled_neighbours_stay_kept():
    for q in ("deals at walmart", "meeting at 2pm", "the dot com bubble",
              "punto de venta", "eat at joes", "game at home tonight", "arroba simbolo teclado"):
        assert not _drop(q), q


# ------------------------------------------------------------------ MUST 3: addresses with any amount of text after
def test_address_followed_by_many_tokens():
    rng = _rng()
    for _ in range(200):
        t = r19.address_long_tail(rng)
        assert _drop(t)
        assert _drop(rc.in_context(rng, t))


# ------------------------------------------------------------------ MUST 4: one number word among digits
def test_one_number_word_among_digits():
    rng = _rng()
    for cls, gen in rc.DIGIT_CLASSES.items():
        for _ in range(150):
            t = r19.one_word(rng, cls, gen(rng))
            assert _drop(t), cls
            assert _drop(rc.in_context(rng, t)), cls


def test_one_number_word_next_to_years_is_list_mandated():
    """priya-spec/1 D14 / THE RULE: no year or date exemption for a number word among digits."""
    for q in ("2024 2025 one piece", "iphone 2023 2024", "the one 2001 2002", "best phone 2025 2026"):
        assert _drop(q), q


def test_number_words_inside_words_need_gap_sized_leftovers():
    """A number word inside a longer word is a digit only when every leftover letter run is a gap of the list (at
    most three letters), and "oh" only as a whole word: neither "alphaone" nor "john" completes a value."""
    rng = _rng()
    found = 0
    while found < 20:
        d = rc.ssn(rng)
        if d[0] == "1":
            assert not sp._spelled_hit("alphaone x" + "x".join((d[1:3], d[3:5], d[5:])))
            assert sp._spelled_hit("one x" + "x".join((d[1:3], d[3:5], d[5:])))
            found += 1
        if d[0] == "0":
            assert not sp._spelled_hit("johnx" + "x".join((d[1:3], d[3:5], d[5:])))
            assert sp._spelled_hit("oh x" + "x".join((d[1:3], d[3:5], d[5:])))
            found += 1


def test_vanity_code_words_glued_with_letter_gaps():
    """The code's number words glued with gap letters are read inside the word ("onexeightyzerozero")."""
    for code in ("zerozero", "threethree", "eighteight"):
        assert sp._spelled_hit("onexeighty" + code + " flowers"), code
        assert sp._spelled_hit("rug cleaner onexeighty" + code + "-callnow tonight"), code


def test_encoded_email_rot13_of_an_encoding_and_long_chunked_hex():
    """The encoded-e-mail search itself (not only the older base64 rule): rot13 applied after base64, and hex of an
    e-mail whose '@' lies past the first 40 encoded characters, chunked in surrounding text."""
    import codecs
    rng = _rng()
    for _ in range(20):
        v = rc.email(rng)
        b = base64.b64encode(v.encode()).decode()
        assert sp.encoded_email(codecs.encode(b, "rot13"))
        local, dom = v.split("@")
        long_v = (local + "." + local + "." + local)[:40] + "@" + dom
        h = long_v.encode().hex()
        assert sp.encoded_email("rug cleaner " + " ".join(h[i:i + 12] for i in range(0, len(h), 12)) + " tonight")
    # a spelled e-mail longer than 40 encoded characters from any chunk start: the whole run must be decoded
    for label in ("northwindtraders", "contosopharmaceuticals", "fabrikamresidences"):
        b = base64.b64encode(f"jo at {label}{label} dot photography".encode()).decode()
        assert sp.encoded_email("precio " + " ".join(b[i:i + 7] for i in range(0, len(b), 7))), label


def test_encoded_email_after_a_tab_in_the_decoded_text():
    rng = _rng()
    for _ in range(20):
        v = rc.email(rng)
        b = base64.b64encode(("contact:\t" + v).encode()).decode()
        assert sp.encoded_email(b) and sp.encoded_email("rug " + rc.chunk(rng, b))


def test_number_words_glued_to_gap_letters():
    rng = _rng()
    for gen in (rc.ssn, rc.nanp, rc.card):
        for _ in range(60):
            d = gen(rng)
            words = rng.choice((rc.EN, rc.ES))
            q = "".join(words[int(c)] + rng.choice("xqkw") for c in d)[:-1]
            assert _drop(q), gen.__name__


# ------------------------------------------------------------------ MUST 6: the glued trim, narrowed
def test_glued_trim_only_for_unit_and_model_words():
    assert sp.longest_digit_run("ddr5 6400 64gb") == 4
    assert sp.longest_digit_run("gen4 2280 2tb") == 4
    assert sp.longest_digit_run("x5 55 0199") == 7               # "x" is not a unit or model word
    assert sp.longest_digit_run("ab55 501 99") == 7
    assert sp.longest_digit_run("me5 55 0199") == 7
    assert sp.longest_digit_run("5 55 0199zz") == 7
    assert sp.longest_digit_run("55 501 99ab") == 7              # two digits glued to "ab" at the end: counted
    assert sp.longest_digit_run("55 501 99gb") == 5              # glued to a unit: part of that word


# ------------------------------------------------------------------ MUST 7: ordinary codes kept by the generic rules
def _upc(rng):
    return r19._check_upc("".join(str(rng.randint(0, 9)) for _ in range(11)))


def test_tire_sizes_spaced_and_slashed_are_not_a_generic_digit_run():
    for q in ("295 75 22 tires", "295/75/22", "p205 35 14 xl price", "175/30/17 116s tires", "lt245 70 17"):
        assert sp.longest_digit_run(sp.detector_view(sp.generic_exempt(q))) < sp.PII_DIGIT_RUN, q


def test_gtin_with_valid_check_digit_is_kept_by_the_generic_rules():
    rng = _rng()
    for _ in range(100):
        u = _upc(rng)
        assert sp._gtin_valid(u)
        assert "#" in sp.generic_exempt(f"upc {u}")
        bad = u[:-1] + str((int(u[-1]) + 1) % 10)
        assert not sp._gtin_valid(bad)
        assert sp.generic_exempt(f"upc {bad}") == f"upc {bad}"          # an invalid check digit is not exempt
        assert sp.generic_exempt(u) == u                                # no code word: not exempt


def test_part_numbers_and_flight_times_are_kept_unless_a_detector_fires():
    for q in ("part number 62-3580-45", "oem part 12-3456-78", "flight 11/14 10:30", "dl1831 9:30",
              "asin b0abcdefgh"):
        assert not _drop(q), q
    rng = _rng()
    for _ in range(50):                                     # a phone or SSN after a part word: the detectors drop it
        d = rc.nanp(rng)
        assert _drop(f"part number {d[:3]}-{d[3:6]}-{d[6:]}")
        s = rc.ssn(rng)
        assert _drop(f"replacement part {s[:3]}-{s[3:5]}-{s[5:]}")


def test_state_code_glued_to_five_digits_is_a_product_code():
    assert not sp.looks_like_address("ge sku dc56249")
    assert sp.looks_like_address("boise id 83702")
    assert sp.looks_like_address("washington dc, 20500")


# ------------------------------------------------------------------ SHOULD / NIT: the E.164 stream rule pinned
def test_e164_stream_eight_digits_and_a_plus_up_to_three_characters_before():
    rng = _rng()
    for gap_before in ("", "x", "qk", "wqk"):
        for _ in range(40):
            d = "".join(str(rng.randint(0, 9)) for _ in range(8))
            q = "+" + gap_before + "q".join((d[:2], d[2:5], d[5:]))
            assert sp.digit_stream_hit(q), gap_before
    d = "12345678"
    assert not sp.digit_stream_hit("+wqkz" + "q".join((d[:2], d[2:5], d[5:])))   # four: not a gap of the list
    assert not sp.digit_stream_hit("q".join(("12", "345", "678")))              # no '+': 8 digits are no value


def test_de_separated_rule_letter_words_after_the_linear_rewrite():
    """A digit group glued to letters on both sides belongs to the word only when the word has MORE letters than
    digits (round 15's rule, kept by round 19's one-pass rewrite of ``_deseparated_hit``)."""
    d = "202" + "555" + "0100"
    assert sp._deseparated_hit("abcde" + d + "fghij")        # 10 letters, 10 digits: a number group
    assert not sp._deseparated_hit("abcdef" + d + "fghij")   # 11 letters, 10 digits: part of the word


# ------------------------------------------------------------------ SHOULD: linear encoded-e-mail search
def test_encoded_email_decodes_once_per_window_start(monkeypatch):
    calls = [0]
    real = sp._decodings

    def counting(s):
        calls[0] += 1
        return real(s)
    monkeypatch.setattr(sp, "_decodings", counting)
    counts = []
    for n in (32, 64, 128):
        calls[0] = 0
        sp.encoded_email(" ".join("QUJD" for _ in range(n)))
        counts.append(calls[0])
    assert counts[1] <= 2.2 * counts[0] and counts[2] <= 2.2 * counts[1], counts


# ------------------------------------------------------------------ SHOULD: Priya classifies each text once
def test_priya_classifies_each_text_once(monkeypatch):
    seen = collections.Counter()
    real = sp.classify_query

    def counting(q, brands, _depth=0):
        if not _depth:
            seen[q] += 1
        return real(q, brands, _depth)
    monkeypatch.setattr(sp, "classify_query", counting)
    rows = [(q, 3, 120, 0.025, 4.0) for q in ("Rug Cleaner Prices", "cheap rug cleaner", "rug cleaner for pets")]
    led = build(gsc(rows + list(SITE)))                    # each text once among the rows
    assert led["rows"]
    assert any(r["origin"] == "derived" for r in led["rows"])          # derived variants are classified once too
    assert seen and max(seen.values()) == 1, [q for q, n in seen.items() if n > 1][:5]


# measured at 2026-11-08.19 (search_performance) on ordinary_r19(24000): 1,760 of 24,000; 1,708 by a rule of the list
# (the digit stream / SSN / vanity on the text as written) and 52 by older rules outside it (the round-12 homoglyph
# view, 39; the round-15 de-separated phone shape whose exchange may start 0 or 1, 13). ADR 0047 has the table.
MEASURED_R19 = {"date": 55, "date_time": 389, "flight": 115, "model_number": 1, "part_number": 17, "price": 1,
                "size": 21, "tire_size": 527, "upc_isbn": 123, "version": 83, "zip_year": 428}
