"""Review round 18 (search_performance 2026-11-08.18): certification against the CLOSED threat list of ADR 0047
("Personal data", the list). Every personal-data value is synthetic and generated at run time
(``privacy_r18_corpus``); no value is written here.

- Every class (card, NANP phone, E.164 phone, SSN, vanity toll-free, e-mail, street address) under every listed
  disguise, alone and in surrounding text: all dropped.
- Composed pairs (two list transforms on one value), 40,000 cases: the residual is pinned (measured: 2, 0.005%).
- The ordinary-query corpus (24,000 queries): the over-drop is pinned by class (measured), and the classes the list
  itself makes personal data are asserted dropped (``LIST_MANDATED_DROP``).
- The context-window check bites: a value only the window check finds."""

from __future__ import annotations

import base64
import collections
import json
import random

import privacy_r18_corpus as rc
import pytest
from gsc_fakes import FakeGoogle
from helpers import rid
from test_gsc import PROP, bind, gsc_harness

from agents import priya
from agents import search_performance as sp

BRANDS = ["acme"]


def _kept(qs):
    return [q for q in qs if sp.classify_query(q, BRANDS) != "pii_suspect"]


# ------------------------------------------------------------------ M1 (the committed sample; 2,000 per class offline)
def test_closed_list_every_class_every_disguise_alone_and_in_context_drops():
    kept = collections.Counter()
    total = collections.Counter()
    for cls, name, place, t, _v in rc.closed_list(per_class=150):
        total[(cls, name, place)] += 1
        if sp.classify_query(t, BRANDS) != "pii_suspect":
            kept[(cls, name, place)] += 1
    assert len(total) == 2 * (4 * len(rc.DIGIT_TRANSFORMS) + 3 + len(rc.EMAIL_SINGLE) + 3)
    assert sum(kept.values()) == 0, dict(kept)


def test_normalised_key_of_every_list_case_is_dropped_too():
    """Priya classifies the engine's text, then her normalised key: the key of a list case is dropped too. Base64 is
    case-sensitive: case-folding turns it into other bytes, so its key is not checked here (the raw text is always
    dropped first, above, and a dropped query never reaches the key)."""
    leaks = [(c, n) for c, n, _p, t, _v in rc.closed_list(per_class=20)
             if not n.startswith("base64") and sp.classify_query(priya.normalize(t), BRANDS) != "pii_suspect"]
    assert leaks == [], leaks[:10]


# ------------------------------------------------------------------ M2
COMPOSED_N = 40000
COMPOSED_RESIDUAL_MAX = 2                  # measured at 2026-11-08.18: 2 of 40,000 (0.005%); the bar is 1%


def test_composed_pairs_residual_is_pinned():
    cases = list(rc.composed(COMPOSED_N))
    assert len(cases) == COMPOSED_N
    kept = [(c, n) for c, n, t, _v in cases if sp.classify_query(t, BRANDS) != "pii_suspect"]
    assert len(kept) <= COMPOSED_RESIDUAL_MAX, kept[:20]
    assert len(kept) <= 0.01 * COMPOSED_N


# ------------------------------------------------------------------ M3: over-drop, pinned by class
# measured at 2026-11-08.18 on ordinary(24000); every class with a drop is disclosed in ADR 0047 (round 18)
OVER_DROP = {"date": 22, "model_number": 2, "size": 20, "version": 53, "zip_year": 65}


def test_ordinary_corpus_over_drop_is_pinned_by_class():
    dropped = collections.Counter(c for c, t in rc.ordinary(24000) if sp.classify_query(t, BRANDS) == "pii_suspect")
    assert dict(dropped) == OVER_DROP
    for cls in ("plain_text", "multilingual", "question", "local", "price", "year", "count_quantity"):
        assert dropped[cls] == 0, cls


# Moved here from the KEEP lists of rounds 13 and 15: each is a digit stream of the list (gaps of at most three
# non-digit characters) whose whole-group windows spell a valid NANP phone or SSN, so the list makes it personal data
# (a date followed by a time; years and a "y" gap). Disclosed in ADR 0047.
LIST_MANDATED_DROP = [
    "2025-11-08 10am", "exam 12/15/2025 9am", "dentist 11/10/2025 3:30", "2025-11-08T09:30 meeting",
    "2025-11-08 09:30 utc", "flight 2025-12-24 6:45am", "meeting 2025-11-08 14:00", "2025-11-08 10:30:00z log",
    "1999 2000 y2k",
]


@pytest.mark.parametrize("q", LIST_MANDATED_DROP)
def test_list_mandated_drops(q):
    assert sp.classify_query(q, BRANDS) == "pii_suspect", q


STILL_KEPT = ["11/8/2025 7pm", "10/31/2025 halloween 7pm", "90210 2024 restaurants", "2025-11-08 hotel",
              "chrome 124.0.6367.91 download", "python 3.12.4", "iphone 15 pro max 256gb", "225/45r17 tires",
              "deals at walmart", "meeting at 2pm", "the dot com bubble",
              "dos camas hotel barato", "one two three song", "800 thread count sheets",
              "rug cleaner near me", "\u6771\u4eac \u30db\u30c6\u30eb \u5b89\u3044", "5w30 oil 5 qt"]


def test_still_kept_moved_list_mandated():
    # priya-spec/1 (C6 any TLD, D20 reversed; G-7): moved from STILL_KEPT - read backwards, "polka dot dress at
    # target" is a mailbox with the TLD "polka"; the independent oracle labels it list-mandated
    from conformance.oracle import priya_list
    q = "polka dot dress at target"
    assert priya_list.mandated(q) and sp.classify_query(q, BRANDS) == "pii_suspect"


@pytest.mark.parametrize("q", STILL_KEPT)
def test_ordinary_neighbours_of_the_new_rules_are_kept(q):
    assert sp.classify_query(q, BRANDS) != "pii_suspect", q


def test_generic_run_leaves_out_short_numbers_glued_to_a_word_at_its_ends():
    # round 18 (generic digit-run rule only): "ddr5" and "64gb" are words, so the run is the speed alone
    assert sp.longest_digit_run("ddr5 6400 64gb") == 4
    assert sp.longest_digit_run("ddr4 3200 16gb review") == 4
    assert sp.longest_digit_run("ddr5 6400 64") == 6               # glued at the start only
    assert sp.longest_digit_run("5 6400 64gb") == 5                # glued at the end only
    assert sp.longest_digit_run("5 6400 64") == 7                  # nothing glued: one run
    assert sp.longest_digit_run("x555 0199") == 7                  # three digits glued: still counted
    assert sp.longest_digit_run("555 0199x") == 7
    assert sp.longest_digit_run("5550 199x") == 7                  # three digits glued at the end: counted
    assert sp.longest_digit_run("rx12 34567") == 5                 # two digits glued at the start: left out
    assert sp.longest_digit_run("tel5550199") == 7                 # a lone group always counts
    assert sp.longest_digit_run("ab12") == 2
    assert sp.longest_digit_run("12ab") == 2
    assert sp.classify_query("ddr5 6400 64gb", BRANDS) != "pii_suspect"
    assert sp.classify_query("ddr5 6000 32gb deal", BRANDS) != "pii_suspect"
    assert sp.classify_query("call 555 0199x", BRANDS) == "pii_suspect"
    assert sp.classify_query("tel5550199", BRANDS) == "pii_suspect"


# ------------------------------------------------------------------ the new rules, one by one
def _rng():
    return random.Random(1818)


def test_digit_stream_letters_and_any_separator_up_to_three():
    rng = _rng()
    for gen in (rc.card, rc.nanp, rc.ssn):
        for _ in range(50):
            d = gen(rng)
            for gap in ("x", "ab", "xyz", "\u2022", "a-", " b ", "\u00e9", "\uff41"):
                assert sp.classify_query(gap.join(d), BRANDS) == "pii_suspect", (gen.__name__, gap)
    # four characters is not a gap of the list (the older filler rules may still catch some)
    assert not sp.digit_stream_hit("123abcd45abcd6789")
    assert sp.digit_stream_hit("123abc45abc6789")


def test_every_format_character_between_digits_or_inside_an_email():
    """List item 3, exhaustively: every Unicode format character (Cf) and the Hangul fillers, between every
    character of a card, a phone, an SSN and an e-mail."""
    import unicodedata
    chars = [chr(c) for c in range(0x110000) if unicodedata.category(chr(c)) == "Cf"]
    chars += ["\u115f", "\u1160", "\u3164", "\uffa0", "\u034f", "\u17b4", "\ufe0f", "\U000e0100"]
    rng = _rng()
    leaks = []
    for ch in chars:
        for gen in (rc.card, rc.nanp, rc.ssn, rc.email):
            if sp.classify_query(ch.join(gen(rng)), BRANDS) != "pii_suspect":
                leaks.append((f"U+{ord(ch):04X}", gen.__name__))
    assert len(chars) > 170 and leaks == [], leaks[:10]


def test_uatp_card_prefix():
    rng = _rng()
    for _ in range(30):
        body = "1" + "".join(str(rng.randint(0, 9)) for _ in range(13))
        d = body + rc._luhn_digit(body)
        assert len(d) == 15 and sp.stream_card(d)
        assert sp.classify_query(" ".join((d[:4], d[4:10], d[10:])), BRANDS) == "pii_suspect"
        assert sp.classify_query("q".join(d), BRANDS) == "pii_suspect"
    assert not sp.stream_card("1" * 16)


def test_diners_36_is_an_issuer_prefix():
    assert sp.card_iin("36" + "0" * 12)


def test_strict_nanp_for_the_stream():
    assert sp.nanp_phone("2025550100") and sp.nanp_phone("12025550100")
    assert not sp.nanp_phone("2020250100") and not sp.nanp_phone("1025550100")


def test_vanity_toll_free_any_separator_with_and_without_the_leading_one():
    rng = _rng()
    for _ in range(200):
        v = rc.vanity(rng)
        q = rc.vanity_form(rng, v)
        assert sp.classify_query(q, BRANDS) == "pii_suspect", "vanity"
        assert sp.classify_query("call " + q + " now", BRANDS) == "pii_suspect", "vanity in context"


def test_email_views_each_disguise_alone_and_chunked():
    rng = _rng()
    for _ in range(60):
        v = rc.email(rng)
        for name in rc.EMAIL_SINGLE:
            q = rc.email_single(rng, name, v)
            assert sp.classify_query(q, BRANDS) == "pii_suspect", name
            assert sp.classify_query("rug cleaner " + q + " thanks", BRANDS) == "pii_suspect", name


def test_rot13_and_reversed_spelled_emails():
    rng = _rng()
    v = rc.email(rng)
    spelled = rc._spelled_email(rng, v)
    for q in (sp.rot13(spelled), spelled[::-1], " ".join(w[::-1] for w in spelled.split()),
              rc._spelled_email(rng, v[::-1]), rc._spelled_email(rng, sp.rot13(v))):
        assert sp.email_views_hit(sp._LOOKALIKE.fold_cased(q)), "view"


def test_base64_email_split_into_chunks_inside_text():
    v = rc.email(_rng())
    b = base64.b64encode(v.encode()).decode()
    for k in (2, 3, 5):
        step = -(-len(b) // k)
        q = "precio " + " ".join(b[i:i + step] for i in range(0, len(b), step)) + ", near me"
        assert sp.encoded_email(q), k
        assert sp.classify_query(q, BRANDS) == "pii_suspect", k


def test_lower_case_base32_written_as_plain_words():
    rng = _rng()
    for _ in range(40):
        v = rc.email(rng)
        b = base64.b32encode(v.encode()).decode().rstrip("=").lower()
        q = "rug cleaner " + rc.chunk(rng, b) + " tonight"
        assert sp.classify_query(q, BRANDS) == "pii_suspect"


def test_two_encodings_deep_chunked_in_context():
    """Base64 of base64 of an e-mail, in chunks, after a word: only the second decoding level finds it."""
    rng = _rng()
    for _ in range(40):
        v = rc.email(rng)
        b = base64.b64encode(base64.b64encode(v.encode())).decode()
        assert sp.classify_query("rug " + rc.chunk(rng, b), BRANDS) == "pii_suspect"


def test_digit_stream_on_the_text_before_percent_decoding():
    """'%55' decodes into a letter: the stream is read on the text as written too (here with a letter that
    case-folding turns into two, so the folded views see longer gaps)."""
    assert sp.classify_query("202\u00df\u00df%555\u00df%0100", BRANDS) == "pii_suspect"


def test_lower_case_base32_with_no_digit_reads_as_plain_words():
    """The case ``_plain_base32`` exists for: lower-case base32 with no digit 2-7 looks like ordinary words, so only
    the plain-word path decodes it (seeded search for such e-mails, about 1 in 750)."""
    rng = _rng()
    found = 0
    while found < 5:
        v = rc.email(rng)
        b = base64.b32encode(v.encode()).decode().rstrip("=").lower()
        if any(c.isdigit() for c in b):
            continue
        found += 1
        for q in (b, rc.chunk(rng, b), "rug cleaner " + rc.chunk(rng, b) + " tonight"):
            assert sp.classify_query(q, BRANDS) == "pii_suspect"


def test_address_inside_surrounding_text():
    for q in ("17 elm way \u3067\u3059", "\u300c 62 oak lane, \U0001f600", "12 kings road x2", "9 mill drive tonight"):
        assert sp.classify_query(q, BRANDS) == "pii_suspect", q


# ------------------------------------------------------------------ the context-window check bites
def test_context_window_check_bites():
    """Base64 of a short readable phrase in context is found only by the window check (``_window_hit``): the
    whole query is not base64, and the token alone is under the word rules' lengths. A mutant that removes the
    window check, empties ``_suspicious_windows`` or calls every token ordinary fails here."""
    for phrase in ("my api key", "access code", "launch code"):
        b = base64.b64encode(phrase.encode()).decode()
        for q in ("rug cleaner " + b, "\u6771\u4eac " + b, b + " rug cleaner"):
            assert sp.classify_query(q, BRANDS) == "pii_suspect", phrase
    assert sp._suspicious_windows("rug cleaner " + base64.b64encode(b"my api key").decode())


# ------------------------------------------------------------------ performance: bounded work per query
def test_long_inputs_are_not_examined_and_adversarial_ones_finish():
    import time
    for q in (" ".join("aB" for _ in range(256)), "1a" * 256, " ".join(["a", "at", "b", "dot"] * 64),
              " ".join("QUJD" for _ in range(100)), "x" * 65536):
        t = time.perf_counter()
        sp.classify_query(q[:65536], BRANDS)
        assert time.perf_counter() - t < 2.0


# ------------------------------------------------------------------ the service path (ingest -> ledger -> API -> disk)
def test_service_path_never_carries_a_list_case(tmp_path):
    cases = [t for _c, _n, _p, t, _v in rc.closed_list(per_class=6)]
    values = [v for c, _n, _p, _t, v in rc.closed_list(per_class=6) if c in ("email", "address")]
    ordinary = [t for _c, t in rc.ordinary(120)]
    fake = FakeGoogle()
    fake.queries = [(t, 1, 30, 3.0) for t in dict.fromkeys(cases + ordinary)]
    fake.dates_clicks = (10, 150)
    h = gsc_harness(tmp_path, fake, data_dir=str(tmp_path / "data"))
    h.tenant("acme", "own", ("acme.test",))
    h.ok(bind(h, "acme", PROP))
    out = h.ok(h.post("/tenants/acme/keyword-ledger", {"request_id": rid()}), 201)
    led = out["ledger"]
    assert led["status"] == "OK" and led["counts"]["rows_dropped_past_cap"] == 0
    texts = {r["query"]["text"] for r in led["rows"]}
    assert not texts & {priya.normalize(t) for t in cases}
    blob = json.dumps(out, ensure_ascii=False) + json.dumps(h.ok(h.get("/tenants/acme/keyword-ledger-runs")),
                                                            ensure_ascii=False)
    for p in (tmp_path / "data").rglob("*"):
        if p.is_file():
            blob += p.read_bytes().decode("utf-8", "replace")
    needles = {x for t in cases for x in (t, priya.normalize(t)) if len(x) >= 6} | set(values)
    assert [n for n in needles if n in blob] == []
