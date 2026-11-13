"""Regression timing for the shared redaction core ``agents.secret_guard`` (core v2 r2 part 1): the inputs that used to
backtrack exponentially or run in quadratic time stay linear, and every faster form decides exactly what the original
pattern decided. All inputs are harmless text (letter runs, short words, innocent URLs, prices)."""

from __future__ import annotations

import itertools
import random
import re
import time

import pytest

from agents import secret_guard as sg

_LETTERS = "thequickbrownfoxjumpsoverthelazydogandkeepsrunning"


def _fill(unit: str, n: int) -> str:
    return (unit * (n // len(unit) + 1))[:n]


FAMILIES = {
    "slug_5th_digit": lambda n: _fill("guide-" + _LETTERS[:22] + "12345 ", n),     # was exponential (_PLAIN_WORD)
    "slug_capital": lambda n: _fill("guide-" + _LETTERS[:22] + "X ", n),
    "slug_accent": lambda n: _fill("guide-" + _LETTERS[:22] + "\u00e9 ", n),
    "slug_percent": lambda n: _fill("guide-" + _LETTERS[:22] + "% ", n),
    "short_pairs": lambda n: _fill("the s id and the s ig of c le or p w ", n),     # was quadratic (pair search)
    "url_dense": lambda n: _fill("See https://example.com/blog/post-12 for more. ", n),
    "url_64_parts": lambda n: _fill("See https://shop.blog.example.com/a/b/c/d/e/f/g?x=1&y=2&z=3 now. ", 64 * 65)
    + _fill("the best shop tools for a small garden ", max(0, n - 64 * 65)),
    "prices": lambda n: _fill("Item costs $12.99 today, was 15.49 in 2024. ", n),
    "eyj_run": lambda n: _fill("eyJ", n),                                            # was quadratic (jwt family)
    "spelled_dot": lambda n: _fill("meet x at " + "dot" * 130 + " ", n),             # was ~50 s at 64 KB
}


def _deep(n: int) -> dict:
    leaf = "the best shop tools for a small garden, price 12.99 "
    per = max(1, n // (15 * len(leaf)))
    rec: dict = {"note": [leaf] * per}
    for i in range(14):
        rec = {f"level_{i}": rec, "text": [leaf] * per}
    return rec


def _timed(fn, arg) -> float:
    best = None
    for _ in range(2):
        t = time.perf_counter()
        fn(arg)
        d = time.perf_counter() - t
        best = d if best is None else min(best, d)
    return best


@pytest.mark.parametrize("name", sorted(FAMILIES))
def test_check_and_guard_record_are_linear_on_harmless_worst_cases(name):
    gen = FAMILIES[name]
    t16 = _timed(sg.check, gen(16 * 1024))
    t64 = _timed(sg.check, gen(64 * 1024))
    assert t64 < 1.5, (name, t64)                                  # ~120 ms measured; generous for slow CI
    assert t64 < 8 * t16 + 0.05, (name, t16, t64)                  # 4x the input, ~4x the time
    assert _timed(sg.guard_record, {"text": gen(64 * 1024)}) < 1.5


def test_guard_record_deep_nesting_is_linear():
    t16, t64 = _timed(sg.guard_record, _deep(16 * 1024)), _timed(sg.guard_record, _deep(64 * 1024))
    assert t64 < 1.5 and t64 < 8 * t16 + 0.05, (t16, t64)


def test_plain_word_accepts_exactly_what_the_nested_pattern_accepted():
    nested = re.compile(r"(?:[A-Z]?[a-z]+)+\d{0,4}|[A-Z]{2,6}\d{0,4}|\d{1,4}")
    for n in range(7):
        for t in itertools.product("aBz19\u00e9%", repeat=n):
            s = "".join(t)
            assert bool(nested.fullmatch(s)) == bool(sg._PLAIN_WORD.fullmatch(s)), s
    t = time.perf_counter()
    assert sg._PLAIN_WORD.fullmatch("a" * 60000 + "12345") is None
    assert time.perf_counter() - t < 0.5


def test_linear_spelled_email_decides_what_the_regex_decides():
    rnd = random.Random(20261109)
    alpha = ["x", " at ", "dot", " ", "a", "1", "-", "[", "]", "(", ")", "<", ">", ".", "K", "\u212a", "\u017f",
             "\u00e9", "D", "O", "T", "AT", "q", "_", "%"]
    for _ in range(20000):
        s = "".join(rnd.choice(alpha) for _ in range(rnd.randint(1, 25)))
        assert (sg._EMAIL_SPELLED.search(s) is not None) == sg._spelled_email(s), repr(s)
    for _ in range(2000):                                          # labels around the 63-character bound
        lens = [rnd.randint(1, 70) for _ in range(5)]
        s = "x at " + "a" * lens[0] + "".join(
            rnd.choice(["", " ", " [", " ("]) + "dot" + rnd.choice(["", " ", "] ", ")"]) + rnd.choice(["a", "dot", "1-"])
            * lens[k] for k in range(rnd.randint(1, 4))) + rnd.choice(["", "b", "1", " ", "\u00e9"])
        assert (sg._EMAIL_SPELLED.search(s) is not None) == sg._spelled_email(s), repr(s)


def test_linear_jwt_form_matches_exactly_when_the_documented_one_does():
    documented, linear = re.compile(dict(sg.PROVIDERS)["jwt"]), dict(sg._PREFIX_EACH)["jwt"]
    rnd = random.Random(7)
    alpha = ["eyJ", "a", "1", "_", "-", ".", " ", "e", "y", "J", "Z"]
    for _ in range(20000):
        s = "".join(rnd.choice(alpha) for _ in range(rnd.randint(1, 30)))
        assert bool(documented.search(s)) == bool(linear.search(s)), repr(s)


def test_split_short_label_with_a_separator_is_still_found_in_its_window():
    assert sg.check("the s id: 12") == "a_label"
    assert sg.check("padding " * 2000 + "p w = x") == "a_label"
    assert sg.check("the s id and the s ig of c le or p w " * 50) is None


def test_url_caps_per_field_fail_closed():
    url = "https://example.com/blog/post-12"
    assert sg.check(" and ".join([url] * sg.URL_TEXT_MAX)) is None
    assert sg.check(" and ".join([url] * (sg.URL_TEXT_MAX + 1))) == "cap"
    long_url = "https://example.com/" + "/".join(["a"] * (sg.URL_PARTS_MAX + 8))
    assert sg.check("see " + long_url) == "cap"
    assert sg.redact_text(" and ".join([url] * (sg.URL_TEXT_MAX + 1)))[0] == sg.WITHHELD
