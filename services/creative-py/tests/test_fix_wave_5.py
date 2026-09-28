"""
Fix wave 5 (AEGIS round 4, Sep 24 2026) — reproductions written to FAIL on
the pre-fix code (integration 978b663) and pass after:

NEW-1  never-say auto-passed ASCII lookalike spellings: "Guaranteed retums"
       (rn for m), "make rnoney fast", "guaranteecl returns" (cl for d),
       "vv" for w, "rni", "miracle kure". Now a similarity gate
       (shared/text.visual_near_miss): visual skeleton (UTS #39's only
       ASCII multi-letter entry m->rn, plus the typographic pairs cl->d,
       vv->w; looser pairs nn/uu/ci/ri/ii and the I->l fold for detection
       only) + bounded Damerau-Levenshtein over windows of caption words;
       skeleton-identical -> reject, within budget -> human_review.
       Exhaustively single-edited and fuzzed; false-positive rate measured
       on tests/ordinary_captions.py; bounded time on 100 KB.
NEW-3  serve.py ran uvicorn's httptools parser with no head limit and no
       head/idle deadlines (a 100-200 MB header was buffered; idle and
       partial-head sockets stayed open). Now h11 with a 16 KiB head cap, a
       10 s head deadline, 5 s keep-alive, bounded concurrency — tested on a
       real socket against the real launcher.
LOW-D  ledger-rust's own load-shed 503 (request never read) was reported as
       "unknown"; and an uncertain human verdict could only be cleared by
       re-sending it. Now that exact 503 is "not recorded", and the same
       reviewer can withdraw an uncertain verdict once the ledger confirms
       it holds no such event.

Ports: 20110-20119 (this wave's range for creative-py real-socket tests).
"""

from __future__ import annotations

import asyncio
import os
import random
import select
import socket
import string
import subprocess
import sys
import time
import zlib
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from _procinfo import rss_kib

from conftest import NOW, TEST_SERVICE_TOKEN, Api, port_range
from flows import ok, zbc_open
from ordinary_captions import CAPTIONS, NEVER_SAY_FP_LIST
from samples import TODAY, zbc_clip, zbc_goal
from shared.clock import FixedClock
from shared.ledger import FakeLedgerClient, HttpLedgerClient, LedgerRecordError

SRC = Path(__file__).resolve().parents[1] / "src"
TAIL = " slowly. This budget myth. Listen on Pod Plus."

# =====================================================================================
# NEW-1 — ASCII lookalike spellings of never-say phrases
# =====================================================================================

NS_PHRASES = ["guaranteed returns", "get rich", "make money", "miracle cure", "wealth secrets"]
# (text, expected outcome): skeleton-identical -> reject; near -> human_review
AEGIS_NEW1 = [
    ("Guaranteed retums", "reject"),       # rn for m
    ("make rnoney fast", "reject"),        # rn for m
    ("guaranteecl returns", "reject"),     # cl for d
    ("vvealth secrets", "reject"),         # vv for w
    ("Get rlch", "human_review"),          # l for i: a human's call, as before this wave (never a reject)
    ("rnake rnoney", "reject"),            # rn for m, twice
    ("Guaranteecl retums", "reject"),      # cl for d and rn for m
    ("GUARANTEED RETUMS", "reject"),       # upper case
    ("make uuoney", "human_review"),       # two wrong letters, within the 2-edit budget of a 9-letter phrase
    ("mirade cure", "reject"),             # d for cl: the reading is symmetric, so this is the phrase too
    ("make rnioney", "human_review"),      # rni for m (one extra letter)
    ("make nnoney", "human_review"),       # nn for m
    ("miracle kure", "human_review"),      # k for c
    ("Guaranteed retrns", "human_review"),  # a dropped letter
    ("get rjch", "human_review"),          # j for i
    ("guaranteed retunrs", "human_review"),  # transposition
    ("getrlch", "human_review"),           # run together
]


@pytest.fixture
def ns_api(api):
    zbc_open(api, goal=zbc_goal(never_say=NS_PHRASES))
    return api


@pytest.mark.parametrize("text,want", AEGIS_NEW1)
def test_new1_ascii_lookalike_spellings_are_rejected_or_held(ns_api, text, want):
    sid = f"n1_{zlib.crc32(text.encode())}"
    for field in ("transcript", "caption", "on_screen_text"):
        body = zbc_clip(f"{sid}{field[0]}", **{field: text + TAIL + (" #ad" if field == "caption" else "")})
        d = ok(ns_api.post("/zbc/clips", body), 201)
        assert d["outcome"] == want, (field, text, d)
        if want == "reject":
            assert any(b["rule_id"].startswith("NS") and "lookalike" in b["reason"] for b in d["broken_rules"]), d
        else:
            assert any("lookalike letters or a small misspelling" in r or "possible never-say" in r
                       for r in d["human_review_reasons"]), d


def _rulebook(registry, phrases):
    from zbc import rulebook_writer
    from zbc.rulebook import RulebookStatus
    from zbc.rulebook_writer import CampaignGoal

    goal = CampaignGoal.model_validate(zbc_goal(never_say=phrases))
    return rulebook_writer.draft(goal, registry, TODAY).model_copy(update={"status": RulebookStatus.LIVE, "live_at": NOW})


# multi-character ASCII lookalikes: letter -> spellings that imitate it
# (the UTS #39 entries, the typographic pairs of view B, and "rni" = rn + one extra letter)
MULTI = {"m": ["rn", "nn", "rni"], "d": ["cl"], "w": ["vv", "uu"], "a": ["ci"], "n": ["ri"], "u": ["ii"]}
UNICODE_SET = {"m": "rn", "d": "cl", "w": "vv", "i": "l"}  # the skeleton's own entries (cost 0 to detect)
FUZZ_PHRASES = [p for p in NEVER_SAY_FP_LIST if len(p.replace(" ", "")) >= 4] + ["wealth secrets"]


def _mutate(rng: random.Random, phrase: str) -> str:
    chars = list(phrase)
    idx = [k for k, c in enumerate(chars) if c.isalpha()]
    kind = rng.choice(["sub", "ins", "del", "swap", "multi", "multi", "split", "join"])
    k = rng.choice(idx)
    if kind == "split":  # a word broken in two
        chars.insert(k + 1, " ")
    elif kind == "join":  # two words run together
        if " " in chars:
            del chars[chars.index(" ")]
        else:
            chars.insert(k + 1, " ")
    elif kind == "sub":
        chars[k] = rng.choice([c for c in string.ascii_lowercase if c != chars[k]])
    elif kind == "ins":
        chars.insert(k + rng.randint(0, 1), rng.choice(string.ascii_lowercase))
    elif kind == "del":
        del chars[k]
    elif kind == "swap":
        pairs = [j for j in idx if j + 1 < len(chars) and chars[j + 1].isalpha() and chars[j] != chars[j + 1]]
        if pairs:
            j = rng.choice(pairs)
            chars[j], chars[j + 1] = chars[j + 1], chars[j]
        else:
            chars[k] = "x"
    else:
        cands = [j for j in idx if chars[j] in MULTI]
        if cands:
            j = rng.choice(cands)
            chars[j] = rng.choice(MULTI[chars[j]])
        else:
            chars[k] = "q"
    return "".join(chars)


def _mutate_unicode_plus_edit(rng: random.Random, phrase: str) -> str:
    """One random edit (or multi-char lookalike), then the UTS #39 lookalikes
    (rn/cl/vv/l) applied to most letters. (The edit comes first so it can't
    land INSIDE a lookalike pair — "c y l" no longer looks like "d".)"""
    out = _mutate(rng, phrase) if rng.random() < 0.8 else phrase
    return "".join(UNICODE_SET.get(c, c) if rng.random() < 0.7 else c for c in out)


def test_new1_fuzz_single_edits_and_multichar_lookalikes_never_auto_pass(registry):
    """Fix wave 6 (N3): "cure" (4 letters) is opted in with `fuzzy`; a short
    entry that is not opted in is exact-only by design."""
    from zbc import clip_review

    rb = _rulebook(registry, [{"phrase": p, "fuzzy": True} if len(p) <= 4 else p for p in FUZZ_PHRASES])
    rng = random.Random(20260924_5)
    for i in range(3000):
        phrase = rng.choice(FUZZ_PHRASES)
        mutated = (_mutate if i % 3 else _mutate_unicode_plus_edit)(rng, phrase)
        if rng.random() < 0.5:
            mutated = mutated.title()
        field = rng.choice(["transcript", "caption", "on_screen_text"])
        base = zbc_clip(f"f5_{i}")
        base[field] = f"{base[field]} {mutated} today"
        d = clip_review.review(clip_review.ClipSubmission.model_validate(base), rb, registry, NOW)
        assert d.outcome != "pass", (i, phrase, repr(mutated), field)


def _every_single_edit(phrase: str):
    """Every substitution, insertion, deletion, adjacent transposition and
    multi-character lookalike of `phrase`, plus each word split or joined."""
    n = len(phrase)
    for k in range(n):
        if phrase[k] == " ":
            yield phrase[:k] + phrase[k + 1:]  # words run together
            continue
        for c in string.ascii_lowercase + " ":
            if c != phrase[k]:
                yield phrase[:k] + c + phrase[k + 1:]
                yield phrase[:k] + c + phrase[k:]
        yield phrase[:k] + phrase[k + 1:]
        if k + 1 < n and phrase[k + 1] not in (" ", phrase[k]):
            yield phrase[:k] + phrase[k + 1] + phrase[k] + phrase[k + 2:]
        for spelling in MULTI.get(phrase[k], ()):
            yield phrase[:k] + spelling + phrase[k + 1:]
    for c in string.ascii_lowercase:
        yield phrase + c
        yield c + phrase


def test_new1_every_single_edit_of_every_phrase_is_caught():
    """Exhaustive (not sampled): for each never-say phrase of the list, every
    one-edit variant and every multi-character lookalike spelling is a near
    miss (never an automatic pass) — in a sentence, in any case."""
    from shared.text import PhraseMatch, match_phrase, near_miss

    checked = 0
    for phrase in FUZZ_PHRASES:
        fuzzy = len(phrase) <= 4  # fix wave 6 (N3): a 4-letter entry is opted in here, exact-only otherwise
        for variant in _every_single_edit(phrase):
            text = f"Honestly, {variant.title()} today. #ad"
            assert match_phrase(text, phrase) is not PhraseMatch.NONE or near_miss(text, phrase, fuzzy), (phrase, variant)
            checked += 1
    assert checked > 5000


def test_new1_false_positive_rate_on_ordinary_captions_is_under_5_percent():
    """The rule's own false positives: ordinary captions (none says a
    never-say phrase) that the similarity gate would send to a human.
    Measured: 6/239 = 2.5% (see the printed list)."""
    from shared.text import PhraseMatch, match_phrase, visual_lookalike_exact, visual_near_miss

    assert len(CAPTIONS) >= 200
    flagged = []
    for c in CAPTIONS:
        assert all(match_phrase(c, p) is PhraseMatch.NONE for p in NEVER_SAY_FP_LIST), c
        hits = [(p, visual_near_miss(c, p)) for p in NEVER_SAY_FP_LIST if visual_near_miss(c, p)]
        assert not any(visual_lookalike_exact(c, p) for p in NEVER_SAY_FP_LIST), c  # never REJECTED
        if hits:
            flagged.append((c, hits))
    rate = len(flagged) / len(CAPTIONS)
    print(f"\nNEW-1 false positives: {len(flagged)}/{len(CAPTIONS)} = {rate:.1%}")
    for c, h in flagged:
        print("   ", repr(c), [(p, x[2]) for p, x in h])
    assert rate < 0.05, flagged


def test_new1_ordinary_captions_through_full_review_are_not_held_by_this_rule(registry):
    from zbc import clip_review

    rb = _rulebook(registry, NEVER_SAY_FP_LIST)
    held = 0
    for i, c in enumerate(CAPTIONS):
        sub = clip_review.ClipSubmission.model_validate(zbc_clip(f"fp{i}", caption=c + " #ad"))
        d = clip_review.review(sub, rb, registry, NOW)
        assert d.outcome != "reject", (c, d.broken_rules)
        held += any("lookalike letters or a small misspelling" in r for r in d.human_review_reasons)
    assert held / len(CAPTIONS) < 0.05, held


def test_new1_short_phrases_get_no_edit_budget():
    """<= 3 letters: skeleton-exact only (one edit from "win" is "in",
    "wine", "won" ...). Documented limitation, see visual_budget().
    Fix wave 6 changed the tiers (N1: 1/2/3 edits for 4-6/7-10/11+
    letters) and made 4-letter entries exact-only unless opted in (N3);
    this test used to assert 1 for 4-8 letters and 2 for 9+."""
    from shared.text import visual_budget, visual_near_miss

    assert visual_budget(3) == 0 and visual_budget(4) == 0 and visual_budget(4, fuzzy=True) == 1
    assert visual_budget(6) == 1 and visual_budget(7) == 2 and visual_budget(10) == 2 and visual_budget(11) == 3
    assert visual_near_miss("We're winning with wine", "win") is None
    assert visual_near_miss("vvin big", "win") is not None


def test_new1_visual_gate_is_linear_time_on_100kb():
    """Fix wave 6 (N1): the gate scans the text once for a whole set of
    phrases (`visual_near_misses`, what Clip Review calls); this test used
    to call the single-phrase `visual_near_miss` sixteen times, which is
    now sixteen scans (~8 s here) and not the production path."""
    from shared.text import visual_near_misses

    def visual_near_miss(text, phrase):
        return visual_near_misses(text, ((phrase, False),))[phrase]

    rng = random.Random(7)
    inputs = [
        " ".join(rng.choice(CAPTIONS) for _ in range(2500))[:100_000],
        ("guaranteed retums get rjch make rnoney " * 3000)[:100_000],
        "a " * 50_000, "rn" * 50_000, "rn m " * 20_000, ("clclvvrn " * 12000)[:100_000],
        " ".join(rng.choice(["guaranteex", "returnsx", "getrich", "makemoney", "rnoney"]) for _ in range(20_000))[:100_000],
        " ".join("".join(rng.choice(string.ascii_lowercase) for _ in range(rng.randint(1, 9)))
                 for _ in range(20_000))[:100_000],
        " ".join("".join(rng.choice("rnclvuiae") for _ in range(rng.randint(1, 9))) for _ in range(20_000))[:100_000],
    ]
    # measured on the build machine: 0.05-3.0 s per 100 KB input for all 16
    # phrases (the worst is random words over the lookalike alphabet, where
    # most windows survive the filters); linear, so 200 KB takes ~2x.
    phrases = tuple((p, False) for p in NEVER_SAY_FP_LIST)
    for s in inputs:
        t0 = time.perf_counter()
        visual_near_misses(s, phrases)
        dt = time.perf_counter() - t0
        assert dt < 6.0, (s[:20], dt)
    s2 = inputs[-1] + " " + inputs[-1]
    t0 = time.perf_counter()
    visual_near_misses(s2, phrases)
    assert time.perf_counter() - t0 < 12.0


def test_new1_osa_distance_is_correct():
    from shared.text import _osa_within

    def ref(a, b):
        d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
        for i in range(len(a) + 1):
            d[i][0] = i
        for j in range(len(b) + 1):
            d[0][j] = j
        for i in range(1, len(a) + 1):
            for j in range(1, len(b) + 1):
                d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] != b[j - 1]))
                if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                    d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
        return d[-1][-1]

    rng = random.Random(3)
    for _ in range(4000):
        a = "".join(rng.choice("abcr") for _ in range(rng.randint(0, 9)))
        b = "".join(rng.choice("abcr") for _ in range(rng.randint(0, 9)))
        for k in (0, 1, 2):
            want = ref(a, b)
            assert _osa_within(a, b, k) == (want if want <= k else None), (a, b, k)


# =====================================================================================
# NEW-3 — request head limits and idle / partial-head deadlines, real socket
# =====================================================================================

PORTS = port_range(range(20110, 20120))  # CREATIVE_TEST_PORTS overrides (fix wave 9)
# The documented bounds (serve.py; pinned by test_new3_launcher_config_is_pinned).
REQUEST_HEAD_TIMEOUT_S = 10.0
KEEP_ALIVE_TIMEOUT_S = 5


def _free_port() -> int:
    for port in PORTS:
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # fix wave 22 (G3): TIME_WAIT is free
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free port in 20110-20119")


def _start(extra_env: dict | None = None):
    port = _free_port()
    env = {**os.environ, "CREATIVE_SERVICE_TOKEN": TEST_SERVICE_TOKEN, "CREATIVE_PORT": str(port),
           **(extra_env or {})}
    env.pop("LEDGER_SERVICE_URL", None)
    env.pop("LEDGER_SERVICE_TOKEN", None)
    proc = subprocess.Popen([sys.executable, "serve.py"], cwd=SRC, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 20
    while True:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                return proc, port
        except httpx.HTTPError:
            pass
        if proc.poll() is not None or time.monotonic() > deadline:
            proc.kill()
            raise RuntimeError("creative-py did not start")
        time.sleep(0.1)


def _stop(proc):
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


@pytest.fixture(scope="module")
def server():
    proc, port = _start()
    try:
        yield proc, port
    finally:
        _stop(proc)


# Fix wave 16: portable (Linux /proc, macOS/BSD ps); measures only the server pid.
_rss_kb = rss_kib


def _health_ok(port: int) -> bool:
    return httpx.get(f"http://127.0.0.1:{port}/health", timeout=3).status_code == 200


def test_new3_oversized_header_is_refused_and_memory_stays_flat(server):
    proc, port = server
    before = _rss_kb(proc.pid)
    status = None
    sent = 0
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(b"GET /health HTTP/1.1\r\nHost: t\r\nX-A: ")
        chunk = b"a" * (1 << 20)
        try:
            for _ in range(200):  # up to 200 MB, the AEGIS reproduction's size
                s.sendall(chunk)
                sent += 1
        except OSError:
            pass
        try:
            s.settimeout(5)
            data = s.recv(200)
            if data.startswith(b"HTTP/1.1 "):
                status = int(data.split(b" ", 2)[1])
        except OSError:
            pass
    time.sleep(0.5)
    after = _rss_kb(proc.pid)
    assert status in (None, 400, 431), status  # refused (or closed), never served
    assert sent < 200, f"server accepted all {sent} MB of a header"
    assert after - before < 16 * 1024, f"RSS grew {before} -> {after} KB"
    assert _health_ok(port)


def _closed_within(sock: socket.socket, bound: float, trickle: bool = False) -> float | None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < bound + 5:
        r, _, _ = select.select([sock], [], [], 0.25)
        if r:
            try:
                if not sock.recv(4096):
                    return time.monotonic() - t0
            except OSError:
                return time.monotonic() - t0
        if trickle:
            try:
                sock.sendall(b"a")
            except OSError:
                return time.monotonic() - t0
    return None


@pytest.mark.parametrize("mode", ["idle", "partial", "trickle"])
def test_new3_idle_partial_and_trickled_heads_are_closed_within_the_deadline(server, mode):
    _, port = server
    bound = REQUEST_HEAD_TIMEOUT_S + 2
    with socket.create_connection(("127.0.0.1", port), timeout=1) as s:
        if mode == "partial":
            s.sendall(b"GET /health HTTP/1.1\r\nHost: t\r\n")
        if mode == "trickle":
            s.sendall(b"GET /health HTTP/1.1\r\nHost: t\r\nX-Slow: ")
        took = _closed_within(s, bound, trickle=(mode == "trickle"))
    assert took is not None and took <= bound, (mode, took)
    assert _health_ok(port)


def test_new3_idle_keep_alive_is_closed_and_keep_alive_still_works(server):
    _, port = server
    req = b"GET /health HTTP/1.1\r\nHost: t\r\n\r\n"
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(req)
        assert b"200" in s.recv(4096).split(b"\r\n", 1)[0]
        time.sleep(1)
        s.sendall(req + req)  # reuse + pipelining still work
        data = b""
        while data.count(b"HTTP/1.1 200") < 2:
            chunk = s.recv(65536)
            assert chunk, data
            data += chunk
        took = _closed_within(s, KEEP_ALIVE_TIMEOUT_S + 2)
    assert took is not None and took <= KEEP_ALIVE_TIMEOUT_S + 2, took


def test_new3_concurrency_is_bounded():
    proc, port = _start({"CREATIVE_MAX_CONCURRENCY": "4"})
    held = []
    try:
        for _ in range(4):
            held.append(socket.create_connection(("127.0.0.1", port), timeout=2))
        time.sleep(0.3)
        r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=3)
        assert r.status_code == 503, r.status_code
        for s in held:
            s.close()
        held.clear()
        time.sleep(0.3)
        assert _health_ok(port)
    finally:
        for s in held:
            s.close()
        _stop(proc)


def test_new3_launcher_config_is_pinned():
    import serve

    src = (SRC / "serve.py").read_text()
    for needle in ("http=_HeadDeadlineH11Protocol", "h11_max_incomplete_event_size=MAX_HEADER_BYTES",
                   "timeout_keep_alive=KEEP_ALIVE_TIMEOUT_S", "limit_concurrency=max_concurrency()"):
        assert needle in src
    from api import MAX_HEADER_BYTES

    assert MAX_HEADER_BYTES == 16 * 1024
    assert serve.REQUEST_HEAD_TIMEOUT_S == REQUEST_HEAD_TIMEOUT_S and serve.KEEP_ALIVE_TIMEOUT_S == KEEP_ALIVE_TIMEOUT_S


def test_new3_middleware_rechecks_head_size_and_bounds_body_delivery(api):
    # any launcher (TestClient bypasses h11): a head over 16 KiB is a 431
    r = api.client.get("/health", headers={"X-Big": "a" * (17 * 1024)})
    assert r.status_code == 431, r.status_code

    from api import BodyLimit

    sent = []

    async def app(scope, receive, send):  # never reached
        raise AssertionError("app called")

    async def receive():
        await asyncio.sleep(3600)

    async def send(msg):
        sent.append(msg)

    mw = BodyLimit(app, read_timeout=0.3)
    t0 = time.monotonic()
    # fix wave 8 (N7-1): a body under no / a non-JSON content type is 415 before it is read, so the
    # delivery deadline is exercised on a JSON body
    asyncio.run(mw({"type": "http", "headers": [(b"content-length", b"10"), (b"content-type", b"application/json")],
                    "raw_path": b"/x"}, receive, send))
    assert time.monotonic() - t0 < 2
    assert sent and sent[0]["status"] == 408


# =====================================================================================
# LOW-D — ledger load-shed 503 is "not recorded"; withdraw an uncertain verdict
# =====================================================================================

SHED = {"error": "ledger-rust is at its connection limit; retry shortly"}


@pytest.mark.parametrize("status,body,want", [
    (503, SHED, False),                               # ledger-rust's own shed: request never read
    (503, {"error": "upstream timed out"}, "unknown"),  # some other 503 (e.g. a proxy): may have landed
    (503, None, "unknown"),
    (500, {"error": "failed to persist event"}, "unknown"),
])
def test_lowd_only_the_ledgers_own_shed_503_is_not_recorded(status, body, want):
    def handler(request):
        return httpx.Response(status, json=body) if body is not None else httpx.Response(status, text="busy")

    client = HttpLedgerClient("http://ledger.test", "tok", transport=httpx.MockTransport(handler))
    with pytest.raises(LedgerRecordError) as ei:
        client.record_event("cp:x", "creative_production", "t", "a", "s", {}, "s")
    assert ei.value.took_effect == want


def test_lowd_ledger_shed_body_matches_ledger_rust_source():
    src = (Path(__file__).resolve().parents[3] / "services/ledger-rust/src/bin/server.rs").read_text()
    shed = src[src.index("async fn shed"):]
    assert '"ledger-rust is at its connection limit; retry shortly"' in shed[:400]
    assert "503 Service Unavailable" in shed[:800]
    serve_fn = src[src.index("async fn serve(listener"):]
    # the shed path answers without parsing the request or touching the ledger. Fix wave 21 (ledger N20-M-1): shed()
    # also takes the shared drain bound — after the 503 it discards (never parses) the unread request bytes, bounded,
    # before closing — so the spawn is `shed(stream, <drains>)`, no longer `shed(stream)`.
    shed_fn = shed[:shed.index("\nasync fn ", 10)] if "\nasync fn " in shed[10:] else shed
    assert "tokio::spawn(shed(stream" in serve_fn and "serve_connection" in serve_fn
    assert "on_ledger" not in shed_fn and "handle(" not in shed_fn and "serve_connection" not in shed_fn


class LoseBeforeCommit(FakeLedgerClient):
    """The request never lands, but the client can't tell (timeout)."""

    lose_next: bool = False

    def record_event(self, *a, **k):
        if self.lose_next:
            self.lose_next = False
            self.calls += 1
            raise LedgerRecordError("ledger response lost: ReadTimeout (test double: NOT committed)")
        return super().record_event(*a, **k)


class CommitThenLose(FakeLedgerClient):
    lose_next: bool = False

    def record_event(self, *a, **k):
        super().record_event(*a, **k)
        if self.lose_next:
            self.lose_next = False
            raise LedgerRecordError("ledger response lost: ReadTimeout (test double: committed)")


REVIEWER = "zbc_clip_human_reviewer"
PASS = {"actor_id": REVIEWER, "outcome": "pass"}
REJECT = {"actor_id": REVIEWER, "outcome": "reject", "broken_rules": [{"rule_id": "QF-01", "reason": "low"}]}


def _uncertain_verdict(led):
    from shared.actors import ActorRegistry, Role

    actors = ActorRegistry()
    actors.add("zbc_clip_human_reviewer_2", [Role.ZBC_CLIP_HUMAN_REVIEWER])
    clock = FixedClock(NOW)
    api = Api(ledger=led, clock=clock, actors=actors)
    zbc_open(api)
    d = ok(api.post("/zbc/clips", zbc_clip("clip_w", resolution_height_px=None)), 201)
    assert d["outcome"] == "human_review"
    led.lose_next = True
    r = api.post("/zbc/clips/clip_w/human-review", PASS)
    assert r.status_code == 503 and r.json()["took_effect"] == "unknown", r.text
    return api, clock


def test_lowd_same_reviewer_withdraws_an_unrecorded_uncertain_verdict():
    led = LoseBeforeCommit()
    api, clock = _uncertain_verdict(led)
    # before: a different verdict is refused
    assert api.post("/zbc/clips/clip_w/human-review", REJECT).status_code == 409
    W = "/zbc/clips/clip_w/human-review/withdraw"
    # another reviewer can't withdraw it; too early is refused
    clock.at = clock.at + timedelta(minutes=5)
    r = api.post(W, {"actor_id": "zbc_clip_human_reviewer_2"})
    assert r.status_code == 403, r.text
    r = api.post(W, {}, as_actor=None)
    assert r.status_code == 401, r.text
    clock.at = NOW + timedelta(seconds=30)
    assert api.post(W, {"actor_id": REVIEWER}).status_code == 409
    clock.at = NOW + timedelta(minutes=5)
    d = ok(api.post(W, {"actor_id": REVIEWER}))
    assert d["outcome"] == "human_review" and d["withdrawn_event_id"].startswith("cp:")
    ev = led.of_type("clip_human_verdict_withdrawn")
    assert len(ev) == 1 and ev[0]["payload"]["withdrawn_event_id"] == d["withdrawn_event_id"]
    assert not led.of_type("clip_human_reviewed")
    # now any verdict can be given, once
    d = ok(api.post("/zbc/clips/clip_w/human-review", REJECT))
    assert d["outcome"] == "reject"
    assert len(led.of_type("clip_human_reviewed")) == 1
    assert api.post(W, {"actor_id": REVIEWER}).status_code == 409  # nothing pending any more


def test_lowd_withdraw_refused_when_the_ledger_holds_the_verdict():
    led = CommitThenLose()
    api, clock = _uncertain_verdict(led)
    clock.at = NOW + timedelta(minutes=5)
    r = api.post("/zbc/clips/clip_w/human-review/withdraw", {"actor_id": REVIEWER})
    assert r.status_code == 409 and "ledger holds" in r.text, r.text
    assert not led.of_type("clip_human_verdict_withdrawn")
    d = ok(api.post("/zbc/clips/clip_w/human-review", PASS))  # re-send resolves it
    assert d["outcome"] == "pass" and len(led.of_type("clip_human_reviewed")) == 1


class CommitThenLoseWithdrawal(LoseBeforeCommit):
    """The verdict is lost before commit; then the WITHDRAWAL's response is lost after commit."""

    def record_event(self, *a, **k):
        if k.get("event_type") == "clip_human_verdict_withdrawn" and not getattr(self, "withdrawal_seen", False):
            self.withdrawal_seen = True
            super().record_event(*a, **k)
            raise LedgerRecordError("ledger response lost: ReadTimeout (test double: committed)")
        return super().record_event(*a, **k)


def test_lowd_a_lost_withdrawal_response_is_retried_as_the_identical_record():
    led = CommitThenLoseWithdrawal()
    api, clock = _uncertain_verdict(led)
    clock.at = NOW + timedelta(minutes=5)
    W = "/zbc/clips/clip_w/human-review/withdraw"
    r = api.post(W, {"actor_id": REVIEWER})
    assert r.status_code == 503 and r.json()["took_effect"] == "unknown", r.text
    assert api.post("/zbc/clips/clip_w/human-review", REJECT).status_code == 409  # still pending
    d = ok(api.post(W, {"actor_id": REVIEWER}))  # retry: the same record, ledger answers idempotently
    assert len(led.of_type("clip_human_verdict_withdrawn")) == 1
    assert led.of_type("clip_human_verdict_withdrawn")[0]["event_id"] == d["event_id"]
    assert ok(api.post("/zbc/clips/clip_w/human-review", REJECT))["outcome"] == "reject"


def test_lowd_withdraw_with_an_unreadable_ledger_changes_nothing():
    led = LoseBeforeCommit()
    api, clock = _uncertain_verdict(led)
    clock.at = NOW + timedelta(minutes=5)
    led.fail_all = True
    r = api.post("/zbc/clips/clip_w/human-review/withdraw", {"actor_id": REVIEWER})
    assert r.status_code == 503 and r.json()["took_effect"] is False, r.text
    led.fail_all = False
    assert api.post("/zbc/clips/clip_w/human-review", REJECT).status_code == 409  # still pending
