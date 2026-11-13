"""priya-spec/1 section 7 (PF-1..PF-3). PF-2: 26 adversarial families at 64 / 128 / 256 / 384 / 512 characters, the
whole Priya path (raw + stored key): CI lane each doubling <= 3.0x (or under a 0.5 ms noise floor) and <= 100 ms at
512; heavy lane <= 2.5x and <= 50 ms. Over 512 characters a query is dropped whole (a card inside a 600-character
query reaches no output). PF-1 (median rise vs the two baselines) is the heavy lane's A/B; PF-3 (one classification
per row) is pinned in tests/test_query_privacy_r19.py."""

from __future__ import annotations

import base64
import random
import time

import privacy_r18_corpus as rc
import pytest
from priya_common import dropped

from agents import priya
from agents import search_performance as sp

R = random.Random(26)


def _b64(n, layers, chunk):
    s, t = "x", ""
    while len(t) < n:
        s += R.choice("abcdefghij .@")
        t = s.encode()
        for _ in range(layers):
            t = base64.b64encode(t)
        t = t.decode()
    t = t[:n]
    return " ".join(t[i:i + chunk] for i in range(0, len(t), chunk)) if chunk else t


FAMILIES = {
    "digit_letter_gaps": lambda n: "1a" * (n // 2), "gap3_digits": lambda n: "1abc" * (n // 4),
    "separator_groups": lambda n: "12-34." * (n // 6), "spelled_words": lambda n: "one two " * (n // 8),
    "spelled_run": lambda n: "onetwothree" * (n // 11), "vanity_like": lambda n: "1 800 " * (n // 6),
    "vanity_gaps": lambda n: "1x8y0z0 " * (n // 8), "at_dot_runs": lambda n: "a at b dot " * (n // 11),
    "at_sign_runs": lambda n: "a@b. " * (n // 5), "marker_mixes": lambda n: "[at](dot){@}<.> " * (n // 16),
    "base64_chunks": lambda n: "QUJD " * (n // 5), "mixed_case_base64": lambda n: "".join(
        random.Random(1).choice("aBcD0123+/") for _ in range(n)), "double_base64": lambda n: _b64(n, 2, 0),
    "double_base64_chunk4": lambda n: _b64(n, 2, 4), "triple_base64_chunk4": lambda n: _b64(n, 3, 4),
    "plain_base32_words": lambda n: "mfrgg zdfmz " * (n // 12), "upper_base32": lambda n: "MFRGGZDF" * (n // 8),
    "hex_chunks": lambda n: "6a61 6e65 " * (n // 10), "percent_runs": lambda n: "%4" * (n // 2),
    "one_char_words": lambda n: "a B " * (n // 4), "address_like": lambda n: "12 elm way " * (n // 11),
    "address_names": lambda n: "12 " + "oak " * (n // 4), "rot13_words": lambda n: "wnar ng rknzcyr " * (n // 16),
    "mixed_all": lambda n: "1a at QUJD %4 one [.] " * (n // 22), "fullwidth": lambda n: "\uff11\uff41" * (n // 2),
    "zero_width_digits": lambda n: "1\u200b" * (n // 2), "filler_words": lambda n: "to 12 " * (n // 6),
}
SIZES = (64, 128, 256, 384, 512)


def _t(q: str) -> float:
    """Best of 3 in process CPU time: on a shared machine the wall clock also counts the time other processes hold
    the CPU (PF-2's bound is on the work; the heavy lane on the quiet reference box may use the wall clock)."""
    best = 1e9
    for _ in range(3):
        t = time.process_time()
        sp.classify_query(q, ["acme"])
        sp.classify_query(priya.normalize(q), ["acme"])
        best = min(best, time.process_time() - t)
    return best


DOUBLINGS = ((64, 128), (128, 256), (256, 512))


def _check(ratio_max: float, abs_max: float, rounds: int = 5):
    """Each size's time is the best over ``rounds`` interleaved rounds (a shared machine's load changes over time,
    so all sizes are measured in every round); each DOUBLING (64->128, 128->256, 256->512) at most ``ratio_max``
    times slower (or under a 0.5 ms noise floor), and 512 characters under ``abs_max`` seconds."""
    bad = {}
    for name, f in FAMILIES.items():
        qs = {n: f(n)[:n] for n in SIZES}
        ts = {n: 1e9 for n in SIZES}
        for _ in range(rounds):
            for n in SIZES:
                ts[n] = min(ts[n], _t(qs[n]))
        for a, b in DOUBLINGS:
            if ts[b] > ratio_max * ts[a] and ts[b] - ts[a] > 0.0005:
                bad[name] = {n: round(ts[n] * 1e3, 2) for n in SIZES}
        if ts[512] > abs_max:
            bad[name] = {n: round(ts[n] * 1e3, 2) for n in SIZES}
    assert bad == {}, bad


@pytest.mark.conformance
def test_PF2_linear_and_bounded_ci():
    assert len(FAMILIES) >= 26
    _check(3.0, 0.100)


@pytest.mark.conformance_heavy
def test_PF2_linear_and_bounded_heavy():
    _check(2.5, 0.050)


@pytest.mark.conformance
def test_PF2_over_long_query_is_dropped_whole():
    d = rc.card(random.Random(600))
    q = ("rug cleaner " * 50)[:580] + " " + d
    assert len(q) > sp.MAX_QUERY_CHARS and dropped(q)


def priya_path(q: str) -> bool:
    """The whole Priya path as ``priya.build`` runs it now: the engine's text, then the stored key only when it
    differs (PF-3); the baselines' path classified the key whenever the text was kept (``_base`` in
    test_priya_lost_protection)."""
    if sp.classify_query(q, ["acme"]) == "pii_suspect":
        return True
    k = priya.normalize(q)
    return k != q and sp.classify_query(k, ["acme"]) == "pii_suspect"


@pytest.mark.conformance_heavy
def test_PF1_median_rise_vs_both_baselines_heavy():
    """Median per-query cost (process CPU time) of the whole Priya path on the OQ corpus (every 4th query), best of
    3, two runs: at most +20% against 644b4901 and 65f8b92a (their classifiers read from git history)."""
    import statistics

    import priya_gen as g
    from test_priya_lost_protection import BASES, _base

    qs = [q for _c, _i, q in g.ordinary()][::4]
    fns = {"head": priya_path, **{r[:8]: _base(r) for r in BASES}}

    def med(fn):
        out = []
        for q in qs:
            best = 1e9
            for _ in range(3):
                t = time.process_time()
                fn(q)
                best = min(best, time.process_time() - t)
            out.append(best)
        return statistics.median(out)
    for _run in range(2):
        m = {k: med(f) for k, f in fns.items()}
        for k in BASES:
            assert m["head"] <= 1.20 * m[k[:8]], m
