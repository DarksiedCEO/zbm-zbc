"""
Fix wave 13 — AEGIS round 12 (review12/probes, logs).

The fail-closed design ruling of fix wave 11 still binds: what the gate cannot read goes to a human;
reading tables only UPGRADE a human's call to a rejection. No reading table is added here.

N12-1 (High) The wave-12 run rule ended a run at every unit holding a letter, so keeping a plain letter
     every two or three glyphs ("⩋ ⍺ k ⋿  ⩋ o ⋂ ⋿ y", "℞ ⍳ s ⋊  f ℞ ⋿ ⋿") passed automatically: 240 of
     1,972 masked forms (review12/probes/ns_short12.py). And the single-letter safety net counted letters
     AFTER canonicalisation had stripped the symbols between them ("⋂ o ℞ i ∫ ⋊" read "o i").
     Now: >= 3 consecutive SINGLE-GLYPH units (one letter, one letter-like symbol read as its letter, or
     one unreadable symbol; separators as in wave 12) holding a letter and >= 2 unreadable symbols are an
     unreadable word (Rule A); and the safety net counts single-glyph units before canonicalisation.
N12-2 (Low) "₩ ₩ ₩ i ₦  ฿ ฿ ฿ i ₲": one unreadable symbol repeated inside such a sequence counts as one
     glyph ("₩₩₩ i ₦").
"""

from __future__ import annotations

import itertools

from conftest import NOW
from samples import TODAY, zbc_clip, zbc_goal

TAIL = " This budget myth. Listen on Pod Plus. #ad"
NS = ["win big", "no risk", "get rich", "burn fat", "win cash", "zero fee", "no fees", "get paid", "fat loss",
      "make money", "risk free", "debt free"]
_n = itertools.count()

# review12/probes/ns_short12.py hand alphabet: symbols the gate cannot read
HAND = dict(a="⍺", b="␢", c="⊂", d="◗", e="⋿", f="∱", h="⊩", i="⍳", k="⋊", l="∟", m="⩋", n="⋂",
            o="○", p="⍴", r="℞", s="∫", t="⟙", u="⋃", w="⍵", x="⨉", z="≥")


def _rulebook(registry, phrases=NS):
    from zbc import rulebook_writer
    from zbc.rulebook import RulebookStatus
    from zbc.rulebook_writer import CampaignGoal

    goal = CampaignGoal.model_validate(zbc_goal(never_say=list(phrases)))
    return rulebook_writer.draft(goal, registry, TODAY).model_copy(update={"status": RulebookStatus.LIVE, "live_at": NOW})


def _review(registry, rb, **fields):
    from zbc import clip_review

    sub = clip_review.ClipSubmission.model_validate(zbc_clip(f"w13_{next(_n)}", **fields))
    return clip_review.review(sub, rb, registry, NOW)


def _masks(p: str):
    letters = [c for c in p if c != " "]
    for mask in itertools.product((0, 1), repeat=len(letters)):
        if any(mask):
            it = iter(mask)
            yield "  ".join(" ".join(HAND.get(c, c) if next(it) else c for c in w) for w in p.split())


# =====================================================================================
# N12-1 — a plain letter kept every two or three glyphs
# =====================================================================================

N121_CASES = ["⩋ ⍺ k ⋿  ⩋ o ⋂ ⋿ y", "℞ ⍳ s ⋊  f ℞ ⋿ ⋿", "n ○  r ⍳ s ⋊", "⍵ ⍳ n  b ⍳ g", "◗ ⋿ b ⟙  f ℞ ⋿ ⋿",
              "␢ u ℞ n  ∱ a ⟙", "⋂ o  ℞ i ∫ k", "⍵ i ⋂  ␢ i g"]


def test_n121_round12_letter_every_few_glyphs_is_an_unreadable_run(registry):
    from shared.text import unreadable_words

    rb = _rulebook(registry)
    for t in N121_CASES:
        assert unreadable_words(t), t
        d = _review(registry, rb, caption=t + TAIL)
        assert d.outcome == "human_review", (t, d.outcome, d.human_review_reasons)
        assert any("unreadable symbols" in r for r in d.human_review_reasons), (t, d.human_review_reasons)


def test_n121_every_masked_form_of_every_short_phrase_is_flagged():
    """All 1,972 masks of review12's ns_short12 at the signal level (the full review of each is the probe)."""
    from shared.text import obfuscation_signals

    n = 0
    for p in NS:
        for cap in _masks(p):
            n += 1
            assert obfuscation_signals(cap + TAIL), cap
    assert n == 1972


def test_n121_every_masked_form_with_two_symbols_is_rule_a():
    """Rule A itself (not only the canonical safety net) catches every mask with >= 2 unreadable symbols."""
    from shared.text import unreadable_words

    for p in NS:
        for cap in _masks(p):
            if sum(1 for c in cap if c in HAND.values()) >= 2:
                assert unreadable_words(cap), cap


def test_n121_single_glyph_safety_net_counts_before_canonicalisation():
    from shared.text import obfuscation_signals

    for t in ["⋂ o ℞ i ∫ ⋊", "⋂ o r i", "w ⍳ n b"]:
        sig = obfuscation_signals(t)
        assert any("single" in s for s in sig), (t, sig)


def test_n121_ordinary_text_stays_clean(registry):
    """Symbols between words, a symbol row with no letter, one symbol beside single letters: not flagged."""
    rb = _rulebook(registry)
    for t in ["Swipe → → → for more", "Mix ⇒ bake ⇒ eat", "Link → in bio", "Friends ∞ forever", "Vegan ✓ Gluten ✓",
              "☆ New video ☆", "√2 ≈ 1.414 and √3 ≈ 1.732", "✦ • ✦ • ✦ gallery night", "Price drop: $49.99 → $39.99",
              "Score: 3 ≥ 2 ≥ 1 wins", "►► skip to 2:10", "Cost tiers € / €€ / €€€ explained", "Plan A → plan B",
              "I ❤️ NY", "a → b is a function", "Rent in ₴ vs € — a comparison thread", "Price drop: €25 → €19",
              "Rated ★★★★★ by readers"]:
        d = _review(registry, rb, caption=t + TAIL)
        assert d.outcome == "pass", (t, d.human_review_reasons)


def test_n121_accepted_cost_single_letter_math_goes_to_a_human(registry):
    """The cost the ruling accepts: single letters between two or more unreadable operators / arrows."""
    rb = _rulebook(registry)
    for t in ["Set theory night: ∅ ⊂ A ⊆ B", "Route: A → B → C", "Grade: A ★ B ☆ C", "x → ∞ limits explained"]:
        d = _review(registry, rb, caption=t + TAIL)
        assert d.outcome == "human_review", (t, d.outcome)


# =====================================================================================
# N12-2 — one unreadable symbol repeated counts as one glyph in the sequence
# =====================================================================================

def test_n122_repeated_symbol_beside_letters_counts_as_one_unit(registry):
    from shared.text import unreadable_words

    rb = _rulebook(registry)
    for t in ["₩ ₩ ₩ i ₦  ฿ ฿ ฿ i ₲", "₩₩₩ i ₦  ฿฿฿ i ₲", "⍵⍵ i ⋂  b i g"]:
        assert unreadable_words(t), t
        d = _review(registry, rb, caption=t + TAIL)
        assert d.outcome != "pass", (t, d.human_review_reasons)
