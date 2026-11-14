"""Review round 16 (search_performance 2026-11-08.16) and war room WR-F020 / WR-F021.

- Detector separators are read by Unicode CATEGORY (every P*, S*, Z* character, and marks after one), never from a
  list: the property test draws its separators from ``unicodedata`` itself.
- 7-digit local numbers with a punctuation / symbol separator and vanity toll-free numbers are phones.
- The classifier's " at " e-mail rule is e-mail SHAPED (the log templater's ``logs._AT_WORD`` is unchanged).
- Context never hides a value: every known-dropped value stays dropped wrapped in words, tabs, emoji, CJK and
  punctuation (``_suspicious_windows``).
Values are public test numbers, 555 numbers and documented example SSNs, built from parts."""

from __future__ import annotations

import itertools
import unicodedata

import attack_corpus as ac
import pytest

from agents import search_performance as sp

J = "".join
VALUES = [[J(("4111", "1111", "1111", "1111"))[i:i + 4] for i in range(0, 16, 4)], ["202", "555", "0100"],
          ["555", "010", "0199"], ["123", "45", "6789"], ["934", "333", "2089"]]


def _category_separators(step_symbols: int = 8):
    """Every P* and Z* character and every ``step_symbols``-th S* character (all of them in the reviewers' tool),
    minus those NFKC turns into a letter or digit."""
    n_sym = 0
    for cp in range(0x110000):
        c = chr(cp)
        cat = unicodedata.category(c)
        if cat[0] not in "PSZ" or any(ch.isalnum() for ch in unicodedata.normalize("NFKC", c)):
            continue
        if cat[0] == "S":
            n_sym += 1
            if n_sym % step_symbols:
                continue
        yield c


def test_property_every_unicode_punctuation_symbol_or_space_is_a_separator():
    leaks = []
    for c in _category_separators():
        for groups in VALUES:
            for shape in ("{v}", "rfc {v}", "{v} 2024"):
                q = shape.format(v=c.join(groups))
                if sp.classify_query(q, ["acme"]) != "pii_suspect":
                    leaks.append((f"U+{ord(c):04X}", q))
    assert leaks == [], leaks[:10]


BARE_SEPARATOR_DROP = ["934•333•2089", "934~333~2089", "934*333*2089", "934・333・2089",
                       "934⁃333⁃2089", "934‧333‧2089", "934˗333˗2089",
                       "934⁄333⁄2089", "934∕333∕2089", "rfc 878–70–3759",
                       "934–333–2089 2024", "934—333—2089", "934−333−2089",
                       "934·333·2089", "934‐333‐2089", "202%555%0100"]
LOCAL_AND_VANITY_DROP = ["version 814.0202", "call 555-0199", "1-800-flo-wers", "800 go fedex", "1 (888) callnow",
                         "814•0202"]


@pytest.mark.parametrize("q", BARE_SEPARATOR_DROP + LOCAL_AND_VANITY_DROP)
def test_round16_phone_and_ssn_shapes_drop(q):
    assert sp.classify_query(q, ["acme"]) == "pii_suspect", q


AT_EMAIL_DROP = ["john at gmail dot com", "john (at) gmail (dot) com", "john at gmail.com", "jane at gmail",
                 "j.doe [at] example [dot] org", "mail me jane at yahoo dot co dot uk", "john {at} outlook {dot} com"]
_HEADS = ["deals", "restaurants", "meeting", "dinner", "jobs", "hotels", "coffee", "parking", "events", "sales",
          "happy hour", "brunch", "shows", "concerts", "classes", "tickets", "rooms", "rentals", "museum", "spa"]
_PLACES = ["walmart", "disney springs", "target", "costco", "the airport", "home", "work", "night", "noon", "2pm",
           "7", "the mall", "times square", "the beach", "harvard", "the park", "the bay", "the met", "amazon",
           "best buy"]


@pytest.mark.parametrize("q", AT_EMAIL_DROP)
def test_spelled_at_emails_still_drop(q):
    assert sp.classify_query(q, ["acme"]) == "pii_suspect", q


def test_ordinary_at_queries_are_kept_over_drop_at_most_one_percent():
    qs = [f"{h} at {p}" for h, p in itertools.product(_HEADS, _PLACES)]
    dropped = [q for q in qs if sp.classify_query(q, ["acme"]) == "pii_suspect"]
    assert len(qs) == 400 and len(dropped) <= 4, dropped                      # measured: 0


def test_mutant_guard_a_date_is_exempt_only_when_it_stands_alone():
    """Kills the mutant that exempts every date (round 14's leak): with digits next to it, a date stays in."""
    assert sp.isolated_dates_removed("2025-11-08 hotel").startswith("#")
    kept_in = sp.isolated_dates_removed("2025/5/5 01")
    assert "2025" in kept_in and "5" in kept_in
    assert sp.classify_query("2025/5/5 01", ["acme"]) == "pii_suspect"         # 8 digits: the generic run rule
    assert sp.classify_query("2025/5/5 hotel", ["acme"]) != "pii_suspect"


WRAPS = ["rug cleaner {v}", "{v} rug cleaner", "東京 {v}", "{v} 東京", "\U0001f600 {v} \U0001f600",
         "{v}\U0001f525", "rug\t{v}\tcleaner", "best pizza near me {v} open now", "({v})", "「{v}」", "{v}!!",
         "call me {v} tonight", "\U0001f4de{v}", "precio {v} barato"]


def test_property_context_never_hides_a_dropped_value_single():
    leaks = []
    for b, n, v in ac.cases():
        assert sp.classify_query(v, ["acme"]) == "pii_suspect", (b, n)
        leaks += [(b, n, w) for w in WRAPS if sp.classify_query(w.format(v=v), ["acme"]) != "pii_suspect"]
    assert leaks == [], leaks[:10]


def test_property_context_never_hides_a_dropped_value_composed_sample():
    leaks = []
    for b, n, v in ac.composed_cases(every=ac.COMPOSED_EVERY * 5):
        leaks += [(b, n, w) for w in WRAPS[:6] if sp.classify_query(w.format(v=v), ["acme"]) != "pii_suspect"]
    assert leaks == [], leaks[:10]


WAR_ROOM_EMBEDDED = ["rug cleaner amFuZS5kb2 VAZXhhbXBs ZS5jb20=", "東京 amFuZS5kb2 VAZXhhbXBs ZS5jb20=",
                     "\U0001f600 amFuZS5kb2 VAZXhhbXBs ZS5jb20= \U0001f600"]


@pytest.mark.parametrize("q", WAR_ROOM_EMBEDDED)
def test_war_room_f020_base64_chunks_in_context_drop(q):
    assert sp.classify_query(q, ["acme"]) == "pii_suspect", q


def test_windows_never_run_the_generic_rules():
    for q in ("firmware 2.3.11.4 router", "1980s 1990s 2000s hits", "windows 10.0.22631 update"):
        assert sp.classify_query(q, ["acme"]) != "pii_suspect", q
