"""P2-3 (priya-spec/2 F-1..F-5, F-10, S2-1..S2-3, OI-1..OI-3, PF-3): the READABLE / WITHHELD verdict in priya.build
and _derive. Stage 1 = the ordinary gate OG (agents.priya_gate), stage 2 = the v1 detectors (classify_query, unchanged).
Withheld-row storage (codes, F-7) is P2-4; metrics conservation (MC) is P2-6."""

from __future__ import annotations

import json

import pytest
from test_priya import SITE, build, coder, gsc

from agents import priya, priya_gate as pg, search_performance as sp
from agents.priya import SourceResult

V = pg.load_vocab()
T = frozenset()
LONG = "kitten " * 74  # 518 characters, every word in V

OG_CASES = [
    ("mail me@example", "OG-1"),
    ("moskva \u043c\u043e2024", "OG-2"),
    ("jane at example dot com", "OG-3"),
    ("qj zx vk", "OG-4"),
    ("one two three four", "OG-5"),
    ("zzqxvbt kitten", "OG-6"),
    ("\u6771\u4eacabc", "OG-7"),
    (LONG, "OG-8"),
]


def bing(rows):
    return SourceResult("bing_webmaster", "OK", "OK", rows, {})


@pytest.mark.parametrize(("q", "code"), OG_CASES)
def test_each_og_rule_withholds_with_its_code(q, code):
    assert priya.verdict(q, [], V, T) == ("WITHHELD", code, None)


def test_stage2_withholds_with_its_s2_code():
    assert priya.verdict("12345 2019", [], V, T) == ("WITHHELD", "S2:pii_suspect", None)
    assert priya.verdict("call 555 123 4567", [], V, T)[:2] == (
        "WITHHELD",
        "S2:pii_suspect",
    )


@pytest.mark.parametrize(
    ("q", "code"),
    [
        ("mail me@example", "OG-1"),
        ("jane at example dot com", "OG-3"),
        ("jane_at_example_dot_com", "OG-3"),
        (LONG, "OG-8"),
    ],
)
def test_both_stages_fire_the_og_code_wins(q, code):
    assert sp.classify_query(q, []) == priya.PII_CLASS
    assert priya.verdict(q, [], V, T)[:2] == ("WITHHELD", code)


def test_readable_needs_both_stages_and_keeps_the_v1_intent_class():
    assert priya.verdict("kitten food", [], V, T) == ("READABLE", None, "other")
    assert priya.verdict("best cat food 2024", [], V, T) == (
        "READABLE",
        None,
        "commercial",
    )


def test_reason_codes_are_a_closed_set_without_values():
    assert priya.REASON_CODES == tuple(f"OG-{n}" for n in range(1, 9)) + (
        "S2:pii_suspect",
    )
    for q, _ in OG_CASES + [("12345 2019", "")]:
        reason = priya.verdict(q, [], V, T)[1]
        assert reason in priya.REASON_CODES and q.strip()[:6] not in reason


def test_build_withholds_rows_keeps_their_metrics_and_never_their_text():
    rows = [
        ("kitten food", 3, 40, 0.075, 2.0),
        ("mail me@example", 1, 7, 0.14, 4.0),
        ("qj zx vk", 0, 5, 0.0, 9.0),
        ("12345 2019", 2, 11, 0.18, 3.0),
        (LONG, 0, 3, 0.0, 8.0),
    ]
    led = build(gsc(rows))
    assert [r["query"]["text"] for r in led["rows"]] == ["kitten food"]
    w = led["counts"]["withheld"]
    assert w["queries"] == 4 and w["clicks"] == 3 and w["impressions"] == 26
    assert w["by_reason"] == {
        "OG-1": {"queries": 1, "clicks": 1, "impressions": 7},
        "OG-4": {"queries": 1, "clicks": 0, "impressions": 5},
        "OG-8": {"queries": 1, "clicks": 0, "impressions": 3},
        "S2:pii_suspect": {"queries": 1, "clicks": 2, "impressions": 11},
    }
    assert led["counts"]["pii_suspect_dropped"] == {
        "queries": 4,
        "clicks": 3,
        "impressions": 26,
    }
    # F-7: one coded WITHHELD row per withheld key, same reasons as the counts.
    assert sorted(r["reason"] for r in led["withheld_rows"]) == sorted(w["by_reason"])
    assert all(r["code"].startswith("{c:query:") for r in led["withheld_rows"])
    blob = json.dumps(led)
    for q in ("me@example", "qj zx vk", "12345 2019", "kitten kitten"):
        assert q not in blob
    assert led["counts"]["gate_status"] == "OK"


def test_over_long_queries_are_withheld_og8_first_and_og1_wins_within_the_bound():
    huge = "kitten " * 3000
    short = (
        "a@b " + "kitten " * 72
    )  # 508 characters: within the bound, so OG-1 still fires first
    assert len(short) <= 512 < len("a@b " + LONG)
    led = build(
        gsc(
            [
                (LONG, 1, 9, 0.11, 2.0),
                (huge, 2, 4, 0.5, 1.0),
                ("a@b " + LONG, 0, 6, 0.0, 3.0),
                (short, 1, 5, 0.2, 2.0),
            ]
        )
    )
    assert led["rows"] == []
    by = led["counts"]["withheld"]["by_reason"]
    # Lead ruling I-17 (12:00 PT): over the bound -> OG-8 before any other rule, so "a@b " + LONG is OG-8, not OG-1.
    assert by == {
        "OG-1": {"queries": 1, "clicks": 1, "impressions": 5},
        "OG-8": {"queries": 3, "clicks": 3, "impressions": 19},
    }


def test_a_key_is_withheld_when_any_spelling_of_it_is_withheld(monkeypatch):
    real = pg.og
    monkeypatch.setattr(
        pg,
        "og",
        lambda q, v, t=frozenset(): (
            ("WITHHOLD", "OG-6") if q == "KITTEN FOOD" else real(q, v, t)
        ),
    )
    led = build(
        gsc([("kitten food", 3, 40, 0.075, 2.0), ("KITTEN FOOD", 1, 2, 0.5, 1.0)])
    )
    assert led["rows"] == [] and "kitten food" not in json.dumps(led)
    assert led["counts"]["withheld"]["by_reason"] == {
        "OG-6": {"queries": 1, "clicks": 4, "impressions": 42}
    }


def test_one_verdict_per_query_text_and_stage2_never_sees_v_or_t(monkeypatch):
    calls = []
    real = sp.classify_query

    def spy(*args, **kw):
        calls.append((args, kw))
        return real(*args, **kw)

    monkeypatch.setattr(sp, "classify_query", spy)
    led = build(
        gsc([("kitten food", 3, 40, 0.075, 2.0), ("qj zx vk", 0, 5, 0.0, 9.0)]),
        bing(
            [("kitten food", "2026-10-1" + str(d), 1, 5, 2.0, 2.0) for d in range(3)]
            + [("qj zx vk", "2026-10-12", 0, 1, 1.0, 1.0)]
        ),
    )
    pf3 = led["counts"]["classifications"]
    assert pf3 == {"query_texts": 2, "verdicts": 2, "stage2_calls": 1}
    observed = [c for c in calls if c[0][0] in ("kitten food", "qj zx vk")]
    assert observed == [
        (("kitten food", ["acme"]), {})
    ]  # once; the OG-withheld text never reaches stage 2


def test_derive_never_emits_a_withheld_variant_or_a_withheld_key(monkeypatch):
    base = build(gsc(SITE))
    derived = [r["query"]["text"] for r in base["rows"] if r["origin"] == "derived"]
    assert derived
    v = derived[0]
    real = pg.og
    monkeypatch.setattr(
        pg,
        "og",
        lambda q, vv, t=frozenset(): ("WITHHOLD", "OG-6") if q == v else real(q, vv, t),
    )
    led = build(gsc(SITE))
    assert v not in {r["query"]["text"] for r in led["rows"]}
    before = base["counts"]["derived_withheld"].get("OG-6", 0)
    assert led["counts"]["derived_withheld"]["OG-6"] == before + 1
    # the same text observed and withheld: never derived (its key would show)
    led = build(gsc(SITE + [(v, 0, 2, 0.0, 5.0)]))
    # (v may still occur inside a LONGER readable text that passed both stages on its own)
    assert v not in {r["query"]["text"] for r in led["rows"]}
    assert priya.row_key(v) not in {r["key"] for r in led["rows"]}
    observed_og6 = base["counts"]["withheld"]["by_reason"].get("OG-6", {"queries": 0})[
        "queries"
    ]
    assert led["counts"]["withheld"]["by_reason"]["OG-6"]["queries"] == observed_og6 + 1


def test_failed_vocabulary_pin_withholds_everything_og6():
    led = priya.build(
        "acme",
        "seo-kwl-" + "0" * 40,
        "2026-11-08T12:00:00+00:00",
        ["acme"],
        gsc([("kitten food", 3, 40, 0.075, 2.0)]),
        SourceResult("bing_webmaster", "NOT_REQUESTED", "NOT_REQUESTED"),
        vocab=pg.Vocab("VOCAB_MISMATCH", None),
        coder=coder(),
    )
    assert led["rows"] == [] and led["counts"]["gate_status"] == "VOCAB_MISMATCH"
    assert led["counts"]["withheld"]["by_reason"] == {
        "OG-6": {"queries": 1, "clicks": 3, "impressions": 40}
    }


OI1 = [
    f"{z} {y}"
    for z in ("12345", "23456", "34567", "45678", "56789", "10101")
    for y in range(1990, 2026)
]
OI2 = [
    f"jane{a}example{d}com" for a in ("_at_", "-at-") for d in ("_dot_", "-dot-")
] + ["mail jane_at_example_dot_com now", "j.doe-at-mail-dot-org"]
OI3 = [
    "+(1) 555 123 4567",
    "+ (44) 20 7946 0958",
    "+-1 202 555 0100",
    "call +ab 1 415 555 2671",
]


@pytest.mark.parametrize(
    ("name", "cases"), [("OI-1", OI1), ("OI-2", OI2), ("OI-3", OI3)]
)
def test_owner_items_have_zero_readable(name, cases):
    readable = [q for q in cases if priya.verdict(q, [], V, T)[0] == "READABLE"]
    assert readable == [], name
