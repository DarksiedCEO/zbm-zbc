"""Adversarial-input harness for every regex in onboarding-py (fix wave 4, R1).

Collects every compiled pattern the service's scanning modules define and
builds hostile inputs for each: fixed shapes (runs of ``a``, ``a@``, ``a/``,
``a:``, alternating classes, blank lines, ...) plus shapes made from the
pattern's own literals ("login=", "eyJ-", "connect.facebook.net/", ...), each
with a few failing tails. Shared by the test module and the live probe.
"""

from __future__ import annotations

import importlib
import re
import time
from typing import Callable

MODULES = [
    "redaction", "guardrails", "memory", "ledger", "onboarding_schema", "onboarding_schema.money",
    "onboarding_schema.requests", "intelligences.i01_client_understanding", "intelligences.i02_conversation",
    "intelligences.i03_priority_fusion", "intelligences.i04_platform_access", "intelligences.i06_audit_baseline",
    "intelligences.i08_risk_anomaly", "intelligences.i11_creator_vetting", "intelligences.i12_brand_campaign",
    "intelligences.i13_learning_loop", "intelligences.i14_contract_obligation", "practices.ad_disclosure",
    "practices.recommend_score", "config", "service", "api",
]

# Patterns that are only ever applied ANCHORED (``match``/``fullmatch`` at one
# position) — never searched. For them the harness times their real use; a
# guard test checks the source never searches with them.
ANCHORED_ONLY = {
    "memory._EMAIL_AT": "match",
    "redaction._KEYWORD": "fullmatch",
    "redaction._TO_LOGIN": "fullmatch",
    "redaction._EMAIL_TOKEN": "fullmatch",
    "redaction._URL_TOKEN": "match",
    "redaction._URL_HEAD": "match",
    "guardrails._LABEL_SUFFIX": "match",
    "ledger._ID": "fullmatch",
    "ledger._NAME": "fullmatch",
    "ledger._HEX64": "fullmatch",
    "onboarding_schema.money.WIRE_PATTERN": "fullmatch",
    "onboarding_schema.money._STATED_PATTERN": "fullmatch",
}

BASE_UNITS = [
    "a", "a@", "a/", "a:", "a b ", "1", "1 ", "1-", "1,", "a.", "a-", "a_", "aA1!", "a=", "&a=", "a@a.", "/ ", "1a",
    "a ", " ", "\n", "a\n", "\n\n ", "$1,", "$1 ", "a' ", 'a"', "a|", "a.a", "A1a", "Ａ", "a​", "a:a@", "a / ",
    "1.", "$ 1", "? ", "a?", "#", "a;", "(", "1(", "\t", "a,", "aa ", "Ab1-", "p a ", "x/Y1abcdefg ",
]
SUFFIXES = ["", " ", "=", "/", ":", "-", "a"]
TAILS = ["", "!", "@"]


def collect_patterns() -> dict[str, re.Pattern]:
    pats: dict[str, re.Pattern] = {}

    def walk(name: str, obj, mod: str) -> None:
        if isinstance(obj, re.Pattern):
            pats.setdefault(f"{mod}.{name}", obj)
        elif isinstance(obj, (list, tuple)):
            for i, x in enumerate(obj):
                walk(f"{name}[{i}]", x, mod)

    for m in MODULES:
        mod = importlib.import_module(m)
        for k, v in vars(mod).items():
            walk(k, v, m)
    return pats


def units_for(p: re.Pattern) -> list[str]:
    words: set[str] = set()
    for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9_\\.\-/]{1,}", p.pattern):
        w = w.replace("\\", "")
        if 2 <= len(w) <= 30:
            words.add(w)
            words.add(w.lower())
    units = list(BASE_UNITS)
    for w in sorted(words)[:40]:
        units += [w + s for s in SUFFIXES]
    return units


def inputs(p: re.Pattern, size: int):
    for u in units_for(p):
        for tail in TAILS:
            yield u, tail, u * max(1, size // len(u)) + tail


def use_of(name: str, p: re.Pattern) -> Callable[[str], object]:
    how = ANCHORED_ONLY.get(name)
    if how == "match":
        return p.match
    if how == "fullmatch":
        return p.fullmatch
    return lambda s: [m.span() for m in p.finditer(s)]


def best_time(fn: Callable[[str], object], s: str, runs: int = 3) -> float:
    """Best of ``runs`` of the calling thread's OWN CPU time (fix wave 6:
    wall-clock time made the bounds depend on machine load — a 1 ms run fits
    in one scheduler quantum, a 40 ms run is pre-empted by other processes —
    and the cost being bounded is CPU work, not waiting)."""
    best = float("inf")
    for _ in range(runs):
        t = time.thread_time()
        fn(s)
        best = min(best, time.thread_time() - t)
    return best
