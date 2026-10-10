"""
Fix wave 4 (AEGIS round 3 + integration run 3, Sep 24 2026) — reproductions
written to FAIL on the pre-fix code (integration e3c5115) and pass after:

NS    never-say auto-passed with plain ASCII symbols / digits standing in
      for letters ("return$", "G€t", "r¡ch", "ri¢h", "6et") and with Latin
      letters the fold table misses (ꭇ ꞃ ᵹ ꜧ ꞅ). Now: a skeleton mapping
      (symbols/digits -> letters) plus a wildcard near-miss matcher send
      any such line to a human; a word mixing letters with symbols/digits
      is never an automatic pass; an unrecognised Latin letter is never an
      automatic pass. Fuzzed; plus a false-positive guard set.
LOST  a clip whose ledger record committed but whose response was lost was
      wedged forever after RETRY_WINDOW (ledger 409 on every identical
      retry) and the API said "did NOT take effect" while the ledger held
      the decision. Now: an uncertain attempt is remembered with its exact
      payload and replayed by an identical retry at any later time; a 409
      on our own id is reported as uncertain, never "did not take effect".
IDEM  a lost response on a creating POST + retry created duplicates
      (brief-0002, brief-0003). Now: Idempotency-Key on every creating POST
      (derived from actor + canonical request when absent).
F8    the escalation block was escaped by a clone at length 31 (inside the
      export tolerance of a 30 s spec), by "Client_Acme" / "client_acme.",
      and by a format re-wrap (mp4 -> m4v).
LIM   request bodies over 1 MiB are refused before parsing; the text
      scanners are linear time; handlers never block the event loop.
"""

from __future__ import annotations

import inspect
import os
import random
import re
import subprocess
import sys
import time
import zlib
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from conftest import NOW, TEST_ACTOR_TOKENS, TEST_FOUNDER_TOKEN, Api
from flows import C, ok, zbc_open, zbm_approved_brief, zbm_work_at_quality
from samples import TODAY, zbc_clip, zbc_goal, zbc_kit_request, zbm_requirements, zbm_work
from shared.clock import FixedClock
from shared.ledger import FakeLedgerClient, HttpLedgerClient, LedgerRecordError

ACTOR_HEADER = "X-Creative-Actor-Token"
TAIL = " slowly. This budget myth. Listen on Pod Plus."
DEVTOOLS = Path(__file__).resolve().parents[1] / "devtools"


def _hdr(actor: str | None = None, key: str | None = None) -> dict:
    h = {ACTOR_HEADER: TEST_ACTOR_TOKENS[actor]} if actor else {}
    if key is not None:
        h["Idempotency-Key"] = key
    return h


# =====================================================================================
# NS — never-say with symbols, digits and unfolded Latin letters
# =====================================================================================

AEGIS_NS = [
    "Guaranteed return$", "G€t rich", "Get r¡ch", "Get ri¢h", "Get ꭇich", "Get ꞃich", "ᵹet rich", "Get riꜧ",
    "Guaranteed ꞅeturns", "Guaranteed retuꞃns", "Guaranteed r€turns", "Guaranteed returnꞅ", "6et rich", "Get rlch",
    "Get r!ch", "Get r|ch", "Gu@ranteed returns", "Guar4nteed returns", "Get ri(h", "Guaranteed re+urns",
    "#GetRich", "#get_rich", "Guaranteed-returns", "Get ri.ch", "Guaranteed retur£s",
]


@pytest.fixture
def gr_api(api):
    zbc_open(api, goal=zbc_goal(never_say=["guaranteed returns", "get rich"]))
    return api


@pytest.mark.parametrize("text", AEGIS_NS)
def test_ns_symbol_digit_and_unfolded_latin_variants_never_auto_pass(gr_api, text):
    sid = f"clip_{zlib.crc32(text.encode())}"
    for field in ("transcript", "caption", "on_screen_text"):
        body = zbc_clip(f"{sid}{field[0]}", **{field: text + TAIL + (" #ad" if field == "caption" else "")})
        d = ok(gr_api.post("/zbc/clips", body), 201)
        assert d["outcome"] != "pass", (field, text, d)


def test_ns_near_miss_names_the_phrase(gr_api):
    d = ok(gr_api.post("/zbc/clips", zbc_clip("clip_nm", transcript="Guaranteed return$" + TAIL)), 201)
    assert d["outcome"] == "human_review"
    assert any("guaranteed returns" in r for r in d["human_review_reasons"]), d


def test_ns_unfolded_latin_letter_is_named(gr_api):
    d = ok(gr_api.post("/zbc/clips", zbc_clip("clip_ul", transcript="Say ꭇ" + TAIL)), 201)
    assert d["outcome"] == "human_review"
    assert any("U+AB47" in r for r in d["human_review_reasons"]), d


def test_ns_mixed_symbol_word_anywhere_is_never_auto_pass(gr_api):
    # not near any never-say phrase: still a human look (rule b)
    d = ok(gr_api.post("/zbc/clips", zbc_clip("clip_mx", caption="The budget myth nobody t@lks about #ad")), 201)
    assert d["outcome"] == "human_review", d
    assert any("letters mixed with" in r for r in d["human_review_reasons"]), d


# Ordinary captions that must still pass automatically (false-positive guard).
ORDINARY = [
    "The budget myth nobody talks about #ad",
    "Don't fall for this budget myth — it's the #1 mistake people make. #ad",
    "This budget myth costs you $20 a week (yes, really). #ad",
    "Save 20% on your budget: myth busted! #ad",
    "Budget myth #3: \"you can't save on $40k.\" Wrong. #ad",
    "Since the 1990s this budget myth won't die... #ad",
    "Budget myth, e.g. the U.S. \"latte factor\" — debunked at 9am. #ad",
    "A self-made budget myth? Co-op savings, 2nd try. #ad",
    "Budget myth: $1,200/month isn't enough? It is. #ad",
    "The budget myth, explained in 60s. @podplus #ad",
    "Paid partnership: the budget myth. $5 off with code MYTH #ad",
]


@pytest.mark.parametrize("caption", ORDINARY)
def test_ns_false_positive_guard_ordinary_captions_still_pass(gr_api, caption):
    d = ok(gr_api.post("/zbc/clips", zbc_clip(f"clip_ok{zlib.crc32(caption.encode())}", caption=caption)), 201)
    assert d["outcome"] == "pass", (caption, d)


def _fuzz_pools():
    from shared.text import SKELETON, fold_table

    folded = set(fold_table())
    unfolded_latin = []
    import unicodedata

    for cp in range(0x100, 0x20000):
        ch = chr(cp)
        name = unicodedata.name(ch, "")
        if name.startswith("LATIN ") and ch.isalpha() and ch not in folded and ch.casefold() not in folded:
            n = unicodedata.normalize("NFKC", ch)
            if len(n) == 1 and not n.isascii() and n not in folded:
                unfolded_latin.append(ch)
    symbols = [c for c in "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~¡¢£¤¥¦§¨©ª«¬®¯°±²³´µ¶·¸¹º»¼½¾¿×÷€™•…"]
    return SKELETON, unfolded_latin, symbols


def test_ns_fuzz_symbol_digit_lookalike_substitution_and_insertion_never_auto_passes(registry):
    """Random substitutions (skeleton symbols, digits, arbitrary symbols,
    Latin letters outside the fold table) and insertions into every never-say
    phrase used by the test rulebooks: never an automatic pass."""
    from zbc import clip_review, rulebook_writer
    from zbc.rulebook import RulebookStatus
    from zbc.rulebook_writer import CampaignGoal

    phrases = ["get rich", "guaranteed returns", "risk free", "double your money"]
    goal = CampaignGoal.model_validate(zbc_goal(never_say=phrases))
    rb = rulebook_writer.draft(goal, registry, TODAY).model_copy(update={"status": RulebookStatus.LIVE, "live_at": NOW})
    skeleton, unfolded, symbols = _fuzz_pools()
    rev: dict[str, list[str]] = {}
    for sym, letters in skeleton.items():
        for letter in letters:
            rev.setdefault(letter, []).append(sym)
    assert unfolded, "no unfolded Latin letters found"
    rng = random.Random(20260924)
    n = 2500
    for i in range(n):
        phrase = rng.choice(phrases)
        chars = list(phrase if rng.random() < 0.5 else phrase.title())
        letters_idx = [k for k, c in enumerate(chars) if c.isalpha()]
        # at least one mutation, at most ~a third of the letters
        for k in rng.sample(letters_idx, rng.randint(1, max(1, len(letters_idx) // 3))):
            kind = rng.random()
            lo = chars[k].lower()
            if kind < 0.4 and rev.get(lo):
                chars[k] = rng.choice(rev[lo])
            elif kind < 0.6:
                chars[k] = rng.choice("0123456789")
            elif kind < 0.8:
                chars[k] = rng.choice(symbols)
            else:
                chars[k] = rng.choice(unfolded)
        if rng.random() < 0.4:  # plus an insertion inside a word
            k = rng.choice(letters_idx)
            chars.insert(k + 1, rng.choice(symbols + list("0123456789")))
        mutated = "".join(chars)
        field = rng.choice(["transcript", "caption", "on_screen_text"])
        base = zbc_clip(f"fz{i}")
        base[field] = f"{base[field]} {mutated}"
        sub = clip_review.ClipSubmission.model_validate(base)
        d = clip_review.review(sub, rb, registry, NOW)
        assert d.outcome != "pass", (i, repr(mutated), field)


# =====================================================================================
# LOST — lost ledger response must not wedge a clip, nor lie
# =====================================================================================

class CommitThenLose(FakeLedgerClient):
    """The ledger commits, then the response is lost (the client sees a timeout)."""

    lose_next: bool = False

    def record_event(self, *a, **k):
        super().record_event(*a, **k)
        if self.lose_next:
            self.lose_next = False
            raise LedgerRecordError("ledger unreachable: ReadTimeout (test double: committed, response lost)")


def _clip_events(ledger, sid):
    return [e for e in ledger.of_type("clip_reviewed") if e["subject_id"] == sid]


def test_lost_clip_response_identical_retry_after_window_takes_effect_with_true_time():
    clock = FixedClock(NOW)
    led = CommitThenLose()
    api = Api(ledger=led, clock=clock)
    zbc_open(api)
    first = clock.now()
    led.lose_next = True
    r = api.post("/zbc/clips", zbc_clip("clip_x"))
    assert r.status_code == 503
    assert r.json()["took_effect"] == "unknown", r.json()
    assert "NOT take effect" not in r.json()["detail"]
    assert len(_clip_events(led, "clip_x")) == 1
    clock.at = clock.at + timedelta(minutes=16)
    for _ in range(2):
        d = ok(api.post("/zbc/clips", zbc_clip("clip_x")), 201)
        assert d["received_at"].startswith(first.isoformat()[:19]), d
    assert len(_clip_events(led, "clip_x")) == 1
    assert ok(api.get("/zbc/clips/clip_x"))["submission_id"] == "clip_x"
    # different content under the same id stays refused
    assert api.post("/zbc/clips", zbc_clip("clip_x", caption="other #ad")).status_code == 409
    clock.at = clock.at + timedelta(days=40)
    d = ok(api.post("/zbc/clips", zbc_clip("clip_x")), 201)
    assert d["received_at"].startswith(first.isoformat()[:19])


def test_lost_clip_response_retry_days_later_still_resolves():
    clock = FixedClock(NOW)
    led = CommitThenLose()
    api = Api(ledger=led, clock=clock)
    zbc_open(api)
    led.lose_next = True
    assert api.post("/zbc/clips", zbc_clip("clip_d")).status_code == 503
    clock.at = clock.at + timedelta(days=9)
    d = ok(api.post("/zbc/clips", zbc_clip("clip_d")), 201)
    assert d["received_at"].startswith(NOW.isoformat()[:19])
    assert len(_clip_events(led, "clip_d")) == 1


def test_lost_human_verdict_retry_after_window_replays_exact_record():
    clock = FixedClock(NOW)
    led = CommitThenLose()
    api = Api(ledger=led, clock=clock)
    zbc_open(api)
    d = ok(api.post("/zbc/clips", zbc_clip("clip_hr", resolution_height_px=None)), 201)
    assert d["outcome"] == "human_review"
    led.lose_next = True
    body = {"actor_id": "zbc_clip_human_reviewer", "outcome": "pass"}
    assert api.post("/zbc/clips/clip_hr/human-review", body).status_code == 503
    clock.at = clock.at + timedelta(hours=3)
    # a DIFFERENT verdict while the first one's outcome is unknown: refused, not recorded
    r = api.post("/zbc/clips/clip_hr/human-review", {**body, "outcome": "reject",
                                                     "broken_rules": [{"rule_id": "QF-01", "reason": "low"}]})
    assert r.status_code == 409, r.text
    d = ok(api.post("/zbc/clips/clip_hr/human-review", body))
    assert d["outcome"] == "pass" and d["decided_at"].startswith(NOW.isoformat()[:19])
    assert len(led.of_type("clip_human_reviewed")) == 1


def test_ledger_409_on_our_own_id_is_reported_as_uncertain_not_as_no_effect():
    from shared.ledger import EvidenceRecorder

    def handler(request):
        return httpx.Response(409, json={"error": "conflict"})

    client = HttpLedgerClient("http://ledger.test", "tok", transport=httpx.MockTransport(handler))
    rec = EvidenceRecorder(client)
    with pytest.raises(LedgerRecordError) as ei:
        rec.record("clip_reviewed", "zbc_clip_review", "clip_z", {"a": 1}, "x")
    assert ei.value.took_effect == "unknown"


def test_connect_refused_is_a_certain_failure_and_timeout_is_uncertain():
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    for handler, want in ((refuse, False), (timeout, "unknown")):
        client = HttpLedgerClient("http://ledger.test", "tok", transport=httpx.MockTransport(handler))
        with pytest.raises(LedgerRecordError) as ei:
            client.record_event("cp:x", "creative_production", "t", "a", "s", {}, "s")
        assert ei.value.took_effect == want, handler


def _wait(url: str, headers=None) -> None:
    for _ in range(100):
        try:
            httpx.get(url, headers=headers, timeout=0.5)
            return
        except httpx.HTTPError:
            time.sleep(0.05)
    raise RuntimeError(f"{url} never came up")


@pytest.fixture
def lossy_ledger():
    """fake ledger server over real HTTP behind devtools/lossy_proxy.py."""
    yield from _lossy_stack()


def _lossy_stack(popen=subprocess.Popen):
    from _procinfo import start_owned
    from conftest import live_ports

    env = {**os.environ, "FAKE_LEDGER_TOKEN": "lossy-test-ledger-token"}
    procs = []
    # Fix wave 22 (G3, N21-C-6): every process is started INSIDE the try, so a failure to start the second one (or
    # anything after the first) still stops the first — the ledger was left running (orphaned) before; and a
    # process that ignores SIGTERM is killed, never left behind by a timeout in the cleanup.
    # Fix wave 26b (scout C5-6): each child is accepted only once IT holds its port (_procinfo.start_owned: the
    # shared picker, the owner check, another port when the child lost the race). `_wait` alone took any answer on
    # a picked port for the ledger's — another process's, when one held it.
    try:
        ledger, lp = start_owned(lambda port: popen([sys.executable, str(DEVTOOLS / "fake_ledger_server.py"), str(port)],
                                                    env=env, stderr=subprocess.DEVNULL), live_ports())
        procs.append(ledger)
        proxy, pp = start_owned(lambda port: popen([sys.executable, str(DEVTOOLS / "lossy_proxy.py"), str(port),
                                                    f"http://127.0.0.1:{lp}"], stderr=subprocess.DEVNULL), live_ports())
        procs.append(proxy)
        _wait(f"http://127.0.0.1:{lp}/ledger/entries")
        _wait(f"http://127.0.0.1:{pp}/__stats")
        yield f"http://127.0.0.1:{pp}", f"http://127.0.0.1:{lp}", "lossy-test-ledger-token"
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()


def test_the_stack_never_orphans_the_ledger_when_the_proxy_fails_to_start():
    """Fix wave 22 (G3, N21-C-6): the proxy's start fails after the ledger started — the ledger is stopped, not
    left running (it was started outside the try and orphaned)."""
    started = []

    def popen(argv, **kw):
        if "lossy_proxy.py" in argv[1]:
            raise OSError("simulated: the proxy could not start")
        p = subprocess.Popen(argv, **kw)
        started.append(p)
        return p

    gen = _lossy_stack(popen)
    with pytest.raises(OSError, match="simulated"):
        next(gen)
    assert len(started) == 1 and started[0].poll() is not None, "the ledger was left running"


def test_lost_clip_response_over_real_http_lossy_proxy(lossy_ledger):
    proxy, ledger_url, tok = lossy_ledger
    clock = FixedClock(NOW)
    api = Api(ledger=HttpLedgerClient(proxy, tok, timeout=5.0), clock=clock)
    zbc_open(api)
    first = clock.now()
    httpx.get(f"{proxy}/__ctl", params={"drop": 1, "match": "/ledger/events"})
    r = api.post("/zbc/clips", zbc_clip("clip_lp"))
    assert r.status_code == 503 and r.json()["took_effect"] == "unknown", r.text
    stats = httpx.get(f"{proxy}/__stats").json()
    assert [d["upstream_status"] for d in stats["dropped"]] == [201]
    clock.at = clock.at + timedelta(minutes=20)
    d = ok(api.post("/zbc/clips", zbc_clip("clip_lp")), 201)
    assert d["received_at"].startswith(first.isoformat()[:19])
    entries = httpx.get(f"{ledger_url}/ledger/entries", headers={"Authorization": f"Bearer {tok}"}).json()
    assert len([e for e in entries if e["event_type"] == "clip_reviewed" and e["subject_id"] == "clip_lp"]) == 1


# =====================================================================================
# IDEM — creating POSTs are idempotent
# =====================================================================================

def test_idem_brief_retry_with_key_returns_original_and_records_nothing(api):
    body = {"requirements": zbm_requirements()}
    r1 = api.client.post("/zbm/briefs", json=body, headers=_hdr("zbm_brief_writer", "k-brief-1"))
    n = len(api.ledger.events)
    r2 = api.client.post("/zbm/briefs", json=body, headers=_hdr("zbm_brief_writer", "k-brief-1"))
    assert r1.status_code == 201 and r2.status_code == 201
    assert r2.json() == r1.json()
    assert len(api.ledger.events) == n and len(api.zbm.briefs) == 1
    other = {"requirements": zbm_requirements(hook="Something else.")}
    r3 = api.client.post("/zbm/briefs", json=other, headers=_hdr("zbm_brief_writer", "k-brief-1"))
    assert r3.status_code == 409, r3.text
    assert len(api.zbm.briefs) == 1


def test_idem_brief_identical_retry_without_key_is_deduplicated(api):
    body = {"requirements": zbm_requirements()}
    r1 = api.client.post("/zbm/briefs", json=body, headers=_hdr("zbm_brief_writer"))
    r2 = api.client.post("/zbm/briefs", json=body, headers=_hdr("zbm_brief_writer"))
    assert r1.status_code == r2.status_code == 201 and r2.json()["brief_id"] == r1.json()["brief_id"]
    assert len(api.zbm.briefs) == 1
    # a fresh key is a deliberate second brief
    r3 = api.client.post("/zbm/briefs", json=body, headers=_hdr("zbm_brief_writer", "second-order"))
    assert r3.status_code == 201 and r3.json()["brief_id"] != r1.json()["brief_id"]


def test_idem_job_and_work_retries(api):
    b = zbm_approved_brief(api)
    j1 = api.client.post(f"/zbm/briefs/{b['brief_id']}/jobs", headers=_hdr(key="job-a"))
    commissions = api.ledger.of_type("production_opened")
    j2 = api.client.post(f"/zbm/briefs/{b['brief_id']}/jobs", headers=_hdr(key="job-a"))
    assert j1.status_code == j2.status_code == 201 and j1.json() == j2.json()
    assert api.ledger.of_type("production_opened") == commissions and len(api.zbm.jobs) == 1
    # without a key: identical retry while the job is untouched returns it too
    j3 = api.client.post(f"/zbm/briefs/{b['brief_id']}/jobs")
    j4 = api.client.post(f"/zbm/briefs/{b['brief_id']}/jobs")
    assert j3.json()["job_id"] == j4.json()["job_id"]
    job = j1.json()["job_id"]
    w1 = api.client.post(f"/zbm/jobs/{job}/work", json=zbm_work(), headers=_hdr(key="w-1"))
    w2 = api.client.post(f"/zbm/jobs/{job}/work", json=zbm_work(), headers=_hdr(key="w-1"))
    assert w1.status_code == w2.status_code == 201 and w1.json() == w2.json()
    assert len(api.zbm.work) == 1


def test_idem_zbc_creating_routes(api):
    from flows import zbc_rights_on_file

    zbc_rights_on_file(api)
    lic_retry = api.post("/rights/licenses", {"actor_id": "rights_desk",
                                              "license": __import__("samples").zbc_license()})
    assert lic_retry.status_code == 201, lic_retry.text  # identical retry of a caller-id record: replayed
    body = {"actor_id": "zbc_rulebook_writer", "goal": zbc_goal()}
    r1 = api.post(f"{C}/rulebooks", body)
    r2 = api.post(f"{C}/rulebooks", body)
    assert r1.status_code == r2.status_code == 201 and r1.json() == r2.json()
    assert len(api.ledger.of_type("rulebook_drafted")) == 1
    zbc_open_after_draft(api)
    k1 = api.client.post(f"{C}/kit", json=zbc_kit_request(), headers=_hdr(key="kit-1"))
    k2 = api.client.post(f"{C}/kit", json=zbc_kit_request(), headers=_hdr(key="kit-1"))
    assert k1.status_code == k2.status_code == 201 and k1.json() == k2.json()
    assert len(api.ledger.of_type("campaign_kit_built")) == 1
    ok(api.post(f"{C}/kit/sign", andre=TEST_FOUNDER_TOKEN))
    c1 = ok(api.post("/zbc/clips", zbc_clip("clip_i")), 201)
    c2 = ok(api.post("/zbc/clips", zbc_clip("clip_i")), 201)
    assert c1 == c2 and len(_clip_events(api.ledger, "clip_i")) == 1
    assert api.post("/zbc/clips", zbc_clip("clip_i", caption="changed #ad")).status_code == 409


def zbc_open_after_draft(api):
    from samples import ZBC_ASSETS, zbc_source

    ok(api.post(f"{C}/rulebooks/1/review", {"actor_id": "zbc_campaign_rulebook"}))
    ok(api.post(f"{C}/rulebooks/1/sign", andre=TEST_FOUNDER_TOKEN))
    ok(api.post(f"{C}/rights-check", {"assets": ZBC_ASSETS}))
    ok(api.post(f"{C}/rulebooks/1/go-live"))
    ok(api.post(f"{C}/moment-map", zbc_source()))
    ok(api.post(f"{C}/hook-sheets"))


def test_idem_bad_key_is_422(api):
    r = api.client.post("/zbm/briefs", json={"requirements": zbm_requirements()},
                        headers=_hdr("zbm_brief_writer", "x" * 300))
    assert r.status_code == 422


# =====================================================================================
# F8 — fingerprint escape of the escalation block
# =====================================================================================

def _escalate(api):
    decl = {**zbm_work()["declared"], "length_seconds": 30.5}
    brief, job, w = zbm_work_at_quality(api, zbm_work(declared=decl))
    q = lambda wid: ok(api.post(f"/zbm/work/{wid}/quality", {"actor_id": "zbm_creative_quality", "notes": ["not premium"]}))
    assert q(w["work_id"])["stage"] == "sent_back"
    w2 = ok(api.post(f"/zbm/jobs/{job['job_id']}/work", zbm_work(declared=decl)), 201)
    ok(api.post(f"/zbm/work/{w2['work_id']}/export-validation"))
    ok(api.post(f"/zbm/work/{w2['work_id']}/rights"))
    assert q(w2["work_id"])["stage"] == "escalated_to_andre"


D1 = zbm_requirements()["deliverables"][0]


@pytest.mark.parametrize("label,over", [
    ("length 31 (same render fits both)", {"deliverables": [{**D1, "length_seconds": 31}]}),
    ("length 29", {"deliverables": [{**D1, "length_seconds": 29}]}),
    ("format re-wrap", {"deliverables": [{**D1, "format": "m4v"}]}),
    ("aspect not reduced", {"deliverables": [{**D1, "aspect_ratio": "18:32"}]}),
])
def test_f8_clone_within_tolerance_is_refused(api, label, over):
    _escalate(api)
    r = api.post("/zbm/briefs", {"requirements": zbm_requirements(**over)})
    assert r.status_code == 409, (label, r.text)


@pytest.mark.parametrize("cid", ["Client_Acme", "client_acme.", "client acme", "ｃｌｉｅｎｔ_ａｃｍｅ", "CLIENT_ACME"])
def test_f8_client_id_variants_cannot_exist(api, cid):
    r = api.post("/zbm/briefs", {"requirements": zbm_requirements(client_id=cid)})
    assert r.status_code == 422, (cid, r.text)


def test_f8_client_id_normalisation_for_fingerprints():
    from zbm.workflow import normalize_client_id

    assert {normalize_client_id(x) for x in ["client_acme", "Client_Acme", "client_acme.", "CLIENT-ACME",
                                             "ｃｌｉｅｎｔ_ａｃｍｅ", " client acme "]} == {"clientacme"}


def test_f8_far_length_is_a_different_deliverable(api):
    _escalate(api)
    r = api.post("/zbm/briefs", {"requirements": zbm_requirements(deliverables=[{**D1, "length_seconds": 45}])})
    assert r.status_code == 201, r.text


# =====================================================================================
# LIM — body size limit, linear-time scanning, never blocking the loop
# =====================================================================================

def test_lim_body_over_one_mib_is_413_before_parsing(api):
    big = b'{"requirements": "' + b"a" * (1024 * 1024 + 10) + b'"}'
    r = api.client.post("/zbm/briefs", content=big, headers={**_hdr("zbm_brief_writer"),
                                                             "content-type": "application/json"})
    assert r.status_code == 413, r.status_code

    def chunks():
        for _ in range(20):
            yield b"a" * 65536

    r = api.client.post("/zbc/clips", content=chunks(), headers={"content-type": "application/json"})
    assert r.status_code == 413, r.status_code


def test_lim_every_route_handler_is_sync_so_it_runs_off_the_event_loop(api):
    from fastapi.routing import APIRoute

    routes = [r for r in api.app.routes if isinstance(r, APIRoute)]
    assert routes
    for r in routes:
        assert not inspect.iscoroutinefunction(r.endpoint), r.path


def _adversarial_inputs(n: int) -> list[str]:
    return ["a" * n, "!" * n, "a!" * (n // 2), "a " * (n // 2), "#@" * (n // 2), "ab1$" * (n // 4),
            "́" * n, "é" * n, "ꭇ" * n, ("g u a r a n t e e d " * (n // 20))[:n], "​" * n,
            "".join(chr(0x41 + (i * 7919) % 0x2000) for i in range(n))]


def regex_cpu(p: re.Pattern, s: str, runs: int = 3) -> float:
    """Fix wave 25, H6 (AEGIS N24-S-11): the CPU the pattern spends on `s` (sub + search + fullmatch), this
    thread's own CPU time, the cyclic GC off, best of `runs`. It was ONE wall-clock reading: under three busy
    loops on a 2-CPU box the regexes measured 0.051-0.080 s against the 0.05 s bound while their CPU time stayed
    10-20 ms — the test measured the scheduler."""
    import gc

    best = float("inf")
    for _ in range(runs):
        was = gc.isenabled()
        gc.disable()
        try:
            t0 = time.thread_time()
            p.sub(" ", s)
            p.search(s)
            p.fullmatch(s)
            best = min(best, time.thread_time() - t0)
        finally:
            if was:
                gc.enable()
    return best


# Fix wave 26 (W26-2b; CI #2 macos-26: `('(.)\\1+', 'éééééééééé', 0.021163, 0.001886)` = ratio 11.2 against the bound
# 9.0, a linear pattern; OPEN.md F-4). "Linear time" is asserted as GROWTH, not as a cost relative to another pattern.
# Fix wave 26b (CI #3 macos-26, CI3-5: `(.)\1+` on U+0301, 6 250 -> 100 000 chars, 0.21 ms -> 21.1 ms = 99x against
# the bound 64): W26-2b compared ONE call on 6 250 chars with one on 100 000. `(.)\1+` keeps a backtracking frame per
# repetition in the regex engine's stack (~90 B per char, tracemalloc: 564 KiB at 6 250, 8 MiB at 100 000; the
# reference scan allocates ~1 KiB at any size), and on the macOS CI VM the 8 MiB case cost 211 ns/char against
# 34 ns/char for the small one (on this box, an M4 Pro, 3.13 and 3.14: 24 and 31 ns/char, growth 22-24x): the
# allocation, not the pattern. Growth is now measured as the SAME WORK at sizes that keep that stack small: GROWTH_SPAN
# calls on GROWTH_SMALL chars against one call on GROWTH_SPAN x as many (each side repeated GROWTH_REPS times so the
# reading is well above the timer); the largest stack is then ~0.7 MiB, the size CI read normally. Linear: ~1;
# quadratic: ~GROWTH_SPAN. GROWTH_RATIO_MAX = 4 is the old bound (growth 64 over a 16x span = 16 ** 1.5) in this form.
GROWTH_SPAN = 16
GROWTH_SMALL = 500
GROWTH_LARGE = GROWTH_SMALL * GROWTH_SPAN
GROWTH_REPS = 4
GROWTH_RATIO_MAX = 4.0
ABS_N = 100_000      # the absolute CPU bound (and the Linux-only ratio) are still read on 100 000 chars


def _regex_ops(p: re.Pattern, s: str) -> None:
    p.sub(" ", s)
    p.search(s)
    p.fullmatch(s)


def regex_same_work(p: re.Pattern, small: str, large: str, span: int = GROWTH_SPAN,
                    reps: int = GROWTH_REPS) -> tuple[float, float, float]:
    """(CPU of span x reps calls on `small`, CPU of reps calls on `large`, their ratio): this thread's CPU time, the
    cyclic GC off, best of 5 each. `large` is `span` x as long as `small`, so a linear pattern does the same work on
    both sides."""
    import gc

    def best(s: str, calls: int) -> float:
        b = float("inf")
        for _ in range(5):
            was = gc.isenabled()
            gc.disable()
            try:
                t0 = time.thread_time()
                for _ in range(calls):
                    _regex_ops(p, s)
                b = min(b, time.thread_time() - t0)
            finally:
                if was:
                    gc.enable()
        return b

    t_small = best(small, span * reps)
    t_large = best(large, reps)
    return t_small, t_large, t_large / t_small


# Fix wave 25, H6: the same three operations with a pattern that is linear by construction (one character class,
# one quantifier) on the same input, measured right after the pattern under test. Their ratio is the work the
# pattern does per character relative to a single scan. The CPU bound alone could not tell a pattern three times
# slower: the worst pair costs 10-17 ms, three times that is 31-52 ms, and 50 ms passed most of it. Measured (wave
# 25, 2-CPU box, 3 busy loops and a co-tenant, 3.12 and 3.13): the worst pair's ratio 4.8-6.1; every operation done
# three times, 14.4-17.7. The bound sits between.
# Fix wave 26 (W26-2b): that ratio is a property of the CPU and the CPython build, not of the pattern alone
# (`(.)\1+` on 'é' 1.8 here, 11.2 on macos-26), so this constant-factor bound is asserted where it was calibrated
# (Linux: this box and the ubuntu CI runners); elsewhere it is printed, and "linear" is the growth bound above.
# Fix wave 26b (F-4, R26-5): this one bound over all patterns could not see a cheap pattern made 3.9x slower (its own
# ratio ~1 becomes ~4, under 9) on any platform, and off Linux only the 50 ms bound saw constant factors at all. A
# changed pattern is now judged against ITS OWN reviewed form, per pattern and per input, on every platform
# (REVIEWED_PATTERNS, PIN_RATIO_MAX below); this ratio and the 50 ms bound still judge the absolute cost.
# Annotated CI (Oct 10 2026): on GitHub's ubuntu 3.12 runner the confusables character class read 11.7x the reference
# on EVERY reading, a linear pattern on a short input: the ratio to an unrelated pattern measures constant factors of
# the runner, not linearity. It is no longer asserted anywhere (it is printed). Linearity is judged by structure
# (``_regex_structure``) and by scaling (the same-work growth bound); constant factors by each pattern's own reviewed
# form (PIN_RATIO_MAX, every platform) and the 50 ms bound.
LINEAR_REF = re.compile(r"[^\w#@]+")
REGEX_RATIO_MAX = 9.0
REGEX_RATIO_CALIBRATED = False
# Fix wave 26b (AEGIS r25 F-10: the 50 ms CPU bound read 34.5 ms in 1 of 20 loaded runs, margin 1.45x). The bound is
# unchanged; a reading over it (or over REGEX_RATIO_MAX) is read again, fresh, up to ABS_ATTEMPTS times, and only a
# pattern over the bound on EVERY reading fails — the form R25B-1 gave onboarding's same-work check. A pattern that
# really costs more than 50 ms per 100 000 chars costs it on every reading; a load transient does not repeat on cue
# (test_lim_an_absolute_cpu_excursion_is_read_again_a_slow_pattern_never_passes).
ABS_ATTEMPTS = 3
ABS_CPU_MAX = 0.05


def _abs_readings(p: re.Pattern, s: str, cpu=None) -> tuple[float, float, list[tuple[float, float]]]:
    """(dt, ref, readings): the pattern's CPU on `s` and the linear reference's, read again while either is over its
    bound (ABS_CPU_MAX; REGEX_RATIO_MAX where calibrated), up to ABS_ATTEMPTS times; the last reading is returned,
    so it is over a bound only if every reading was."""
    cpu = cpu or regex_cpu
    readings: list[tuple[float, float]] = []
    for _ in range(ABS_ATTEMPTS):
        dt, ref = cpu(p, s), cpu(LINEAR_REF, s)
        readings.append((round(dt, 6), round(ref, 6)))
        if dt < ABS_CPU_MAX and not (REGEX_RATIO_CALIBRATED and dt / ref >= REGEX_RATIO_MAX):
            break
    return dt, ref, readings


# The constructs that can make a backtracking engine super-linear: an unbounded repeat inside another unbounded repeat,
# and a backreference. A pattern with one is linear only for a reason a reviewer checked; it must be listed here with
# that reason, so a new or changed pattern with such a construct fails until reviewed (the scaling check below still
# runs on it).
STRUCTURE_REVIEWED = {   # name in shared.text -> (SHA-256 prefix of the reviewed pattern, why it is linear)
    "_ORDINARY_WORD": ("70493736c0fcb795", "each outer iteration starts with a punctuation char the inner class excludes"),
    "_TAG_WORD": ("bd7ea5ed7981871f", "each outer iteration starts with '_', which the inner classes exclude"),
    "_LETTERLIKE_NAME": ("1b780c91f5070144", "each outer iteration starts with a space, which [A-Z-] excludes"),
    "_REGIONAL_STREAM": ("6eb04bdcd21ac348", "the inner run (spacing marks) and the regional indicator that ends each "
                                             "outer iteration are disjoint"),
    "_RUNS": ("5c8d4a253d0dc165", "one group of one char repeated: one pass, no alternative to retry"),
    "_DIVIDER": ("abcd023c5cb841aa", "one group of one char repeated: one pass, no alternative to retry"),
}


def _regex_structure(p: re.Pattern) -> list[str]:
    """The super-linear-capable constructs in ``p`` (sre's own parse tree): nested unbounded repeats (possessive and
    atomic ones excepted) and backreferences."""
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
                lo, hi, sub = av
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


def test_lim_every_regex_in_text_module_is_linear_time():
    """Linearity is asserted two ways, neither against another pattern's speed (annotated CI, Oct 10 2026: a ratio to
    an unrelated reference regex measured the runner's constant factors and failed a linear pattern every time):

    * structurally: every pattern's sre parse tree is free of nested unbounded repeats and backreferences, or the
      pattern (by name and content hash) is in STRUCTURE_REVIEWED with the reason it is still linear (deterministic,
      no timing; a changed pattern must be reviewed again);
    * by scaling, loosely: the same work on 16x longer inputs (adversarial runs) costs at most GROWTH_RATIO_MAX (4)
      times the CPU of the short side, best of 5; a quadratic pattern reads ~16 there
      (``test_lim_the_growth_bound_fails_superlinear_patterns_and_passes_a_costly_linear_one``).

    The 50 ms bound per 100 000 chars stays (absolute cost). The ratio to LINEAR_REF is printed only."""
    import shared.text as text

    import hashlib

    named = {k: v for k, v in vars(text).items() if isinstance(v, re.Pattern)}
    patterns = list(named.values())
    assert len(patterns) >= 2
    for name, p in named.items():
        risky = _regex_structure(p)
        reviewed = STRUCTURE_REVIEWED.get(name)
        same = reviewed is not None and hashlib.sha256(p.pattern.encode()).hexdigest()[:16] == reviewed[0]
        assert not risky or same, (name, p.pattern[:80], risky, "a super-linear-capable construct no reviewer has "
                                   "explained (or the reviewed pattern changed)")
    worst, worst_ratio, at, worst_growth, grew = 0.0, 0.0, None, 0.0, None
    for p in patterns:
        for small, large, s in zip(_adversarial_inputs(GROWTH_SMALL), _adversarial_inputs(GROWTH_LARGE),
                                   _adversarial_inputs(ABS_N)):
            t_small, t_large, growth = regex_same_work(p, small, large)
            dt, ref, readings = _abs_readings(p, s)
            worst = max(worst, dt)
            if growth > worst_growth:
                worst_growth, grew = growth, (p.pattern, s[:10])
            if dt / ref > worst_ratio:
                worst_ratio, at = dt / ref, (p.pattern, s[:10])
            assert growth < GROWTH_RATIO_MAX, (p.pattern, s[:10], t_small, t_large, growth)
            assert dt < ABS_CPU_MAX, (p.pattern, s[:10], "every reading (dt, ref) over the bound", readings)
            if REGEX_RATIO_CALIBRATED:
                assert dt / ref < REGEX_RATIO_MAX, (p.pattern, s[:10], "every reading (dt, ref) over", readings)
    print(f"\nLIM regex: worst CPU {worst * 1000:.1f} ms per {ABS_N} chars (bound {ABS_CPU_MAX * 1000:.0f} ms); worst same-work growth "
          f"{GROWTH_SPAN} x {GROWTH_SMALL} vs {GROWTH_LARGE} chars {worst_growth:.2f}x (bound {GROWTH_RATIO_MAX}) at "
          f"{grew!r}; worst CPU ratio to a single linear scan {worst_ratio:.2f} (bound {REGEX_RATIO_MAX}, "
          f"{'asserted' if REGEX_RATIO_CALIBRATED else 'NOT asserted on ' + sys.platform}) at {at!r}")


def test_lim_the_growth_bound_fails_superlinear_patterns_and_passes_a_costly_linear_one():
    """Fix wave 26 (W26-2b), same-work form since 26b: the growth bound on its own, on every platform. Two quadratic
    patterns fail it (a lookahead to the end of the input after every match; a backreference to an unbounded
    group); a pattern that matches exactly what `(.)\1+` matches with ~5x its CPU per character (linear: it
    reproduces CI #2's macOS reading on Linux, 21.2 ms and ratio 9.4 on 'é' x 100 000) passes it."""
    n = 1_000      # the quadratic term must dominate the per-match overhead (16 000 chars on the large side)
    for quadratic, s in ((re.compile(r"[^\w#@]+(?=[\s\S]*$)"), "a!"), (re.compile(r"(.+)\1+", re.DOTALL), "ab1$")):
        t_small, t_large, growth = regex_same_work(quadratic, s * (n // len(s)), s * (n * GROWTH_SPAN // len(s)))
        assert growth >= GROWTH_RATIO_MAX, (quadratic.pattern, t_small, t_large, growth)
    costly = re.compile(r"(.)(?:(?=\1)(?=[\s\S]{1,4})\1)+", re.DOTALL)
    assert [m.span() for m in costly.finditer("aabccc dd")] == [m.span() for m in re.finditer(r"(.)\1+", "aabccc dd")]
    for s in ("\u00e9", "a", "a!", "\u0301"):
        t_small, t_large, growth = regex_same_work(costly, s * (GROWTH_SMALL // len(s)), s * (GROWTH_LARGE // len(s)))
        assert growth < GROWTH_RATIO_MAX, (costly.pattern, s, t_small, t_large, growth)


def test_lim_an_absolute_cpu_excursion_is_read_again_a_slow_pattern_never_passes():
    """Fix wave 26b (AEGIS r25 F-10), with scripted readings: one reading over the 50 ms bound followed by a normal
    one passes (a load transient); a pattern over it on every reading fails."""
    p, s = re.compile("x"), "x"

    def scripted(values):
        it = iter(values)
        return lambda pat, inp: next(it)

    dt, ref, readings = _abs_readings(p, s, scripted([0.06, 0.003, 0.02, 0.003]))
    assert dt < ABS_CPU_MAX and len(readings) == 2, readings
    dt, ref, readings = _abs_readings(p, s, scripted([0.06, 0.003] * ABS_ATTEMPTS))
    assert dt >= ABS_CPU_MAX and len(readings) == ABS_ATTEMPTS, readings


# Fix wave 26b (AEGIS r25 F-4 and r26 R26-5): per-pattern constant-factor bounds, on every platform. The ratio to
# LINEAR_REF above is one bound over all patterns (a pattern 3.9x slower than its own form passes it when its own
# ratio is ~1) and is asserted on Linux only; elsewhere only the 50 ms bound could see a constant-factor slowdown.
# A per-pattern bound against a FIXED number of milliseconds is a property of the machine; against the pattern's own
# reviewed form it is not. REVIEWED_PATTERNS holds, for every pattern of shared.text, the form reviewed (literal
# source, or the reviewed construction for the classes built from tables), and the test compares, per pattern and per
# adversarial input, the CPU of the module's pattern with the CPU of that reviewed form — the same work on the same
# input on the same machine at the same moment, so the ratio is ~1 for an unchanged or equivalent pattern on any CPU
# and under any load, and the slowdown factor for a slower one. Measured on the build box (M4 Pro, 3.13): an unchanged
# pattern against itself 1.00-1.24 per input; `[^\w#@]+` rewritten as `(?:[^\w#@]|(?!))+` (same matches) 1.5-3.9 per
# input, 3.9 at worst; `_ORDINARY_WORD` behind a lookahead per letter up to 3.8. PIN_RATIO_MAX = 2.0 sits between: a
# pattern 2x slower than its reviewed form on any adversarial input fails, on every platform. A pattern that changes
# without a slowdown passes (no pin update needed); a NEW pattern must be added here (the test fails until it is),
# and the ratio-to-LINEAR_REF and 50 ms bounds above still judge its absolute cost.
def _reviewed_char_class(chars) -> str:
    """The reviewed construction of shared.text._char_class (code-point RANGES), frozen here so that a change to the
    module's construction is timed against it rather than copied by it."""
    cps = sorted(map(ord, chars))
    parts, i = [], 0
    while i < len(cps):
        j = i
        while j + 1 < len(cps) and cps[j + 1] == cps[j] + 1:
            j += 1
        parts.append(re.escape(chr(cps[i])) + ("-" + re.escape(chr(cps[j])) if j > i else ""))
        i = j + 1
    return "[" + "".join(parts) + "]"


REVIEWED_PATTERNS = {
    "_NON_WORD": (r"[^\w#@]+", re.UNICODE),
    "_LATIN_NAME": (r"LATIN (?:SMALL CAPITAL |SMALL |CAPITAL )?LETTER (?:SMALL CAPITAL |SCRIPT |DOTLESS |LONG )?"
                    r"([A-Z])(?: WITH .+)?", 0),
    "_CURRENCY_MATH_RE": (lambda t: "[" + re.escape("".join(t._CM_SOURCE)) + "]", 0),
    "_ORDINARY_WORD": (r"[^\W\d_]+(?:['’.&-][^\W\d_]+)*", 0),
    "_TAG_WORD": (r"[#@](?:[^\W\d_]|[0-9])+(?:_(?:[^\W\d_]|[0-9])+)*", 0),
    "_NUMBER_WORD": (r"[$€£¥]?[0-9][0-9,.]*(?:st|nd|rd|th|s|k|m|b|x|p|am|pm|h|hr|hrs|min|mins|yr|yrs|mo)?",
                     re.IGNORECASE),
    "_WORD_SPLIT": (r"[\s/–—…]+", 0),
    "_LETTERLIKE_NAME": (r"(?:MATHEMATICAL|FULLWIDTH|CIRCLED|PARENTHESIZED|SQUARED|NEGATIVE|CROSSED|"
                         r"TORTOISE SHELL BRACKETED|REGIONAL INDICATOR|DOUBLE-STRUCK|SCRIPT|BLACK-LETTER|TURNED|"
                         r"REVERSED|ROTATED|INVERTED|MODIFIER LETTER|SUPERSCRIPT|SUBSCRIPT|LATIN)\b(?: [A-Z-]+)*? "
                         r"(?:CAPITAL|SMALL|LETTER) ([A-Z])", 0),
    "_LETTERLIKE_RE": (lambda t: _reviewed_char_class(t.LETTERLIKE), 0),
    "_MAPPED_RE": (lambda t: _reviewed_char_class({*t.LETTERLIKE, t.BRAILLE_BLANK}), 0),
    "_REGIONAL_RUN": ("[\U0001F1E6-\U0001F1FF]+", 0),
    "_REGIONAL_STREAM": ("[\U0001F1E6-\U0001F1FF](?:[\\s\u00ad\u034f\u180e\u200b-\u200f\u2060-\u2064\ufe00-\ufe0f"
                         "\u20e3\ufeff]*[\U0001F1E6-\U0001F1FF])*", 0),
    "_RUNS": (r"(.)\1+", re.DOTALL),
    "_AZ_ONLY": (r"[^a-z]+", 0),
    "_SYMBOL_SCAN": (r"[\w#@]+|[^\w#@\s]", 0),
    "_TAG_RUN": (r"[#@][\w#@]*", 0),
    "_ALNUM": (r"[^\W_]", 0),
    "_DIVIDER": (r"([\u2500-\u25FF])\1{2,}", 0),
    "_BAR_NAME": (r"VERTICAL (?:LINE|BAR|EM DASH|EN DASH|LOW LINE|WAVY LOW LINE)$|DANDA$|PASEQ$|^DIVIDES$", 0),
    "_EMOJI_LETTER_WORD": (lambda t: "(?<!\\S)(?:" + _reviewed_char_class(t._ENCLOSED_LETTERS)
                           + "\uFE0F)+(?=[\\s.,!?;:]|$)", 0),
}
PIN_N = GROWTH_LARGE      # 8 000 chars: the stack-safe size of the growth check (CI3-5)
PIN_REPS = 2
PIN_RATIO_MAX = 2.0
PIN_ATTEMPTS = 3          # an over-bound reading is read again (fresh, both sides), as R25B-1 / F-10


def _reviewed(text, name: str) -> re.Pattern:
    src, flags = REVIEWED_PATTERNS[name]
    return re.compile(src(text) if callable(src) else src, flags)


def regex_vs_reviewed(p: re.Pattern, reviewed: re.Pattern, s: str, reps: int = PIN_REPS) -> tuple[float, float]:
    """(CPU of `p`, CPU of `reviewed`) for reps x (sub + search + fullmatch) on `s`: this thread's CPU time, the
    cyclic GC off, the two sides interleaved, best of 5 each."""
    import gc

    bp = br = float("inf")
    for _ in range(5):
        for side in (0, 1):
            pat = p if side == 0 else reviewed
            was = gc.isenabled()
            gc.disable()
            try:
                t0 = time.thread_time()
                for _ in range(reps):
                    _regex_ops(pat, s)
                t = time.thread_time() - t0
            finally:
                if was:
                    gc.enable()
            if side == 0:
                bp = min(bp, t)
            else:
                br = min(br, t)
    return bp, br


def _slowdowns_vs_reviewed(p: re.Pattern, reviewed: re.Pattern, n: int = PIN_N) -> list[tuple]:
    """Per adversarial input of `n` chars, the readings (CPU of p, CPU of reviewed, ratio) of the last attempt: an
    input whose ratio is at or over PIN_RATIO_MAX is read again, up to PIN_ATTEMPTS times."""
    out = []
    for s in _adversarial_inputs(n):
        for _ in range(PIN_ATTEMPTS):
            tp, tr = regex_vs_reviewed(p, reviewed, s)
            ratio = tp / max(tr, 1e-9)
            if ratio < PIN_RATIO_MAX:
                break
        out.append((s[:6], round(tp * 1000, 3), round(tr * 1000, 3), round(ratio, 2)))
    return out


def test_lim_every_regex_in_text_module_costs_no_more_than_its_reviewed_form():
    import shared.text as text

    names = {k for k, v in vars(text).items() if isinstance(v, re.Pattern)}
    assert names == set(REVIEWED_PATTERNS), ("a pattern of shared.text has no reviewed form here (add it to "
                                             "REVIEWED_PATTERNS) or a reviewed one is gone",
                                             sorted(names ^ set(REVIEWED_PATTERNS)))
    worst, changed = (0.0, None), []
    for name in sorted(names):
        p, reviewed = getattr(text, name), _reviewed(text, name)
        if (p.pattern, p.flags) != (reviewed.pattern, reviewed.flags):
            changed.append(name)
        rows = _slowdowns_vs_reviewed(p, reviewed)
        top = max(rows, key=lambda r: r[3])
        if top[3] > worst[0]:
            worst = (top[3], (name, top[0]))
        assert top[3] < PIN_RATIO_MAX, (f"{name} costs {top[3]}x its reviewed form on {top[0]!r} (bound "
                                        f"{PIN_RATIO_MAX}, every reading over it); per input (input, ms, reviewed ms, "
                                        f"ratio): {rows}")
    print(f"\nLIM regex vs reviewed form: worst {worst[0]:.2f}x at {worst[1]!r} (bound {PIN_RATIO_MAX}); "
          f"changed since review (timed, not slower): {changed or 'none'}")


def test_lim_the_reviewed_form_bound_fails_a_slower_equivalent_pattern_and_passes_an_equal_one():
    r"""Fix wave 26b (F-4): the per-pattern bound on its own, on every platform. `[^\w#@]+` (the module's `_NON_WORD`)
    rewritten as `(?:[^\w#@]|(?!))+` matches exactly the same text at up to ~3.9x the CPU (AEGIS's 3.9x slower
    `_NON_WORD`): it fails. The same pattern compiled as a distinct object (`(?:)` appended: same matches, same work)
    passes, which is the noise floor of the comparison."""
    base = re.compile(r"[^\w#@]+")
    slower = re.compile(r"(?:[^\w#@]|(?!))+")
    probe = "a!! b#@ \u200b\u200b c"
    assert [m.span() for m in slower.finditer(probe)] == [m.span() for m in base.finditer(probe)]
    rows = _slowdowns_vs_reviewed(slower, base)
    assert max(r[3] for r in rows) >= PIN_RATIO_MAX, rows
    rows = _slowdowns_vs_reviewed(re.compile(r"[^\w#@]+(?:)"), base)
    assert max(r[3] for r in rows) < PIN_RATIO_MAX, rows


def test_lim_text_scanners_are_linear_time():
    from shared import text

    phrases = ["guaranteed returns", "get rich", "risk free", "double your money"]
    for s in _adversarial_inputs(50_000):
        t0 = time.thread_time()  # fix wave 25 (scout A C3; R-HYGIENE L1): this thread's CPU time, not the wall clock
        for p in phrases:
            text.match_phrase(s, p)
        text.obfuscation_signals(s)
        text.non_latin_letters(s)
        text.mixed_symbol_words(s)
        text.unfolded_latin_letters(s)
        dt = time.thread_time() - t0
        assert dt < 2.0, (s[:10], dt)


# --- LOST sweep: server-assigned ids (brief, job, work, kit) ------------------------------

class LoseType(FakeLedgerClient):
    """Commit, then lose the response, for one event type once."""

    target: str = ""

    def record_event(self, **kw):
        super().record_event(**kw)
        if kw["event_type"] == self.target:
            self.target = ""
            raise LedgerRecordError("ledger response lost: ReadTimeout (test double: committed)")


def _one_record_per_subject(led, event_type):
    subs = [e["subject_id"] for e in led.of_type(event_type)]
    return len(subs) == len(set(subs))


def test_lost_sweep_brief_id_is_never_reused_for_other_content_and_retry_replays():
    led = LoseType()
    api = Api(ledger=led, clock=FixedClock(NOW))
    led.target = "brief_drafted"
    r = api.post("/zbm/briefs", {"requirements": zbm_requirements()})
    assert r.status_code == 503 and r.json()["took_effect"] == "unknown"
    # a different brief meanwhile must not take the id the ledger may already hold
    other = ok(api.post("/zbm/briefs", {"requirements": zbm_requirements(hook="Another hook.")}), 201)
    assert other["brief_id"] == "brief-0002"
    first = ok(api.post("/zbm/briefs", {"requirements": zbm_requirements()}), 201)
    assert first["brief_id"] == "brief-0001"
    assert _one_record_per_subject(led, "brief_drafted") and len(led.of_type("brief_drafted")) == 2
    assert sorted(api.zbm.briefs) == ["brief-0001", "brief-0002"]


def test_lost_sweep_job_work_and_kit_replay_once():
    led = LoseType()
    api = Api(ledger=led, clock=FixedClock(NOW))
    b = zbm_approved_brief(api)
    led.target = "production_opened"
    assert api.post(f"/zbm/briefs/{b['brief_id']}/jobs").status_code == 503
    job = ok(api.post(f"/zbm/briefs/{b['brief_id']}/jobs"), 201)
    assert len(led.of_type("production_opened")) == 1 and len(api.zbm.jobs) == 1
    led.target = "work_submitted"
    assert api.post(f"/zbm/jobs/{job['job_id']}/work", zbm_work()).status_code == 503
    w = ok(api.post(f"/zbm/jobs/{job['job_id']}/work", zbm_work()), 201)
    assert len(led.of_type("work_submitted")) == 1 and w["round"] == 1 and len(api.zbm.work) == 1

    led2 = LoseType()
    api2 = Api(ledger=led2, clock=FixedClock(NOW))
    from flows import zbc_live
    from samples import zbc_source

    zbc_live(api2)
    ok(api2.post(f"{C}/moment-map", zbc_source()))
    ok(api2.post(f"{C}/hook-sheets"))
    led2.target = "campaign_kit_built"
    assert api2.post(f"{C}/kit", zbc_kit_request()).status_code == 503
    kit = ok(api2.post(f"{C}/kit", zbc_kit_request()), 201)
    assert len(led2.of_type("campaign_kit_built")) == 1 and kit["kit_id"] == led2.of_type("campaign_kit_built")[0]["subject_id"]


def test_lim_the_structure_check_flags_nested_repeats_and_backreferences_only():
    """The structural half of the linearity check on its own: the textbook catastrophic patterns are flagged, plain
    character-class runs and bounded repeats are not."""
    for bad in (r"(a+)+$", r"(?:\w+\s?)*x", r"(.+)\1", r"(\w*)*"):
        assert _regex_structure(re.compile(bad)), bad
    for good in (r"[^\w#@]+", r"[\s/]+", r"(?:ab){2,5}c+", r"[#@][\w#@]*", r"(?:x|y)z+"):
        assert not _regex_structure(re.compile(good)), good
