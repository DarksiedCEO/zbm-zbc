"""
Fix wave 12 — AEGIS round 11 (review11/probes, logs).

The fail-closed design ruling of fix wave 11 still binds: what the gate cannot read goes to a human;
reading tables only UPGRADE a human's call to a rejection.

N11-1 (High) Symbols spaced out one per word ("⩋ ⍺ ⋊ ⋿  ⩋ ○ ⋂ ⋿ y", "₥ ₳ ₭ €  ₥ ○ ₦ € ¥", or separated by
     "━━━" dividers) passed: Rule A judged each whitespace word alone and skipped a lone symbol. (a) A run
     of >= 3 consecutive single-symbol words is now read as one word; (b) the currency / math reading is
     matched with the main gate's collapse / windowed matching, so a spaced reading of an exact phrase
     rejects as spaced ASCII does.
N11-2 (Medium) A phrase split one word per field over three fields passed (fields were only read in
     pairs); so did a phrase whose first word opens a field and whose second word is a whole short field.
N11-3 (Medium, ruling gap) "♏e︱✝ be︱︱y fa✝": a word mixing Latin letters with emoji-listed symbols and
     vertical-bar punctuation inside it.
N11-4 (Low) The price exemption let "₥1 ₳1 ₭1 €1 ₥1 01 ₦1 €1 ¥1" pass: prices are exempt at most two in
     a row.
N11-5 (Low) The never-say caps counted raw characters, not what the gate scans ("㎉" is "kcal").
"""

from __future__ import annotations

import itertools

import pytest
from conftest import NOW
from samples import TODAY, zbc_clip, zbc_goal

TAIL = " This budget myth. Listen on Pod Plus. #ad"
NS = ["make money", "risk free", "debt free", "cash prize", "win big", "burn fat", "easy money", "no risk", "free money",
      "get rich", "quit your job", "work from home", "secret method", "melt belly fat", "passive income", "zero fees",
      "lose weight fast", "miracle cure", "instant approval", "beat the market"]
_n = itertools.count()


def _rulebook(registry, phrases=NS):
    from zbc import rulebook_writer
    from zbc.rulebook import RulebookStatus
    from zbc.rulebook_writer import CampaignGoal

    goal = CampaignGoal.model_validate(zbc_goal(never_say=list(phrases)))
    return rulebook_writer.draft(goal, registry, TODAY).model_copy(update={"status": RulebookStatus.LIVE, "live_at": NOW})


def _review(registry, rb, **fields):
    from zbc import clip_review

    sub = clip_review.ClipSubmission.model_validate(zbc_clip(f"w12_{next(_n)}", **fields))
    return clip_review.review(sub, rb, registry, NOW)


# the round-11 hand alphabet of symbols the gate cannot read (review11/probes/ns_ruleA11.py; g and y are
# readable there, so they stay letters)
U = dict(a="⍺", b="␢", c="⊂", d="◗", e="⋿", f="∱", h="⊩", i="⍳", j="⌡", k="⋊", l="∟", m="⩋", n="⋂", o="○", p="⍴",
         q="⊕", r="℞", s="∫", t="⟙", u="⋃", v="∇", w="⍵", x="⨉", z="≥")


def _spaced(p: str, sep: str = " ", wsep: str = "  ", table: dict = U) -> str:
    return wsep.join(sep.join(table.get(c, c) for c in w) for w in p.split())


# =====================================================================================
# N11-1 — spaced-out symbols
# =====================================================================================

N111_CASES = ["⩋ ⍺ ⋊ ⋿  ⩋ ○ ⋂ ⋿ y", "⋂ ○  ℞ ⍳ ∫ ⋊", "₥ ₳ ₭ €  ₥ ○ ₦ € ¥", "≥ ⋿ ℞ ○  ∱ ⋿ ⋿ ∫", "╫ ○ ┳",
              "━━━".join("⩋⍺⋊⋿") + "━━━" + "━━━".join("⩋○⋂⋿") + "y"]


def test_n111_round11_spaced_symbols_are_never_a_pass(registry):
    from shared.text import unreadable_words

    rb = _rulebook(registry)
    for t in N111_CASES:
        assert unreadable_words(t), t
        d = _review(registry, rb, caption=t + TAIL)
        assert d.outcome != "pass", (t, d.human_review_reasons)


@pytest.mark.parametrize("sep", [" ", " ", " ", "　", " ​", "​ ​", "━━━", " ━━━ ", " 1 ", "1 ",
                                 " . ", "·", " 🔥 "])
def test_n111_every_phrase_spaced_with_any_separator(registry, sep):
    """Spaces, thin / hair / ideographic spaces, zero-width spaces, dividers, digits, punctuation and emoji
    between the symbols do not break the run: every phrase, one symbol per letter, goes to a human."""
    rb = _rulebook(registry)
    missed = []
    for p in NS:
        t = _spaced(p, sep, sep + sep)
        d = _review(registry, rb, caption=t + TAIL)
        if d.outcome == "pass":
            missed.append((p, t))
    assert not missed, missed


def test_n111_a_spaced_exact_table_reading_is_a_rejection_like_spaced_ascii(registry):
    """(b): the currency / math reading goes through the main gate's collapse / windowed matching: read
    through the table, a spaced phrase rejects exactly when the same letters in ASCII do."""
    rb = _rulebook(registry, NS + ["no credit check"])
    cm = {"m": "₥", "a": "₳", "k": "₭", "e": "€", "o": "⊙", "n": "₦", "y": "¥", "d": "₫", "b": "฿", "t": "₮", "f": "₣",
          "r": "®", "c": "¢", "h": "♄", "i": "¡"}
    outcomes = []
    for p in ["make money", "debt free", "no credit check"]:
        for sep, wsep in [(" ", "  "), (" ", " "), ("​", " "), ("━━━", "━━━"), (" ", " ")]:
            ascii_t = wsep.join(sep.join(w) for w in p.split())
            sym_t = _spaced(p, sep, wsep, cm)
            a = _review(registry, rb, caption=ascii_t + TAIL)
            s = _review(registry, rb, caption=sym_t + TAIL)
            outcomes.append((p, repr(sep), a.outcome, s.outcome))
            if a.outcome == "reject":
                assert s.outcome == "reject", (sym_t, s.outcome, s.human_review_reasons[:2])
                assert any("currency / math" in b.reason for b in s.broken_rules), s.broken_rules
            else:
                assert s.outcome != "pass", (sym_t, s.outcome)
    print("\nN11-1 (b) spaced ASCII vs spaced table symbols:", outcomes)
    assert sum(1 for x in outcomes if x[2] == "reject") >= 10, outcomes


def test_n111_single_symbols_in_ordinary_text_stay_clean(registry):
    """A repeated arrow, one symbol between words, two symbols in a row: not a run."""
    rb = _rulebook(registry)
    for t in ["Swipe → → → for more", "Mix ⇒ bake ⇒ eat", "Link → in bio", "Friends ∞ forever", "Ages ≥18 only",
              # fix wave 13 (N12-1): "Set theory night: ∅ ⊂ A ⊆ B" is now an accepted cost (test_fix_wave_13)
              "Vegan ✓ Gluten ✓", "☆ New video ☆", "✿ spring collection ✿",
              "√2 ≈ 1.414 and √3 ≈ 1.732", "✦ • ✦ • ✦ gallery night", "Rent in ₴ vs € — a comparison thread",
              "Price drop: $49.99 → $39.99", "Score: 3 ≥ 2 ≥ 1 wins", "►► skip to 2:10", "Cost tiers € / €€ / €€€ explained"]:
        d = _review(registry, rb, caption=t + TAIL)
        assert d.outcome == "pass", (t, d.human_review_reasons)


# =====================================================================================
# N11-4 — the price exemption applies to at most two prices in a row
# =====================================================================================

def test_n114_a_row_of_prices_spelling_a_phrase_goes_to_a_human(registry):
    from shared.text import unreadable_words

    rb = _rulebook(registry)
    for t in ["₥1 ₳1 ₭1 €1  ₥1 01 ₦1 €1 ¥1", "₥0 ₳0 ₭0 €0  ₥0 00 ₦0 €0 ¥0", "0₥ 0₳ 0₭ 0€", "₦0 ⊙0  ℞1 ¡1 $1 ₭1",
              "₫1 €1 ฿1 ₮1  ₣1 ®1 €1 €1"]:
        assert unreadable_words(t), t
        d = _review(registry, rb, caption=t + TAIL)
        assert d.outcome != "pass", (t, d.human_review_reasons)


def test_n114_one_or_two_prices_are_still_prices(registry):
    rb = _rulebook(registry)
    for t in ["Kits from ₹499 only", "We raised €1.5m this year", "Tickets 12,50€ at the door", "Tip jar ₿0.01 welcome",
              "Coffee ₽250, pastry ₽180", "Now ₹499 ₹399 only", "€5 or ₹450 at the door", "Tickets ₹499 early bird, ₹799 at the door"]:
        d = _review(registry, rb, caption=t + TAIL)
        assert d.outcome == "pass", (t, d.human_review_reasons)


# =====================================================================================
# N11-2 — a phrase over three fields; a phrase at the inner side of a field's edge
# =====================================================================================

FIELDS = ["caption", "on_screen_text", "transcript", "account_bio"]
NS3 = ["work from home", "quit your job", "lose weight fast", "beat the market"]


def _three_field_case(p: str, perm: tuple[str, str, str]) -> dict:
    """review11/probes/ns_3fields11.py: word 1 ends field perm[0], word 2 is field perm[1], word 3 opens perm[2]."""
    w = p.split()
    f = {perm[0]: "Daily vlogs " + w[0], perm[1]: w[1], perm[2]: w[2] + " tonight"}
    f["caption"] = f.get("caption", "hello") + TAIL
    if "transcript" in f:
        f["transcript"] += " This budget myth costs you. Listen on Pod Plus."
    return f


def _at_edges(f: dict, p: str, perm) -> bool:
    """The three words sit where a reading across the fields puts them side by side: word 1 is the LAST
    word of its field, word 2 its whole field, word 3 the FIRST word of its field."""
    w = p.split()
    return (f[perm[0]].split()[-1] == w[0] and f[perm[1]].strip() == w[1] and f[perm[2]].split()[0] == w[2])


def test_n112_a_phrase_one_word_per_field_over_three_fields_goes_to_a_human(registry):
    rb = _rulebook(registry, NS3)
    edge_missed, other = [], []
    for p in NS3:
        for perm in itertools.permutations(FIELDS, 3):
            f = _three_field_case(p, perm)
            d = _review(registry, rb, **f)
            if _at_edges(f, p, perm):
                if d.outcome == "pass":
                    edge_missed.append((p, f))
            else:
                other.append(d.outcome)
    print(f"\nN11-2 three-field spreads at the field edges: {sum(1 for p in NS3 for perm in itertools.permutations(FIELDS, 3) if _at_edges(_three_field_case(p, perm), p, perm))} "
          f"cases, {len(edge_missed)} passed; the others (a word followed or preceded by more text in its own field): "
          f"{ {o: other.count(o) for o in set(other)} }")
    assert not edge_missed, edge_missed
    # the round-11 example
    d = _review(registry, rb, account_bio="Daily vlogs work", on_screen_text="from", caption="home tonight" + TAIL)
    assert d.outcome == "human_review" and any("'work from home'" in r for r in d.human_review_reasons), d.human_review_reasons


def test_n112_a_short_field_beside_the_inner_side_of_another_fields_edge(registry):
    """Fields have no reading order: a short field ("money") can be read right after the opening words of
    another field ("make ..."), or right before its closing words — as the regional reading already does."""
    rb = _rulebook(registry, NS)
    for f in [dict(caption="make" + TAIL, on_screen_text="money"),
              dict(caption="Get this" + TAIL, on_screen_text="rich"),
              dict(on_screen_text="work", account_bio="Daily vlogs from home"),
              dict(on_screen_text="quit", account_bio="Daily vlogs your job"),
              dict(caption="lose weight" + TAIL, account_bio="fast")]:
        d = _review(registry, rb, **f)
        assert d.outcome == "human_review" and any("never-say" in r for r in d.human_review_reasons), (f, d.outcome, d.human_review_reasons)


# =====================================================================================
# N11-3 — Latin letters mixed with emoji-listed symbols / vertical bars inside a word
# =====================================================================================

def test_n113_letters_mixed_with_letter_shaped_emoji_and_bars_go_to_a_human(registry):
    rb = _rulebook(registry)
    for t in ["♏e︱✝ be︱︱y fa✝", "be︱︱y", "♏e︱✝ be｜｜y fa✝", "wi✝h♏e", "ge✝ ri✝✝ch"]:
        d = _review(registry, rb, caption=t + TAIL)
        assert d.outcome != "pass", (t, d.human_review_reasons)


def test_n113_ordinary_emoji_and_bars_stay_clean(registry):
    rb = _rulebook(registry)
    for t in ["I ❤️ NY", "✨glow✨ up", "Tuesday │ Wednesday │ Thursday", "✝ Sunday mass at 10",
              "♏ season starts today", "✅ done ❌ not done", "🔥hot🔥 deals"]:
        d = _review(registry, rb, caption=t + TAIL)
        assert d.outcome == "pass", (t, d.human_review_reasons)


# =====================================================================================
# N11-5 — the never-say caps count what the gate scans
# =====================================================================================

def test_n115_caps_count_the_nfkc_casefolded_length():
    from zbc.rulebook_writer import MAX_NEVER_SAY_CHARS, MAX_NEVER_SAY_PHRASE_CHARS, never_say_over_caps

    # 8 raw characters, 32 once NFKC-normalised ("㎉" is "kcal"): over the per-phrase cap
    assert never_say_over_caps(["㎉" * 8]), never_say_over_caps(["㎉" * 8])
    assert never_say_over_caps(["ⅷⅷⅷ ⅷⅷⅷ ⅷⅷⅷ ⅷ"])  # 15 raw, 43 scanned
    # a list of 290 raw characters that reads as ~1,100
    phrases = [" ".join("㎉ⅷⅧ"[(i + k) % 3] * 9 for k in range(3))[:MAX_NEVER_SAY_PHRASE_CHARS] for i in range(10)]
    assert sum(len(p) for p in phrases) <= MAX_NEVER_SAY_CHARS
    over = never_say_over_caps(phrases)
    assert over and any("total" in w for w in over), over
    # ASCII is unchanged; "ß" casefolds to "ss" (what the gate scans)
    assert not never_say_over_caps(["a" * MAX_NEVER_SAY_PHRASE_CHARS])
    assert never_say_over_caps(["ß" * 16])


@pytest.mark.parametrize("path", ["draft", "revision", "edit"])
def test_n115_an_expanding_phrase_is_refused_at_approval(api, path):
    from flows import C, ok, zbc_live, zbc_rights_on_file

    phrases = ["guaranteed returns", "㎉㎉㎉ ⅷⅷⅷ ㎉㎉㎉"]  # 15 raw characters, 39 scanned
    goal = zbc_goal(never_say=phrases)
    if path == "draft":
        zbc_rights_on_file(api)
        r = api.post(f"{C}/rulebooks", {"actor_id": "zbc_rulebook_writer", "goal": goal})
    elif path == "revision":
        zbc_live(api)
        r = api.post(f"{C}/revisions", {"actor_id": "zbc_rulebook_writer", "goal": goal})
    else:
        zbc_rights_on_file(api)
        ok(api.post(f"{C}/rulebooks", {"actor_id": "zbc_rulebook_writer", "goal": zbc_goal()}), 201)
        r = api.put(f"{C}/rulebooks/1", {"actor_id": "zbc_rulebook_writer", "goal": goal})
    assert r.status_code in (200, 201), r.text
    v = r.json()["version"]
    assert any("never-say" in w and "long" in w for w in r.json()["warnings"]), r.json()["warnings"]
    rv = api.post(f"{C}/rulebooks/{v}/review", {"actor_id": "zbc_campaign_rulebook"})
    assert rv.status_code == 422, (rv.status_code, rv.text[:300])
