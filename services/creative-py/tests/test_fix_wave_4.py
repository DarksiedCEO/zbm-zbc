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
# (Linux: this box and the ubuntu CI runners); elsewhere it is printed, and "linear" is the growth bound above. A
# pattern 3x slower by a constant factor is therefore caught on Linux only (on other platforms only if it crosses
# the 50 ms CPU bound).
LINEAR_REF = re.compile(r"[^\w#@]+")
REGEX_RATIO_MAX = 9.0
REGEX_RATIO_CALIBRATED = sys.platform.startswith("linux")


def test_lim_every_regex_in_text_module_is_linear_time():
    import shared.text as text

    patterns = [v for v in vars(text).values() if isinstance(v, re.Pattern)]
    assert len(patterns) >= 2
    worst, worst_ratio, at, worst_growth, grew = 0.0, 0.0, None, 0.0, None
    for p in patterns:
        for small, large, s in zip(_adversarial_inputs(GROWTH_SMALL), _adversarial_inputs(GROWTH_LARGE),
                                   _adversarial_inputs(ABS_N)):
            t_small, t_large, growth = regex_same_work(p, small, large)
            dt = regex_cpu(p, s)
            ref = regex_cpu(LINEAR_REF, s)
            worst = max(worst, dt)
            if growth > worst_growth:
                worst_growth, grew = growth, (p.pattern, s[:10])
            if dt / ref > worst_ratio:
                worst_ratio, at = dt / ref, (p.pattern, s[:10])
            assert growth < GROWTH_RATIO_MAX, (p.pattern, s[:10], t_small, t_large, growth)
            assert dt < 0.05, (p.pattern, s[:10], dt)
            if REGEX_RATIO_CALIBRATED:
                assert dt / ref < REGEX_RATIO_MAX, (p.pattern, s[:10], dt, ref)
    print(f"\nLIM regex: worst CPU {worst * 1000:.1f} ms per {ABS_N} chars (bound 50 ms); worst same-work growth "
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
