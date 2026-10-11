"""Adversarial-input harness for every regex in onboarding-py (fix wave 4, R1).

Collects every compiled pattern the service's scanning modules define and
builds hostile inputs for each: fixed shapes (runs of ``a``, ``a@``, ``a/``,
``a:``, alternating classes, blank lines, ...) plus shapes made from the
pattern's own literals ("login=", "eyJ-", "connect.facebook.net/", ...), each
with a few failing tails. Shared by the test module and the live probe.
"""

from __future__ import annotations

import gc
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
    return lambda s: _drain_matches(p.finditer(s))


def _drain_matches(it) -> int:
    """Run a search to the end the way the service does (every match produced and its span taken) WITHOUT keeping
    the results: fix wave 22 (G9, AEGIS N21-C-8) — the harness used to build a list of every span, and on a 100 KB
    input of 33,333 matches those retained tuples drove the interpreter's cyclic GC (15 collections, each walking the
    growing list), so the 10 KB → 100 KB ratio measured the harness's garbage, not the regex (``_DOLLAR`` on
    ``$1,`` × N: 20.2 against the bound 20 under load; the pattern itself is linear)."""
    n = 0
    for m in it:
        m.span()
        n += 1
    return n


def best_time(fn: Callable[[str], object], s: str, runs: int = 3) -> float:
    """Best of ``runs`` of the calling thread's OWN CPU time (fix wave 6:
    wall-clock time made the bounds depend on machine load — a 1 ms run fits
    in one scheduler quantum, a 40 ms run is pre-empted by other processes —
    and the cost being bounded is CPU work, not waiting). Fix wave 22 (G9): the
    cyclic GC is OFF while a run is timed, so a run measures the pattern's work,
    not a collection the harness's (or the suite's) allocations happened to
    trigger inside it."""
    best = float("inf")
    for _ in range(runs):
        was = gc.isenabled()
        gc.disable()
        try:
            t = time.thread_time()
            fn(s)
            best = min(best, time.thread_time() - t)
        finally:
            if was:
                gc.enable()
    return best


def best_time_back_to_back(fn: Callable[[str], object], items: list[str], runs: int = 3) -> float:
    """Best of ``runs`` of the thread CPU time to run ``fn`` over every item back to back, as ONE timed block (GC off,
    as in ``best_time``). Fix wave 23: the linearity ratios compared a short run (10 KB, ~1 ms) with a run 10x longer
    (100 KB, ~10-20 ms), and a linear pattern occasionally measured over the bound 20 under load (w23, 3.13, three
    busy loops: ``redaction._URL_PART`` 'a;'+'@' 1.05 ms -> 21.2 ms, ratio 20.2 — about one ratio check in 14,000;
    3 x 234 checks per Python showed median 10.1, max 12.3). The two sides are not the same measurement under load:
    a ~1 ms run fits in one scheduler slice with a warm cache, a 10-20 ms run is sliced and refills its cache on
    every resume, which thread CPU time counts. That is the likely cause, not a proven one (no excursion was caught
    in the act). Timing 10 x 10 KB back to back is the same work over the same duration as one 100 KB run, so what
    differs between the two sides is only how the cost grows with the length of one input (median 0.99, max 1.35)."""
    best = float("inf")
    for _ in range(runs):
        was = gc.isenabled()
        gc.disable()
        try:
            t = time.thread_time()
            for s in items:
                fn(s)
            best = min(best, time.thread_time() - t)
        finally:
            if was:
                gc.enable()
    return best


# ---------------------------------------------------------------------------------------------- structure and review
# Fix wave G (Oct 10 2026): the per-run absolute CPU bounds of test_r1_every_pattern_linear_on_adversarial_input
# failed under machine load (6.90 ms against a scaled 6.88 ms bound on 10 KB). Every run re-measured a constant of
# (pattern, flags, CPython's sre engine); the test now pins each pattern's content (``pin``) and judges its structure
# (``structure``) deterministically, and keeps the same-work growth check. The absolute cost of a pinned pattern is
# measured once, when it is reviewed, by ``review`` (``python tests/redos_harness.py``), on each supported Python.

PER_100KB_S = 0.050   # the finding's bound: 50 ms per 100 KB of hostile input (worst pattern ~25 ms on the dev box)
# fix wave 6: the bound is scaled by how much slower THIS machine is than the dev box it was derived on, measured on a
# known linear reference regex (~8 ms of CPU there), never below 1 and never past 8
_REF = re.compile(r"(?i)\bcan(?:no|')?t\s+(?:\w+\s+){0,3}?zzz\b")
_REF_INPUT = "cannot " * (100_000 // 7) + "!"
REF_NOMINAL_S = 0.008
SLOWDOWN_CAP = 8.0


def slowdown() -> float:
    t = best_time(lambda s: _drain_matches(_REF.finditer(s)), _REF_INPUT, runs=5)
    return min(SLOWDOWN_CAP, max(1.0, t / REF_NOMINAL_S))


def pin(p: re.Pattern) -> str:
    """The content hash a pattern is reviewed under: its flags and its source (SHA-256, 16 hex digits)."""
    import hashlib
    return hashlib.sha256(f"{p.flags}:{p.pattern}".encode()).hexdigest()[:16]


def structure(p: re.Pattern) -> list[str]:
    """The super-linear-capable constructs in ``p`` (sre's own parse tree, as creative-py's fix wave 4 test): an
    unbounded repeat inside another unbounded repeat, and a backreference. Possessive repeats and atomic groups never
    backtrack into, so they are not walked."""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        try:
            from re import _constants as C, _parser as P
        except ImportError:  # pragma: no cover - before 3.11
            import sre_constants as C
            import sre_parse as P
    found: list[str] = []

    def walk(items, depth: int) -> None:
        for op, av in items:
            name = str(op)
            if name in ("MAX_REPEAT", "MIN_REPEAT"):
                _lo, hi, sub = av
                unbounded = hi == C.MAXREPEAT
                if unbounded and depth:
                    found.append("nested unbounded repeat")
                walk(sub, depth + unbounded)
            elif name == "SUBPATTERN":
                walk(av[-1], depth)
            elif name == "BRANCH":
                for b in av[1]:
                    walk(b, depth)
            elif name in ("ASSERT", "ASSERT_NOT"):
                walk(av[1], depth)
            elif name in ("GROUPREF", "GROUPREF_EXISTS"):
                found.append("backreference")
    walk(P.parse(p.pattern, p.flags), 0)
    return found


def worst_shapes(fn: Callable[[str], object], p: re.Pattern, k: int = 3) -> list[tuple[float, str, str]]:
    """Every hostile shape at 4 KB, ranked by this thread's CPU (best of 1): the ``k`` costliest (time, unit, tail).
    The ranking only picks the shapes the growth check scales; no bound is asserted on these readings."""
    ranked = [(best_time(fn, s, 1), u, tail) for u, tail, s in inputs(p, 4_000)]
    ranked.sort(reverse=True)
    return ranked[:k]


def review(names: list[str] | None = None) -> int:
    """The review measurement for pinned patterns: per pattern, the three worst shapes' best-of-5 CPU on 100 KB
    against the finding's bound (scaled by ``slowdown``, as the per-run test did). Run on an unloaded machine, once
    per supported Python, when a pattern is added or changed; then pin it in test_fix_wave4.PATTERN_PINS. Prints
    every pattern's pin. Exit status 1 when any pattern is over the bound."""
    pats = collect_patterns()
    slow = slowdown()
    worst, over = (0.0, ""), []
    for name in names or sorted(pats):
        p = pats[name]
        fn = use_of(name, p)
        for _t, unit, tail in worst_shapes(fn, p):
            t100 = best_time(fn, unit * (100_000 // len(unit)) + tail, runs=5)
            worst = max(worst, (t100, f"{name} {unit!r}+{tail!r}"))
            if t100 >= PER_100KB_S * slow:
                over.append(f"{name} {unit!r}+{tail!r}: {t100 * 1000:.2f} ms / 100 KB")
        print(f"{pin(p)}  {name}")
    print(f"slowdown {slow:.2f}x; worst 100 KB CPU {worst[0] * 1000:.2f} ms at {worst[1]} "
          f"(bound {PER_100KB_S * slow * 1000:.1f} ms)")
    for line in over:
        print("OVER", line)
    return 1 if over else 0


if __name__ == "__main__":
    import os
    import sys

    here = os.path.dirname(os.path.abspath(__file__))
    sys.path[:0] = [here, os.path.join(os.path.dirname(here), "src")]
    os.environ.setdefault("ONBOARDING_SERVICE_TOKEN", "redos-review-" + "0" * 32)   # api.py refuses to import without
    raise SystemExit(review(sys.argv[1:] or None))
