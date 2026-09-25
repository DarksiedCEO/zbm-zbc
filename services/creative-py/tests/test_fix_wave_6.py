"""
Fix wave 6 (AEGIS round 5, Sep 24 2026) — reproductions written to FAIL on
the pre-fix code (integration 6881664) and pass after:

N1  never-say auto-passed readable respellings: stretched letters ("geeet
    riiich", 93/100), doubled letters in short phrases ("gget ricch",
    30/30), one lookalike plus a split into more tokens than the phrase
    has words ("ge t rl ch", "pas si ve inc orne", 47/84), "make r n oney",
    and the phrase's words with a filler between them ("make big money").
    Root cause: a 1-edit budget for short phrases and the 3-token window
    ceiling. Now `shared/text.visual_near_miss` compares the phrase with
    the LETTER STREAM of the caption (spaces removed; token splits are
    irrelevant), after the visual skeleton and a run collapse, under a
    budget of 1/2/3 edits for 4-6/7-10/11+ letters; an exact reading
    that starts and ends on word boundaries -> reject; the phrase's words
    in order within 2 intervening words -> human_review (adjacency policy,
    ADR 0005 decision 22).
N3  the false-positive rate was dominated by 4-letter entries ("cure" ->
    sure/pure/core/care/cute). Entries of <= 4 letters get budget 0
    (exact / skeleton reading only) unless the rulebook author opts in
    per entry ({"phrase": "cure", "fuzzy": true}); the Rulebook Writer and
    Campaign Rulebook warn on short entries. Rate measured on BOTH corpora.
N2  a 1 MiB junk body produced a 13.5 MiB 422 (FastAPI echoes `input` per
    error); 20 of them stalled /health for 36 s and cost two fast clients
    a 408. Now a bounded validation-error body (no input echo, first 20
    errors, capped `loc`, < 8 KiB), rendered off the event loop.
N5  after a CERTAINLY-not-recorded verdict (ledger shed 503, connection
    refused) a different verdict was refused for 15 minutes with a message
    saying the outcome "may be on the ledger". Certain means certain: a
    different verdict is allowed at once; the wording is fixed.

Ports: 20300-20319 (this wave's range for creative-py real-socket tests).
"""

from __future__ import annotations

import json
import os
import random
import socket
import string
import subprocess
import sys
import threading
import time
import zlib
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from conftest import NOW, TEST_SERVICE_TOKEN, Api
from flows import ok, zbc_open
from ordinary_captions import CAPTIONS, NEVER_SAY_FP_LIST
from samples import TODAY, zbc_clip, zbc_goal
from shared.clock import FixedClock
from shared.ledger import FakeLedgerClient

SRC = Path(__file__).resolve().parents[1] / "src"
TESTS = Path(__file__).resolve().parent
TAIL = " slowly. This budget myth. Listen on Pod Plus."

# The AEGIS round-5 independent corpus, VERBATIM from the review's
# probes/my_captions.py (117 captions, of which 6 say a phrase exactly and
# are excluded by the probe; 16-phrase list with "cure").
AEGIS_NEVER_SAY = [
    'guaranteed returns', 'get rich', 'make money', 'risk free',
    'double your money', 'no risk', 'miracle cure', 'passive income',
    'financial freedom', 'overnight success', 'lose weight fast', 'clinically proven',
    'doctor recommended', 'free money', 'get rich quick', 'cure',
]
AEGIS_CAPTIONS = [
    'Make sure you patch test before trying a new retinol #ad',
    'Pure hyaluronic acid, nothing else. Link in bio',
    'The cutest travel-size set just dropped',
    'Sunscreen every day. Yes, even in winter.',
    'My 3-step routine for dry winter skin',
    'We reformulated the cleanser — softer, same price',
    'Core collection back in stock this Friday',
    'Take care of your skin barrier first, everything else second',
    'Free shipping on orders over $40 this weekend',
    'Glass skin is a routine, not a product',
    'Our new lip balm is 100% vegan and smells like summer',
    'Real reviews from real customers, no filters',
    "Sure, you could skip leg day. Don't.",
    'Five core moves you can do on a hotel floor',
    'Your first pull-up: the 6-week plan that worked for me',
    'Rest days are training days too',
    'Low-impact cardio for bad knees',
    'How I lost the last 5 pounds without cutting carbs',
    'The stretch routine I do every night before bed',
    'Protein pancakes that actually taste like pancakes',
    "Weight loss is slow. That's the point.",
    'Get 20% off your first month with code MOVE',
    'Walking pad review: worth the desk space?',
    'Kettlebell basics, no gym required',
    'Rich, dark, and a little bitter — our new espresso blend',
    'Cure your Sunday scaries with a slow roast and a nap',
    'Make more of your leftovers with this one trick',
    'Pure maple syrup from a family farm in Vermont',
    'Cute little tarts, big lemon flavour',
    'The sourdough starter that survived my vacation',
    'Meal prep for people who hate meal prep',
    'No rush: the 6-hour ragu you can make on a Sunday',
    'Taste test: store-bought vs homemade hummus',
    'Air fryer wings, three ways',
    'A cure for boring lunches: the 10-minute grain bowl',
    'Iced matcha, lightly sweetened, made at home for $1',
    'Three days in Lisbon on a $500 budget',
    "The only carry-on I'll ever need #ad",
    'Make sure your passport has six months left before you book',
    'Safe travels start with a copy of your documents in the cloud',
    'Where to eat in Tokyo when you only have one night',
    'Our honest review of the overnight train to Vienna',
    'Points and miles for beginners, no jargon',
    'Road trip playlist: 4 hours, zero skips',
    'Travel insurance explained in one minute',
    'Free walking tours are the best way to see a city',
    'Battery lasted two full days in our test',
    'Is the new keyboard worth the upgrade? Short answer: maybe',
    "Cable management that doesn't cost a fortune",
    'The app that finally made me stick to a budget',
    'Get your files in order before the laptop dies',
    'Core i7 vs Ryzen 7 for video editing',
    "We tried the smart ring for a month. Here's the data.",
    'Noise-cancelling for under $100, honestly good',
    'Password manager setup in five minutes',
    'One charger for everything, finally',
    "Investing involves risk. Here's what that means in plain English.",
    'Building an emergency fund on a variable income',
    'The 1% rule for pay raises',
    'Money conversations to have before moving in together',
    'How compound interest works, with a real example',
    'Paying off debt: avalanche or snowball?',
    'Retirement math for people in their 20s',
    'For money reasons, we moved out of the city. No regrets.',
    'Index funds are boring. Boring is good.',
    'Rich dad, poor dad, or just a dad with a spreadsheet?',
    'Make money moves: the 15-minute weekly check-in',
    'Take the free budgeting template, link in bio',
    'There are no guarantees in investing — anyone who says otherwise is selling something',
    "We double checked every fee on this card so you don't have to",
    "Low-risk doesn't mean no-risk. Know the difference.",
    'Restock alert: the oat linen set is back',
    'Buy one, get one 50% off through Sunday',
    'Made to order, shipped in 3 days',
    'Made money from your closet? Our resale guide is live',
    'Sizing guide: measure twice, order once',
    'New colourways just landed',
    'Returns are free for 30 days, no questions asked',
    'Our packaging is now fully recyclable',
    'Last chance: sale ends at midnight',
    'Bundle and save on the full skincare set',
    'Loved by 40,000 customers (and their dogs)',
    'Handmade in small batches in Portland',
    'New episode: how a bakery went from one oven to three locations',
    'We asked a doctor about sleep. Her answer surprised us.',
    'Doctor recommended? We asked three of them.',
    'Behind the scenes of our first studio shoot',
    'Q&A Friday: send your questions by tonight',
    'Thanks for 100k! Giveaway details in the comments',
    'Recording day. Coffee count: four.',
    'The clip that got us 2M views, explained',
    "Our editor's secret weapon for fast cuts",
    'Full episode is up now, link in bio',
    'Sleep training week one: honest update',
    'Make sure the crib is away from the window and cords',
    'Clinically tested, fragrance-free, gentle enough for newborns',
    'The overnight oats my toddler will actually eat',
    'Pure joy: first day of school pictures',
    'Cure for cabin fever: a rainy-day scavenger hunt',
    'Declutter one drawer a day for a month',
    'Our living room makeover for under $300',
    'Cute and functional: the entryway bench we built',
    "Doctors recommend 150 minutes of movement a week. Here's how we fit it in.",
    'Take it slow: a gentle morning routine for anxious days',
    'The candle that made the whole house smell like fall',
    'Sign up today and get your first box free',
    "Limited edition. Once it's gone, it's gone.",
    'We read every comment. Keep them coming.',
    'Tag a friend who needs to see this',
    'Which one are you picking? Left or right?',
    'Proven by 3 years of daily use, not a lab',
    'Fast results? No. Real results? Yes.',
    'Success stories from our community this month',
    'Quick tip: hold the phone at eye level for better selfies',
    'Freedom to cancel any time, no contracts',
    'Secure checkout, always',
    'Lose the clutter, keep the memories: our photo scanning service',
]
assert len(AEGIS_CAPTIONS) == 117
PHR5 = ["guaranteed returns", "get rich", "make money", "miracle cure", "wealth secrets", "passive income",
        "risk free", "no risk", "free money", "financial freedom"]


def _rulebook(registry, phrases):
    from zbc import rulebook_writer
    from zbc.rulebook import RulebookStatus
    from zbc.rulebook_writer import CampaignGoal

    goal = CampaignGoal.model_validate(zbc_goal(never_say=phrases))
    return rulebook_writer.draft(goal, registry, TODAY).model_copy(update={"status": RulebookStatus.LIVE, "live_at": NOW})


def _review(registry, rb, text, i="x", field="caption"):
    from zbc import clip_review

    base = zbc_clip(f"w6_{i}")
    base[field] = text + TAIL + (" #ad" if field == "caption" else "")
    return clip_review.review(clip_review.ClipSubmission.model_validate(base), rb, registry, NOW)


# =====================================================================================
# N1 — readable respellings of never-say phrases
# =====================================================================================

# (text, expected outcome) — the AEGIS round-5 auto-passes (probes/ns_evade5.py)
AEGIS_N1 = [
    ("geeet riiich", "reject"),               # stretched letters (3+) read as the phrase
    ("riskk freeee", "human_review"),         # a doubled letter is never a reading (met/meet, of/off): a human's call
    ("no rrriisk", "human_review"),
    ("gget ricch", "human_review"),           # doubled letters: two edits in an 8-letter phrase (a human's call)
    ("rrisk ffree", "human_review"),
    ("ge t rl ch", "human_review"),           # l for i + split: the i/l fold never rejects
    ("rn ak em on ey", "reject"),             # rn for m + split into 5 tokens: reads as "make money"
    ("pas si ve inc orne", "reject"),
    ("gua ran teecl ret urns", "reject"),
    ("guaran teecl re turns", "reject"),
    ("vveal th sec rets", "reject"),
    ("make r n oney", "reject"),
    ("gett richh", "human_review"),
    ("get really rich", "human_review"),      # words in order, one filler
    ("get so rich", "human_review"),
    ("make some money", "human_review"),
    ("make good money", "human_review"),
    ("make big rnoney", "human_review"),      # filler + lookalike word
    ("make big money", "human_review"),
    ("guaranteed monthly returns", "human_review"),
    ("make rnoneyy", "human_review"),
]


@pytest.mark.parametrize("text,want", AEGIS_N1)
def test_n1_readable_respellings_never_auto_pass(registry, text, want):
    rb = _rulebook(registry, PHR5)
    for field in ("caption", "on_screen_text", "transcript"):
        d = _review(registry, rb, text, f"{zlib.crc32(text.encode())}{field[0]}", field)
        assert d.outcome == want, (field, text, d.outcome, d.broken_rules, d.human_review_reasons)


def test_n1_every_aegis_round5_candidate_is_caught(registry):
    """The full candidate list of probes/ns_evade5.py: zero automatic passes
    ("cure" is not on that probe's list and is the one legitimate pass)."""
    rb = _rulebook(registry, PHR5)
    cands = [
        "ge t rl ch", "ge t rlch", "get rl ch", "rna ke rno ney", "rn ak em on ey", "rnak e rnon ey", "ma ke rno ney",
        "gua ran teecl ret urns", "guaran teecl re turns", "rnira cle cu re", "vveal th sec rets", "pas sive inc orne",
        "get. rich.", "get (rich)", "get — rich", "get rich", "get\trich", "get rich!", "get... rlch",
        "get really rich", "get so rich", "make some money", "make good money", "make big rnoney", "make big money",
        "guaranteed 10% returns", "guaranteed monthly returns", "get rich or die tryin",
        "gett richh", "get riich", "geet rich", "gett rlch", "get richh now", "ge rich", "getr ich", "get ric h",
        "make m0ney", "rnake m0ney", "make rnoneyy", "rnake rnoneyy", "gvaranteed returns", "guaranteed retvrns",
        "guarante ed returns", "guaranteed retu rns", "guaranteed re-turns", "guaranteed re turns",
        "financial freeclorn", "financaial freeclorn", "financlal freedorn", "passlve lncome", "passive incorne",
        "passive lncorne", "rlsk free", "risk fre e", "no rlsk", "no-rlsk", "n0 risk", "free rnoney", "free monney",
        "wealth secretz", "vvealth secretz", "wea1th secrets", "vvea1th secrets", "vvealth 5ecrets",
        "make mo-ney", "make mo- ney", "make rno-ney", "guaran-teecl returns",
        "GET RlCH", "MAKE RNONEY", "M A K E rnoney", "make  rnoney",
        "make rn.oney", "make r.noney", "make r n oney", "guaranteec.l returns",
    ]
    passes = [t for i, t in enumerate(cands) if _review(registry, rb, t, f"c{i}").outcome == "pass"]
    assert passes == [], passes


LOOK = {"m": "rn", "d": "cl", "w": "vv"}


def _gen_lookalike_split(rng, p):
    sq = p.replace(" ", "")
    pos = [k for k, c in enumerate(sq) if c in LOOK]
    if not pos:
        return None
    k = rng.choice(pos)
    s = sq[:k] + LOOK[sq[k]] + sq[k + 1:]
    ntok = len(p.split()) + rng.choice([2, 3])
    if len(s) <= 2 * ntok:
        return None
    cuts = sorted(rng.sample(range(2, len(s) - 1), ntok - 1))
    if any(b - a < 2 for a, b in zip([0] + cuts, cuts + [len(s)])):
        return None
    return " ".join(s[a:b] for a, b in zip([0] + cuts, cuts + [len(s)]))


def _gen_stretched(rng, p):
    w = list(p)
    k = rng.choice([j for j, c in enumerate(w) if c.isalpha()])
    w[k] = w[k] * rng.choice([2, 3, 4])
    k2 = rng.choice([j for j, c in enumerate(w) if c.isalpha() and len(w[j]) == 1])
    w[k2] = w[k2] * 3
    return "".join(w)


def _gen_doubled(rng, p):
    w = list(p)
    for k in rng.sample([j for j, c in enumerate(w) if c.isalpha()], 2):
        w[k] = w[k] + w[k]
    return "".join(w)


def _gen_filler(rng, p):
    words = p.split()
    if len(words) < 2:
        return None
    fill = rng.choice([["so"], ["really"], ["big"], ["some", "good"], ["very", "very"], ["a", "lot", "of"][:2]])
    at = rng.randrange(1, len(words))
    return " ".join(words[:at] + fill + words[at:])


def _gen_split(rng, p):
    """Split into 2-letter-or-longer chunks (no single-letter runs), more tokens than words."""
    sq = p.replace(" ", "")
    ntok = len(p.split()) + rng.choice([1, 2, 3])
    if len(sq) <= 2 * ntok:
        return None
    for _ in range(10):
        cuts = sorted(rng.sample(range(2, len(sq) - 1), ntok - 1))
        if all(b - a >= 2 for a, b in zip([0] + cuts, cuts + [len(sq)])):
            return " ".join(sq[a:b] for a, b in zip([0] + cuts, cuts + [len(sq)]))
    return None


def test_n1_fuzz_stretched_doubled_split_filler_and_lookalike_split_never_auto_pass(registry):
    """The AEGIS round-5 generators (probes/ns_evade5b.py, classes A/B/C)
    plus filler and plain splits, across every phrase of both lists."""
    from zbc import clip_review

    phrases = sorted(set(PHR5 + [p for p in NEVER_SAY_FP_LIST if len(p.replace(" ", "")) > 4]))
    rb = _rulebook(registry, phrases)
    rng = random.Random(20260924_6)
    gens = [_gen_lookalike_split, _gen_stretched, _gen_doubled, _gen_filler, _gen_split]
    made = 0
    for i in range(1500):
        p = rng.choice(phrases)
        t = gens[i % len(gens)](rng, p)
        if t is None:
            continue
        if rng.random() < 0.3:
            t = t.title()
        field = rng.choice(["transcript", "caption", "on_screen_text"])
        base = zbc_clip(f"w6f_{i}")
        base[field] = f"{base[field]} {t} today"
        d = clip_review.review(clip_review.ClipSubmission.model_validate(base), rb, registry, NOW)
        assert d.outcome != "pass", (i, p, repr(t), field)
        made += 1
    assert made > 1000


def test_n1_round4_generators_still_never_auto_pass(registry):
    """"cure" (4 letters) is opted in here — under N3 a short entry that is
    NOT opted in is exact-only, by design (test_n3_short_entry_is_exact_only_unless_fuzzy)."""
    from test_fix_wave_5 import FUZZ_PHRASES, _mutate, _mutate_unicode_plus_edit
    from zbc import clip_review

    rb = _rulebook(registry, [{"phrase": p, "fuzzy": True} if len(p) <= 4 else p for p in FUZZ_PHRASES])
    rng = random.Random(20260924_66)
    for i in range(1500):
        phrase = rng.choice(FUZZ_PHRASES)
        mutated = (_mutate if i % 3 else _mutate_unicode_plus_edit)(rng, phrase)
        base = zbc_clip(f"w6r4_{i}")
        base["caption"] = f"{base['caption']} {mutated} today"
        d = clip_review.review(clip_review.ClipSubmission.model_validate(base), rb, registry, NOW)
        assert d.outcome != "pass", (i, phrase, repr(mutated))


def test_n1_exact_reading_rejects_but_a_typo_or_the_il_fold_only_holds(registry):
    """The reject rule is narrow: the window reads as the phrase once rn/m,
    cl/d, vv/w are read alike and stretched runs (3+) are collapsed. A
    doubled letter (met/meet, of/off are different words), a real edit or
    the i/l fold is a human's call."""
    rb = _rulebook(registry, PHR5)
    assert _review(registry, rb, "make r n oney", "a").outcome == "reject"
    assert _review(registry, rb, "makemoney", "b").outcome == "reject"
    assert _review(registry, rb, "geeet riiich", "c").outcome == "reject"
    assert _review(registry, rb, "gget ricch", "d").outcome == "human_review"
    assert _review(registry, rb, "get rlch", "e").outcome == "human_review"
    assert _review(registry, rb, "get rjch", "f").outcome == "human_review"
    # crossing a real word boundary mid-word is not a reading of the phrase
    assert _review(registry, rb, "the target rich environment", "g").outcome == "pass"
    assert _review(registry, rb, "your budget rich in detail", "h").outcome == "pass"


def test_n1_budget_tiers_and_short_entry_policy():
    from shared.text import visual_budget

    assert visual_budget(3) == 0
    assert visual_budget(4) == 0 and visual_budget(4, fuzzy=True) == 1  # N3: <= 4 letters opt in
    assert visual_budget(5) == 1 and visual_budget(6) == 1
    assert visual_budget(7) == 2 and visual_budget(10) == 2
    assert visual_budget(11) == 3 and visual_budget(40) == 3


def test_n1_stream_gate_names_the_window(registry):
    from shared.text import visual_near_miss

    d, reads, window = visual_near_miss("so pas si ve inc orne today", "passive income")
    assert d == 0 and reads and window == "pas si ve inc orne"
    d, reads, window = visual_near_miss("just geet rich now", "get rich")
    assert d == 0 and not reads and window == "geet rich"  # a doubled letter: distance 0 after the collapse, not a reading
    d, reads, window = visual_near_miss("just geeet rich now", "get rich")
    assert d == 0 and reads and window == "geeet rich"      # a stretched letter IS a reading
    assert visual_near_miss("target rich", "get rich") is None


def test_n1_adjacency_policy_in_order_within_two_words():
    from shared.text import near_miss, phrase_words_in_order

    assert phrase_words_in_order("you can make big money here", "make money") == "make big money"
    assert phrase_words_in_order("make so much money", "make money") == "make so much money"
    assert phrase_words_in_order("make a lot of money", "make money") is None  # three between: not adjacent
    assert phrase_words_in_order("money you make", "make money") is None  # out of order
    assert phrase_words_in_order("guaranteed monthly returns", "guaranteed returns") is not None
    assert phrase_words_in_order("make big rnoney", "make money") == "make big rnoney"  # lookalike word
    assert phrase_words_in_order("cure", "cure") is None  # single word: nothing to space out
    assert "in order" in near_miss("make big money", "make money")


def test_n1_stream_gate_is_bounded_on_100kb():
    """Documented bound: < 8 s per 100 KB for a 16-phrase list on the build
    machine, for ordinary AND adversarial text (the scan is bit-parallel
    and linear in the text regardless of its content: every phrase of a
    pack advances in the same pass)."""
    from shared.text import visual_near_misses

    rng = random.Random(7)
    inputs = [
        " ".join(rng.choice(CAPTIONS) for _ in range(2500))[:100_000],
        ("guaranteed retums get rjch make rnoney " * 3000)[:100_000],
        "a " * 50_000, "rn" * 50_000, "rn m " * 20_000, ("clclvvrn " * 12000)[:100_000],
        " ".join(rng.choice(["guaranteex", "returnsx", "getrich", "makemoney", "rnoney"]) for _ in range(20_000))[:100_000],
        " ".join("".join(rng.choice(string.ascii_lowercase) for _ in range(rng.randint(1, 9)))
                 for _ in range(20_000))[:100_000],
        " ".join("".join(rng.choice("rnclvuiae") for _ in range(rng.randint(1, 9))) for _ in range(20_000))[:100_000],
        "geeet riiich " * 8000, "ge t rl ch " * 10000, "make so money " * 7000,
    ]
    phrases = tuple((p, False) for p in NEVER_SAY_FP_LIST)
    worst = 0.0
    for s in inputs:
        t0 = time.perf_counter()
        visual_near_misses(s, phrases)
        dt = time.perf_counter() - t0
        worst = max(worst, dt)
        assert dt < 8.0, (s[:20], dt)
    print(f"\nN1 stream gate, worst input: {worst:.2f}s per 100 KB, {len(phrases)} phrases")


def test_n1_bit_parallel_scan_matches_the_reference_osa():
    """The packed Damerau (OSA) scan with restricted starts, against a plain
    DP: for every aligned end j and pattern, the scan reports a hit iff
    min over starts s of (0 if s is a token start else 1) + OSA(P, S[s:j])
    <= k. Random small alphabets so transpositions and pair boundaries are
    exercised."""
    from shared.text import _osa_within, _Pack

    def ref_osa(a, b):
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

    rng = random.Random(11)
    for trial in range(300):
        alpha = rng.choice(["ab", "abc", "abcr"])
        pats = []
        for _ in range(rng.randint(1, 4)):
            m = rng.randint(1, 7)
            pats.append(("".join(rng.choice(alpha) for _ in range(m)), rng.randint(0, 3)))
        n = rng.randint(1, 30)
        S = "".join(rng.choice(alpha) for _ in range(n))
        ST = bytes(1 if p == 0 or rng.random() < 0.3 else 0 for p in range(n + 1))
        EN = bytes(1 if p == n or rng.random() < 0.3 else 0 for p in range(n + 1))
        pack = _Pack(pats)
        got = {(j, i) for j, i in pack.scan(S, ST, EN)}
        for j in range(1, n + 1):
            if not EN[j]:
                continue
            for i, (p, k) in enumerate(pats):
                best = min((0 if ST[s] else 1) + ref_osa(p, S[s:j]) for s in range(0, j + 1))
                assert ((j, i) in got) == (best <= k), (trial, S, ST, EN, pats, j, i, best)
        # the exact confirmation used on a hit agrees with the reference too
        for p, k in pats:
            for s in range(n):
                want = ref_osa(p, S[s:])
                assert _osa_within(S[s:], p, k) == (want if want <= k else None)


# =====================================================================================
# N3 — short entries: budget 0 unless opted in; FP rate on both corpora
# =====================================================================================

def _fp_rate(captions, phrases, fuzzy=()):
    from shared.text import PhraseMatch, match_phrase, near_miss, visual_lookalike_exact, visual_near_misses

    spec = tuple((p, p in fuzzy) for p in phrases)
    ordinary = [c for c in captions if all(match_phrase(c, p) is PhraseMatch.NONE for p in phrases)]
    flagged = []
    for c in ordinary:
        res = visual_near_misses(c, spec)
        hits = [(p, res[p][0], res[p][2]) for p, _ in spec if res[p]]
        assert not any(visual_lookalike_exact(c, p, f) for p, f in spec), c  # never REJECTED
        nm = [p for p, f in spec if near_miss(c, p, f)]
        if hits or nm:
            flagged.append((c, hits, nm))
    return flagged, ordinary


def test_n3_false_positive_rate_on_the_aegis_corpus_with_cure_is_at_most_3_percent():
    flagged, ordinary = _fp_rate(AEGIS_CAPTIONS, AEGIS_NEVER_SAY)
    rate = len(flagged) / len(ordinary)
    print(f"\nN3 AEGIS corpus (with 'cure'): {len(flagged)}/{len(ordinary)} = {rate:.1%}")
    for c, h, nm in flagged:
        print("   ", repr(c), h, nm)
    assert rate <= 0.03, flagged


def test_n3_false_positive_rate_on_the_implementer_corpus_is_under_5_percent():
    flagged, ordinary = _fp_rate(CAPTIONS, NEVER_SAY_FP_LIST)
    rate = len(flagged) / len(ordinary)
    print(f"\nN3 implementer corpus (with 'cure'): {len(flagged)}/{len(ordinary)} = {rate:.1%}")
    for c, h, nm in flagged:
        print("   ", repr(c), h, nm)
    assert rate < 0.05, flagged


def test_n3_short_entry_is_exact_only_unless_fuzzy():
    from shared.text import near_miss, visual_near_miss

    for word in ("sure", "pure", "core", "care", "cute", "curl", "secure", "cured"):
        assert visual_near_miss(f"a {word} thing", "cure") is None, word
        assert near_miss(f"a {word} thing", "cure") is None, word
    assert visual_near_miss("a kure thing", "cure", fuzzy=True) is not None
    assert visual_near_miss("cu re", "cure") is not None          # a split is still the word
    assert visual_near_miss("ᴄure it", "cure") is not None         # lookalike letters fold first


def test_n3_rulebook_author_opts_a_short_entry_in_and_the_writer_warns(registry):
    from zbc import rulebook_writer
    from zbc.rulebook import RuleKind
    from zbc.rulebook_writer import CampaignGoal

    goal = CampaignGoal.model_validate(zbc_goal(never_say=["get rich", "cure", {"phrase": "scam", "fuzzy": True}]))
    rb = rulebook_writer.draft(goal, registry, TODAY)
    ns = {r.params["phrase"]: r.params for r in rb.rules_of(RuleKind.NEVER_SAY)}
    assert ns["get rich"] == {"phrase": "get rich"}
    assert ns["cure"] == {"phrase": "cure"}
    assert ns["scam"] == {"phrase": "scam", "fuzzy": True}
    assert any("cure" in w and "4 letters" in w for w in rb.warnings), rb.warnings
    assert any("scam" in w and "fuzzy" in w for w in rb.warnings), rb.warnings
    assert not rb.blocking_issues


def test_n3_campaign_rulebook_review_warns_on_short_entries_without_blocking(api):
    from flows import C

    goal = zbc_goal(never_say=["get rich", "cure"])
    rb = ok(api.post(f"{C}/rulebooks", {"actor_id": "zbc_rulebook_writer", "goal": goal}), 201)
    assert any("cure" in w for w in rb["warnings"]), rb
    d = ok(api.post(f"{C}/rulebooks/1/review", {"actor_id": "zbc_campaign_rulebook"}))
    assert d["status"] == "approved", d
    assert any("cure" in w and "4 letters" in w for w in d["review_warnings"]), d


def test_n3_fuzzy_entry_flows_through_clip_review(registry):
    rb = _rulebook(registry, ["cure", {"phrase": "scam", "fuzzy": True}])
    assert _review(registry, rb, "a pure delight", "p").outcome == "pass"
    assert _review(registry, rb, "a kure for that", "k").outcome == "pass"
    assert _review(registry, rb, "total skam", "s").outcome == "human_review"
    assert _review(registry, rb, "cu re it", "c").outcome == "reject"


# =====================================================================================
# N2 — 422 amplification
# =====================================================================================

PORTS = range(20300, 20320)
JUNK_1MIB = json.dumps({"caption": "x" * 1_000_000}).encode()          # 12 missing fields, each echoing the input
JUNK_60K_KEYS = json.dumps({f"k{i}": "x" for i in range(60_000)}).encode()  # 60k extra_forbidden errors


def _free_port() -> int:
    for port in PORTS:
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free port in 20300-20319")


def _start(extra_env: dict | None = None):
    port = _free_port()
    env = {**os.environ, "CREATIVE_SERVICE_TOKEN": TEST_SERVICE_TOKEN, "CREATIVE_PORT": str(port), **(extra_env or {})}
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


def _rss_kb(pid: int) -> int:
    for line in open(f"/proc/{pid}/status"):
        if line.startswith("VmRSS"):
            return int(line.split()[1])
    raise RuntimeError("no VmRSS")


@pytest.mark.parametrize("name,body", [("1 MiB junk", JUNK_1MIB), ("60k unknown keys", JUNK_60K_KEYS)], ids=["1mib", "60k"])
def test_n2_validation_error_body_is_bounded_and_never_echoes_input(api, name, body):
    assert len(body) <= 1024 * 1024
    t0 = time.perf_counter()
    r = api.client.post("/zbc/clips", content=body, headers={"Content-Type": "application/json"})
    dt = time.perf_counter() - t0
    assert r.status_code == 422, r.status_code
    assert len(r.content) < 8 * 1024, (name, len(r.content))
    assert b"xxxx" not in r.content and b"k59999" not in r.content, name  # no input echo
    d = r.json()
    assert d["error"] in ("RequestValidationError", "PayloadTooManyMembers"), d
    assert all("input" not in e for e in d.get("errors", []))
    print(f"\nN2 {name}: {len(r.content)} bytes, {dt * 1000:.0f} ms, {d.get('error_count')} error(s) counted")
    assert dt < 0.1, dt


def test_n2_unknown_keys_are_counted_not_enumerated(api):
    """Under the member cap the framework validates and every unknown key
    is an error: counted, first few named, never all listed. Over the cap
    the body is refused before the framework sees it."""
    body = json.dumps({**zbc_clip("x"), **{f"k{i}": "x" for i in range(3000)}}).encode()
    r = api.client.post("/zbc/clips", content=body, headers={"Content-Type": "application/json"})
    d = r.json()
    assert r.status_code == 422 and d["unknown_fields"]["count"] == 3000, d
    assert len(d["unknown_fields"]["first"]) <= 20 and d["truncated"] is True and len(r.content) < 8 * 1024
    r = api.client.post("/zbc/clips", content=JUNK_60K_KEYS, headers={"Content-Type": "application/json"})
    assert r.status_code == 422 and r.json()["error"] == "PayloadTooManyMembers" and len(r.content) < 1024


def test_n2_other_error_bodies_are_bounded(api):
    """Sweep: the CreativeError handler truncates reasons and issues; an
    unhandled exception is a fixed JSON 500 with no message."""
    from flows import C

    big = "r" * 200_000
    zbc_open(api)
    d = ok(api.post("/zbc/clips", zbc_clip("clip_n2", resolution_height_px=None)), 201)
    assert d["outcome"] == "human_review"
    r = api.post("/zbc/clips/clip_n2/human-review",
                 {"actor_id": "zbc_clip_human_reviewer", "outcome": "reject", "broken_rules": [{"rule_id": big, "reason": big}]})
    assert r.status_code == 422, r.status_code
    assert len(r.content) < 8 * 1024, len(r.content)
    r = api.client.post(f"{C}/rulebooks", content=b'{"goal": ' + b'[' * 3000 + b']' * 3000 + b'}',
                        headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and len(r.content) < 8 * 1024 and r.json()["error"] == "PayloadTooDeep", r.text
    r = api.client.post(f"{C}/rulebooks", content=b'{"goal": ' + b'[' * 40 + b']' * 40 + b'}',
                        headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["error"] == "PayloadTooDeep", r.text
    r = api.client.post(f"{C}/rulebooks", content=b'{"goal": {"angles": [' + b'"k",' * 5000 + b'"k"]}}',
                        headers={"Content-Type": "application/json"})
    assert r.status_code == 422 and r.json()["error"] == "PayloadTooManyMembers", r.text
    r = api.client.post("/zbc/clips", content=b'{"moment_ids": [' + b'"k",' * 4000 + b'"k"]}',
                        headers={"Content-Type": "application/json"})
    assert r.status_code == 422 and r.json()["error"] == "RequestValidationError" and len(r.content) < 8 * 1024, r.text


def _flood(port: int, body: bytes, nconc: int, reps: int, sample_health=True):
    codes: dict = {}
    sizes: list[int] = []
    health: list[float] = []
    stop = threading.Event()

    def worker():
        with httpx.Client(timeout=120) as c:
            for _ in range(reps):
                try:
                    r = c.post(f"http://127.0.0.1:{port}/zbc/clips", content=body,
                               headers={"Authorization": f"Bearer {TEST_SERVICE_TOKEN}", "Content-Type": "application/json"})
                    codes[r.status_code] = codes.get(r.status_code, 0) + 1
                    sizes.append(len(r.content))
                except Exception as e:  # noqa: BLE001
                    codes[type(e).__name__] = codes.get(type(e).__name__, 0) + 1

    def prober():
        with httpx.Client(timeout=30) as c:
            while not stop.is_set():
                t = time.perf_counter()
                try:
                    c.get(f"http://127.0.0.1:{port}/health")
                except Exception as e:  # noqa: BLE001
                    codes["health_" + type(e).__name__] = codes.get("health_" + type(e).__name__, 0) + 1
                health.append(time.perf_counter() - t)
                time.sleep(0.1)

    ths = [threading.Thread(target=worker) for _ in range(nconc)]
    pr = threading.Thread(target=prober)
    t0 = time.perf_counter()
    for t in ths:
        t.start()
    if sample_health:
        pr.start()
    for t in ths:
        t.join()
    stop.set()
    if sample_health:
        pr.join()
    return codes, sizes, sorted(health), time.perf_counter() - t0


@pytest.mark.parametrize("name,body", [("1 MiB junk", JUNK_1MIB), ("60k unknown keys", JUNK_60K_KEYS)], ids=["1mib", "60k"])
def test_n2_twenty_concurrent_junk_posts_keep_health_fast_and_legit_clients_served(server, name, body):
    """AEGIS round 5 (probes/cre_amp.py): 20 concurrent 1 MiB junk posts ->
    /health 36 s, RSS 308 MB, two fast clients got 408. Now: every junk
    post is a small 422, /health stays under 500 ms, a legitimate client
    is answered (never 408) while the flood runs."""
    proc, port = server
    legit: dict = {}

    def legit_client():
        with httpx.Client(timeout=60) as c:
            for i in range(10):
                try:
                    r = c.post(f"http://127.0.0.1:{port}/zbc/clips", json=zbc_clip(f"legit_{i}"),
                               headers={"Authorization": f"Bearer {TEST_SERVICE_TOKEN}"})
                    legit[r.status_code] = legit.get(r.status_code, 0) + 1
                except Exception as e:  # noqa: BLE001
                    legit[type(e).__name__] = legit.get(type(e).__name__, 0) + 1
                time.sleep(0.2)

    lt = threading.Thread(target=legit_client)
    lt.start()
    codes, sizes, health, elapsed = _flood(port, body, nconc=20, reps=3)
    lt.join()
    rss = _rss_kb(proc.pid) // 1024
    print(f"\nN2 {name} x20 conc x3: {codes} in {elapsed:.1f}s; max 422 body {max(sizes)} B; "
          f"/health p50={health[len(health) // 2] * 1000:.0f}ms max={health[-1] * 1000:.0f}ms; RSS {rss} MB; legit {legit}")
    assert codes == {422: 60}, codes
    assert max(sizes) < 8 * 1024
    assert health and health[-1] < 0.5, health[-1]
    assert 408 not in legit and sum(v for k, v in legit.items() if isinstance(k, int)) == 10, legit
    assert rss < 250, rss


def test_n2_single_junk_post_is_answered_in_under_100ms_on_the_socket(server):
    proc, port = server
    with httpx.Client(timeout=30) as c:
        for body in (JUNK_1MIB, JUNK_60K_KEYS):
            c.post(f"http://127.0.0.1:{port}/zbc/clips", content=body,
                   headers={"Authorization": f"Bearer {TEST_SERVICE_TOKEN}", "Content-Type": "application/json"})  # warm
            t0 = time.perf_counter()
            r = c.post(f"http://127.0.0.1:{port}/zbc/clips", content=body,
                       headers={"Authorization": f"Bearer {TEST_SERVICE_TOKEN}", "Content-Type": "application/json"})
            dt = time.perf_counter() - t0
            print(f"\nN2 single {len(body) // 1024} KB junk post: {r.status_code} {len(r.content)} B in {dt * 1000:.0f} ms")
            assert r.status_code == 422 and len(r.content) < 8 * 1024
            assert dt < 0.1, dt


# =====================================================================================
# N5 — a CERTAINLY-not-recorded verdict does not hold a different verdict
# =====================================================================================

REVIEWER = "zbc_clip_human_reviewer"
PASS = {"actor_id": REVIEWER, "outcome": "pass"}
REJECT = {"actor_id": REVIEWER, "outcome": "reject", "broken_rules": [{"rule_id": "QF-01", "reason": "low"}]}


def test_n5_certainly_unrecorded_verdict_allows_a_different_verdict_at_once():
    """Before: a different verdict was refused for RETRY_WINDOW (15 min) with
    "an outcome that may be on the ledger" — after a failure that was
    CERTAIN (took_effect false: connection refused, ledger shed 503)."""
    clock = FixedClock(NOW)
    led = FakeLedgerClient()
    api = Api(ledger=led, clock=clock)
    zbc_open(api)
    d = ok(api.post("/zbc/clips", zbc_clip("clip_n5", resolution_height_px=None)), 201)
    assert d["outcome"] == "human_review"
    led.fail_next = True
    r = api.post("/zbc/clips/clip_n5/human-review", PASS)
    assert r.status_code == 503 and r.json()["took_effect"] is False, r.text
    clock.at = NOW + timedelta(seconds=10)
    d = ok(api.post("/zbc/clips/clip_n5/human-review", REJECT))  # no hold: the ledger certainly has nothing
    assert d["outcome"] == "reject" and d["decided_at"].startswith(clock.now().isoformat()[:19])
    assert len(led.of_type("clip_human_reviewed")) == 1
    assert led.of_type("clip_human_reviewed")[0]["payload"]["decision"]["outcome"] == "reject"
    # the earlier PASS can no longer land: the clip is decided
    assert api.post("/zbc/clips/clip_n5/human-review", PASS).status_code == 409


def test_n5_identical_retry_of_a_certain_failure_still_reuses_its_time():
    clock = FixedClock(NOW)
    led = FakeLedgerClient()
    api = Api(ledger=led, clock=clock)
    zbc_open(api)
    ok(api.post("/zbc/clips", zbc_clip("clip_n5b", resolution_height_px=None)), 201)
    led.fail_next = True
    assert api.post("/zbc/clips/clip_n5b/human-review", PASS).status_code == 503
    clock.at = NOW + timedelta(minutes=5)
    d = ok(api.post("/zbc/clips/clip_n5b/human-review", PASS))
    assert d["decided_at"].startswith(NOW.isoformat()[:19])  # the first attempt's time (fix wave 2, N1)


def test_n5_uncertain_verdict_refusal_wording_is_accurate():
    from test_fix_wave_5 import LoseBeforeCommit, _uncertain_verdict

    led = LoseBeforeCommit()
    api, clock = _uncertain_verdict(led)
    r = api.post("/zbc/clips/clip_w/human-review", REJECT)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "outcome is unknown" in detail and "withdraw" in detail and "wait" not in detail, detail
