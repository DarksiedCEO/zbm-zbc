"""
Fix wave 10 — AEGIS round 9 (review9/probes, logs).

N9-1 (blocker) Exact never-say phrases written in currency / math-symbol
     "fancy text" (₥₳₭€ ₥⊙₦€¥) passed clean: those code points were in no
     letter map, and the stripped-share fail-safe ignored Sc/Sm/Sk.
     Now (a) a curated, glyph-checked table of currency / math lookalikes
     is part of the SKELETON symbol reading (a never-say phrase read through
     it -> human_review; since fix wave 11 an EXACT reading is a reject),
     and (b) a word made mostly of Sc/Sm/Sk (+ the table) symbols counts
     toward the fail-safe (since fix wave 11: Rule A, "unreadable symbols");
     prices, percentages and math stay clean.
N9-2 (blocker, wave-9 regression) Regional indicators were read as letters
     only when some run was not exactly two long, so pair-spaced phrases
     (🇲🇦 🇰🇪 🇨🇦 🇸🇭) passed and a genuine row of flags went to a human.
     Now regional indicators are never a signal by themselves; they are
     always ALSO read as letters for phrase matching, and a never-say hit
     under that reading is a human's call (never a reject: it can be flags).
N9-7 Braille-styled phrases glued to a long word passed (the long word
     diluted the fail-safe's window). Now the grade-1 Braille letters are
     letter-like (read, and a signal), U+2800 is a space, and the fail-safe
     also judges any run of STRIPPED_MIN consecutive stripped characters.
N9-5 False positives: an enclosed-letter emoji with VS16 standing alone
     (🅿️, Ⓜ️, 🅰️🅱️🅾️), hashtags with digits (#5k), and the England /
     Scotland / Wales subdivision flags.
N9-3 The never-say cost claim in ADR 0005 was ~30x low; a hard cap on
     never-say phrases per rulebook (approval refuses above it, 422).
N9-4 Two concurrent requests with one idempotency key each ran the review.
"""

from __future__ import annotations

import itertools
import threading
import time
import unicodedata

import pytest
from aegis8_corpus import CAPTIONS as CORPUS8
from aegis8_corpus import NEVER_SAY_A, NEVER_SAY_B
from conftest import NOW
from samples import TODAY, zbc_clip, zbc_goal

NS_C = ["work from home", "earn cash fast", "no credit check", "cash prize", "win big", "secret method",
        "lose ten pounds", "guaranteed approval"]
NS9 = NEVER_SAY_A + NEVER_SAY_B + NS_C  # the round-9 probe's lists A + B + C
TAIL = " This budget myth. Listen on Pod Plus. #ad"
PAD = "Honest review of the budgeting app we used for three months, what worked and what did not for our family"
_n = itertools.count()


def _rulebook(registry, phrases=NS9):
    from zbc import rulebook_writer
    from zbc.rulebook import RulebookStatus
    from zbc.rulebook_writer import CampaignGoal

    goal = CampaignGoal.model_validate(zbc_goal(never_say=list(phrases)))
    return rulebook_writer.draft(goal, registry, TODAY).model_copy(update={"status": RulebookStatus.LIVE, "live_at": NOW})


def _run(registry, rb, fields: dict):
    """The AEGIS round-9 probe's `run()` (ns_evade9.py)."""
    from zbc import clip_review

    base = zbc_clip(f"w10_{next(_n)}")
    for k, v in fields.items():
        base[k] = v + (TAIL if k == "caption" else "")
    if "caption" not in fields:
        base["caption"] = "hello" + TAIL
    return clip_review.review(clip_review.ClipSubmission.model_validate(base), rb, registry, NOW)


def _ns_reason(d, phrase: str) -> bool:
    return any("never-say" in r and repr(phrase) in r for r in d.human_review_reasons)


# =====================================================================================
# N9-1 — currency / math-symbol letter styles
# =====================================================================================

# The round-9 probe's cases (ns_evade9.txt, H-currency-pure, H-one-word-currency-pure, S-pad-currency-pure).
N91_CASES = [("₥₳₭€ ₥⊙₦€¥", "make money"), ("₫€฿₮ ₣®€€", "debt free"), ("₩⊙®₭ ₣®⊙₥ ♄⊙₥€", "work from home"),
             ("₦⊙ ¢®€₫¡₮ ¢♄€¢₭", "no credit check"), ("฿∪®₦ ₣₳₮", "burn fat"), ("⊕∪¡₮ ¥⊙∪® ⌡⊙฿", "quit your job"),
             ("₥₳₭€ money", "make money"), (PAD + " ₥₳₭€ ₥⊙₦€¥", "make money"), (PAD + " ₫€฿₮ ₣®€€", "debt free")]

# The currency / math lookalikes this wave reads as letters, written down INDEPENDENTLY of the
# implementation (each checked against its Unicode name / glyph): symbol -> the letter(s) it may be.
CURRENCY_MATH = {
    "₥": "m", "₳": "a", "₭": "k", "€": "e", "⊙": "o", "₦": "n", "¥": "y", "₩": "w", "®": "r", "₣": "f",
    "♄": "h", "₫": "d", "฿": "b", "₮": "t", "¢": "c", "¡": "i", "∪": "u", "⊕": "q", "⌡": "j", "₤": "l",
    "₱": "p", "§": "s", "₴": "s", "₲": "g", "₵": "c", "₡": "c", "₢": "c", "₺": "t", "₸": "t", "₹": "r",
    "₽": "p", "₿": "b", "∩": "n", "∈": "e", "∀": "a", "∂": "d", "√": "v", "⨯": "x",
}

# Ordinary prices, percentages, math and currency text: must stay a clean pass.
PRICES = [
    "€5 off, $10, 50%", "Prices in $/€/£", "Save $$$ today", "₹499 only this week", "£3.50 flat white ☕",
    "¥1200 ramen in Shibuya", "Tickets ₩15,000 at the door", "Rent ₱8,000/month", "±5% tolerance on every cut",
    "Scores ≥ 90 get a sticker", "∞ possibilities", "Only 3€ per coffee", "20% off everything 🎉",
    "Price drop: $49.99 → $39.99", "€€€ saved with this one trick", "A ∪ B homework help", "Coffee ☕ + croissant 🥐 = $6",
    "We take ₿ and cards", "Bundle: 3 × $12 = $36", "Pay €12,50 or £11", "© 2026 BrandCo ® all rights reserved",
    "Ages 5–12 · €8 per class", "Delivery in 3–5 days · €4.99", "Up 12% ↑ since June",
]


def test_n91_round9_currency_cases_go_to_a_human(registry):
    """(a)+(b): the round-9 MISSes are never a pass. Fix wave 11 (design ruling: a reading table only
    UPGRADES a human's call to a rejection): each of these reads EXACTLY as its phrase through the table,
    so it is now a rejection citing the currency / math reading (wave 10 asserted human_review here)."""
    rb = _rulebook(registry)
    out = []
    for text, phrase in N91_CASES:
        d = _run(registry, rb, {"caption": text})
        out.append((text, d.outcome, any("currency / math" in b.reason and repr(phrase) in b.reason
                                         for b in d.broken_rules), d.human_review_reasons[:2]))
    bad = [o for o in out if o[1] != "reject" or not o[2]]
    print(f"\nN9-1 round-9 currency cases: {len(out)}, not rejected through the currency / math reading: {len(bad)}")
    for o in bad:
        print("   ", o)
    assert not bad


def test_n91_a_every_table_symbol_is_read_as_its_letter():
    """(a): every symbol of the table is a SKELETON reading of its letter, and a phrase written
    entirely in them is a near miss of it."""
    from shared.text import SKELETON, near_miss

    missing = [(s, L) for s, L in CURRENCY_MATH.items() if L not in SKELETON.get(s, "")]
    assert not missing, missing
    by_letter: dict[str, list[str]] = {}
    for s, L in CURRENCY_MATH.items():
        by_letter.setdefault(L, []).append(s)
    for s, L in CURRENCY_MATH.items():
        # the letter's word in a two-word phrase, the symbol standing for it; every other letter in symbols too
        phrase = {"q": "quit job", "x": "tax cut", "j": "quit job", "v": "save cash"}.get(L, f"{L}ard work")
        written = " ".join("".join((s if c == L else by_letter.get(c, [c])[0]) for c in w) for w in phrase.split())
        assert near_miss(written, phrase) is not None, (s, L, written, phrase)


def test_n91_b_a_symbol_word_counts_toward_the_failsafe_a_price_does_not():
    """Fix wave 11: the fail-safe for a symbol-lettered word is Rule A ("unreadable symbols",
    `unreadable_words`), which replaced wave 10's count of such words in `stripped_share` (this test
    asserted stripped_share > limit for them)."""
    from shared.text import STRIPPED_SHARE_LIMIT, obfuscation_signals, stripped_share, unreadable_words

    for t in ["₥₳₭€", "₥₳₭€ money", "Big news ₫€฿₮ ₣®€€ today", PAD + " ₥₳₭€ ₥⊙₦€¥", "₦⊙ ¢®€₫¡₮ ¢♄€¢₭"]:
        assert unreadable_words(t), t
        assert any(s.startswith("unreadable symbols") for s in obfuscation_signals(t)), t
    for t in PRICES:
        assert stripped_share(t) <= STRIPPED_SHARE_LIMIT, (t, stripped_share(t))
        assert not unreadable_words(t), (t, unreadable_words(t))


def test_n91_sweep_every_phrase_in_currency_style_is_never_a_pass(registry):
    """Class sweep: every never-say phrase of lists A+B+C with EVERY letter the table has written as
    one of its symbols (round-robin, so every table entry is used), in the caption and the bio."""
    phrases = NS9 + ["tax refund"]  # lists A+B+C have no 'x'
    rb = _rulebook(registry, phrases)
    by_letter: dict[str, list[str]] = {}
    for s, L in CURRENCY_MATH.items():
        by_letter.setdefault(L, []).append(s)
    turn = {L: itertools.cycle(v) for L, v in by_letter.items()}
    used, passes, n = set(), [], 0
    for p in phrases:
        for field in ("caption", "account_bio"):
            chars = []
            for c in p:
                if c in turn:
                    s = next(turn[c])
                    used.add(s)
                    chars.append(s)
                else:
                    chars.append(c)
            t = "".join(chars)
            n += 1
            d = _run(registry, rb, {field: t})
            if d.outcome == "pass":
                passes.append((t, field))
    print(f"\nN9-1 sweep: {n} cases, automatic passes {len(passes)}; table symbols used {len(used)}/{len(CURRENCY_MATH)}")
    assert not passes, passes[:10]
    assert used == set(CURRENCY_MATH), set(CURRENCY_MATH) - used


def test_n91_prices_percentages_math_stay_clean(registry):
    rb = _rulebook(registry)
    flagged = []
    for t in PRICES:
        d = _run(registry, rb, {"caption": t})
        if d.outcome != "pass":
            flagged.append((t, d.outcome, d.human_review_reasons[:2]))
    print(f"\nN9-1 price / math guard: flagged {len(flagged)}/{len(PRICES)}")
    assert not flagged, flagged


# =====================================================================================
# N9-2 — regional indicators: a reading, never a signal by themselves
# =====================================================================================

def _ri(s: str) -> str:
    return "".join(chr(0x1F1E6 + ord(c) - 97) if "a" <= c <= "z" else c for c in s)


N92_PAIRS = [("🇲🇦 🇰🇪 🇨🇦 🇸🇭", "make cash"), ("🇼🇮 🇳🇧 🇮🇬", "win big"), ("🇷🇮 🇸🇰 🇫🇷 🇪🇪", "risk free"),
             ("🇪🇦 🇸🇾 🇨🇦 🇸🇭", "easy cash"), ("🇩🇪 🇧🇹 🇫🇷 🇪🇪", "debt free")]


def test_n92_round9_flag_pair_phrases_go_to_a_human(registry):
    rb = _rulebook(registry, NS9 + ["make cash", "easy cash"])
    out = []
    for text, phrase in N92_PAIRS:
        d = _run(registry, rb, {"caption": text})
        out.append((text, d.outcome, _ns_reason(d, phrase), d.human_review_reasons[:2]))
    bad = [o for o in out if o[1] != "human_review" or not o[2]]
    print(f"\nN9-2 round-9 flag-pair cases: {len(out)}, not human_review with a never-say reason: {len(bad)}")
    for o in bad:
        print("   ", o)
    assert not bad


def test_n92_any_regional_hit_is_human_review_never_reject(registry):
    """Glued runs, pair-spaced runs, single indicators beside letters, zero-width joins: a never-say
    reading is a human's call (it could be flags), whatever the run lengths."""
    rb = _rulebook(registry)
    out = []
    for p in ("make money", "get rich", "guaranteed returns", "burn fat", "debt free", "risk free"):
        squashed = p.replace(" ", "")
        pairs = " ".join(squashed[i:i + 2] for i in range(0, len(squashed), 2))
        for t in (_ri(p), _ri(squashed), _ri(pairs), " ".join(_ri(w[0]) + w[1:] for w in p.split()),
                  "​".join(_ri(p))):
            d = _run(registry, rb, {"caption": t})
            out.append((t, p, d.outcome, _ns_reason(d, p)))
    bad = [o for o in out if o[2] != "human_review" or not o[3]]
    print(f"\nN9-2 regional readings: {len(out)} cases, not human_review with a never-say reason: {len(bad)}")
    assert not bad, bad[:10]


def test_n92_real_flags_are_not_a_signal(registry):
    from shared.text import letterlike_chars, obfuscation_signals

    rb = _rulebook(registry)
    for t in ["Team 🇧🇷🇯🇵🇰🇪🇨🇦 watch party at the pub", "Made in 🇬🇧, shipped worldwide", "Proudly 🇺🇸 owned",
              "🇯🇵🇰🇷🇹🇼 food tour", "Euro trip 🇫🇷 🇩🇪 🇮🇹 🇪🇸", "🇭🇪🇱🇱🇴 🇫🇷🇮🇪🇳🇩🇸"]:
        assert letterlike_chars(t) == [], t
        assert not any("letter-like" in s for s in obfuscation_signals(t)), t
        d = _run(registry, rb, {"caption": t})
        assert d.outcome == "pass", (t, d.outcome, d.human_review_reasons)


# =====================================================================================
# N9-7 — Braille
# =====================================================================================

BRAILLE = {c: chr(0x2800 + v) for c, v in zip(
    "abcdefghijklmnopqrstuvwxyz", [1, 3, 9, 25, 17, 11, 27, 19, 10, 26, 5, 7, 13, 29, 21, 15, 31, 23, 14, 30, 37, 39,
                                   58, 45, 61, 53])}


def _braille(s: str, space: str = " ") -> str:
    return "".join(BRAILLE.get(c, space if c == " " else c) for c in s)


def test_n97_round9_glued_and_interleaved_braille_is_never_a_pass(registry):
    rb = _rulebook(registry)
    out = []
    for p in ("make money", "get rich", "risk free", "debt free", "easy money", "guaranteed returns"):
        s = _braille(p)
        for t in (s.replace(" ", "⠀") + "Supercalifragilisticexpialidocious " + PAD,
                  " ".join(f"{x}ordinaryword" for x in s.split()) + " " + PAD):
            d = _run(registry, rb, {"caption": t})
            out.append((t[:40], d.outcome))
    bad = [o for o in out if o[1] == "pass"]
    print(f"\nN9-7 glued / interleaved Braille: {len(out)} cases, passes {len(bad)}")
    assert not bad, bad


def test_n97_braille_letters_are_read(registry):
    from shared.text import canonical, letterlike_chars

    assert canonical(_braille("hello world", "⠀")) == "hello world"
    assert letterlike_chars(_braille("hello")), "Braille letters are styled letters (a signal)"
    rb = _rulebook(registry)
    for t in (_braille("make money"), _braille("make money", "⠀"), PAD + " " + _braille("get rich")):
        d = _run(registry, rb, {"caption": t})
        assert d.outcome == "reject", (t, d.outcome, d.human_review_reasons[:2])


def test_n97_a_run_of_stripped_characters_is_judged_on_its_own():
    from shared.text import STRIPPED_SHARE_LIMIT, stripped_share

    for t in ["𝌀𝌁𝌂Supercalifragilisticexpialidocious " + PAD, "⡍⡁⡅⡑ordinaryword ⡍⡕⡝⡑⡽ordinaryword " + PAD]:
        assert stripped_share(t) > STRIPPED_SHARE_LIMIT, (t, stripped_share(t))


def test_n97_braille_blank_used_as_a_spacer_is_not_a_signal(registry):
    """U+2800 is what creators paste for blank caption lines."""
    rb = _rulebook(registry)
    for t in ["Line one\n⠀\nLine two", "New menu\n⠀\n⠀\nsee you Friday"]:
        d = _run(registry, rb, {"caption": t})
        assert d.outcome == "pass", (t, d.human_review_reasons)


# =====================================================================================
# N9-5 — false positives on the round-9 corpus
# =====================================================================================

ENGLAND = "\U0001F3F4" + "".join(chr(0xE0000 + ord(c)) for c in "gbeng") + "\U000E007F"
SCOTLAND = "\U0001F3F4" + "".join(chr(0xE0000 + ord(c)) for c in "gbsct") + "\U000E007F"
WALES = "\U0001F3F4" + "".join(chr(0xE0000 + ord(c)) for c in "gbwls") + "\U000E007F"
N95_ORDINARY = [
    "🅿️ Free parking behind the shop, follow the signs", "Ⓜ️ Two stops from the metro, easy to find",
    "Blood drive Saturday 🅰️🅱️🅾️ all types needed", "Morning run done #runnersofinstagram #5k #mondaymotivation",
    "#streetphotography #35mm #filmisnotdead", "#travel #lisbon #tram28 #portugal",
    f"Go {ENGLAND} England! match day at the bar", f"Scotland trip {SCOTLAND} highlands and castles",
    f"Six Nations {WALES} Cymru am byth", "Type 🅾️ donors wanted", "#2024recap #top10 #covid19",
]


def test_n95_round9_false_positives_pass(registry):
    rb = _rulebook(registry)
    flagged = []
    for t in N95_ORDINARY:
        d = _run(registry, rb, {"caption": t})
        if d.outcome != "pass":
            flagged.append((t, d.human_review_reasons[:2]))
    print(f"\nN9-5 round-9 FP captions: flagged {len(flagged)}/{len(N95_ORDINARY)}")
    for f in flagged:
        print("   ", f)
    assert not flagged


def test_n95_the_exemptions_do_not_open_a_hole(registry):
    rb = _rulebook(registry)
    tag = lambda s: "".join(chr(0xE0000 + ord(c)) for c in s)
    vs = lambda s: "".join(chr(0x1F170 + ord(c) - 97) + "️" if c.isalpha() else c for c in s)
    cases = [
        "hello " + tag("make money"),                      # tag letters carrying text
        "\U0001F3F4" + tag("usca") + "\U000E007F Cali",    # a tag sequence that is not one of the three
        "\U0001F3F4" + tag("gbeng"),                        # no cancel tag
        f"Go {ENGLAND}{tag('x')} England",                  # a valid flag followed by more tag letters
        vs("make money"), vs("get rich"),                    # emoji-presentation letters spelling a phrase
        "#g3t #r1ch", "#m4ke #m0ney", "#getrich",            # hashtags carrying a phrase
        "東京の朝ごはん 🍙 onigiri for breakfast",              # non-Latin routing unchanged
    ]
    out = [(t, _run(registry, rb, {"caption": t})) for t in cases]
    passes = [(t, d.outcome) for t, d in out if d.outcome == "pass"]
    assert not passes, passes
    # the tag cases still name the tag characters
    for t, d in out[:4]:
        assert any("tag character" in r for r in d.human_review_reasons), (t, d.human_review_reasons)


# =====================================================================================
# N9-3 — never-say cap
# =====================================================================================

def test_n93_rulebook_approval_refuses_more_never_say_phrases_than_the_cap(api):
    from flows import C, zbc_rights_on_file
    from zbc.rulebook_writer import MAX_NEVER_SAY

    zbc_rights_on_file(api)
    phrases = [f"never phrase {i:04d}" for i in range(MAX_NEVER_SAY + 1)]
    rb = api.post(f"{C}/rulebooks", {"actor_id": "zbc_rulebook_writer", "goal": zbc_goal(never_say=phrases)})
    assert rb.status_code == 201, rb.text
    assert any(str(MAX_NEVER_SAY) in w for w in rb.json()["warnings"]), rb.json()["warnings"]
    r = api.post(f"{C}/rulebooks/{rb.json()['version']}/review", {"actor_id": "zbc_campaign_rulebook"})
    assert r.status_code == 422, (r.status_code, r.text)
    assert str(MAX_NEVER_SAY) in r.json()["detail"] and str(MAX_NEVER_SAY + 1) in r.json()["detail"], r.json()
    # nothing was truncated: the draft still holds every phrase, still a draft
    got = api.get(f"{C}/rulebooks/{rb.json()['version']}").json()
    assert got["status"] == "draft"
    assert sum(1 for x in got["rules"] if x["kind"] == "never_say") == MAX_NEVER_SAY + 1


def test_n93_approval_at_the_cap_is_allowed(api):
    from flows import C, zbc_rights_on_file
    from zbc.rulebook_writer import MAX_NEVER_SAY

    zbc_rights_on_file(api)
    # fix wave 11 (N10-4): the list must also fit MAX_NEVER_SAY_CHARS in total (wave 10 used 30 phrases of
    # 17 characters, 510 in all, which is now over that budget)
    phrases = [f"never {i:04d}" for i in range(MAX_NEVER_SAY)]
    rb = api.post(f"{C}/rulebooks", {"actor_id": "zbc_rulebook_writer", "goal": zbc_goal(never_say=phrases)})
    assert rb.status_code == 201, rb.text
    r = api.post(f"{C}/rulebooks/{rb.json()['version']}/review", {"actor_id": "zbc_campaign_rulebook"})
    assert r.status_code == 200 and r.json()["status"] == "approved", (r.status_code, r.text[:500])


def test_n93_review_cost_at_the_cap_on_the_aegis_generator(registry):
    """probes/ns_cost8b.py's generator (seed 81) at MAX_NEVER_SAY phrases, every field at its maximum,
    every cache cold, automatic and routed to a human (no rejection cuts the signals short), and with a
    regional indicator in every field (the regional-indicator reading, N9-2, then runs as well): one
    review stays under 2 s of CPU on the reference host (scaled by the machine's measured slowdown)."""
    from test_fix_wave_9 import _cold, _cpu_slowdown
    from zbc import clip_review
    from zbc.rulebook_writer import MAX_NEVER_SAY

    phrases, cases = _cost_cases(MAX_NEVER_SAY)
    rb = _rulebook(registry, phrases)
    slow = _cpu_slowdown()
    worst = 0.0
    ri = "\U0001F1E6 "
    for label, over in cases:
        for flagged in (False, True):
            fields = {k: (ri + v[len(ri):] if flagged else v) for k, v in over.items()}  # the end (disclosure) kept
            sub = clip_review.ClipSubmission.model_validate(zbc_clip(f"c{next(_n)}", **fields))
            for route in ((), ("forced",)):
                _cold()
                c0 = time.thread_time()
                d = clip_review.review(sub, rb, registry, NOW, route_to_human=route)
                cpu = time.thread_time() - c0
                worst = max(worst, cpu)
                print(f"\nN9-3 never_say={MAX_NEVER_SAY} {label:26s} regional={flagged!s:5s} routed={bool(route)!s:5s} "
                      f"{cpu * 1000:6.0f} ms CPU -> {d.outcome}")
    slow = max(slow, _cpu_slowdown())
    print(f"N9-3 worst {worst * 1000:.0f} ms CPU; machine slowdown {slow:.2f}x")
    assert worst <= 2.0 * slow, (worst, slow)


def _cost_cases(nphr: int):
    """The generator of probes/ns_cost8b.py (seed 81: syllable words, the same six mutations, the same three
    texts), drawn fresh for `nphr` phrases (the probe draws its 100-phrase set before its 1,000-phrase one,
    so the exact strings differ from the probe's; the construction is the same)."""
    import random

    rng = random.Random(81)
    syll = ["ka", "ro", "mi", "ne", "tu", "sa", "lo", "vi", "de", "pa", "ri", "go", "fe", "zu", "ba", "no"]

    def word():
        return "".join(rng.choice(syll) for _ in range(rng.randint(2, 4)))

    def mutate(w):
        k = rng.randrange(6)
        if k == 0:
            return "".join(c for c in w if c not in "aeiou") or w
        if k == 1:
            return w.replace("m", "rn").replace("i", "l").replace("o", "0")
        if k == 2:
            return w[:-1] + rng.choice("$@1!💰")
        if k == 3:
            return w[0] + w[0] + w[1:]
        if k == 4:
            return w[:len(w) // 2] + " " + w[len(w) // 2:]
        return w + rng.choice(["💰", "📈", "🔥"])

    phrases = list(dict.fromkeys(f"{word()} {word()}" + (f" {word()}" if rng.random() < .3 else "")
                                 for _ in range(nphr * 2)))[:nphr]
    pw = [w for p in phrases for w in p.split()]

    def text(n):
        out, L = [], 0
        while L < n:
            t = mutate(rng.choice(pw))
            out.append(t)
            L += len(t) + 1
        return " ".join(out)[: n - 60]

    return phrases, [
        ("near-miss caption+bio 5k", {"caption": text(5000) + TAIL, "account_bio": text(5000)}),
        ("near-miss all fields max", {"caption": text(5000) + TAIL, "on_screen_text": text(5000),
                                      "account_bio": text(5000), "transcript": text(50000) + " This budget myth. Listen on Pod Plus."}),
        ("first-words only 50k", {"transcript": " ".join(mutate(p.split()[0]) for p in rng.choices(phrases, k=9000))[:49900]
                                  + " This budget myth. Listen on Pod Plus."})]


# =====================================================================================
# N9-4 — one review per idempotency key, however many retries are in flight
# =====================================================================================

@pytest.mark.parametrize("keyed", [True, False])
def test_n94_concurrent_retries_share_one_review(api, monkeypatch, keyed):
    """Two concurrent requests with the same Idempotency-Key (or, without one, the same submission id):
    the second waits for the first's result instead of paying for a second review."""
    from flows import zbc_open
    from zbc import clip_review, workflow

    zbc_open(api)
    calls, first_in, second_in, release = [], threading.Event(), threading.Event(), threading.Event()
    real = clip_review.review

    def slow(*a, **k):
        calls.append(threading.get_ident())
        if len(calls) == 1:
            first_in.set()
            assert release.wait(20)
        else:
            second_in.set()
        return real(*a, **k)

    monkeypatch.setattr(workflow.clip_review, "review", slow)
    headers = {"Idempotency-Key": "retry-me-1"} if keyed else {}
    body = zbc_clip("retry_1")
    got: dict = {}

    def post(name):
        got[name] = api.client.post("/zbc/clips", json=body, headers=headers)

    a = threading.Thread(target=post, args=("a",))
    a.start()
    assert first_in.wait(20)
    b = threading.Thread(target=post, args=("b",))
    b.start()
    second_in.wait(1.5)  # the second request is given time to start its own review (the defect)
    release.set()
    a.join(30)
    b.join(30)
    assert got["a"].status_code == 201 and got["b"].status_code == 201, (got["a"].text, got["b"].text)
    assert got["a"].json() == got["b"].json()
    assert len(calls) == 1, f"{len(calls)} reviews for one idempotency key"
    assert "true" in (got["a"].headers.get("Idempotent-Replayed", ""), got["b"].headers.get("Idempotent-Replayed", ""))


def test_n94_a_failed_first_attempt_does_not_strand_the_waiter(api, monkeypatch):
    """If the first request's review raises, the waiting retry runs its own (the marker is released)."""
    from flows import zbc_open
    from zbc import clip_review, workflow

    zbc_open(api)
    calls, first_in, release = [], threading.Event(), threading.Event()
    real = clip_review.review

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            first_in.set()
            assert release.wait(20)
            raise RuntimeError("review crashed")
        return real(*a, **k)

    monkeypatch.setattr(workflow.clip_review, "review", flaky)
    headers = {"Idempotency-Key": "retry-me-2"}
    got: dict = {}

    def post(name):
        try:
            got[name] = api.client.post("/zbc/clips", json=zbc_clip("retry_2"), headers=headers)
        except RuntimeError as exc:  # TestClient re-raises server exceptions
            got[name] = exc

    a = threading.Thread(target=post, args=("a",))
    a.start()
    assert first_in.wait(20)
    b = threading.Thread(target=post, args=("b",))
    b.start()
    time.sleep(0.5)
    release.set()
    a.join(30)
    b.join(30)
    assert isinstance(got["a"], RuntimeError)
    assert got["b"].status_code == 201, got["b"]
