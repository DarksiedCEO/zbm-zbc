"""Fix wave 4 (Sep 24 2026) — AEGIS round-3 findings on onboarding-py.

R1  HIGH   quadratic-time credential redactor, reachable without auth (access
           log) and before any length check (``mode="before"`` validator):
           every pattern is linear-time on hostile input; length caps come
           first (field max_length, 1 MiB body, 8 KiB request target, bounded
           log lines); scanning never runs on the event loop and has a
           per-request budget; /health stays responsive under attack on a
           real uvicorn.
A1  MEDIUM audit result records were not owed: the rulings after the
           detection call are owed (written by the next operation, same ids)
           and a retry never re-sends the account data to detection.
P1  LOW    owner ruling (refuse): payments are tracked only for a creator
           whose activation is complete; otherwise 409, nothing recorded.
D1  LOW    DOB plausibility: before 1900, or an age over 120, is a 422.
I1         restart: the creating operations derive their event ids from
           ONBOARDING_INSTANCE_ID, so a restarted process's retry dedupes at
           the ledger; later operations never collide with pre-restart ids.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

import redaction
import redos_harness as H
from config import ConfigError, OnboardingConfig, load_config
from conftest import TEST_SERVICE_TOKEN, client_for, make_service, start_body, start_live
from ledger import FakeLedgerClient, LedgerWriteError
from onboarding_schema import ClipperApplication
from onboarding_schema import requests as rq

SRC = Path(__file__).resolve().parents[1] / "src"
PATTERNS = H.collect_patterns()

# Fix wave 6: a known LINEAR reference regex measured this machine against the dev box, and the per-pattern absolute
# bounds (50 ms per 100 KB, 5 ms per 10 KB) were scaled by it; the machine-independent linearity check (10x the input
# may cost at most 20x the CPU) was asserted in addition. Fix wave 23 / 26b: an over-bound ratio is re-measured as one
# 100 KB input against ten 10 KB inputs back to back (the same bytes, the same duration; linear ideal 1, bound 2x),
# up to SAME_WORK_ATTEMPTS times; a quadratic pattern costs ~10x there.
#
# Fix wave G (Oct 10 2026): the absolute bounds still failed under load (6.90 ms against a scaled 6.88 ms on 10 KB,
# twice in one day on a shared box). They re-measured, on every run, a constant of (pattern, flags, CPython's sre
# engine). That constant is now fixed deterministically instead: every pattern is pinned by content (PATTERN_PINS,
# redos_harness.pin: flags and source), and was measured under the same bound when pinned
# (python tests/redos_harness.py = redos_harness.review: three worst shapes, best of 5 on 100 KB, scaled by the
# same slowdown; Oct 10 2026 on the 2-vCPU box, both Pythons: worst 39.6 ms on 3.13, 35.4 ms on 3.12, against
# 68-70 ms). A new or changed pattern fails PATTERN_PINS until it is measured and pinned again. Its structure is
# judged deterministically too: sre's parse tree has no unbounded repeat inside another and no backreference, or the
# pattern is in STRUCTURE_REVIEWED (name, pin, why it is linear). The same-work growth check on the three worst shapes
# is kept as it was (ratios of the same work, not wall-clock bounds), and the whole scanners keep their absolute CPU
# bounds below (10 KB < 0.25 s, 1 MB < 4 s), so a gross constant-factor regression still fails on every run.
LINEAR_RATIO = 20.0  # 10 KB -> 100 KB, i.e. 2x the linear ideal of 10
SAME_WORK_RATIO = LINEAR_RATIO / 10
SAME_WORK_ATTEMPTS = 3   # fix wave 26b (R25B-1): an over-bound same-work reading is measured again, up to this often

STRUCTURE_REVIEWED = {   # name -> (pin of the reviewed pattern, why it is linear)
    "redaction._REDACTED_RUN": ("4df38cc7a3a61af6", "each outer iteration must start with the literal '[REDACTED]', whose '[' the inner class excludes: any input splits one way only"),
}

# Every pattern of the service as reviewed (redos_harness.review), by its content pin.
PATTERN_PINS = {
    "guardrails._DOLLAR": "d84f53e020d2df77",
    "guardrails._GUARANTEE": "0309cb95074d2171",
    "guardrails._GUARANTEE_REQUEST": "0901beedb41b761f",
    "guardrails._HUMAN_REQUEST": "7826e402a0f9e5a6",
    "guardrails._INJECTION[0][1]": "9449d0786eab594b",
    "guardrails._INJECTION[1][1]": "01dcec56264bc427",
    "guardrails._INJECTION[2][1]": "30b5eb8d217dbfb1",
    "guardrails._INJECTION[3][1]": "3d320939527395c8",
    "guardrails._INJECTION[4][1]": "f9f444f2254a938f",
    "guardrails._INJECTION[5][1]": "67939734cab45c7e",
    "guardrails._INJECTION[6][1]": "2d9d893460e1f48c",
    "guardrails._INJECTION[7][1]": "83a07e4ab4f3015a",
    "guardrails._LABEL_SUFFIX": "16ce44d1d7842daf",
    "guardrails._NEGATED_OK": "c5a4eb4a0495422f",
    "guardrails._WORD_DOLLAR": "aa9a29c4d76c2e71",
    "intelligences.i02_conversation._CHANGE_OBJECT": "648b295e21847e82",
    "intelligences.i02_conversation._CHANGE_VERB": "b9cddcd2f545b96f",
    "intelligences.i02_conversation._CREDENTIAL_ASK": "84b203c08719cb80",
    "intelligences.i02_conversation._SPANISH_REQUEST": "cdda5487bbfdf2b3",
    "intelligences.i04_platform_access.TAG_PATTERNS[0][1]": "a323a09793184c33",
    "intelligences.i04_platform_access.TAG_PATTERNS[1][1]": "3d7a5454dc930c68",
    "intelligences.i04_platform_access.TAG_PATTERNS[2][1]": "d9934d3a52b00309",
    "intelligences.i04_platform_access.TAG_PATTERNS[3][1]": "a0b9de988ed589c6",
    "intelligences.i04_platform_access.TAG_PATTERNS[4][1]": "7b802024901a3f2b",
    "intelligences.i04_platform_access.TAG_PATTERNS[5][1]": "015a7dce971be633",
    "intelligences.i04_platform_access._META_FILE": "f3f5994fb9a8ab3b",
    "intelligences.i04_platform_access._META_HOST": "fe653b76100c7d7b",
    "intelligences.i04_platform_access._QUOTE": "b683600134208e4f",
    "ledger._HEX64": "e95a2856b9a7b554",
    "ledger._ID": "62c14ef943c8fe48",
    "ledger._NAME": "e83e9dfe0419fc84",
    "memory._EMAIL": "cfd659d770854d21",
    "memory._EMAIL_AT": "b23e21aa9a0cf7e1",
    "memory._LONG_NUM": "2e4180243fd60f50",
    "memory._PHONE": "29e9e3e32d9dd44c",
    "memory._URL": "7b840699d6305321",
    "onboarding_schema.money.WIRE_PATTERN": "bb8e7516783d4493",
    "onboarding_schema.money._STATED_PATTERN": "9be162b7561b0312",
    "practices.ad_disclosure._MARKER": "09f8ae8ecc7eb5bd",
    "redaction._ACCOUNT_LONG": "e2e5e7478b6520a5",
    "redaction._BANK_VALUE": "dc82ba6626fcda5c",
    "redaction._BEARER": "771a917b8e8c354c",
    "redaction._CARD": "7d19ee7a7d0f3c89",
    "redaction._CREDS_PAIR": "0217111a28e4960e",
    "redaction._CREDS_VALUE": "3e2344b4dbd1e7a2",
    "redaction._CUE": "8c3b3c174f49226b",
    "redaction._CUE_STRONG": "e08b362c50f01ff5",
    "redaction._EMAIL_PAIR": "b8cb2b3eec29c161",
    "redaction._EMAIL_TOKEN": "a315dd55359675fa",
    "redaction._GET_IN_WITH": "080889689d48631b",
    "redaction._IBAN": "deddcf0b1cb08bf4",
    "redaction._JWT": "c4076ee1bb185a64",
    "redaction._KEYWORD": "6972819ec017d936",
    "redaction._LOGIN_NEAR": "8b42211b68f06aef",
    "redaction._LOGIN_PAIR": "c9ad43f898e7b867",
    "redaction._LONG_DIGITS": "d70a950b37483e6f",
    "redaction._PASS_EXPLICIT": "7c2c44ec3132e24e",
    "redaction._PIN_VALUE": "b45fca886ac17749",
    "redaction._PREFIXED": "ec536e739c643c5c",
    "redaction._PW_EXPLICIT": "77f848c5c1a9873c",
    "redaction._PW_SPACE": "67757057132d3a76",
    "redaction._PW_VERB": "1d33fa02e971d65a",
    "redaction._QUERY_PAIR": "642b418452c6a0ca",
    "redaction._REDACTED_RUN": "4df38cc7a3a61af6",
    "redaction._SECRET_VALUE": "a1215c537ee2cbc2",
    "redaction._SEPARATED": "f61bd7d2e60052c3",
    "redaction._SLASH_PAIR": "c774a63f57eaf03f",
    "redaction._SSN": "b456d441eb991653",
    "redaction._SSN_VALUE": "170dc6da1b19e022",
    "redaction._TOKENISH": "f979f56dcd3f6df2",
    "redaction._TO_LOGIN": "3508f15c32ee4f38",
    "redaction._URL_HEAD": "93b0023627e30a6b",
    "redaction._URL_PART": "350e009687848b0d",
    "redaction._URL_TOKEN": "217cb296b63cb9fc",
    "redaction._URL_USERINFO": "22a7401fe05aba91",
    "redaction._URL_USERINFO_SECRET": "4137a7c7bf48e75f",
    "redaction._USE_TO_LOGIN": "9157aedc45dd7f44",
    "redaction._WORDISH": "ffcc93d339a70e44",
}


class FailOn(FakeLedgerClient):
    """Fake ledger that refuses the listed event types (until healed)."""

    def __init__(self, *types):
        super().__init__()
        self.fail_types = set(types)

    def record_event(self, event_id, department, event_type, *a):
        if event_type in self.fail_types:
            raise LedgerWriteError("fake ledger: write refused")
        return super().record_event(event_id, department, event_type, *a)


def _app(**over):
    body = {"creator_id": "clip_1", "legal_name": "Casey Clipper", "date_of_birth": "2000-01-01",
            "follower_count": 20000, "avg_engagement_rate": 0.05, "fake_follower_ratio": 0.02,
            "content_history_posts": 120, "network_fit_tags": ["beauty"], "w9_received": True,
            "creator_agreement_signed": True, "disclosure_training_completed": True}
    body.update(over)
    return body


def _count(svc, t):
    return svc.ledger.types().count(t)


# =============================================================================
# R1 — every pattern is linear-time on hostile input
# =============================================================================


def test_r1_harness_sees_every_scanning_pattern():
    # The harness walks the modules; the patterns the finding named must be in it.
    for name in ("redaction._SLASH_PAIR", "redaction._EMAIL_PAIR", "redaction._EMAIL_TOKEN", "redaction._JWT",
                 "redaction._LOGIN_PAIR", "redaction._CREDS_PAIR", "redaction._URL_USERINFO", "guardrails._WORD_DOLLAR",
                 "memory._EMAIL", "memory._URL", "intelligences.i04_platform_access.TAG_PATTERNS[3][1]",
                 "guardrails._INJECTION[3][1]"):
        assert name in PATTERNS, name
    assert len(PATTERNS) >= 70


def test_r1_anchored_only_patterns_are_never_searched():
    """Patterns timed by their anchored use (match/fullmatch) must never be
    used with search/finditer/sub/findall/split anywhere in the service."""
    source = "\n".join(p.read_text() for p in SRC.rglob("*.py"))
    for qualified in H.ANCHORED_ONLY:
        name = qualified.rsplit(".", 1)[1]
        assert qualified in PATTERNS, qualified
        bad = re.findall(rf"\b{re.escape(name)}\.(search|finditer|sub|subn|findall|split)\(", source)
        assert not bad, (qualified, bad)


@pytest.mark.parametrize("name", sorted(PATTERNS))
def test_r1_every_pattern_linear_on_adversarial_input(name):
    p = PATTERNS[name]
    # 1. deterministic: the pattern is the one measured under the bound when it was pinned (fix wave G: this replaced
    #    the per-run absolute CPU bounds) -- a new or changed pattern fails here, before anything is timed
    assert PATTERN_PINS.get(name) == H.pin(p), (
        f"{name} is not the reviewed pattern (pin {H.pin(p)}, reviewed {PATTERN_PINS.get(name)}): measure it with "
        f"`python tests/redos_harness.py {name}` on each supported Python (exit 0: under the bound), then pin it")
    # 2. deterministic: no super-linear-capable construct, unless reviewed with the reason it is linear
    risky = H.structure(p)
    reviewed = STRUCTURE_REVIEWED.get(name)
    assert not risky or (reviewed is not None and reviewed[0] == H.pin(p)), (name, p.pattern[:80], risky)
    # 3. the three worst hostile shapes (ranked by CPU at 4 KB; the ranking asserts nothing): 10 KB -> 100 KB must
    #    scale linearly, as the same work, whatever the machine
    fn = H.use_of(name, p)
    for _, unit, tail in H.worst_shapes(fn, p):
        _assert_scales_linearly(name, fn, unit, tail)


def test_r1_every_pattern_is_pinned_and_every_pin_is_a_pattern():
    assert set(PATTERN_PINS) == set(PATTERNS), sorted(set(PATTERN_PINS) ^ set(PATTERNS))
    assert set(STRUCTURE_REVIEWED) <= set(PATTERNS)
    assert all(PATTERN_PINS[n] == pin for n, (pin, _why) in STRUCTURE_REVIEWED.items())


def test_r1_the_structure_check_flags_nested_repeats_and_backreferences_only():
    """The structural check on its own (as creative-py's): the textbook catastrophic patterns are flagged; plain
    character-class runs, bounded repeats, possessive and atomic forms are not."""
    for bad in (r"(a+)+$", r"(?:\w+\s?)*x", r"(.+)\1", r"(\w*)*", r"(?:a|aa)+(?:b*)+"):
        assert H.structure(re.compile(bad)), bad
    for good in (r"[^\w#@]+", r"[\s/]+", r"(?:ab){2,5}c+", r"\w++(?:\s\w++)*+", r"(?>\w+)\.\w+", r"(?:x|y)z+"):
        assert not H.structure(re.compile(good)), good


def test_r1_a_pin_changes_with_the_source_and_the_flags():
    base = re.compile(r"a+b")
    assert H.pin(base) == H.pin(re.compile(r"a+b"))
    assert len({H.pin(base), H.pin(re.compile(r"a+b ")), H.pin(re.compile(r"a+b", re.IGNORECASE))}) == 3


def _assert_scales_linearly(name, fn, unit, tail):
    s10 = unit * (10_000 // len(unit)) + tail
    t10 = H.best_time(fn, s10, runs=5)
    s100 = unit * (100_000 // len(unit)) + tail
    t100 = H.best_time(fn, s100, runs=5)
    # timer floor of 0.2 ms so a microsecond t10 does not make the ratio noise
    if t100 < LINEAR_RATIO * max(t10, 0.0002):
        return  # within 2x of linear even against the short run (load only inflates the long one)
    # Fix wave 23: before it fails, the ratio is re-measured against ten 10 KB inputs back to back — the same work
    # and the same duration as the 100 KB run (a single ~1 ms run measured the scheduler, not the pattern: see
    # redos_harness.best_time_back_to_back). Timer floor 2 ms (0.2 ms x 10). A quadratic pattern fails this too.
    # Fix wave 26b (AEGIS r25b R25B-1): one over-bound reading is measured again (fresh 100 KB and 10 x 10 KB runs), up
    # to SAME_WORK_ATTEMPTS times; only a pattern over the bound EVERY time fails. Linear patterns read ~1.0 here and
    # the quadratic mutant 5.6-6.1, so a transient (3.07 once in 4 full runs for `_CHANGE_OBJECT`) passes on a later
    # attempt and a quadratic pattern fails all of them.
    attempts = []
    for _ in range(SAME_WORK_ATTEMPTS):
        t100 = H.best_time(fn, s100, runs=5)
        t10x10 = H.best_time_back_to_back(fn, [s10] * 10, runs=5)
        attempts.append((round(t100 * 1000, 2), round(t10x10 * 1000, 2), round(t100 / max(t10x10, 0.002), 2)))
        if t100 < SAME_WORK_RATIO * max(t10x10, 0.002):
            return
    raise AssertionError(f"{name}: {unit!r}+{tail!r} not linear: {t10 * 1000:.2f} ms on 10 KB; every attempt over "
                         f"{SAME_WORK_RATIO} (100 KB ms, 10 x 10 KB ms, ratio): {attempts}")


def test_r1_a_transient_same_work_excursion_is_measured_again_a_quadratic_never_passes(monkeypatch):
    """AEGIS r25b R25B-1: `_CHANGE_OBJECT` failed the same-work check in 1 of 4 full 3.13 runs (ratio 3.07 against 2.0)
    while, measured alone, it scaled linearly 0/30 — and here its ratio reads 0.89-1.01, a quadratic mutant's 5.6-6.1.
    One excursion is not a property of the pattern: an over-bound reading is now measured again, up to
    SAME_WORK_ATTEMPTS times, and only a pattern over the bound every time fails. Shown with scripted timings: a
    first reading of 3.07 then 1.0 passes; 6.0 every time fails "not linear"."""
    def scripted(t10, t100s, t10x10s):
        it100, it1010 = iter(t100s), iter(t10x10s)
        first = {"s10": t10}

        def best_time(fn, s, runs=3):
            return first.pop("s10") if len(s) < 50_000 and "s10" in first else next(it100)
        monkeypatch.setattr(H, "best_time", best_time)
        monkeypatch.setattr(H, "best_time_back_to_back", lambda fn, items, runs=3: next(it1010))
    unit, tail = "x" * 10, ""
    scripted(1e-5, [0.0061, 0.00614, 0.002], [0.002, 0.002])        # first 100 KB read, then ratio 3.07, then 1.0
    _assert_scales_linearly("transient", lambda s: None, unit, tail)
    scripted(1e-5, [0.012] * (SAME_WORK_ATTEMPTS + 1), [0.002] * SAME_WORK_ATTEMPTS)      # 6.0 every time
    with pytest.raises(AssertionError, match="not linear"):
        _assert_scales_linearly("quadratic", lambda s: None, unit, tail)


def test_r1_the_ratio_check_fails_a_quadratic_pattern_on_its_own():
    """Fix wave 23: the same-work ratio catches a quadratic pattern on its own (no absolute bound: fix wave G removed
    them from the per-pattern test). Every '?' starts a match whose lookahead scans to the '@' at the end: O(n) per match, n/100
    matches — 10x the input costs ~100x. The linear control (the lookahead stops at the next character) passes."""
    unit, tail = "?" + " " * 99, "@"
    quadratic = re.compile(r"\?(?=[^@]*@)")
    with pytest.raises(AssertionError, match="not linear"):
        _assert_scales_linearly("quadratic mutant", H.use_of("mutant", quadratic), unit, tail)
    _assert_scales_linearly("linear control", H.use_of("control", re.compile(r"\?(?= )")), unit, tail)


# The whole scanners, on the hostile shapes of the finding, up to 1 MB.
SCANNER_UNITS = ["a", "a@", "a/", "a:", "a b ", "1,", "a.", "\n", "Ａ", "aA1!", "login ", "login=", "acct=", "user:",
                 "creds=", "eyJ-", "connect.facebook.net/", "p a s s ", "$1 ", "http://a:", "?a=", "1 ", "ignore the ",
                 "cut ", "x/Y1abcdefg "]


def _scanners():
    import guardrails
    import memory
    from intelligences import i02_conversation as i02
    from intelligences import i04_platform_access as i04

    return {
        "find_credential": redaction.find_credential,
        "scrub": redaction.scrub,
        "redact_url": redaction.redact_url,
        "normalize": redaction.normalize,
        "check_outbound_rules": lambda s: (guardrails.guarantee_violations(s), guardrails.unlabeled_dollar_figures(s),
                                           [m.span() for m in guardrails._WORD_DOLLAR.finditer(s)]),
        "scan_for_injection": lambda s: guardrails.scan_for_injection(s, "test"),
        "decide_reply": i02.decide_reply,
        "strip_identifiers": lambda s: memory.strip_identifiers(s, ("Acme",)),
        "scan_tags": i04.scan_tags,
    }


@pytest.mark.parametrize("scanner", sorted(_scanners()))
def test_r1_scanners_linear_up_to_1mb(scanner):
    fn = _scanners()[scanner]
    at_100kb = []
    for unit in SCANNER_UNITS:
        small = H.best_time(fn, unit * (10_000 // len(unit)) + "!", 1)
        assert small < 0.25, f"{scanner} {unit!r}: {small:.2f}s on 10 KB"  # fails fast if quadratic
        at_100kb.append((H.best_time(fn, unit * (100_000 // len(unit)) + "!", 1), unit))
    for _, unit in sorted(at_100kb, reverse=True)[:5]:  # the five worst shapes, at 1 MB
        # fix wave 22 (G9 class): the ratio's base is a best of 3, and a 1 MB sample over the bound is re-measured
        # best of 3 before it fails (one sample each measured scheduling noise: normalize '1 ' — median ratio 13.5,
        # single-sample max 19.6, one 3.12 suite run 22.2 > 20; best of 3: max 14.5-15.1 over 10 pairs on 3.12 and
        # 3.13). A superlinear scanner fails the re-measure too. The single 100 KB sample above only ranks shapes.
        s100 = unit * (100_000 // len(unit)) + "!"
        t100 = H.best_time(fn, s100, 3)
        s1m = unit * (1_000_000 // len(unit)) + "!"
        t1m = H.best_time(fn, s1m, 1)
        if t1m >= max(20 * t100, 0.05):
            # fix wave 23 (the same class as the per-pattern ratio): re-measured best of 3 against ten 100 KB
            # inputs back to back — the same work and duration as the 1 MB run — before it fails
            t1m = H.best_time(fn, s1m, 3)
            t100x10 = H.best_time_back_to_back(fn, [s100] * 10, 3)
            assert t1m < max(SAME_WORK_RATIO * t100x10, 0.05), \
                f"{scanner} {unit!r}: 100 KB {t100:.3f}s, 10 x 100 KB {t100x10:.3f}s, 1 MB {t1m:.3f}s"
        # linear: 10x the input costs ~10x (never 100x), and 1 MB stays cheap
        assert t1m < 4.0, f"{scanner} {unit!r}: {t1m:.2f}s on 1 MB"


def test_r1_redact_text_linear_at_its_largest_input():
    # Client free text is at most 20,000 characters (documents); 100 KB here.
    for unit in SCANNER_UNITS:
        t = H.best_time(redaction.redact_text, unit * (100_000 // len(unit)) + "!", 1)
        assert t < 2.0, f"redact_text {unit!r}: {t:.2f}s on 100 KB"


def test_r1_the_aegis_probe_shapes_are_fast():
    # redos.py (AEGIS round 3): 20,000 "a" took 7.6 s in find_credential.
    n = 20_000
    cases = {"a*n": "a" * n, "a@*n": "a@" * (n // 2), "login a*n": "login " + "a" * n, "(login a)*": "login a " * (n // 8),
             "get in ": "get in " * (n // 7), "x/": "x/" * (n // 2), "a b ": "a b " * (n // 4), "user ": "user x" * (n // 6),
             "digits": "1" * n, "1 ": "1 " * (n // 2), "pass ": "pass " * (n // 5), "a:": "a:" * (n // 2), "use ": "use a " * (n // 6)}
    for k, s in cases.items():
        t = H.best_time(redaction.find_credential, s)
        assert t < 0.25, f"{k}: {t:.2f}s"
        t = H.best_time(redaction.scrub, s)
        assert t < 0.25, f"scrub {k}: {t:.2f}s"


# =============================================================================
# R1 — length caps come before any scan
# =============================================================================


@pytest.fixture
def scan_calls(monkeypatch):
    calls = []
    real = redaction.find_credential

    def counting(text):
        calls.append(len(text) if isinstance(text, str) else 0)
        return real(text)

    monkeypatch.setattr(redaction, "find_credential", counting)
    return calls


def test_r1_over_long_field_is_refused_without_any_scan(scan_calls):
    t = time.thread_time()                 # fix wave 25 (scout A O2; R-HYGIENE L1): this thread's CPU, not wall
    with pytest.raises(ValidationError):
        rq.MessageRequest.model_validate({"text": "a" * 60_000})
    assert time.thread_time() - t < 0.2
    assert scan_calls == [], "a 60 KB message was scanned before its 5,000-character limit was checked"
    with pytest.raises(ValidationError):
        rq.DocumentRequest.model_validate({"name": "n", "text": "a@" * 30_000})
    with pytest.raises(ValidationError):
        rq.WebsiteScanRequest.model_validate({"html": "a/" * 300_000})
    with pytest.raises(ValidationError):
        ClipperApplication.model_validate(_app(bio="a:" * 5_000))
    # only the short, already-valid identifier ("clip_1") was checked
    assert scan_calls == [len("clip_1")]


def test_r1_every_inbound_string_field_is_length_capped():
    """No request model has an unbounded string. (``AuditRequest.account_data``
    rows are free-form JSON: bounded by the 1 MiB body cap, and scrubbed.)"""
    import typing

    from annotated_types import MaxLen
    from pydantic import BaseModel
    from pydantic.types import StringConstraints

    import onboarding_schema as S

    def bounded(meta) -> bool:
        return any((isinstance(m, StringConstraints) and (m.max_length is not None or m.pattern is not None))
                   or isinstance(m, MaxLen) for m in meta)

    def unbounded_strings(tp, meta=()) -> bool:
        origin = typing.get_origin(tp)
        if origin is typing.Annotated:
            base, *more = typing.get_args(tp)
            return unbounded_strings(base, tuple(meta) + tuple(more))
        if tp is str:
            return not bounded(meta)
        if origin is typing.Literal or tp is typing.Any or (isinstance(tp, type) and issubclass(tp, BaseModel)):
            return False
        return any(unbounded_strings(a) for a in typing.get_args(tp) if a is not type(None))

    models = [m for m in list(vars(rq).values()) + list(vars(S).values())
              if isinstance(m, type) and issubclass(m, S.Inbound) and m is not S.Inbound]
    problems = [f"{m.__name__}.{n}" for m in models for n, f in m.model_fields.items()
                if unbounded_strings(f.annotation, f.metadata)]
    assert problems == [], problems


def test_r1_body_over_1_mib_is_413_from_content_length_and_while_streaming(scan_calls):
    svc = make_service(all_fakes=True)
    c = client_for(svc)
    assert c.post("/onboarding/clients", json=start_body()).status_code == 201
    n0 = len(svc.ledger.events)
    del scan_calls[:]
    big = json.dumps({"html": "a" * 1_100_000})
    r = c.post("/onboarding/clients/client_a/access/website-scan", content=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 413, r.text

    def chunks():  # no Content-Length: chunked transfer, cut off while streaming
        yield b'{"html": "'
        for _ in range(40):
            yield b"a" * 32_768
        yield b'"}'

    r = c.post("/onboarding/clients/client_a/access/website-scan", content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413, r.text
    assert scan_calls == [] and len(svc.ledger.events) == n0
    # at the cap it is accepted (500,000 characters of html is within 1 MiB)
    ok = c.post("/onboarding/clients/client_a/access/website-scan", json={"html": "<html>" + "a" * 499_000 + "</html>"})
    assert ok.status_code == 200, ok.text


def test_r1_request_target_over_8_kib_is_414():
    c = client_for(make_service())
    assert c.get("/health?x=" + "a" * 9_000).status_code == 414
    assert c.get("/health?x=" + "a" * 7_000).status_code == 200


def _access(path):
    return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                             ("127.0.0.1:1", "GET", path, "1.1", 404), None)


def test_r1_access_log_line_is_capped_before_it_is_scrubbed():
    for path in ("/" + "a" * 20_000 + "?" + "&".join(f"k{i}=aA1!aA1!aA1!" for i in range(60_000)),
                 "/" + "a@" * 500_000, "/x?" + "login=" * 200_000, "/" + "orders/" * 150_000):
        rec = _access(path)
        t = time.thread_time()             # fix wave 25 (scout A O2; R-HYGIENE L1): this thread's CPU, not wall
        redaction.scrub_log_record(rec)
        assert time.thread_time() - t < 0.2
        assert len(rec.getMessage()) < redaction.LOG_PATH_MAX + 200
    rec = _access("/" + "orders/" * 150_000)
    redaction.scrub_log_record(rec)
    assert "truncated" in rec.getMessage()
    other = logging.LogRecord("onboarding.x", logging.INFO, __file__, 1, "%s", ("login " + "a" * 1_000_000,), None)
    t = time.thread_time()
    redaction.scrub_log_record(other)
    assert time.thread_time() - t < 0.5 and len(other.getMessage()) < redaction.LOG_TEXT_MAX + 200


# =============================================================================
# R1 — scanning never runs on the event loop; per-request budget
# =============================================================================


def test_r1_body_validation_and_scanning_run_off_the_event_loop(monkeypatch):
    on_loop = []
    real = redaction.find_credential

    def spy(text):
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return real(text)


    monkeypatch.setattr(redaction, "find_credential", spy)
    svc = make_service(all_fakes=True)
    c = client_for(svc)
    assert c.post("/onboarding/clients", json=start_body()).status_code == 201
    assert c.post("/onboarding/clients/client_a/messages", json={"text": "hello there"}).status_code == 200
    assert c.post("/onboarding/clients/client_a/messages", json={"text": "my password is Hunter2!!"}).status_code == 422
    assert c.post("/onboarding/clients/client_a/messages", json={"text": "x" * 6_000}).status_code == 422
    assert c.post("/onboarding/clients/nobody/messages", json={"text": "hi"}).status_code == 404
    assert c.post("/onboarding/clients/client_a/messages", json={"text": "we guarantee"}).status_code == 200
    assert on_loop and not any(on_loop), "credential scanning ran on the event loop"


def test_r1_scan_budget_refuses_a_body_it_could_not_check(monkeypatch):
    svc = make_service(all_fakes=True)
    # Fix wave 5 (NEW-2): the budget is floor + per-KB CPU time; both tiny.
    svc.config = replace(svc.config, scan_budget_seconds=1e-9, scan_cpu_ms_per_kb=0)
    c = client_for(svc)
    r = c.post("/onboarding/clients", json=start_body())
    assert r.status_code == 422, r.text
    assert r.json()["detail"][0]["type"] == "scan_budget_exceeded"
    assert svc.ledger.events == [] and svc.clients == {}
    # Fix wave 5 (NEW-2): the budget is the thread's CPU time, so the budget
    # is spent by burning CPU (this test used time.sleep, i.e. asserted the
    # wall-clock budget that refused concurrent benign bodies).
    with pytest.raises(redaction.ScanBudgetExceeded):
        with redaction.scan_budget(1e-9):
            end = time.thread_time() + 0.002
            while time.thread_time() < end:
                pass
            redaction.find_credential("hello")
    assert redaction.find_credential("hello") is None  # no budget outside a request


def test_r1_input_caps_are_configuration():
    cfg = load_config({"ONBOARDING_MAX_BODY_BYTES": "2048", "ONBOARDING_MAX_REQUEST_TARGET_BYTES": "512",
                       "ONBOARDING_SCAN_BUDGET_SECONDS": "2.5"})
    assert (cfg.max_body_bytes, cfg.max_request_target_bytes, cfg.scan_budget_seconds) == (2048, 512, 2.5)
    assert (OnboardingConfig().max_body_bytes, OnboardingConfig().max_request_target_bytes) == (1_048_576, 8192)
    with pytest.raises(ConfigError):
        OnboardingConfig(scan_budget_seconds=0)


# =============================================================================
# R1 — real uvicorn: /health answers within 1 s while an attacker sends a
# max-size hostile URL and body concurrently
# =============================================================================


def _start_server() -> tuple[subprocess.Popen, int]:
    """Fix wave 26b (scout C5-6): the server is this test's only once IT holds the port (conftest.start_live, the
    shared owner-checked helper); any 200 on a picked port used to count."""
    def launch(port):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("LEDGER_", "DETECTION_"))}
        env.update({"ONBOARDING_SERVICE_TOKEN": TEST_SERVICE_TOKEN, "ONBOARDING_PORT": str(port),
                    "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"})
        return subprocess.Popen([sys.executable, "-m", "api"], cwd=str(SRC), env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)

    proc, port = start_live(launch)
    for _ in range(100):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.5).status_code == 200:
                return proc, port
        except httpx.HTTPError:
            time.sleep(0.1)
    proc.kill()
    proc.wait()
    raise AssertionError("server did not start")


def test_r1_real_uvicorn_health_stays_responsive_under_hostile_url_and_body():
    proc, port = _start_server()
    base = f"http://127.0.0.1:{port}"
    auth = {"Authorization": f"Bearer {TEST_SERVICE_TOKEN}"}
    results: dict[str, list] = {"attack": [], "health": []}
    stop = threading.Event()

    def attack_url():
        # unauthenticated: a request target at the 8 KiB cap (logged and
        # scrubbed) and one far over it (414, logged capped)
        with httpx.Client(timeout=60) as c:
            for path in ("/" + "a@" * 4_000, "/x?" + "a/" * 15_000, "/" + "a:" * 4_000, "/y?" + "login=" * 5_000):
                t = time.perf_counter()
                r = c.get(base + path)
                results["attack"].append(("url", r.status_code, time.perf_counter() - t))

    def attack_body():
        with httpx.Client(timeout=60, headers=auth) as c:
            for unit in ("a@", "a/", "login="):
                html = unit * (499_000 // len(unit))
                t = time.perf_counter()
                r = c.post(base + "/onboarding/clients/nobody/access/website-scan", json={"html": html})
                results["attack"].append(("html", r.status_code, time.perf_counter() - t))
            rows = [{"note": "a@" * 2_000, "k": "user:" * 800} for _ in range(90)]
            t = time.perf_counter()
            r = c.post(base + "/onboarding/clients/nobody/audit", json={"account_data": {"orders": rows}})
            results["attack"].append(("audit", r.status_code, time.perf_counter() - t))
            t = time.perf_counter()
            r = c.post(base + "/onboarding/clients/nobody/messages", json={"text": "a" * 60_000})
            results["attack"].append(("long_message", r.status_code, time.perf_counter() - t))

    def health():
        with httpx.Client(timeout=5) as c:
            while not stop.is_set():
                t = time.perf_counter()
                r = c.get(base + "/health")
                results["health"].append((r.status_code, time.perf_counter() - t))
                time.sleep(0.05)

    try:
        h = threading.Thread(target=health)
        h.start()
        attackers = [threading.Thread(target=attack_url), threading.Thread(target=attack_body)]
        for a in attackers:
            a.start()
        for a in attackers:
            a.join(120)
        stop.set()
        h.join(10)
    finally:
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
    kinds = sorted({k for k, _, _ in results["attack"]})
    assert kinds == ["audit", "html", "long_message", "url"], results["attack"]
    statuses = {(k, s) for k, s, _ in results["attack"]}
    assert ("url", 414) in statuses and ("long_message", 422) in statuses and ("html", 404) in statuses, statuses
    assert all(d < 10 for _, _, d in results["attack"]), results["attack"]
    assert len(results["health"]) >= 5
    slow = [x for x in results["health"] if x[0] != 200 or x[1] >= 1.0]
    assert not slow, (slow, max(d for _, d in results["health"]))
    assert "--- Logging error ---" not in out and "Traceback" not in out, out[-2000:]


# =============================================================================
# A1 — audit rulings are owed records; a retry never re-sends account data
# =============================================================================

AUDIT = {"account_data": {"orders": [{"id": "o1"}]}}


def _started(ledger):
    svc = make_service(all_fakes=True, ledger=ledger)
    c = client_for(svc)
    assert c.post("/onboarding/clients", json=start_body()).status_code == 201
    return svc, c


@pytest.mark.parametrize("fail", ["revenue_recovery_ruling", "risk_ruling"])
def test_a1_audit_rulings_are_written_by_the_next_operation(fail):
    svc, c = _started(FailOn(fail))
    r = c.post("/onboarding/clients/client_a/audit", json=AUDIT)
    assert r.status_code == 503 and r.json()["proceeded"] is True, r.text
    assert r.json()["outside_effects_done"] == ["revenue_recovery_detect"]
    assert len(svc.rr.calls) == 1
    svc.ledger.fail_types = set()
    # any next operation on the client writes BOTH owed rulings (same ids)
    assert c.post("/onboarding/clients/client_a/messages", json={"text": "hello"}).status_code == 200
    assert _count(svc, "revenue_recovery_ruling") == 1 and _count(svc, "risk_ruling") == 1
    types = svc.ledger.types()
    assert types.index("revenue_recovery_ruling") < types.index("risk_ruling")
    assert len(svc.rr.calls) == 1


@pytest.mark.parametrize("fail", ["revenue_recovery_ruling", "risk_ruling"])
def test_a1_audit_retry_does_not_resend_account_data(fail):
    svc, c = _started(FailOn(fail))
    assert c.post("/onboarding/clients/client_a/audit", json=AUDIT).status_code == 503
    svc.ledger.fail_types = set()
    r = c.post("/onboarding/clients/client_a/audit", json=AUDIT)
    assert r.status_code == 200, r.text
    assert len(svc.rr.calls) == 1, "the retry sent the account data to detection again"
    assert _count(svc, "revenue_recovery_request") == 1
    assert _count(svc, "revenue_recovery_ruling") == 1 and _count(svc, "risk_ruling") == 1
    ids = [e["event_id"] for e in svc.ledger.events]
    assert len(ids) == len(set(ids))
    assert r.json()["baseline"]["finding_count"] == 3
    assert svc.clients["client_a"].baseline is not None and svc.clients["client_a"].audit_known is None
    # a later, different audit is new data: detection is called for it, and
    # its rulings are NEW records (same findings, so same payloads: the
    # retry above must have advanced the client's sequence)
    assert c.post("/onboarding/clients/client_a/audit", json={"account_data": {"orders": [{"id": "o2"}]}}).status_code == 200
    assert len(svc.rr.calls) == 2
    assert _count(svc, "revenue_recovery_ruling") == 2 and _count(svc, "risk_ruling") == 2


def test_a1_audit_retry_after_a_later_operation_writes_no_duplicate_rulings():
    svc, c = _started(FailOn("risk_ruling"))
    assert c.post("/onboarding/clients/client_a/audit", json=AUDIT).status_code == 503
    svc.ledger.fail_types = set()
    assert c.post("/onboarding/clients/client_a/plan", json={"client_priorities": ["x"]}).status_code == 409
    r = c.post("/onboarding/clients/client_a/audit", json=AUDIT)
    assert r.status_code == 200, r.text
    assert len(svc.rr.calls) == 1
    assert _count(svc, "revenue_recovery_ruling") == 1 and _count(svc, "risk_ruling") == 1
    assert _count(svc, "revenue_recovery_request") == 1


def test_a1_detection_failure_is_still_recorded_and_retry_calls_again():
    svc, c = _started(FakeLedgerClient())
    svc.rr.fail = True
    assert c.post("/onboarding/clients/client_a/audit", json=AUDIT).status_code == 502
    assert _count(svc, "revenue_recovery_failed") == 1
    svc.rr.fail = False
    assert c.post("/onboarding/clients/client_a/audit", json=AUDIT).status_code == 200
    assert len(svc.rr.calls) == 2  # no result was known: the retry must call again


# =============================================================================
# P1 — payments only for a creator whose activation is complete
# =============================================================================


def _minor_dob(svc) -> str:
    today = svc.age_evaluation_date()
    return date(today.year - 16, today.month, min(today.day, 28)).isoformat()


def test_p1_payment_for_a_declined_minor_is_refused_and_not_recorded():
    svc = make_service(all_fakes=True)
    c = client_for(svc)
    r = c.post("/zbc/creators/applications", json=_app(date_of_birth=_minor_dob(svc), w9_received=True))
    assert r.status_code == 201 and r.json()["vetting"]["outcome"] == "decline"
    r = c.post("/zbc/creators/clip_1/payments", json={"request_id": "pay-659", "amount_usd": "50.00"})
    assert r.status_code == 409, r.text
    assert "not activated" in r.json()["detail"] and r.json()["vetting_outcome"] == "decline"
    assert _count(svc, "creator_payment_tracked") == 0 and svc.creators["clip_1"].payments == []


def test_p1_payment_with_w9_but_activation_incomplete_is_refused():
    svc = make_service()  # honest stand-ins: vetting approves, activation is blocked
    c = client_for(svc)
    r = c.post("/zbc/creators/applications", json=_app())
    assert r.json()["vetting"]["outcome"] == "approve" and r.json()["activation"]["activated"] is False
    r = c.post("/zbc/creators/clip_1/payments", json={"request_id": "pay-670", "amount_usd": "50.00"})
    assert r.status_code == 409 and "activation is complete" in r.json()["detail"], r.text
    assert _count(svc, "creator_payment_tracked") == 0


def test_p1_payment_for_an_activated_creator_is_tracked():
    svc = make_service(all_fakes=True)
    c = client_for(svc)
    assert c.post("/zbc/creators/applications", json=_app()).json()["activation"]["activated"] is True
    r = c.post("/zbc/creators/clip_1/payments", json={"request_id": "pay-679", "amount_usd": "50.00"})
    assert r.status_code == 200 and _count(svc, "creator_payment_tracked") == 1


# =============================================================================
# D1 — DOB plausibility
# =============================================================================


@pytest.mark.parametrize("dob", ["0001-01-01", "1066-10-14", "1899-12-31"])
def test_d1_dob_before_1900_is_422(dob):
    svc = make_service(all_fakes=True)
    r = client_for(svc).post("/zbc/creators/applications", json=_app(date_of_birth=dob))
    assert r.status_code == 422, r.text
    assert svc.ledger.events == [] and svc.creators == {}


def test_d1_dob_implying_age_over_120_is_422_and_120_is_accepted():
    svc = make_service(all_fakes=True)
    c = client_for(svc)
    today = svc.age_evaluation_date()
    over = date(today.year - 121, today.month, today.day) - timedelta(days=1)  # 121 on the server's date
    r = c.post("/zbc/creators/applications", json=_app(date_of_birth=over.isoformat()))
    assert r.status_code == 422 and "over 120" in r.json()["detail"], r.text
    assert svc.ledger.events == [] and svc.creators == {}
    exactly = date(today.year - 120, today.month, today.day)
    r = c.post("/zbc/creators/applications", json=_app(creator_id="clip_2", date_of_birth=exactly.isoformat()))
    assert r.status_code == 201 and r.json()["vetting"]["outcome"] == "approve", r.text


# =============================================================================
# I1 — restart: event ids of the creating operations are stable
# =============================================================================


def _non_anchor(ledger) -> int:
    """Bug sweep D: the evidence line's ``log_anchor`` is not one of the operation's own events."""
    return sum(1 for e in ledger.events if e["event_type"] != "log_anchor")


def test_i1_restarted_process_retry_of_start_dedupes_and_later_events_never_collide():
    ledger = FakeLedgerClient()
    first = make_service(all_fakes=True, ledger=ledger)
    c1 = client_for(first)
    assert c1.post("/onboarding/clients", json=start_body()).status_code == 201
    assert c1.post("/onboarding/clients/client_a/messages", json={"text": "can you change my budget"}).status_code == 200
    n = _non_anchor(ledger)
    # "restart": a new process, same ONBOARDING_INSTANCE_ID (default), same ledger
    second = make_service(all_fakes=True, ledger=ledger)
    c2 = client_for(second)
    assert c2.post("/onboarding/clients", json=start_body()).status_code == 201
    assert _non_anchor(ledger) == n, "the restarted process's retry of the same start was recorded twice"
    # a NEW event at the same position of the new history is recorded, not deduped
    assert c2.post("/onboarding/clients/client_a/messages", json={"text": "please pause my ads"}).status_code == 200
    assert _non_anchor(ledger) == n + 1
    ids = [e["event_id"] for e in ledger.events]
    assert len(ids) == len(set(ids))


def test_i1_instance_id_is_configuration_and_separates_instances():
    assert load_config({"ONBOARDING_INSTANCE_ID": "onb-eu-1"}).instance_id == "onb-eu-1"
    with pytest.raises(ConfigError):
        OnboardingConfig(instance_id="bad id!")
    ledger = FakeLedgerClient()
    a = make_service(ledger=ledger, config=OnboardingConfig(instance_id="a"))
    b = make_service(ledger=ledger, config=OnboardingConfig(instance_id="b"))
    client_for(a).post("/zbc/creators/applications", json=_app())
    client_for(b).post("/zbc/creators/applications", json=_app(creator_id="clip_1"))
    ids = [e["event_id"] for e in ledger.events]
    assert len(ids) == len(set(ids)) and len(ids) >= 4
