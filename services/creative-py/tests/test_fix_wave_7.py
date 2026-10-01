"""
Fix wave 7 (Sep 24, 2026) — AEGIS round-6 findings against creative-py.

NEW-1  the single 4,096 JSON-member cap of fix wave 6 refused a legal
       Moment Map (the model allows 2,000 segments = 10,003 members; 820
       segments -> 422). Now every route's cap is computed from its request
       model (shared/request_limits.py) and can never be tighter than it.
NEW-2  vowel-drop respellings ("mk mny", "grnteed rtrns") auto-passed.
NEW-3  homophone / phonetic respellings ("phree money", "get ritch")
       auto-passed, and the per-word share rule of fix wave 6 REDUCED the
       match: two edits in one short word ("get rchi", "make munny") passed
       although the phrase budget allowed them.
       Two new signals (consonant skeleton, phonetic key) each route to
       human_review on their own; the share rule is now a letter-share
       (>= 60% of the word's letters), never a hard per-word cap.
NEW-7  `account_bio` was stored and never scanned. Every text field is
       scanned by every prohibiting rule (never-say, obfuscation, mixed
       symbol words, non-Latin letters); must-say and the angle keywords
       look at the clip's own text, disclosure at the caption.

Miss rates are reported for the AEGIS round-6 generators (ns_evade6.py
classes A/B/C/E verbatim, the vowel-drop sweep of ns_vowel6, the
two-edits-in-one-word sweep of ns_share6.py) and for this wave's own
generator that combines the classes; the false-positive rate is measured on
all three corpora (corpus6.py verbatim, the round-5 AEGIS corpus, the
implementer corpus).
"""

from __future__ import annotations

import itertools
import json
import random
import string
import time
import typing

import pytest
from conftest import NOW
from flows import C, ok, zbc_live, zbc_open
from ordinary_captions import CAPTIONS as IMPL_CAPTIONS
from samples import TODAY, zbc_clip, zbc_goal
from test_fix_wave_6 import AEGIS_CAPTIONS as AEGIS5_CAPTIONS

TAIL = " This budget myth. Listen on Pod Plus. #ad"

# The AEGIS round-6 phrase list (probes/corpus6.py, verbatim).
NEVER_SAY = [
    "guaranteed returns", "get rich", "make money", "risk free", "double your money", "no risk",
    "miracle cure", "passive income", "financial freedom", "overnight success", "lose weight fast",
    "clinically proven", "doctor recommended", "free money", "get rich quick", "cure",
]
MULTI = [p for p in NEVER_SAY if " " in p]

# AEGIS round-6 evasion classes, VERBATIM from probes/ns_evade6.py.
AEGIS_A = ["gte rich", "get rcih", "get irch", "gte rcih", "amke money", "make mnoey", "make moeny", "make mnoye",
           "gauranteed returns", "guaranteed retruns", "guaranteed rteurns", "gauranteed rteurns",
           "risk fere", "rsik free", "rsik fere", "passvie income", "passive incoem", "passvie incoem",
           "finacnial freedom", "financial freeodm", "mriacle cure", "miracle crue", "free mnoey", "fere money",
           "lsoe weight fast", "lose wieght fast", "lose weight fsat", "dobule your money", "double yuor mnoey"]
AEGIS_B = ["gt rch", "get rch", "gt rich", "mk mny", "make mny", "mk money", "grnteed rtrns", "guarnteed returns",
           "guaranteed rtrns", "rsk free", "risk fr", "pssve incme", "passive incm", "fnancial frdm", "financial frdm",
           "fre mny", "free mny", "mrcl cure", "miracle cr", "n rsk", "no rsk", "dbl yr mny", "double yr money",
           "lose wght fst", "overnight sccss", "ovrnght success"]
AEGIS_C = ["get ritch", "get riche", "get rytch", "get reech", "get ritsch", "make munny", "make mony", "make monie",
           "make munnie", "mayk money", "maik munny", "gauranteed returnz", "garanteed returns", "garunteed returnz",
           "guaranteed reeturns", "risk phree", "risc free", "risk freee", "passiv incum", "passive inkum",
           "fynancial freedum", "financhal freedom", "mirakle cure", "miracle kyoor", "free munny", "phree money",
           "kno risk", "no risque", "dubble your munny", "loose weight fast", "luze wait fast", "overnite success",
           "clinicly proven", "klinically pruven", "doctor recomended", "docter recommended"]
AEGIS_E = ["get rchi", "get rihc", "get reech", "get rtich", "get rickh", "get richhh", "get rrich", "geet rich", "gett rich",
           "make munny", "make mnoye", "make monney", "make mooney", "make muney", "make monei", "make mony",
           "risk fere", "risk frree", "risk ffree", "rsk fre", "free munny", "free monney", "no rsik", "noo risk",
           "passive incmoe", "passive inocme", "passive incme", "passive inncome", "miracle cuer", "miracle curre",
           "get rich quikc", "get rich qick", "lose weight fsta", "lose wieght fsat"]
# The round-6 misses (MISS(pass) lines of logs/ns_evade6.txt) in classes A/B/C/E: 37 lines, 34 distinct.
AEGIS_MISSES = ["make mnoye", "mk mny", "make mny", "mk money", "grnteed rtrns", "pssve incme", "passive incm",
                "fnancial frdm", "financial frdm", "fre mny", "free mny", "miracle cr", "n rsk", "dbl yr mny",
                "double yr money", "lose wght fst", "overnight sccss", "get rytch", "get ritsch", "make munny",
                "make monie", "make munnie", "mayk money", "maik munny", "passiv incum", "passive inkum",
                "miracle kyoor", "free munny", "phree money", "no risque", "dubble your munny", "luze wait fast",
                "get rchi", "lose weight fsta"]
# The AEGIS round-6 bio cases (ns_evade6.py class H and live_creative6.py): a never-say
# phrase in the bio alone, or spread over the caption and the bio.
AEGIS_BIO = [
    ({"caption": "hello", "account_bio": "get rich"}, "reject"),
    ({"caption": "hello", "account_bio": "guaranteed returns with my link"}, "reject"),
    ({"caption": "hello", "account_bio": "GET RICH QUICK: link in bio"}, "reject"),
    ({"caption": "hello", "account_bio": "GET RICH with my link. guaranteed returns!"}, "reject"),
    ({"caption": "get", "account_bio": "rich"}, "human_review"),        # spread over two fields: a human's call
    ({"caption": "rich", "on_screen_text": "get"}, "human_review"),      # ... in either order
    ({"on_screen_text": "get", "account_bio": "rich"}, "human_review"),
    # fix wave 8 (AEGIS round 7, N7-5): a phrase spread over two fields is judged by EVERY signal, so a
    # respelled half in each field is a human's call (was "pass": only the exact words counted)
    ({"caption": "make", "account_bio": "mny"}, "human_review"),
    # ... but a joint is only read when the tail holds something close to the phrase's first word: "clips
    # daily" has nothing close to "clinically", so the stream's 3-edit window "clips daily proven" is not
    # a spread (still "pass", as ADR 0005 decision 27 wanted)
    ({"caption": "Proven by 3 years of daily use, not a lab", "account_bio": "Clips daily"}, "pass"),
    ({"caption": "hello", "account_bio": "mk mny with my link"}, "human_review"),
    ({"caption": "hello", "account_bio": "Guarantеed returns"}, "reject"),        # Cyrillic е: folded, exact
    ({"caption": "hello", "account_bio": "make rnoney"}, "reject"),                    # rn for m: reads as the phrase
    ({"caption": "hello", "account_bio": "g u a r a n t e e d  r e t u r n s"}, "reject"),  # reads as it (wave 6)
    ({"caption": "hello", "account_bio": "return$ guaranteed"}, "human_review"),       # mixed symbol word
    ({"caption": "hello", "account_bio": "Приветствую! clips daily"}, "human_review"),  # non-Latin letters
    ({"caption": "hello", "account_bio": "clips​ daily"}, "human_review"),         # invisible inside a word
]


# The AEGIS round-6 corpus (probes/corpus6.py), VERBATIM (130 captions), so the test
# runs without the review directory.
CORPUS6 = [
    'Leg day is not optional. Full routine in the pinned comment #ad',
    'Get ready for the 30-day plank challenge, starts Monday',
    'New resistance bands, five levels, one bag. Link below',
    'Rich in protein, low in sugar — the bar I actually eat after lifting',
    'Mobility first, heavy second. Save this warm-up',
    'We made the running belt smaller because you asked',
    'Rest days are training days. Sleep is the supplement',
    'Free shipping this weekend on every pair of lifters',
    'Get your form checked before you add weight',
    'Make room in your week for two easy runs',
    'Cured meats board for two, Fridays after 5',
    'Rich, dark, no bitterness — the new espresso blend is here',
    'Our cold brew now comes in a 64 oz growler',
    'Sunday brunch bookings open. Bring the whole table',
    "Made with real butter, nothing you can't pronounce",
    'The secret to crispy skin? Dry brine overnight',
    'Two new flavours: fig and black pepper, honey lavender',
    'Delivery now reaches the east side. Finally.',
    'Half-price pastries after 4pm, while they last',
    'Get the recipe card free with every order this month',
    'Ship the update, then write the changelog. We built the tool for both',
    'Dark mode is live. You asked, we listened',
    'Invoices in one click. Try it free for 14 days',
    'Your dashboard now loads twice as fast on mobile',
    'New: export to CSV without leaving the report',
    'Onboarding takes eight minutes. We timed it',
    'Security update rolling out today; no action needed',
    'The API now returns money as strings. Read why on the blog',
    'Beta testers wanted for the scheduling feature. DM us',
    'Migrated 40,000 accounts overnight with zero downtime',
    'Three beds, one garden, ten minutes to the station. Open house Saturday',
    'Refinanced? Make sure your insurance still matches',
    'This kitchen was a budget renovation. Swipe for the before',
    'Rent or buy? Our free calculator lays out both',
    'New listing in the Heights: brick, light, quiet street',
    'Staging tip: fewer things, bigger rooms',
    "We sold this one in nine days. Here's what worked",
    'Get pre-approved before you fall in love with a house',
    "Property taxes went up. Here's what that means for your payment",
    'Landlords: the new deposit rules start in October',
    'Shoulder season in Lisbon: fewer crowds, same light',
    'Carry-on only for ten days. The packing list is in bio',
    'We upgraded the seats on the Denver route',
    'Book direct and breakfast is on us',
    'Rich history, cheap trains, great coffee: two days in Turin',
    'Points booking guide updated for 2027',
    'The lake house sleeps eight and has a dock',
    'Overnight ferry vs early flight? We tried both',
    'Travel insurance is boring until you need it. Get covered',
    'Family rooms now bookable for the winter break',
    'Grain-free kibble, now in a 20 lb bag',
    'Our vet-formulated treats are back in stock',
    'The harness that survived a husky. Link in bio',
    'Adoption fees waived this weekend at the shelter',
    'Slow feeder bowls for dogs who inhale dinner',
    'Kitten starter kits ship free over $40',
    'Recommended by our trainer: the long line for recall work',
    'Dental chews on subscription, cancel anytime',
    'Cats do not care about your schedule. The auto feeder does',
    'Free nail trim with any grooming booking this month',
    'The linen shirt is back in six colours',
    'Made in Portugal, worn everywhere',
    'Sizes now run XS to 4XL. About time',
    'Rich navy, gold buttons, a coat for twenty winters',
    'Wide-fit boots finally exist. You are welcome',
    'Restock alert: the cropped puffer, all sizes',
    'Sale ends midnight. No code needed',
    'Free returns for 60 days, no questions',
    'Our denim is cut for real hips',
    'Layer it, belt it, sleep in it. The oversized cardigan',
    'Cohort 12 opens enrolment Thursday. 40 seats',
    'Learn to read a balance sheet in a weekend',
    'New module: pricing your freelance work with confidence',
    'Office hours every Tuesday, recorded for the time zones',
    'The Python course is free for students with an .edu email',
    'Certificate on completion, accepted by three employers so far',
    'Quick win: the 5-minute inbox reset',
    'Homework is optional. Results are not',
    'Get feedback on your portfolio from working designers',
    'We cut the course from 12 hours to 7. Same content, less waffle',
    'The sofa arrives in a box and takes ten minutes',
    'Solid oak, no veneer, delivered assembled',
    'Rich walnut finish, now on the bookshelf too',
    'Blackout curtains that actually black out',
    'Non-toxic, fast-drying, one coat. The wall paint',
    'Overnight delivery on mattresses in the city',
    'Weighted blanket, 15 lb, washable cover',
    'Made to last: the cast iron pan with a lifetime warranty',
    'Our candles burn for 60 hours. We counted',
    'Free assembly on orders over $500 this week',
    'The 2027 model gets 40 more miles of range',
    'Winter tyres fitted while you wait',
    'Trade-in values are up. Get a quote in two minutes',
    'Detailing package: interior, exterior, engine bay',
    'Lease deals end Friday. No hidden fees',
    'Free brake check with any oil change',
    'Rich leather, quiet cabin, the new sedan reviewed',
    'EV charging now at all four locations',
    'Your dash cam footage is admissible. Ours saves in 4K',
    'Fleet pricing available for five vehicles or more',
    'Episode 88: the founder who turned down the buyout',
    'New episode every Wednesday, early on the app',
    'We read every review. Even that one',
    'Behind the scenes of the studio move, part two',
    "Ask us anything for Friday's mailbag",
    'The interview we almost cut. Full version on YouTube',
    'Subscribe for the bonus episode on pricing',
    'Rich audio, no ads, on the premium tier',
    'Transcripts now available for every episode',
    'Get the newsletter before the podcast drops',
    'Budget spreadsheet template, free to download',
    'Investing involves risk. Read the prospectus first',
    'Rates changed. Here is what a 0.25 move does to a mortgage',
    'Tax season checklist for freelancers, updated',
    'Emergency fund first, then everything else',
    'The card has no annual fee and a 2 percent rebate',
    'Money market vs high-yield savings, explained plainly',
    'Retirement contributions: the deadline is April 15',
    'A fee-free account for students, no minimum balance',
    'Our advisers are salaried, not commissioned',
    'Doors 7, band 8, curfew 11. See you there',
    'Volunteers needed for the river clean-up Saturday',
    'Farmers market moves indoors from November',
    'Tickets for the winter fair go on sale tomorrow at noon',
    'Photo booth included with every wedding package',
    "Made a mistake on your order? Reply and we'll fix it",
    'The quick fix for a squeaky door: paraffin wax',
    'Get well soon cards, hand printed, sold in packs of six',
    'Free parking after 6 for restaurant guests',
    'Pure maple syrup, tapped in March, bottled in April',
]


# --- helpers -----------------------------------------------------------------------------

def _rulebook(registry, phrases=NEVER_SAY):
    from zbc import rulebook_writer
    from zbc.rulebook import RulebookStatus
    from zbc.rulebook_writer import CampaignGoal

    goal = CampaignGoal.model_validate(zbc_goal(never_say=list(phrases)))
    return rulebook_writer.draft(goal, registry, TODAY).model_copy(update={"status": RulebookStatus.LIVE, "live_at": NOW})


_n = itertools.count()


def _review(registry, rb, text=None, **fields):
    from zbc import clip_review

    base = zbc_clip(f"w7_{next(_n)}")
    if text is not None:
        base["caption"] = text + TAIL
    for k, v in fields.items():
        # the other rules stay satisfied: disclosure in the caption, must-say + keywords in the transcript
        base[k] = v + {"caption": " #ad", "transcript": " This budget myth. Listen on Pod Plus."}.get(k, "")
    return clip_review.review(clip_review.ClipSubmission.model_validate(base), rb, registry, NOW)


def _misses(registry, rb, texts):
    out = []
    for t in texts:
        d = _review(registry, rb, t)
        if d.outcome == "pass":
            out.append(t)
    return out


# =====================================================================================
# NEW-2 / NEW-3 — vowel-drop, phonetic and two-edits-in-one-word respellings
# =====================================================================================

def test_new2_new3_every_aegis_round6_miss_is_now_caught(registry):
    """The 34 distinct A/B/C/E captions that auto-passed in round 6
    (logs/ns_evade6.txt): none passes; none is REJECTED either (a
    respelling by sound or skeleton is a human's call)."""
    assert len(set(AEGIS_MISSES)) == 34
    rb = _rulebook(registry)
    for t in AEGIS_MISSES:
        d = _review(registry, rb, t)
        assert d.outcome == "human_review", (t, d.outcome, d.broken_rules, d.human_review_reasons)


@pytest.mark.parametrize("name,cases", [("A", AEGIS_A), ("B", AEGIS_B), ("C", AEGIS_C), ("E", AEGIS_E)])
def test_new2_new3_aegis_round6_classes_have_zero_misses(registry, name, cases):
    rb = _rulebook(registry)
    miss = _misses(registry, rb, cases)
    print(f"\nNEW-2/3 AEGIS ns_evade6 class {name}: {len(miss)}/{len(cases)} miss {miss}")
    assert not miss, miss


def test_new2_new3_the_share_rule_never_reduces_a_match(registry):
    """NEW-3: two edits in one short word, the others exact, must be a
    human's call when the phrase's budget allows two edits (it did:
    "get rich" is 7 letters, budget 2) — and the pieces that lost the
    word ("more" keeps 3 of "money"'s 5 letters = 60%, in; "for" keeps
    2 of "free"'s 4 = 50%, out)."""
    from shared.text import WORD_SHARE, letter_share, visual_near_miss

    assert WORD_SHARE == 0.6
    assert visual_near_miss("get rchi", "get rich") == (2, False, "get rchi")
    assert visual_near_miss("make munny", "make money") == (2, False, "make munny")
    assert letter_share("munny", "money") == 0.6 and letter_share("for", "free") == 0.5
    assert visual_near_miss("for money", "free money") is None       # "for" has lost "free"
    assert visual_near_miss("three money", "free money") is None
    assert visual_near_miss("make more", "make money") == (2, False, "make more")  # 60%: a human's call
    rb = _rulebook(registry)
    for t in ("get rchi", "make munny", "make more"):
        assert _review(registry, rb, t).outcome == "human_review", t
    for t in ("for money reasons we moved", "three money habits", "from one oven to three"):
        assert _review(registry, rb, t).outcome == "pass", t


def test_new2_consonant_skeleton_and_budget():
    from shared.text import consonant_skeleton, skeleton_budget, skeleton_near_miss

    assert consonant_skeleton("money") == "mn" and consonant_skeleton("income") == "incm"
    assert consonant_skeleton("success") == "scs" and consonant_skeleton("your") == "yr"
    assert [skeleton_budget(n) for n in (1, 3, 4, 6, 7, 12)] == [0, 0, 1, 1, 2, 2]
    assert skeleton_near_miss("mk mny", "make money") == (0, "mk mny")
    assert skeleton_near_miss("grnteed rtrns", "guaranteed returns") == (0, "grnteed rtrns")
    assert skeleton_near_miss("get rch", "get rich") == (0, "get rch")
    assert skeleton_near_miss("gt rch qck", "get rich quick") == (0, "gt rch qck")
    assert skeleton_near_miss("rsk fre", "risk free") == (0, "rsk fre")
    # the guards: a function word that the phrase lacks; a consonant difference in a fully
    # vowelled word; a window short of a whole word
    for text, phrase in (("for many", "free money"), ("make my day", "make money"), ("risk for you", "risk free"),
                         ("no rush", "no risk"), ("form check", "free money"), ("overnight oats", "overnight success"),
                         ("double your veggies", "double your money"), ("doctors recommend", "doctor recommended"),
                         ("a nurse on night shift", "no risk")):
        assert skeleton_near_miss(text, phrase) is None, (text, phrase)
    # N3: a short entry is exact-only unless opted in
    assert skeleton_near_miss("car wash", "cure") is None
    assert skeleton_near_miss("cr it", "cure", fuzzy=True) == (0, "cr")


def test_new3_phonetic_key_equivalences():
    from shared.text import phonetic_key, phonetic_near_miss

    same = [("free", "phree"), ("rich", "ritch"), ("rich", "rytch"), ("rich", "reech"), ("rich", "riche"),
            ("rich", "ritsch"), ("money", "munny"), ("money", "monie"), ("money", "munnie"), ("make", "mayk"),
            ("make", "maik"), ("returns", "returnz"), ("returns", "reeturns"), ("guaranteed", "gauranteed"),
            ("guaranteed", "garunteed"), ("risk", "risc"), ("risk", "risque"), ("no", "kno"), ("passive", "passiv"),
            ("income", "incum"), ("income", "inkum"), ("financial", "fynancial"), ("financial", "financhal"),
            ("freedom", "freedum"), ("miracle", "mirakle"), ("cure", "kyoor"), ("double", "dubble"),
            ("your", "yuor"), ("lose", "loose"), ("lose", "luze"), ("weight", "wait"), ("weight", "wieght"),
            ("overnight", "overnite"), ("success", "sccss"), ("clinically", "clinicly"), ("clinically", "klinically"),
            ("proven", "pruven"), ("doctor", "docter"), ("recommended", "recomended"), ("get", "gt"),
            ("write", "rite"), ("knight", "night"), ("phone", "fone"), ("judge", "juj")]
    for a, b in same:
        assert phonetic_key(a) == phonetic_key(b), (a, b, phonetic_key(a), phonetic_key(b))
    assert phonetic_key("free") == "FR" and phonetic_key("rich") == "RX" and phonetic_key("money") == "MN"
    assert phonetic_near_miss("phree money", "free money") == "phree money"
    assert phonetic_near_miss("luze wait fast", "lose weight fast") == "luze wait fast"
    assert phonetic_near_miss("kno risque", "no risk") == "kno risque"
    assert phonetic_near_miss("get so ritch", "get rich") == "get so ritch"      # adjacency policy
    assert phonetic_near_miss("get very very very ritch", "get rich") is None      # beyond it
    assert phonetic_near_miss("get rich", "get rich") is None                      # exact: not a respelling
    assert phonetic_near_miss("for many", "free money") is None                    # function word
    assert phonetic_near_miss("car wash", "cure") is None and phonetic_near_miss("kar", "cure", fuzzy=True) == "kar"


def test_new3_first_key_letter_of_a_run_is_fixed_by_its_first_four_letters():
    """The pruning claim of the run-of-tokens pass, brute-forced: for any
    token of 4+ letters, the key of the token plus anything after it starts
    with the first key letter of its first four letters."""
    from shared.text import phonetic_key

    rng = random.Random(1)
    for _ in range(100_000):
        t = "".join(rng.choice(string.ascii_lowercase) for _ in range(rng.randint(4, 7)))
        u = "".join(rng.choice(string.ascii_lowercase) for _ in range(rng.randint(0, 5)))
        k, h = phonetic_key(t + u), phonetic_key(t[:4])[:1]
        assert not (k and h) or k[0] == h, (t, u, k, h)
    assert phonetic_near_miss_split_ok()


def phonetic_near_miss_split_ok():
    from shared.text import phonetic_near_miss

    return all(phonetic_near_miss(t, p) for t, p in (("rizkphree", "risk free"), ("phree m oney", "free money"),
                                                     ("risk ph rree", "risk free"), ("lose way t fast", "lose weight fast"),
                                                     ("phinncialfreedom", "financial freedom"), ("x knorisk y", "no risk")))


def test_new2_new3_each_signal_routes_to_human_review_on_its_own(registry):
    from shared.text import near_miss, phonetic_near_miss, skeleton_near_miss, visual_near_miss

    rb = _rulebook(registry)
    # skeleton only ("fnncl" has no soft c, so its key is not "financial"'s; 7 letters from it visually)
    assert skeleton_near_miss("fnncl frdm", "financial freedom") == (0, "fnncl frdm")
    assert phonetic_near_miss("fnncl frdm", "financial freedom") is None
    assert visual_near_miss("fnncl frdm", "financial freedom") is None
    assert "vowels dropped" in near_miss("fnncl frdm", "financial freedom")
    # phonetic only
    assert phonetic_near_miss("phree money", "free money") and not skeleton_near_miss("phree money", "free money")
    assert visual_near_miss("phree money", "free money") is None
    assert "sounds like" in near_miss("phree money", "free money")
    for t in ("fnncl frdm", "phree money"):
        d = _review(registry, rb, t)
        assert d.outcome == "human_review" and not d.broken_rules, (t, d)
        assert any("NS-" in r and "possible never-say" in r for r in d.human_review_reasons), d.human_review_reasons


# --- generators --------------------------------------------------------------------------

V = set("aeiou")


def _drop_all_vowels(w):
    return w[0] + "".join(c for c in w[1:] if c not in V)


def gen_vowel_drops(phrases):
    """ns_vowel6: every word vowel-dropped; each single word vowel-dropped."""
    out = []
    for p in phrases:
        ws = p.split()
        out.append(" ".join(_drop_all_vowels(w) for w in ws))
        for i, w in enumerate(ws):
            out.append(" ".join(ws[:i] + [_drop_all_vowels(w)] + ws[i + 1:]))
    return [t for t in out if t not in phrases]


def gen_two_edits_one_word(phrases, rng, per_word=12):
    """ns_share6.py: exactly two readable edits in ONE word (drop two vowels;
    drop a vowel + double a consonant; swap a vowel + double a consonant),
    the other words exact, for words of up to 6 letters."""
    from shared.text import visual_budget

    def variants(w):
        outs = set()
        vi = [i for i, c in enumerate(w) if c in V]
        ci = [i for i, c in enumerate(w) if c not in V]
        for a, b in itertools.combinations(vi, 2):
            outs.add(w[:a] + w[a + 1:b] + w[b + 1:])
        for a in vi:
            for c in ci:
                x = w[:a] + w[a + 1:]
                c2 = c if c < a else c - 1
                outs.add(x[:c2] + x[c2] + x[c2:])
        for a in vi:
            for sub in "ueaoi":
                if sub == w[a]:
                    continue
                x = w[:a] + sub + w[a + 1:]
                for c in ci:
                    outs.add(x[:c] + x[c] + x[c:])
        return sorted(o for o in outs if o != w and len(o) >= 2)

    out = []
    for p in phrases:
        words = p.split()
        if visual_budget(len(p.replace(" ", ""))) < 2:
            continue
        for wi, w in enumerate(words):
            if len(w) > 6:
                continue
            vs = variants(w)
            rng.shuffle(vs)
            for v in vs[:per_word]:
                out.append(" ".join(words[:wi] + [v] + words[wi + 1:]))
    return out


HOMOPHONE_SWAPS = [("ph", "f"), ("f", "ph"), ("ck", "k"), ("c", "k"), ("k", "c"), ("ee", "ea"), ("ee", "ie"), ("ea", "ee"),
                   ("i", "y"), ("y", "i"), ("o", "u"), ("u", "oo"), ("ou", "ow"), ("s", "z"), ("z", "s"), ("igh", "i"),
                   ("eigh", "ay"), ("tch", "ch"), ("ch", "tch"), ("qu", "kw"), ("wr", "r"), ("kn", "n")]


def gen_combined(phrases, rng, n=600, transpositions=True):
    """This wave's own generator: EVERY case combines at least two classes —
    a vowel drop, a homophone swap, a doubled letter, an adjacent
    transposition (when `transpositions`), a split into more tokens or a
    join — applied to different words (or the same word) of the phrase."""
    def vowel_drop(w):
        vi = [i for i, c in enumerate(w) if c in V and i > 0]
        if not vi:
            return w
        i = rng.choice(vi)
        return w[:i] + w[i + 1:]

    def homophone(w):
        opts = [(a, b) for a, b in HOMOPHONE_SWAPS if a in w]
        if not opts:
            return w
        a, b = rng.choice(opts)
        return w.replace(a, b, 1)

    def double(w):
        i = rng.randrange(len(w))
        return w[:i] + w[i] + w[i:]

    def transpose(w):
        if len(w) < 3:
            return w
        i = rng.randrange(len(w) - 1)
        return w[:i] + w[i + 1] + w[i] + w[i + 2:]

    def split(w):
        if len(w) < 3:
            return w
        i = rng.randrange(1, len(w))
        return w[:i] + " " + w[i:]

    ops = [vowel_drop, homophone, double, split] + ([transpose] if transpositions else [])
    out = []
    while len(out) < n:
        p = rng.choice(phrases)
        ws = p.split()
        k = rng.choice((2, 2, 3))
        picks = [rng.choice(ops) for _ in range(k)]
        targets = [rng.randrange(len(ws)) for _ in range(k)]
        for op, ti in zip(picks, targets):
            ws[ti] = op(ws[ti])
        t = " ".join(ws)
        if rng.random() < 0.15:
            t = t.replace(" ", "", 1)  # a join
        if t.replace(" ", "") != p.replace(" ", "") and t not in out:
            out.append(t)
    return out


def test_new2_vowel_drop_sweep_has_zero_misses(registry):
    """ns_vowel6: all words vowel-dropped was 12/15 passes, one word 18/33."""
    rb = _rulebook(registry)
    cases = gen_vowel_drops(MULTI)
    miss = _misses(registry, rb, cases)
    print(f"\nNEW-2 vowel-drop sweep: {len(miss)}/{len(cases)} miss {miss}")
    assert not miss, miss


def test_new3_two_edits_in_one_word_sweep(registry):
    """ns_share6.py: two readable edits in one short word (the fix-wave-6
    share rule let 3/248 through; the control of one edit in each of two
    words 2/168). Reported; the bound is the measured rate (no
    overstatement): a vowel swap plus a doubled consonant in a 2-4 letter
    word can leave a word that shares under 60% of its letters."""
    rb = _rulebook(registry)
    cases = gen_two_edits_one_word(MULTI, random.Random(6))
    miss = _misses(registry, rb, cases)
    print(f"\nNEW-3 two edits in one word: {len(miss)}/{len(cases)} = {len(miss) / len(cases):.1%} miss {miss[:40]}")
    assert len(miss) / len(cases) <= 0.02, miss


@pytest.mark.parametrize("transpositions,bound", [(False, 0.02), (True, 0.03)])
def test_new2_new3_combined_generator(registry, transpositions, bound):
    """Reported with its misses; the bounds are the measured rates (0.7%
    and 1.8%) rounded up, not a claim of completeness: a transposition
    stacked on another edit in a 2-4 letter word ("nu riks", "src free")
    is 3+ edits from the phrase and beyond the letter budget."""
    rb = _rulebook(registry)
    cases = gen_combined(MULTI, random.Random(7), transpositions=transpositions)
    miss = _misses(registry, rb, cases)
    rate = len(miss) / len(cases)
    print(f"\nNEW-2/3 combined generator ({'with' if transpositions else 'without'} transpositions): "
          f"{len(miss)}/{len(cases)} = {rate:.1%} miss {miss}")
    assert rate <= bound, miss


# --- false positives -----------------------------------------------------------------------

def _gate_fp(captions, phrases, fuzzy=()):
    from shared.text import PhraseMatch, match_phrase, near_miss, visual_lookalike_exact

    spec = [(p, p in fuzzy) for p in phrases]
    ordinary = [c for c in captions if all(match_phrase(c, p) is PhraseMatch.NONE for p in phrases)]
    flagged = []
    for c in ordinary:
        assert not any(visual_lookalike_exact(c, p, f) for p, f in spec), c
        nm = [(p, near_miss(c, p, f)) for p, f in spec if near_miss(c, p, f)]
        if nm:
            flagged.append((c, nm))
    return flagged, ordinary


@pytest.mark.parametrize("name,captions", [("corpus6", CORPUS6), ("AEGIS round-5", AEGIS5_CAPTIONS),
                                           ("implementer", IMPL_CAPTIONS)])
def test_new2_new3_never_say_gate_false_positives_at_most_3_percent(registry, name, captions):
    """The never-say gate alone (every signal: symbols, visual, adjacency,
    skeleton, phonetic), with "cure" on the list, on each corpus."""
    flagged, ordinary = _gate_fp(captions, NEVER_SAY)
    rate = len(flagged) / len(ordinary)
    print(f"\nNEW-2/3 gate FP on {name}: {len(flagged)}/{len(ordinary)} = {rate:.1%}")
    for c, nm in flagged:
        print("   ", repr(c), [(p, h[:50]) for p, h in nm])
    assert rate <= 0.03, flagged
    # and the full pipeline: nothing REJECTED by a never-say rule
    rb = _rulebook(registry)
    for c in ordinary:
        d = _review(registry, rb, c)
        assert not any(b.rule_id.startswith("NS-") for b in d.broken_rules), (c, d.broken_rules)


def test_new2_new3_signals_are_bounded_on_100kb():
    """Documented bound for the two new signals together: < 6 s per 100 KB
    for a 16-phrase list on the build machine (measured: 1.3 s worst for
    both together, on ordinary and adversarial text). The
    skeleton scan is the same bit-parallel pass as the visual gate; the
    phonetic match is one pass over the token keys plus a run-of-tokens
    pass pruned by consonant count and first key letter."""
    from shared.text import phonetic_near_miss, skeleton_near_misses

    rng = random.Random(7)
    inputs = [
        " ".join(rng.choice(IMPL_CAPTIONS) for _ in range(2500))[:100_000],
        "mk mny " * 14000, "gt rch " * 14000, "phree money " * 8000, "b " * 50000, "mn " * 33000,
        " ".join("".join(rng.choice(string.ascii_lowercase) for _ in range(rng.randint(1, 9))) for _ in range(20_000))[:100_000],
        " ".join("".join(rng.choice("mnkrsftaeiouy") for _ in range(rng.randint(1, 9))) for _ in range(20_000))[:100_000],
    ]
    phrases = tuple((p, False) for p in NEVER_SAY)
    worst = 0.0
    for s in inputs:
        t0 = time.thread_time()  # fix wave 25 (scout A C3; R-HYGIENE L1): this thread's CPU time, not the wall clock
        skeleton_near_misses(s, phrases)
        for p, f in phrases:
            phonetic_near_miss(s, p, f)
        dt = time.thread_time() - t0
        worst = max(worst, dt)
        assert dt < 6.0, (s[:20], dt)
    print(f"\nNEW-2/3 skeleton + phonetic signals, worst input: {worst:.2f}s per 100 KB, {len(phrases)} phrases")


# =====================================================================================
# NEW-7 — every text field is scanned
# =====================================================================================

def test_new7_text_fields_cover_every_free_text_field_of_the_model():
    from zbc.clip_review import CLIP_TEXT_FIELDS, TEXT_FIELDS, ClipSubmission

    free_text = [n for n, f in ClipSubmission.model_fields.items() if f.annotation is str and f.default == ""]
    assert sorted(free_text) == sorted(TEXT_FIELDS), free_text
    assert "account_bio" in TEXT_FIELDS and set(CLIP_TEXT_FIELDS) < set(TEXT_FIELDS)


@pytest.mark.parametrize("fields,want", AEGIS_BIO)
def test_new7_aegis_bio_cases(registry, fields, want):
    rb = _rulebook(registry)
    d = _review(registry, rb, **fields)
    assert d.outcome == want, (fields, d.outcome, d.broken_rules, d.human_review_reasons)
    if want == "human_review":
        assert any("account_bio" in r or "NS-" in r for r in d.human_review_reasons), d.human_review_reasons



def test_new7_every_prohibiting_rule_scans_every_field(registry):
    """Sweep: for each text field, a never-say phrase (exact -> reject), a
    lookalike (-> reject), a near miss, a mixed symbol word, an invisible
    character, a non-Latin letter -> never a pass, with the field named."""
    from zbc.clip_review import TEXT_FIELDS

    rb = _rulebook(registry)
    for field in TEXT_FIELDS:
        assert _review(registry, rb, **{field: "get rich"}).outcome == "reject", field
        assert _review(registry, rb, **{field: "make rnoney"}).outcome == "reject", field
        for text, needle in (("get rlch", "NS-"), ("return$ soon", f"symbols/digits in {field}"),
                             ("cl­ips daily", f"obfuscation in {field}"), ("Привет clips", f"non-Latin letter(s) in {field}")):
            d = _review(registry, rb, **{field: text})
            assert d.outcome == "human_review", (field, text, d.outcome)
            assert any(needle in r for r in d.human_review_reasons), (field, text, d.human_review_reasons)


def test_new7_requirements_look_where_they_live(registry):
    """Must-say and the angle keywords are about the CLIP (caption,
    on-screen text, transcript); a bio saying them does not satisfy them.
    Disclosure is in the caption (unchanged)."""
    from zbc import clip_review

    rb = _rulebook(registry)

    def judge(**over):
        sub = clip_review.ClipSubmission.model_validate(zbc_clip("w7_req", **over))
        return clip_review.review(sub, rb, registry, NOW)

    assert judge(caption="The budget myth nobody talks about #ad", on_screen_text="Myth",
                 transcript="Listen on Pod Plus.").outcome == "pass"
    d = judge(caption="The budget myth nobody talks about #ad", on_screen_text="Myth", transcript="nothing",
              account_bio="Listen on Pod Plus")
    assert d.outcome == "reject" and any(b.rule_id.startswith("MS-") for b in d.broken_rules), d
    d = judge(caption="nothing here #ad", on_screen_text="", transcript="Listen on Pod Plus.", account_bio="budget myth")
    assert d.outcome == "human_review" and any("keywords" in r for r in d.human_review_reasons), d
    d = judge(caption="The budget myth nobody talks about", on_screen_text="Myth", transcript="Listen on Pod Plus.",
              account_bio="#ad")
    assert d.outcome == "reject" and any(b.rule_id.startswith("DC-") for b in d.broken_rules), d


def test_new7_bio_through_the_api(api):
    zbc_open(api, zbc_goal(never_say=["guaranteed returns", "get rich", "make money"]))
    d = ok(api.post("/zbc/clips", zbc_clip("bio1", account_bio="GET RICH with my link. guaranteed returns!")), 201)
    assert d["outcome"] == "reject" and {b["rule_id"] for b in d["broken_rules"]} >= {"NS-01", "NS-02"}, d
    d = ok(api.post("/zbc/clips", zbc_clip("bio2", caption="get #ad", account_bio="rich")), 201)
    assert d["outcome"] == "human_review" and any("spread over" in r for r in d["human_review_reasons"]), d
    d = ok(api.post("/zbc/clips", zbc_clip("bio3", account_bio="mk mny fast")), 201)
    assert d["outcome"] == "human_review", d


# =====================================================================================
# NEW-1 — per-route JSON member caps computed from the models
# =====================================================================================

def _segments(n: int) -> dict:
    return {"source_asset_id": "src_ep42", "duration_seconds": 3600 * 3,
            "segments": [{"segment_id": f"s{i}", "start_seconds": i * 10, "end_seconds": i * 10 + 10,
                          "transcript": "budget myth" if i % 7 == 0 else "talk"} for i in range(n)]}


def test_new1_a_legal_moment_map_of_820_and_2000_segments_is_accepted(api):
    """AEGIS live_creative6.py: 800 segments -> 200, 820 -> 422
    PayloadTooManyMembers (4,103 members), 2,000 (the model's maximum,
    10,003 members) -> 422."""
    zbc_live(api)
    for n in (100, 800, 820, 1000, 2000):
        r = api.post(f"{C}/moment-map", _segments(n))
        assert r.status_code == 200, (n, r.status_code, r.text[:200])
        assert len(r.json()["moments"]) + len(r.json()["rejected"]) == n
    r = api.post(f"{C}/moment-map", _segments(2001))
    assert r.status_code == 422 and r.json()["error"] == "RequestValidationError", r.text  # the model's own limit


def _maximal_shape(tp, meta=()):
    """A JSON value with the largest member count the type admits (values
    are placeholders: the shape gate counts, it does not validate)."""
    from pydantic import BaseModel
    from shared.request_limits import _flatten, _max_len

    meta = _flatten(meta)
    origin = typing.get_origin(tp)
    if origin is typing.Annotated:
        base, *extra = typing.get_args(tp)
        return _maximal_shape(base, [*meta, *extra])
    if origin in (typing.Union, __import__("types").UnionType):
        from shared.request_limits import worst_case_json_members

        return _maximal_shape(max(typing.get_args(tp), key=lambda a: worst_case_json_members(a, meta)), meta)
    if origin in (list, tuple, set, frozenset):
        n = _max_len(meta, "")
        return [_maximal_shape(typing.get_args(tp)[0]) for _ in range(n)]
    if origin is dict:
        n = _max_len(meta, "")
        return {f"k{i}": _maximal_shape(typing.get_args(tp)[1]) for i in range(n)}
    if isinstance(tp, type) and issubclass(tp, BaseModel):
        return {name: _maximal_shape(f.annotation, f.metadata) for name, f in tp.model_fields.items()}
    return "x"


def _members(node) -> int:
    if isinstance(node, dict):
        return len(node) + sum(_members(v) for v in node.values())
    if isinstance(node, list):
        return len(node) + sum(_members(v) for v in node)
    return 0


def test_new1_every_route_accepts_its_maximal_legal_body(api):
    """For EVERY route with a JSON body: the body with the most members the
    model admits (a) counts exactly `worst_case_json_members`, (b) is
    under the route's cap by the headroom, (c) passes the shape gate (the
    middleware answers anything but PayloadTooManyMembers), and one more
    member than the cap is refused with the route's own number."""
    from api import DEFAULT_JSON_MEMBERS, json_shape_violation
    from fastapi.routing import APIRoute
    from shared.request_limits import HEADROOM, LIMIT_STEP, member_limit_for, worst_case_json_members

    routes = [r for r in api.app.routes if isinstance(r, APIRoute) and r.body_field is not None]
    assert len(routes) >= 20 and len(api.app.state.member_limits) == len(routes)
    seen = set()
    for r in routes:
        model = r.body_field.field_info.annotation
        worst = worst_case_json_members(model)
        cap = member_limit_for(model)
        body = _maximal_shape(model)
        assert _members(body) == worst, (r.path, _members(body), worst)
        assert worst * HEADROOM <= cap < worst * HEADROOM + LIMIT_STEP, (r.path, worst, cap)
        assert cap == next(c for m, _, p, c in api.app.state.member_limits if p == r.path and r.methods & m)
        assert json_shape_violation(json.dumps(body).encode(), cap) is None, r.path
        method = sorted(r.methods)[0]
        path = r.path.format(row_id="row_x", brief_id="b", job_id="j", work_id="w", campaign_id="camp_pod_01",
                             version=1, submission_id="s")
        resp = api.client.request(method, path, content=json.dumps(body).encode(),
                                  headers={"Content-Type": "application/json"})
        assert resp.json().get("error") != "PayloadTooManyMembers", (r.path, resp.status_code, resp.text[:200])
        over = {**body, **{f"extra{i}": 1 for i in range(cap - worst + 1)}}
        assert _members(over) == cap + 1
        resp = api.client.request(method, path, content=json.dumps(over).encode(),
                                  headers={"Content-Type": "application/json"})
        assert resp.status_code == 422 and resp.json()["error"] == "PayloadTooManyMembers", (r.path, resp.text[:200])
        assert str(cap) in resp.json()["detail"]
        seen.add((r.path, cap))
        print(f"\nNEW-1 {method:4s} {r.path:52s} {model.__name__:18s} worst {worst:6d} cap {cap:6d}")
    caps = dict(seen)
    assert caps["/zbc/campaigns/{campaign_id}/moment-map"] >= 10003 * HEADROOM
    assert caps["/zbc/clips"] < 4096 < caps["/zbc/campaigns/{campaign_id}/moment-map"]
    assert min(caps.values()) == DEFAULT_JSON_MEMBERS == 64


def test_new1_default_cap_for_other_paths_and_depth_cap_kept(api):
    from api import DEFAULT_JSON_MEMBERS, MAX_JSON_DEPTH

    assert MAX_JSON_DEPTH == 32
    r = api.client.post("/no/such/route", content=json.dumps({f"k{i}": 1 for i in range(DEFAULT_JSON_MEMBERS + 1)}).encode(),
                        headers={"Content-Type": "application/json"})
    assert r.status_code == 422 and r.json()["error"] == "PayloadTooManyMembers"
    r = api.client.post("/no/such/route", content=json.dumps({f"k{i}": 1 for i in range(DEFAULT_JSON_MEMBERS)}).encode(),
                        headers={"Content-Type": "application/json"})
    assert r.status_code == 404
    r = api.client.post(f"{C}/moment-map", content=b'{"segments": ' + b"[" * 40 + b"]" * 40 + b"}",
                        headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["error"] == "PayloadTooDeep"


def test_new1_a_request_model_without_a_bound_cannot_be_added():
    from pydantic import BaseModel
    from shared.request_limits import UnboundedField, worst_case_json_members

    class Bad(BaseModel):
        items: list[str]

    class BadDict(BaseModel):
        m: dict[str, int]

    for m in (Bad, BadDict):
        with pytest.raises(UnboundedField):
            worst_case_json_members(m)


