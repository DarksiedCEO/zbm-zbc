"""
Fix wave 9 — AEGIS round 8 (review8/probes, logs).

H1  Exact never-say phrases written in enclosed-letter styles passed:
    negative squared (🅼🅰🅺🅴), negative circled (🅜🅐🅚🅔), regional
    indicators (🇲🇦🇰🇪) — 36/36. `canonical()` folded every symbol with
    no NFKC decomposition to a space. Now (a) a GENERATED map of every
    letter-like symbol to its Latin letter (`shared/text.LETTERLIKE`,
    derived from Unicode names at import; its coverage is checked here
    against an independent derivation over every code point of the
    blocks), (b) any letter-like symbol in customer-facing text is an
    obfuscation signal (never an automatic pass) and an exact phrase once
    mapped is a reject, (c) a fail-safe: a field that canonicalisation
    strips by more than STRIPPED_SHARE_LIMIT is a human's call, so an
    unknown symbol style can never auto-pass, (d) fuzz across every block
    and every never-say phrase: 0 automatic passes.
M1  Clip Review cost 4.7 s CPU at 100 phrases (AEGIS generator, every
    field at max) and ran under the service-wide workflow lock. Now the
    worst case is bounded (<= 1.5 s CPU, measured here with the same
    generator) and the review runs OFF the lock (the lock is taken to
    check and commit only).
M2  A symbol outside the lexicon beside a phrase-minus-one-word fragment
    ("make 💱", "risk ∅", "beat the 📈"), a line break between word and
    symbol ("make\\n💰"), or the symbol in another field passed. Now any
    symbol / emoji (not a letter, not punctuation) in a missing word's
    place is a stand-in, across line breaks and field boundaries.
L2  `retired_rule_ids` grew without bound and every version kept its own
    copy (60 churn revisions x 1,000 phrases: 96 MiB, 30 MiB GET body).
L4  415 was answered before authentication.
(L3, the flaky /health timing test, is fixed in test_fix_wave_6.py.)
"""

from __future__ import annotations

import itertools
import json
import random
import re
import threading
import time
import tracemalloc
import unicodedata

import pytest
from aegis8_corpus import CAPTIONS as CORPUS8
from aegis8_corpus import EMOJI as CORPUS8_EMOJI
from aegis8_corpus import NEVER_SAY_A, NEVER_SAY_B
from conftest import NOW, TEST_SERVICE_TOKEN
from samples import TODAY, zbc_clip, zbc_goal

NS8 = NEVER_SAY_A + NEVER_SAY_B
TAIL = " This budget myth. Listen on Pod Plus. #ad"
_n = itertools.count()


def _rulebook(registry, phrases=NS8):
    from zbc import rulebook_writer
    from zbc.rulebook import RulebookStatus
    from zbc.rulebook_writer import CampaignGoal

    goal = CampaignGoal.model_validate(zbc_goal(never_say=list(phrases)))
    return rulebook_writer.draft(goal, registry, TODAY).model_copy(update={"status": RulebookStatus.LIVE, "live_at": NOW})


def _run(registry, rb, fields: dict):
    """The AEGIS round-8 probe's `run()`: the fields given, the disclosure /
    must-say TAIL appended to the caption (a caption of "hello" + TAIL when
    none is given), the sample clip's other fields."""
    from zbc import clip_review

    base = zbc_clip(f"w9_{next(_n)}")
    for k, v in fields.items():
        base[k] = v + (TAIL if k == "caption" else "")
    if "caption" not in fields:
        base["caption"] = "hello" + TAIL
    return clip_review.review(clip_review.ClipSubmission.model_validate(base), rb, registry, NOW)


def _never_say_flag(d) -> bool:
    return any(b.rule_id.startswith("NS-") for b in d.broken_rules) or any(
        "never-say" in r or "letter-like" in r or "stripped" in r or "letters mixed" in r for r in d.human_review_reasons)


# =====================================================================================
# H1 — letter-like symbols
# =====================================================================================

# An INDEPENDENT derivation of the letter-like code points (not the implementation's): every code
# point of the blocks named in the finding whose Unicode name designates one Latin letter, plus the
# small capitals and superscript / subscript Latin letters wherever they are.
_BLOCKS = [(0x2460, 0x24FF, "Enclosed Alphanumerics"), (0x1F100, 0x1F1FF, "Enclosed Alphanumeric Supplement"),
           (0x1D400, 0x1D7FF, "Mathematical Alphanumeric Symbols"), (0xFF00, 0xFFEF, "Halfwidth and Fullwidth Forms"),
           (0x2100, 0x214F, "Letterlike Symbols")]
_IN_BLOCK = re.compile(r".*(?:CAPITAL|SMALL|LETTER) ([A-Z])")
_ANYWHERE = re.compile(r"(?:LATIN LETTER SMALL CAPITAL|MODIFIER LETTER (?:SMALL|CAPITAL)|LATIN SUBSCRIPT SMALL LETTER"
                       r"|SUPERSCRIPT LATIN SMALL LETTER) ([A-Z])")


def _reference_letterlike() -> dict[str, str]:
    out: dict[str, str] = {}
    for lo, hi, _ in _BLOCKS:
        for cp in range(lo, hi + 1):
            name = unicodedata.name(chr(cp), "")
            m = _IN_BLOCK.fullmatch(name)
            if m and not any(s in name for s in ("GREEK", "CYRILLIC", "DIGIT", "HEBREW", "KATAKANA", "HANGUL")):
                out[chr(cp)] = m.group(1).lower()
    for cp in range(0x80, 0x30000):
        m = _ANYWHERE.fullmatch(unicodedata.name(chr(cp), ""))
        if m:
            out[chr(cp)] = m.group(1).lower()
    return out


REFERENCE = _reference_letterlike()


def test_h1_reference_derivation_is_not_empty():
    assert len(REFERENCE) > 1000, len(REFERENCE)
    for ch in "🅼🅜🇲ⓜ⒨🄼𝐦ｍᴍᵐ℘":
        assert ch in REFERENCE, ch


_REGIONAL_REF = {ch: letter for ch, letter in REFERENCE.items() if unicodedata.name(ch).startswith("REGIONAL INDICATOR")}


def test_h1_every_letterlike_code_point_folds_to_its_letter():
    """Coverage of the generated map against every code point of the blocks. Fix wave 10 (N9-2): the
    regional indicators are the exception — canonical() no longer folds them (a run of them is also a
    row of flags); they are READ as their letters only by `regional_reading`, whose never-say hits Clip
    Review sends to a human."""
    from shared.text import canonical, regional_reading

    assert len(_REGIONAL_REF) == 26
    wrong = [(f"U+{ord(ch):04X}", unicodedata.name(ch), canonical(ch)) for ch, letter in REFERENCE.items()
             if ch not in _REGIONAL_REF and canonical(ch) != letter]
    print(f"\nH1 letter-like code points: {len(REFERENCE)}; not folded to their letter: {len(wrong)}")
    assert not wrong, wrong[:20]
    for ch, letter in _REGIONAL_REF.items():
        assert canonical(ch) == "" and regional_reading(ch) == letter, ch


def test_h1_the_map_is_generated_from_unicode_names_and_covers_the_reference():
    from shared import text

    plain = {ch: letter for ch, letter in REFERENCE.items() if ch not in _REGIONAL_REF}
    assert set(plain) <= set(text.LETTERLIKE), sorted(f"U+{ord(c):04X}" for c in set(plain) - set(text.LETTERLIKE))[:20]
    for ch, letter in plain.items():
        assert text.LETTERLIKE[ch] == letter, (ch, unicodedata.name(ch))
    # fix wave 10 (N9-2): regional indicators are a separate reading, never in the letter-like map
    assert text.REGIONAL_LETTERS == _REGIONAL_REF
    assert not set(_REGIONAL_REF) & set(text.LETTERLIKE)
    # nothing ordinary is in it: no ASCII, no Latin-1 letter, no letter with a diacritic
    for ch in text.LETTERLIKE:
        assert ord(ch) >= 0x80 and not ("À" <= ch <= "ÿ"), ch
        assert " WITH " not in unicodedata.name(ch, ""), ch


def _styles() -> dict[str, dict[str, str]]:
    """Every letter-like STYLE (the reference entries grouped by name without the final letter),
    lower-case letter -> that style's character."""
    styles: dict[str, dict[str, str]] = {}
    for ch, letter in REFERENCE.items():
        style = unicodedata.name(ch).rsplit(" ", 1)[0].replace(" CAPITAL", "").replace(" SMALL", "")
        styles.setdefault(style, {}).setdefault(letter, ch)
    return styles


STYLES = _styles()


def _write(phrase: str, style: dict[str, str]) -> str:
    return "".join(style.get(c, c) for c in phrase.lower())


def test_h1_aegis_round8_enclosed_phrases_are_rejected(registry):
    """The AEGIS probe's exact cases (ns_q8.py / ns_scripts8.py): 36/36 passed. Fix wave 10 (N9-2): the
    regional-indicator cases (ri, riz) are a human's call with a never-say reason, never a rejection
    (a run of regional indicators is also a row of flags); the enclosed styles are still rejected."""
    rb = _rulebook(registry)
    sq = lambda s: "".join(chr(0x1F170 + ord(c) - 97) if c.isalpha() else c for c in s)
    nc = lambda s: "".join(chr(0x1F150 + ord(c) - 97) if c.isalpha() else c for c in s)
    ri = lambda s: "".join(chr(0x1F1E6 + ord(c) - 97) if c.isalpha() else c for c in s)
    riz = lambda s: "".join(chr(0x1F1E6 + ord(c) - 97) + "​" if c.isalpha() else c for c in s)
    out = []
    for f, want in ((sq, "reject"), (nc, "reject"), (ri, "human_review"), (riz, "human_review")):
        for p in ("get rich", "make money", "guaranteed returns", "zero fees", "burn fat", "easy money"):
            for field in ("caption", "account_bio"):
                d = _run(registry, rb, {field: f(p)})
                named = want == "reject" or any("never-say" in r and repr(p) in r for r in d.human_review_reasons)
                out.append((f(p), field, d.outcome if named else "no never-say reason", want,
                            [b.rule_id for b in d.broken_rules]))
    bad = [o for o in out if o[2] != o[3]]
    print(f"\nH1 AEGIS enclosed styles: {len(out)} cases, not as expected {len(bad)}")
    assert not bad, bad[:10]


def test_h1_every_style_every_phrase_exact_is_rejected(registry):
    """(b)/(d): every never-say phrase of lists A+B written in every letter-like style (a letter the
    style lacks stays ASCII, as an evader would write it) is a REJECT, in the caption and the bio —
    except regional indicators (fix wave 10, N9-2): a human's call with a never-say reason."""
    rb = _rulebook(registry)
    misses, n = [], 0
    for name, style in sorted(STYLES.items()):
        for p in NS8:
            for field in ("caption", "account_bio"):
                n += 1
                d = _run(registry, rb, {field: _write(p, style)})
                if name.startswith("REGIONAL INDICATOR"):
                    if d.outcome != "human_review" or not any("never-say" in r and repr(p) in r for r in d.human_review_reasons):
                        misses.append((name, _write(p, style), field, d.outcome))
                elif d.outcome != "reject":
                    misses.append((name, _write(p, style), field, d.outcome))
    print(f"\nH1 {len(STYLES)} styles x {len(NS8)} phrases x 2 fields = {n}: not as expected {len(misses)}")
    assert not misses, misses[:10]


def test_h1_fuzz_mixed_styles_and_invisibles_never_auto_pass(registry):
    """(d): random per-letter styles from every block, random zero-width characters and spaces
    between letters, every phrase: 0 automatic passes."""
    rb = _rulebook(registry)
    rng = random.Random(9)
    style_list = list(STYLES.values())
    passes, n = [], 0
    for p in NS8:
        for _ in range(12):
            out = []
            for c in p.lower():
                st = rng.choice(style_list)
                out.append(st.get(c, c) if c.isalpha() and rng.random() < 0.8 else c)
                if rng.random() < 0.15:
                    out.append(rng.choice(["​", "‍", "️", " "]))
            t = "".join(out)
            n += 1
            d = _run(registry, rb, {rng.choice(["caption", "account_bio", "on_screen_text"]): t})
            if d.outcome == "pass":
                passes.append(t)
    print(f"\nH1 fuzz: {n} mixed-style phrases, automatic passes {len(passes)}")
    assert not passes, passes[:10]


def test_h1_styled_text_is_itself_a_signal(registry):
    """Styled letters are an obfuscation signal even when no phrase is there. Fix wave 10: regional
    indicators are not (N9-2: "🇭🇪🇱🇱🇴 🇫🇷🇮🇪🇳🇩🇸" is also a row of flags; with no never-say reading it
    passes, tests/test_fix_wave_10.py), Braille letters are (N9-7)."""
    rb = _rulebook(registry)
    for t in ["𝐇𝐞𝐥𝐥𝐨 𝐟𝐫𝐢𝐞𝐧𝐝𝐬", "🅷🅴🅻🅻🅾 🅵🆁🅸🅴🅽🅳🆂", "🅗🅔🅛🅛🅞", "ｈｅｌｌｏ ｆｒｉｅｎｄｓ",
              "ʜᴇʟʟᴏ ꜰʀɪᴇɴᴅꜱ", "ʰᵉˡˡᵒ", "ⓗⓔⓛⓛⓞ", "⒣⒠⒧⒧⒪", "⠓⠑⠇⠇⠕ ⠋⠗⠊⠑⠝⠙⠎"]:
        d = _run(registry, rb, {"caption": t})
        assert d.outcome == "human_review", (t, d.outcome, d.human_review_reasons)
        assert any("letter-like" in r or "compatibility form" in r or "lookalike" in r for r in d.human_review_reasons), (t, d.human_review_reasons)


def test_h1_a_flag_is_not_styled_text(registry):
    """A single flag (two regional indicators) is a flag emoji, not letters; FP guard."""
    rb = _rulebook(registry)
    for t in ["Made in 🇬🇧, shipped worldwide", "Proudly 🇺🇸 owned", "🇯🇵 trip recap", "Ciao 🇮🇹"]:
        d = _run(registry, rb, {"caption": t})
        assert d.outcome == "pass", (t, d.outcome, d.human_review_reasons)


def test_h1_failsafe_unknown_symbol_style_never_auto_passes(registry):
    """(c): a style nobody mapped — 8-dot Braille patterns, box drawing, private-use glyphs — is
    stripped by canonicalisation; a field losing more than STRIPPED_SHARE_LIMIT of its characters is a
    human's call. (Fix wave 10, N9-7: the 26 grade-1 Braille LETTERS are now read as letters, so the
    unmapped style here is the same cells with dot 7 added, which are no letter.)"""
    from shared.text import STRIPPED_SHARE_LIMIT, stripped_share

    assert 0.2 <= STRIPPED_SHARE_LIMIT <= 0.5
    assert stripped_share("⡍⡁⡅⡑ ⡍⡕⡝⡑⡽" + TAIL) > STRIPPED_SHARE_LIMIT  # not diluted by ordinary text around it
    rb = _rulebook(registry)
    braille = {c: chr(ord(b) + 0x40) for c, b in zip("abcdefghijklmnopqrstuvwxyz", "⠁⠃⠉⠙⠑⠋⠛⠓⠊⠚⠅⠇⠍⠝⠕⠏⠟⠗⠎⠞⠥⠧⠺⠭⠽⠵")}
    for t in ["".join(braille.get(c, c) for c in p) for p in ("make money", "get rich", "zero fees")] + [
            " ", "▟▙▛▜ ▚▞▚", "𝌀𝌁𝌂 𝌃𝌄"]:
        assert stripped_share(t) > STRIPPED_SHARE_LIMIT, t
        d = _run(registry, rb, {"caption": t})
        assert d.outcome != "pass", (t, d.outcome)
        assert any("stripped" in r for r in d.human_review_reasons), (t, d.human_review_reasons)
    # ordinary emoji-heavy text is not "stripped": pictographs, flags, skin tones, ZWJ sequences
    for t in ["🎉🎉🎉 🔥🔥", "Thank you 🙏🏽 for 10k 👨‍👩‍👧", "Five ⭐⭐⭐⭐⭐", "Sale ends at midnight ⏰", "Coffee ☕ + croissant 🥐 = $6"]:
        assert stripped_share(t) <= STRIPPED_SHARE_LIMIT, (t, stripped_share(t))


def test_h1_m2_corpus8_false_positives(registry):
    """FP guard on the AEGIS round-8 corpus (170 captions, lists A+B): before this wave 3/170 were
    flagged (0 by symbols); the letter-like signal, the fail-safe and the symbol rule add at most 1%."""
    rb = _rulebook(registry)
    flagged = []
    for i, c in enumerate(CORPUS8):
        d = _run(registry, rb, {"caption": c})
        if d.outcome != "pass":
            flagged.append((c, d.human_review_reasons[:2]))
    new = [f for f in flagged if any("letter-like" in r or "stripped" in r or "a symbol standing" in r for r in f[1])]
    print(f"\nH1/M2 corpus8 A+B: flagged {len(flagged)}/170; by the wave-9 rules {len(new)}")
    for f in flagged:
        print("   ", f)
    assert len(new) <= 1, new
    assert len(flagged) <= 4, flagged


# =====================================================================================
# M2 — symbols standing for a word
# =====================================================================================

AEGIS8_Q = [
    "guaranteed ↑", "guaranteed ⇧ returns", "make ＄", "make ＄＄＄", "make 💰️", "make 💰🏽", "make :moneybag:", "make :money_with_wings:",
    "get 🤑", "free 🤑", "double your 💲💲", "passive 🪙🪙", "make 💱", "make 🏧 fast", "get 🍾 quick", "burn 🍔", "zero 💳 fees",
    "debt 🆓", "debt ✈️ free", "instant ✔️", "no ⚠️ effects", "no side 🤢", "make 🄼🄾🄽🄴🅈", "make 🅼🅾🅽🅴🆈", "make 🇲🇴🇳🇪🇾",
    "get Ⓡⓘⓒⓗ", "risk ∅", "∅ risk", "no ⚖ fast", "lose ⚖ fast", "easy 💵", "anti ⏳", "reverse ⏳", "beat the 📈",
    "quit your 💼", "cash back ✅", "make ｍｏｎｅｙ", "make 💰 💰 💰", "make 🎰", "get 🫰 quick", "guaranteed 🔝"]
AEGIS8_R = ["make\n💰", "guaranteed\n📈", "get\n💎", "free\n💸", "debt\n🆓", "easy\n💵", "make\n\n💰", "gt\nrch", "mk\nmny",
            "grnteed\nrtrns", "burn\nphat", "zero\nfeez"]
AEGIS8_S = [("make", "💰"), ("guaranteed", "📈"), ("free", "💸"), ("easy", "💵"), ("get", "💎"), ("debt", "🆓")]


@pytest.mark.parametrize("cls", ["Q", "R", "S"])
def test_m2_aegis_round8_symbol_classes_are_never_an_automatic_pass(registry, cls):
    rb = _rulebook(registry)
    if cls == "Q":
        cases = [(t, {"caption": t}) for t in AEGIS8_Q]
    elif cls == "R":
        cases = [(repr(t), {"caption": t}) for t in AEGIS8_R]
    else:
        cases = []
        for a, b in AEGIS8_S:
            cases.append((f"caption {a!r} + bio {b!r}", {"caption": a, "account_bio": b}))
            cases.append((f"on_screen {a!r} + transcript {b!r}", {"on_screen_text": a, "transcript": b}))
    misses = [label for label, fields in cases if not _never_say_flag(_run(registry, rb, fields))]
    print(f"\nM2 class {cls}: {len(cases)} cases, missed {len(misses)}: {misses}")
    assert not misses, misses


def test_m2_disclosure_first_variants(registry):
    """ns_q8.py: the same stand-ins with the disclosure FIRST (the symbol closes or sits mid-caption)."""
    rb = _rulebook(registry)
    from zbc import clip_review

    head = "This budget myth. Listen on Pod Plus. #ad "
    cases = ["risk ∅", "∅ risk", "make 🎰. Link below", "make 💱 today", "guaranteed 🔝 every month"]
    for c in cases:
        d = clip_review.review(clip_review.ClipSubmission.model_validate(zbc_clip(f"q{next(_n)}", caption=head + c)),
                               rb, registry, NOW)
        assert _never_say_flag(d), (c, d.outcome, d.human_review_reasons)


def test_m2_symbol_rule_unit():
    from shared.text import symbol_stand_in

    # any symbol (not a letter, not punctuation) in a missing word's place, whatever follows it
    for text, phrase in [("make 💱 This budget myth", "make money"), ("risk ∅", "risk free"), ("∅ risk", "no risk"),
                         ("beat the 📈 today", "beat the market"), ("make\n💰", "make money"), ("make\n\n💰", "make money"),
                         ("get 🍕\nquick", "get rich quick"), ("instant ✔️ now", "instant approval"),
                         ("guaranteed ↑ every month", "guaranteed returns"), ("no side 🤢 at all", "no side effects"),
                         ("make + more", "make money")]:
        assert symbol_stand_in(text, phrase), (text, phrase)
    # not a stand-in: a price, symbols alone or beside function words only, the phrase itself, other words,
    # a symbol beside no fragment, or a lexicon symbol that names the very word it sits beside
    for text, phrase in [("make $20 in a week", "make money"), ("$ $", "make money"), ("your 💰", "double your money"),
                         ("make money 💰", "make money"), ("made 💰", "make money"), ("Sold out twice 🎉 Back Friday", "make money"),
                         ("Guaranteed ✅ delivery before Christmas", "guaranteed returns"),
                         ("Doctor 🩺 appointments online now", "doctor recommended"), ("no 🎯", "no risk"),
                         ("make it 🎰", "make money"), ("get 🎉 🎉", "get rich quick")]:
        assert not symbol_stand_in(text, phrase), (text, phrase)


# =====================================================================================
# M1 — review cost and the workflow lock
# =====================================================================================

def _aegis8_cost_cases():
    """probes/ns_cost8.py, VERBATIM generator (seed 81), the 100-phrase cases."""
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

    nphr = 100
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

    cases = [("near-miss caption+bio 5k", {"caption": text(5000) + " This budget myth. Listen on Pod Plus. #ad", "account_bio": text(5000)}),
             ("near-miss all fields max", {"caption": text(5000) + " This budget myth. Listen on Pod Plus. #ad", "on_screen_text": text(5000),
                                           "account_bio": text(5000), "transcript": text(50000) + " This budget myth. Listen on Pod Plus."}),
             ("first-words only 50k", {"transcript": " ".join(mutate(p.split()[0]) for p in rng.choices(phrases, k=9000))[:49900]
                                       + " This budget myth. Listen on Pod Plus."})]
    return phrases, cases


# M1 bounds (fix wave 11 follow-up: the old 1.5 s bound failed at 1.503 s on the unmodified 978bcaa tree
# inside the full run — a flaky test). Measured inside the full test run on the reference host (2 vCPU
# Intel Xeon @ 2.80 GHz, Python 3.11.15, Sep 24-25, 2026, six full runs, one on 978bcaa and one from fix wave
# 10): worst reviewed case 1.19-1.51 s CPU, routed-to-a-human variant 1.46-1.91 s. The bounds are 1.5x the worst measured value: outside the
# run-to-run noise (about 25% in-suite), still failing on any 2x regression of the review (the class
# these tests exist for: the fix-wave-10 memo-eviction cliff was ~20x). The measured figures are the
# documented budget (ADR 0005 gap 16).
REVIEW_CPU_BOUND_S = 2.25  # at 100 never-say phrases, every field at its maximum (M1): 1.5 x 1.51 s
FORCED_CPU_BOUND_S = 2.9  # the same, routed to a human: 1.5 x 1.91 s
CPU_CALIBRATION_S = 0.120  # `_cpu_workload`, best of 3, on the reference 2-vCPU host, idle (fix wave 9)


def _cpu_workload() -> float:
    """CPU seconds (this thread's) of a fixed workload that uses none of the service's code (so a
    regression in the review cannot hide in the scale factor) but stresses what the review stresses:
    many small objects in a large dict, and big-integer arithmetic."""
    import gc

    rng = random.Random(5)
    enabled = gc.isenabled()
    gc.disable()  # the collector's cost depends on the test process's heap, not on the machine
    try:
        c0 = time.thread_time()
        d = {}
        x = 1
        mask = (1 << 4096) - 1
        for i in range(60_000):
            d[(rng.random(), i)] = [i, str(i)]
            x = ((x << 3) ^ (x >> 5) ^ i) & mask
        sorted(d)
        return time.thread_time() - c0
    finally:
        if enabled:
            gc.enable()


def _cpu_slowdown() -> float:
    """How much slower than CPU_CALIBRATION_S this machine runs the workload right now, 1x..4x
    (the onboarding-py harness: on an idle machine the bound is the bound)."""
    return min(4.0, max(1.0, min(_cpu_workload() for _ in range(3)) / CPU_CALIBRATION_S))


def _cold():
    from shared import text
    from zbc import clip_review

    text.clear_memos()
    for mod in (text, clip_review):
        for v in list(vars(mod).values()):
            if hasattr(v, "cache_clear"):
                v.cache_clear()


def test_m1_review_cost_at_100_phrases_on_the_aegis_generator(registry):
    """probes/ns_cost8.py at 100 phrases: every cache cold, CPU time of one review, best of 2."""
    from zbc import clip_review

    phrases, cases = _aegis8_cost_cases()
    rb = _rulebook(registry, phrases)
    slow = _cpu_slowdown()
    worst = 0.0
    for label, over in cases:
        sub = clip_review.ClipSubmission.model_validate(zbc_clip(f"c{next(_n)}", **over))
        runs = []
        for _ in range(2):
            _cold()
            c0 = time.thread_time()  # this thread's CPU: another thread of the test process is not the review's cost
            d = clip_review.review(sub, rb, registry, NOW)
            runs.append(time.thread_time() - c0)
        cpu = min(runs)
        worst = max(worst, cpu)
        print(f"\nM1 never_say=100 {label:28s} review {cpu * 1000:6.0f} ms CPU (cold) -> {d.outcome}")
        assert d.outcome != "pass"
    # a stricter variant than the AEGIS generator's: the same all-fields text with the clip routed to a human
    # (no rejection cuts the similarity signals short) — measured and reported, bounded more loosely
    sub = clip_review.ClipSubmission.model_validate(zbc_clip(f"c{next(_n)}", **cases[1][1]))
    _cold()
    c0 = time.thread_time()
    clip_review.review(sub, rb, registry, NOW, route_to_human=("forced",))
    forced = time.thread_time() - c0
    print(f"M1 all fields max, routed to a human: {forced * 1000:.0f} ms CPU (cold); machine slowdown {slow:.2f}x")
    slow = max(slow, _cpu_slowdown())  # measured again after the reviews: load that came meanwhile counts
    print(f"M1 machine slowdown (before / after the reviews, larger kept): {slow:.2f}x")
    assert worst <= REVIEW_CPU_BOUND_S * slow, (worst, slow)
    assert forced <= FORCED_CPU_BOUND_S * slow, (forced, slow)


def test_m1_clip_review_runs_off_the_workflow_lock(api, monkeypatch):
    """A slow review (held here on an event) must not stall a rulebook draft or a brief: the
    review is pure over its inputs and runs without the lock; the lock is taken to commit."""
    from flows import zbc_open
    from zbc import clip_review, workflow

    zbc_open(api)
    entered, release = threading.Event(), threading.Event()
    real = clip_review.review

    def slow(*a, **k):
        entered.set()
        assert release.wait(20)
        return real(*a, **k)

    monkeypatch.setattr(workflow.clip_review, "review", slow)
    result: dict = {}
    t = threading.Thread(target=lambda: result.setdefault("clip", api.post("/zbc/clips", zbc_clip("slow_1"))))
    t.start()
    try:
        assert entered.wait(20)
        # while the review is held: another campaign's rulebook draft and a ZBM brief go through
        other = {}

        def work():
            other["rb"] = api.post("/zbc/campaigns/camp_other/rulebooks",
                                   {"actor_id": "zbc_rulebook_writer", "goal": zbc_goal(campaign_id="camp_other")})
            other["brief"] = api.post("/zbm/briefs", {"requirements": __import__("samples").zbm_requirements()})

        w = threading.Thread(target=work)
        w.start()
        w.join(10)
        stalled = w.is_alive()
    finally:
        release.set()
        t.join(30)
    assert not stalled, "a rulebook draft / brief waited for a clip review (review holds the workflow lock)"
    assert other["rb"].status_code == 201, other["rb"].text
    assert other["brief"].status_code == 201, other["brief"].text
    assert result["clip"].status_code == 201, result["clip"].text
    assert api.zbc.decisions["slow_1"].outcome == result["clip"].json()["outcome"]


def test_m1_a_change_during_the_review_is_not_committed_stale(api, monkeypatch):
    """The review ran on a registry snapshot; a registry write that lands while it runs makes the
    commit re-run the review under the lock (never a decision on state that is gone)."""
    from flows import zbc_open
    from zbc import clip_review, workflow

    zbc_open(api)
    calls = []
    real = clip_review.review
    reg = api.zbc.registry

    def racing(sub, rb, registry, now, **k):
        calls.append(registry)
        if len(calls) == 1:
            sp = next(r for r in rb.rules if r.kind.value == "spec_length")
            row = reg.rows[sp.rationale_row_ids[0]]
            reg.commit(row.model_copy(update={"expires_at": TODAY.replace(year=TODAY.year - 1)}))  # expired meanwhile
        return real(sub, rb, registry, now, **k)

    monkeypatch.setattr(workflow.clip_review, "review", racing)
    r = api.post("/zbc/clips", zbc_clip("race_1"))
    assert r.status_code == 201, r.text
    assert len(calls) == 2, len(calls)  # recomputed under the lock
    d = r.json()
    assert d["outcome"] == "human_review" and any("registry rows" in x for x in d["human_review_reasons"]), d


# =====================================================================================
# L2 — retired rule ids
# =====================================================================================

def _churn(registry, revisions=60, phrases=1000):
    from zbc import rulebook_writer
    from zbc.rulebook_writer import CampaignGoal

    def goal(k):
        return CampaignGoal.model_validate(zbc_goal(never_say=[f"rev{k} phrase {i}" for i in range(phrases)]))

    versions = [rulebook_writer.draft(goal(0), registry, TODAY)]
    for k in range(1, revisions + 1):
        versions.append(rulebook_writer.revise(versions[-1], goal(k), registry, TODAY, k + 1))
    return versions


def test_l2_retired_ids_are_stored_compactly_and_never_reused(registry):
    tracemalloc.start()
    try:
        from zbc import rulebook_writer  # noqa: F401 (imported before measuring)

        base = tracemalloc.get_traced_memory()[0]
        versions = _churn(registry)
        per_version = []
        for v in versions:
            meta = {k: x for k, x in v.model_dump(mode="json").items() if k != "rules"}
            per_version.append(len(json.dumps(meta)))
        total = tracemalloc.get_traced_memory()[0] - base
    finally:
        tracemalloc.stop()
    last = versions[-1]
    assert last.retired_rule_count == 60 * 1000
    # never reused: every live id of every version is above the high-water mark of its predecessor
    for prev, cur in zip(versions, versions[1:]):
        fresh = cur.rule_ids() - prev.rule_ids()
        assert all(prev.is_retired(i) is False and cur.is_retired(i) is False for i in fresh)
        assert all(cur.is_retired(i) for i in prev.rule_ids() - cur.rule_ids())
    retired_state = sum(per_version)
    print(f"\nL2 60 churn revisions x 1000: retired {last.retired_rule_count}; per-version metadata "
          f"{min(per_version)}-{max(per_version)} B (sum {retired_state / 2**10:.0f} KiB); traced {total / 2**20:.1f} MiB")
    assert max(per_version) < 4096, max(per_version)  # O(1) per version, whatever was retired before
    assert retired_state < 10 * 2**20


def test_l2_memory_per_revision_does_not_grow_with_history(registry):
    """O(current): the 60th revision costs what the 2nd did."""
    from zbc import rulebook_writer
    from zbc.rulebook_writer import CampaignGoal

    versions = _churn(registry, revisions=2)
    tracemalloc.start()
    try:
        def cost(prev, k):
            g = CampaignGoal.model_validate(zbc_goal(never_say=[f"x{k} phrase {i}" for i in range(1000)]))
            before = tracemalloc.get_traced_memory()[0]
            rb = rulebook_writer.revise(prev, g, registry, TODAY, prev.version + 1)
            return rb, tracemalloc.get_traced_memory()[0] - before

        rb, early = cost(versions[-1], 1)
        for k in range(2, 60):
            rb, late = cost(rb, k)
    finally:
        tracemalloc.stop()
    print(f"\nL2 memory of one revision: early {early / 2**10:.0f} KiB, 60th {late / 2**10:.0f} KiB")
    assert late < early * 1.25 + 64 * 1024, (early, late)


def test_l2_rulebooks_get_is_summarised_and_retired_ids_paginated(api):
    from zbc.rulebook import RulebookStatus

    versions = _churn(api.zbc.registry)
    cid = versions[0].campaign_id
    for v in versions[:-1]:
        api.zbc.rulebooks.commit(v.model_copy(update={"status": RulebookStatus.SUPERSEDED}))
    api.zbc.rulebooks.commit(versions[-1].model_copy(update={"status": RulebookStatus.LIVE, "live_at": NOW}))
    t0 = time.perf_counter()
    r = api.get(f"/zbc/campaigns/{cid}/rulebooks")
    dt = time.perf_counter() - t0
    assert r.status_code == 200
    body = r.json()
    print(f"\nL2 GET rulebooks (61 versions): {len(r.content)} B in {dt * 1000:.0f} ms")
    assert len(r.content) < 2**20 and dt < 0.2, (len(r.content), dt)
    assert body["total"] == 61 and len(body["versions"]) <= 61
    assert body["versions"][-1]["retired_rule_count"] == 60_000
    # one version: its rules, a retired-id summary, never the whole retired list
    t0 = time.perf_counter()
    one = api.get(f"/zbc/campaigns/{cid}/rulebooks/61")
    dt1 = time.perf_counter() - t0
    assert one.status_code == 200 and len(one.content) < 2**20 and dt1 < 0.2, (len(one.content), dt1)
    assert "retired_rule_ids" not in one.json() and one.json()["retired_rule_count"] == 60_000
    # the retired ids, a page at a time
    page = api.get(f"/zbc/campaigns/{cid}/rulebooks/61/retired-rule-ids", params={"offset": 0, "limit": 500}).json()
    assert page["total"] == 60_000 and len(page["ids"]) == 500 and page["ids"][0] == "NS-01"
    last = api.get(f"/zbc/campaigns/{cid}/rulebooks/61/retired-rule-ids", params={"offset": 59_900, "limit": 500}).json()
    assert len(last["ids"]) == 100 and last["ids"][-1] == "NS-60000" and last["next_offset"] is None
    assert api.get(f"/zbc/campaigns/{cid}/rulebooks/61/retired-rule-ids", params={"limit": 5000}).status_code == 422


# =====================================================================================
# L4 — authentication before content type
# =====================================================================================

def test_l4_unauthenticated_requests_learn_nothing_about_content_types(api):
    c = api.client
    for headers in ({}, {"Authorization": "Bearer wrong-token"}):
        h = {**headers, "Content-Type": "text/plain"}
        r = c.post("/zbc/clips", content=b"hello", headers=h if headers else {**h, "Authorization": ""})
        assert r.status_code == 401, (headers, r.status_code, r.text)
        r = c.post("/zbc/clips", content=b"x" * 2_000_000, headers={**h, "Authorization": headers.get("Authorization", "")})
        assert r.status_code == 401, r.status_code  # not 413 either
    # authenticated: the content type is judged
    r = c.post("/zbc/clips", content=b"hello", headers={"Content-Type": "text/plain"})
    assert r.status_code == 415
    # /health is open
    assert c.get("/health", headers={"Authorization": ""}).status_code == 200


def test_l4_unauthenticated_on_a_real_server(tmp_path):
    import httpx
    from test_fix_wave_6 import _start, _stop

    proc, port = _start()
    try:
        r = httpx.post(f"http://127.0.0.1:{port}/zbc/clips", content=b"hello", headers={"Content-Type": "text/plain"})
        assert r.status_code == 401, r.status_code
        r = httpx.post(f"http://127.0.0.1:{port}/zbc/clips", content=b"hello",
                       headers={"Content-Type": "text/plain", "Authorization": f"Bearer {TEST_SERVICE_TOKEN}"})
        assert r.status_code == 415, r.status_code
    finally:
        _stop(proc)
