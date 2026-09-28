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
from conftest import TEST_SERVICE_TOKEN, client_for, free_test_port, make_service, start_body
from ledger import FakeLedgerClient, LedgerWriteError
from onboarding_schema import ClipperApplication
from onboarding_schema import requests as rq

SRC = Path(__file__).resolve().parents[1] / "src"
PATTERNS = H.collect_patterns()

# The per-pattern bound from the finding: 50 ms per 100 KB of hostile input,
# on an unloaded dev box (the worst pattern measures ~25 ms there: 2x margin).
PER_100KB_S = 0.050
# Early-out at 10 KB (the same rate) so a quadratic pattern fails in
# milliseconds instead of running for minutes at 100 KB.
PER_10KB_S = 0.005

# Fix wave 6: an AEGIS run under a concurrent cargo build measured
# _NEGATED_OK at 5.4 ms/10 KB against the 5 ms bound (2.3 ms unloaded). Two
# changes. (1) The harness times the thread's own CPU (``time.thread_time``),
# so pre-emption by other processes no longer counts. (2) The absolute bounds
# are scaled by how much slower THIS machine is than the dev box they were
# derived on: a known LINEAR reference regex is timed right before each
# pattern on the same 100 KB shape (~8 ms of CPU on the dev box); the factor
# never tightens the bounds (min 1) and never relaxes them past 8x. A
# machine-independent linearity check (10x the input may cost at most 20x
# the CPU, never ~100x) is asserted in addition, so a quadratic pattern can
# never hide behind a slow machine.
_REF = re.compile(r"(?i)\bcan(?:no|')?t\s+(?:\w+\s+){0,3}?zzz\b")
_REF_INPUT = "cannot " * (100_000 // 7) + "!"
REF_NOMINAL_S = 0.008
SLOWDOWN_CAP = 8.0
LINEAR_RATIO = 20.0  # 10 KB -> 100 KB


def slowdown() -> float:
    t = H.best_time(lambda s: H._drain_matches(_REF.finditer(s)), _REF_INPUT, runs=5)
    return min(SLOWDOWN_CAP, max(1.0, t / REF_NOMINAL_S))


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
    fn = H.use_of(name, p)
    slow = slowdown()
    # 1. every hostile shape at 4 KB (a quadratic pattern already fails here), ranked
    ranked = []
    for u, tail, s in H.inputs(p, 4_000):
        t = H.best_time(fn, s, 1)
        if t > 10 * PER_10KB_S * slow:
            t = H.best_time(fn, s, 2)  # not a scheduling hiccup?
            assert t <= 10 * PER_10KB_S * slow, f"{name}: {u!r}+{tail!r} took {t * 1000:.1f} ms on 4 KB (slowdown {slow:.1f}x)"
        ranked.append((t, u, tail))
    ranked.sort(reverse=True)
    # 2. the three worst shapes: 10 KB (fail fast), then 100 KB against the
    #    bound, and 10 KB -> 100 KB must scale linearly whatever the machine
    for _, unit, tail in ranked[:3]:
        s10 = unit * (10_000 // len(unit)) + tail
        t10 = H.best_time(fn, s10, runs=5)                 # best of 5, like the 100 KB run it is the ratio's base
        assert t10 < PER_10KB_S * slow, f"{name}: {unit!r}+{tail!r} took {t10 * 1000:.1f} ms on 10 KB (slowdown {slow:.1f}x)"
        s100 = unit * (100_000 // len(unit)) + tail
        t100 = H.best_time(fn, s100, runs=5)
        assert t100 < PER_100KB_S * slow, f"{name}: {unit!r}+{tail!r} took {t100 * 1000:.1f} ms on 100 KB (slowdown {slow:.1f}x)"
        # timer floor of 0.2 ms so a microsecond t10 does not make the ratio noise
        assert t100 < LINEAR_RATIO * max(t10, 0.0002), \
            f"{name}: {unit!r}+{tail!r} not linear: {t10 * 1000:.2f} ms on 10 KB, {t100 * 1000:.1f} ms on 100 KB"


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
    for t100, unit in sorted(at_100kb, reverse=True)[:5]:  # the five worst shapes, at 1 MB
        t1m = H.best_time(fn, unit * (1_000_000 // len(unit)) + "!", 1)
        # linear: 10x the input costs ~10x (never 100x), and 1 MB stays cheap
        assert t1m < max(20 * t100, 0.05), f"{scanner} {unit!r}: 100 KB {t100:.3f}s, 1 MB {t1m:.3f}s"
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
    t = time.perf_counter()
    with pytest.raises(ValidationError):
        rq.MessageRequest.model_validate({"text": "a" * 60_000})
    assert time.perf_counter() - t < 0.2
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
        t = time.perf_counter()
        redaction.scrub_log_record(rec)
        assert time.perf_counter() - t < 0.2
        assert len(rec.getMessage()) < redaction.LOG_PATH_MAX + 200
    rec = _access("/" + "orders/" * 150_000)
    redaction.scrub_log_record(rec)
    assert "truncated" in rec.getMessage()
    other = logging.LogRecord("onboarding.x", logging.INFO, __file__, 1, "%s", ("login " + "a" * 1_000_000,), None)
    t = time.perf_counter()
    redaction.scrub_log_record(other)
    assert time.perf_counter() - t < 0.5 and len(other.getMessage()) < redaction.LOG_TEXT_MAX + 200


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


def _start_server(port: int) -> subprocess.Popen:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LEDGER_", "DETECTION_"))}
    env.update({"ONBOARDING_SERVICE_TOKEN": TEST_SERVICE_TOKEN, "ONBOARDING_PORT": str(port), "PYTHONUNBUFFERED": "1",
                "PYTHONDONTWRITEBYTECODE": "1"})
    proc = subprocess.Popen([sys.executable, "-m", "api"], cwd=str(SRC), env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    for _ in range(100):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.5).status_code == 200:
                return proc
        except httpx.HTTPError:
            time.sleep(0.1)
    proc.kill()
    raise AssertionError("server did not start")


def test_r1_real_uvicorn_health_stays_responsive_under_hostile_url_and_body():
    port = free_test_port()
    proc = _start_server(port)
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
    r = c.post("/zbc/creators/clip_1/payments", json={"amount_usd": "50.00"})
    assert r.status_code == 409, r.text
    assert "not activated" in r.json()["detail"] and r.json()["vetting_outcome"] == "decline"
    assert _count(svc, "creator_payment_tracked") == 0 and svc.creators["clip_1"].payments == []


def test_p1_payment_with_w9_but_activation_incomplete_is_refused():
    svc = make_service()  # honest stand-ins: vetting approves, activation is blocked
    c = client_for(svc)
    r = c.post("/zbc/creators/applications", json=_app())
    assert r.json()["vetting"]["outcome"] == "approve" and r.json()["activation"]["activated"] is False
    r = c.post("/zbc/creators/clip_1/payments", json={"amount_usd": "50.00"})
    assert r.status_code == 409 and "activation is complete" in r.json()["detail"], r.text
    assert _count(svc, "creator_payment_tracked") == 0


def test_p1_payment_for_an_activated_creator_is_tracked():
    svc = make_service(all_fakes=True)
    c = client_for(svc)
    assert c.post("/zbc/creators/applications", json=_app()).json()["activation"]["activated"] is True
    r = c.post("/zbc/creators/clip_1/payments", json={"amount_usd": "50.00"})
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


def test_i1_restarted_process_retry_of_start_dedupes_and_later_events_never_collide():
    ledger = FakeLedgerClient()
    first = make_service(all_fakes=True, ledger=ledger)
    c1 = client_for(first)
    assert c1.post("/onboarding/clients", json=start_body()).status_code == 201
    assert c1.post("/onboarding/clients/client_a/messages", json={"text": "can you change my budget"}).status_code == 200
    n = len(ledger.events)
    # "restart": a new process, same ONBOARDING_INSTANCE_ID (default), same ledger
    second = make_service(all_fakes=True, ledger=ledger)
    c2 = client_for(second)
    assert c2.post("/onboarding/clients", json=start_body()).status_code == 201
    assert len(ledger.events) == n, "the restarted process's retry of the same start was recorded twice"
    # a NEW event at the same position of the new history is recorded, not deduped
    assert c2.post("/onboarding/clients/client_a/messages", json={"text": "please pause my ads"}).status_code == 200
    assert len(ledger.events) == n + 1
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
