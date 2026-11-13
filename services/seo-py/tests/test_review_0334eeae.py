"""The independent scoped review of commit 0334eeae (the fix round for the review of cbdf5e78), closed.

MUST A  glued multi-word script labels were no longer read: the Arabic "password" and "secret word" labels written
        with their two words glued (with a tatweel, with an invisible joiner), and the Greek label glued; ffd28ca8
        and cbdf5e78 read them (the glued Arabic natural spellings had been removed from
        ``tests/test_review_ffd28ca8.py``: restored there). The cause: the multi-word entries were not declared glued
        and the separator between their words was mandatory; declaring them glued on the word view (hamza, madda
        and accents folded) re-read the ordinary Arabic phrases of review cbdf5e78 S-2.
SHOULD 1 behavioural mutants survived the review tests: each rule is pinned below by a minimal positive and negative.
SHOULD 2 the collision tests used snapshots only: a test now DERIVES the boundary joins from the label entries
        themselves against a bundled ordinary-word sample per script (the snapshots stay).
SHOULD 3 the ADR wording of residual (g); NIT 1 a Latin-form shortcut that was not output-identical; NIT 2 test-only
        code in the production module.

The cause, fixed (ADR 0051 decision 74): a multi-word script label written GLUED is read AS WRITTEN
(``secret_guard._SCRIPT_GLUED`` on the view ``_WRITTEN``: lower case, the tatweel dropped, no mark, accent, hamza
or madda folded; the joiners and invisible characters are already gone from every inspection view), its words as
the entry lists them (and the Greek label's declared accented spellings), the declared proclitics and endings,
the same word boundaries as the spaced form; one pattern for both lanes. The word view is not used for it, so the
hamza and madda forms of the S-2 phrases stay other words. The forms ffd28ca8 read that the boundary rule did not
(the Korean compound's two words with a hyphen between them, the Greek stem ffd28ca8 listed) are declared
spellings (``_SCRIPT_FORMS``: ``apart``, ``written``).

Every credential-shaped value is generated at run time; no assertion message prints a label or a value (indexes
and counts only). Non-ASCII text is written as escapes.
"""

from __future__ import annotations

import random
import re
import string
import unicodedata

import pytest

import test_review_2b26d980 as prev
import test_review_cbdf5e78 as cbd
from agents import secret_guard as sg

SEED = 20261115
LOWER = string.ascii_lowercase
WORD = "\u0643\u0644\u0645\u0629 \u0627\u0644\u0645\u0631\u0648\u0631"          # Arabic "password" ("the word of passing")
SECRET = "\u0643\u0644\u0645\u0629 \u0627\u0644\u0633\u0631"                    # Arabic "the secret word"
PERSIAN = "\u0631\u0645\u0632 \u0639\u0628\u0648\u0631"
GREEK_ENTRY = "\u03ba\u03c9\u03b4\u03b9\u03ba\u03bf\u03c2 \u03c0\u03c1\u03bf\u03c3\u03b2\u03b1\u03c3"
GREEK_CODE = "\u03ba\u03c9\u03b4\u03b9\u03ba\u03cc\u03c2"
GREEK_ACCENTED = tuple(GREEK_CODE + " " + w for w in (
    "\u03c0\u03c1\u03cc\u03c3\u03b2\u03b1\u03c3\u03b7", "\u03c0\u03c1\u03cc\u03c3\u03b2\u03b1\u03c3\u03b7\u03c2",
    "\u03c0\u03c1\u03bf\u03c3\u03b2\u03ac\u03c3\u03b5\u03c9\u03c2", "\u03c0\u03c1\u03bf\u03c3\u03b2\u03ac\u03c3\u03b5\u03b9\u03c2",
    "\u03c0\u03c1\u03bf\u03c3\u03b2\u03ac\u03c3\u03b5\u03c9\u03bd"))
GREEK_FFD_STEM = "\u03ba\u03c9\u03b4\u03b9\u03ba\u03bf\u03c3\u03c0\u03c1\u03bf\u03c3\u03b2"   # the key ffd28ca8 listed
KOREAN = "\ube44\ubc00\ubc88\ud638"                                             # "secret" + "number"
KOREAN_SECRET, KOREAN_NUMBER = "\ube44\ubc00", "\ubc88\ud638"
HEBREW = "\u05e1\u05d9\u05e1\u05de\u05d4"
RUSSIAN = "\u043f\u0430\u0440\u043e\u043b\u044c"
HINDI = "\u092a\u093e\u0938\u0935\u0930\u094d\u0921"
TATWEEL = "\u0640"
JOINERS = {"zwj": "\u200d", "zwnj": "\u200c", "zwsp": "\u200b", "shy": "\u00ad", "wj": "\u2060"}
PROCLITIC_AR = "\u0648\u0641\u0628\u0644\u0643"                                  # and, so, with, to, as


@pytest.fixture(autouse=True)
def _fresh_guard_caches():
    prev.clear_guard_caches()
    yield
    prev.clear_guard_caches()


def value(r) -> str:
    return "".join(r.choice(LOWER) for _ in range(5)) + str(r.randint(10, 99)) + r.choice("!#%")


masked, flagged = cbd.masked, cbd.flagged


def read(label: str, r) -> list:
    """[flagged, masked] after ": " + value and after " is " + value, bare and inside a sentence."""
    out = []
    for sep in (": ", " is "):
        t = f"{label}{sep}{value(r)}"
        out += [flagged(t), masked(t), flagged(f"Note the {t} today."), masked(f"Note the {t} today.")]
    return out


def glued_forms() -> list:
    """(name, text): the glued spellings of the Arabic and Greek labels review 0334eeae requires read."""
    out = []
    for name, e in (("word", WORD), ("secret", SECRET)):
        g = e.replace(" ", "")
        out += [(f"{name}.glued", g), (f"{name}.with", "\u0628" + g), (f"{name}.and_to", "\u0648\u0644" + g),
                (f"{name}.tatweel_join", e.replace(" ", TATWEEL)), (f"{name}.tatweel_in", g[:2] + TATWEEL + g[2:]),
                (f"{name}.tatweel_both", g[:2] + TATWEEL + g[2:4] + TATWEEL + g[4:])]
        for jn, j in JOINERS.items():
            out += [(f"{name}.{jn}_join", e.replace(" ", j)), (f"{name}.{jn}_in", g[:2] + j + g[2:])]
    g = GREEK_ENTRY.replace(" ", "")
    for ending in ("", "\u03b7", "\u03b7\u03c2", "\u03b5\u03c9\u03c2"):
        out += [(f"greek{ending!r}.final_sigma", g + ending), (f"greek{ending!r}.medial", g.replace("\u03c2", "\u03c3") + ending),
                (f"greek{ending!r}.upper", (g + ending).upper()), (f"greek{ending!r}.title", (g + ending).title())]
    for i, a in enumerate(GREEK_ACCENTED):
        ga = a.replace(" ", "")
        out += [(f"greek_acc{i}", ga), (f"greek_acc{i}.upper", ga.upper()), (f"greek_acc{i}.title", a.title().replace(" ", "")),
                (f"greek_acc{i}.medial", ga.replace("\u03c2\u03c0", "\u03c3\u03c0"))]
        for jn, j in JOINERS.items():
            out.append((f"greek_acc{i}.{jn}", a.replace(" ", j)))
    out += [("greek_ffd_stem", GREEK_FFD_STEM), ("greek_ffd_stem.upper", GREEK_FFD_STEM.upper())]
    out += [(n + ".nfd", unicodedata.normalize("NFD", t)) for n, t in list(out)]
    return out


# ================================================================================================ MUST A

def test_the_glued_arabic_and_greek_labels_are_read_as_written():
    r = random.Random(SEED)
    forms = glued_forms()
    assert len(forms) >= 150
    bad = [n for n, t in forms if read(t, r) != [True] * 8]
    assert bad == []


def test_the_s2_phrases_stay_other_words_glued_elongated_and_with_joiners():
    """The 60 Arabic phrases of review cbdf5e78 S-2 ("with the word" + "family" + "secret", hamza and madda as
    written): 0 masked, 0 flagged, in their forms and with a tatweel or an invisible joiner put in."""
    r = random.Random(SEED + 1)
    phrases = list(cbd.ARABIC_PHRASES)
    assert len(phrases) == 60
    more = [p[:3] + TATWEEL + p[3:] for p in phrases] + [p.replace(" ", TATWEEL) for p in phrases]
    more += [p[:3] + j + p[3:] for p in phrases for j in JOINERS.values()]
    more += [unicodedata.normalize("NFD", p) for p in phrases]
    for p in phrases + more:
        for sep in (": ", " is ", " - "):
            t = f"{p}{sep}{value(r)}"
            assert not masked(t) and not flagged(t), phrases.index(p) if p in phrases else -1
        assert not flagged(p)


def test_the_glued_alternative_reads_the_hamza_and_madda_as_written():
    r = random.Random(SEED + 2)
    for hamza in ("\u0622", "\u0623", "\u0625"):                       # alef with madda, hamza above, hamza below
        for e in (WORD, SECRET):
            g = e.replace(" ", "")
            assert read(g.replace("\u0627", hamza, 1), r) == [False] * 8
    # the glued label with its proclitics is read, a letter that is not one of them is another word
    for e in (WORD, SECRET):
        g = e.replace(" ", "")
        assert all(read(p + g, r) == [True] * 8 for p in PROCLITIC_AR)
        assert all(read(p + q + g, r) == [True] * 8 for p in PROCLITIC_AR for q in PROCLITIC_AR)
        # a letter that is not a proclitic, three proclitics, a word glued after it, the label inside a longer word:
        # not the label as a whole word (bare); before a value, a label inside a longer written word (round 7,
        # decision 75, as ffd28ca8 read it: letters after it in the strict lane only)
        for x in ("\u0627" + g, PROCLITIC_AR[:3] + g, g + "\u064a\u0629", "\u0627\u0644" + g):
            assert not flagged(x) and not flagged(f"Note the {x} today.")
        assert read("\u0627" + g, r) == [True] * 8 and read(PROCLITIC_AR[:3] + g, r) == [True] * 8
        assert read(g + "\u064a\u0629", r) == [True, False] * 4 and read("\u0627\u0644" + g, r) == [True] * 8


def test_the_known_trap_glued_on_the_word_view_masks_the_s2_phrases(monkeypatch):
    """Declaring the Arabic entries glued on the WORD VIEW (hamza, madda folded) reads the glued label too, but masks
    the S-2 phrases again: why the glued rule reads the view as written."""
    r = random.Random(SEED + 3)
    spells = [sp[:6] + (True,) if e in (WORD, SECRET) else sp for e, sp in zip(sg._SCRIPT, sg._SCRIPT_SPELL)]
    monkeypatch.setattr(sg, "_SCRIPT_SEQ", sg._seq_pattern(sp for sp in spells if not sp[0]))
    monkeypatch.setattr(sg, "_SCRIPT_GLUED", re.compile("(?!x)x"))
    prev.clear_guard_caches()
    assert read(WORD.replace(" ", ""), r) == [True] * 8
    got = sum(masked(f"{p}: {value(r)}") or flagged(p) for p in cbd.ARABIC_PHRASES)
    assert got >= 24


def test_mutation_without_the_glued_pattern_the_glued_labels_are_not_read(monkeypatch):
    r = random.Random(SEED + 4)
    monkeypatch.setattr(sg, "_SCRIPT_GLUED", re.compile("(?!x)x"))
    # (round 7: and the glued spellings of the in-word rule, decision 75, which read the same words as written)
    monkeypatch.setattr(sg, "_INWORD", {k: (p[0], None) for k, p in sg._INWORD.items()})
    prev.clear_guard_caches()
    forms = [t for n, t in glued_forms() if not n.startswith("greek_ffd_stem")]
    assert all(not any(read(t, r)) for t in forms)


def test_the_greek_final_sigma_may_be_written_medial_never_the_other_way():
    r = random.Random(SEED + 5)
    g = GREEK_ENTRY.replace(" ", "") + "\u03b7"
    assert read(g, r) == [True] * 8 and read(g.replace("\u03c2", "\u03c3"), r) == [True] * 8
    # "code" + "to" (written with its final sigma) + "basis", glued: other words, accented or not
    to_basis = "\u03ba\u03c9\u03b4\u03b9\u03ba\u03bf\u03c2\u03c0\u03c1\u03bf\u03c2\u03b2\u03b1\u03c3\u03b7"
    assert read(to_basis, r) == [False] * 8
    assert read(GREEK_CODE + "\u03c0\u03c1\u03bf\u03c2\u03b2\u03ac\u03c3\u03b7\u03c2", r) == [False] * 8
    # (review of cbb6d7b7 F1: each Greek letter of the glued label may carry an accent, as cbdf5e78 read it: an accent
    # on another letter is the label written with that accent)
    assert read(GREEK_CODE + "\u03c0\u03c1\u03bf\u03c3\u03b2\u03ac\u03c3\u03b7\u03c2", r) == [True] * 8


def test_the_korean_compound_is_read_with_its_words_apart():
    r = random.Random(SEED + 6)
    for sep in (" ", "_", "-"):
        assert read(KOREAN_SECRET + sep + KOREAN_NUMBER, r) == [True] * 8
        assert read(KOREAN_SECRET + sep + KOREAN_NUMBER + "\ub97c", r) == [True] * 8          # + a particle
        # + not one: not the label as a whole word; before a value, letters after a label (round 7, decision 75:
        # the strict lane, as ffd28ca8 read it)
        assert read(KOREAN_SECRET + sep + KOREAN_NUMBER + "\ud654", r) == [True, False] * 4
        assert not flagged(KOREAN_SECRET + sep + KOREAN_NUMBER + "\ud654")
    # (review of cbb6d7b7 F2: joined by another separator, the strict lane reads the compound's words, as ffd28ca8
    # and cbdf5e78 read them; the client view never did)
    for sep in (", ", "/", " / ", "+", ".", ". "):
        assert read(KOREAN_SECRET + sep + KOREAN_NUMBER, r) == [True, False] * 4


def test_the_stem_ffd28ca8_listed_is_read_as_it_read_it():
    r = random.Random(SEED + 7)
    assert read(GREEK_FFD_STEM, r) == [True] * 8 and read(GREEK_FFD_STEM.upper(), r) == [True] * 8
    for s in (" ", "_", "-", "."):
        assert read(s.join(GREEK_FFD_STEM), r) == [True] * 8
    # glued to another Greek letter it is another word (and it is no prefilter of ordinary text: "code" alone),
    # spelled or not, before or after
    # (round 7, decision 75: before a value, the stem inside a longer written word is read, as ffd28ca8 read it:
    # with letters after it in the strict lane, with a letter before it in both; bare, it is another word)
    assert read(GREEK_FFD_STEM + "\u03b9\u03bf", r) == [True, False] * 4
    assert read("\u03b1" + GREEK_FFD_STEM, r) == [True] * 8
    assert not flagged(GREEK_FFD_STEM + "\u03b9\u03bf") and not flagged("\u03b1" + GREEK_FFD_STEM)
    for s in (" ", "_", "-", "."):
        assert read("\u03b1" + s.join(GREEK_FFD_STEM), r) == [False] * 8
        assert read(s.join(GREEK_FFD_STEM) + "\u03b1", r) == [False] * 8
    assert read(GREEK_CODE, r) == [False] * 8


def test_the_import_check_reads_every_glued_entry(monkeypatch):
    sg._check_label_lists()
    monkeypatch.setattr(sg, "_SCRIPT_GLUED", re.compile("(?!x)x"))
    with pytest.raises(ValueError):
        sg._check_label_lists()
    monkeypatch.undo()
    # a pattern the strict lane finds INSIDE the glued entry but that does not take it whole stops it too
    monkeypatch.setattr(sg, "_SCRIPT_GLUED", re.compile("."))
    with pytest.raises(ValueError):
        sg._check_glued("_SCRIPT", sg._SCRIPT)
    monkeypatch.undo()
    # (a written spelling the pattern does not take stops the import too)
    forms = dict(sg._SCRIPT_FORMS)
    forms[GREEK_ENTRY] = dict(forms[GREEK_ENTRY], written=forms[GREEK_ENTRY]["written"] + (GREEK_CODE + " \u03c4\u03bf",))
    monkeypatch.setattr(sg, "_SCRIPT_FORMS", forms)
    with pytest.raises(ValueError):
        sg._check_glued("_SCRIPT", sg._SCRIPT)


# ================================================================================================ SHOULD 1

def test_label_words_stand_apart_by_one_space_underscore_or_hyphen_only():
    """Mutant: any one to three characters between label words."""
    r = random.Random(SEED + 8)
    for e in (WORD, SECRET, PERSIAN, GREEK_ACCENTED[1]):
        for sep in (" ", "_", "-"):
            assert read(e.replace(" ", sep), r) == [True] * 8, sep
        # (review of cbb6d7b7 F2: another join between the words is read by the strict lane only, as ffd28ca8 and
        # cbdf5e78 read it; the client view reads a space, "_" or "-" only)
        for sep in (",", ", ", "/", " / ", "+", "  -", "=", "|", " _ "):
            assert read(e.replace(" ", sep), r) == [True, False] * 4, sep


def test_the_korean_particles_are_a_closed_list():
    """Mutant: an open list (any letters of the script after the noun)."""
    r = random.Random(SEED + 9)
    for e in ("\uc554\ud638", "\ud1a0\ud070", KOREAN):
        for p in ("\uc744", "\ub294", "\uc5d0\uc11c", "\ub4e4\uc758"):
            assert read(e + p, r) == [True] * 8
        # "encryption", "tokenization", "ciphertext": other words, never masked; (round 7, decision 75) before a
        # value the strict lane reads the label with letters after it, as ffd28ca8 did; bare, not
        for x in ("\ud654", "\ubb38", "\ud654\ub41c"):
            assert read(e + x, r) == [True, False] * 4 and not flagged(e + x)


def test_hebrew_takes_up_to_three_proclitic_letters():
    """Mutant: four proclitic letters."""
    r = random.Random(SEED + 10)
    for pre in ("\u05d4", "\u05d5\u05d4", "\u05d5\u05e9\u05d4", "\u05d5\u05db\u05e9"):
        assert read(pre + HEBREW, r) == [True] * 8
    # four: not the label as a whole word (bare, and inside a sentence with no value); before a value, a label
    # inside a longer written word (round 7, decision 75, as ffd28ca8 read it)
    for pre in ("\u05d5\u05db\u05e9\u05d4", "\u05d5\u05de\u05e9\u05d4", "\u05e9\u05d5\u05d4\u05d1"):
        assert not flagged(pre + HEBREW) and not flagged(f"Note the {pre}{HEBREW} today.")
        assert read(pre + HEBREW, r) == [True] * 8


def test_a_two_letter_label_spelled_with_a_hyphen_or_dot_is_a_word_of_its_own():
    """Mutant: ``_spelled_whole`` without its right-hand joined-letter lookahead."""
    r = random.Random(SEED + 11)
    two = "\ube44\ubc88"
    for s in "-.":
        assert read(two[0] + s + two[1], r) == [True] * 8
        # a letter joined after it, and before it: not masked; (review of cbb6d7b7 F3) the strict lane reads the
        # spelled units with any non-letters between them, as ffd28ca8 and cbdf5e78 read them
        assert read(two[0] + s + two[1] + s + "\uac00", r) == [True, False] * 4
        assert read("\uac00" + s + two[0] + s + two[1], r) == [True, False] * 4


def test_spelled_units_stand_apart_by_one_or_two_separators():
    """Mutant: up to three separators between two spelled units."""
    r = random.Random(SEED + 12)
    for s in "_-.":
        assert read(s.join(RUSSIAN), r) == [True] * 8
        assert read((s * 2).join(RUSSIAN), r) == [True] * 8
        # three: not masked; (review of cbb6d7b7 F3) flagged by the strict lane, as ffd28ca8 and cbdf5e78 read it
        assert read((s * 3).join(RUSSIAN), r) == [True, False] * 4


def test_a_spelled_mark_may_stay_on_its_letter():
    """Mutant: a mark must be separated from its letter in the spelled forms."""
    r = random.Random(SEED + 13)
    on_letter = " ".join(("\u092a\u093e", "\u0938", "\u0935", "\u0930\u094d", "\u0921"))
    apart = " ".join(HINDI)
    assert read(on_letter, r) == [True] * 8 and read(apart, r) == [True] * 8
    thai = cbd.THAI
    thai_on = " ".join(("\u0e23", "\u0e2b\u0e31", "\u0e2a", "\u0e1c\u0e48", "\u0e32", "\u0e19"))
    assert thai_on.replace(" ", "") == thai and read(thai_on, r) == [True] * 8


GREEK_BLOCKS = ((0x370, 0x3ff), (0x1f00, 0x1fff))


def test_every_letter_and_mark_of_both_greek_blocks_continues_a_greek_word():
    """Mutant: a narrower Greek word class. Any letter or mark of the Greek and Coptic block and of Greek Extended
    (one that stays one such character in the inspection view) glued before or after the label makes another
    word."""
    r = random.Random(SEED + 14)
    chars = []
    for a, b in GREEK_BLOCKS:
        for o in range(a, b + 1):
            c = chr(o)
            v = sg.normalise("x" + c + "x")[1:-1]
            if unicodedata.category(c)[0] in "LM" and len(v) == 1 and unicodedata.category(v)[0] in "LM" and \
                    any(x <= ord(v) <= y for x, y in GREEK_BLOCKS):
                chars.append(c)
    assert len(chars) >= 300
    # spaced (the word view folds every accented letter into the basic block) and GLUED (the view as written keeps
    # the letters of Greek Extended as they are: only a class of both blocks stops them)
    # (round 7, decision 75: before a value a label inside a longer written word is read, as ffd28ca8 and cbdf5e78
    # read it; so the word class is pinned on the bare form, where the boundary rule alone decides, and on the
    # client view after the label, which reads no letter after it)
    for label in (GREEK_ACCENTED[1], GREEK_ACCENTED[1].replace(" ", "")):
        bad = [hex(ord(c)) for c in chars if flagged(f"{c}{label}") or flagged(f"x {c}{label} x")]
        assert bad == []
        bad = [hex(ord(c)) for c in chars if flagged(f"{label}{c}") or masked(f"{label}{c}: {value(r)}")]
        assert bad == []


# ================================================================================================ SHOULD 2

def _letters(w: str) -> str:
    return sg._script_letters(sg.normalise(w))


def derived_joins() -> dict:
    """lang -> joins (tuples of words) of the bundled sample whose letters hold a label key ACROSS a word boundary
    (a suffix of one word + a prefix of the next; or + all of a middle word), for every script entry and every
    declared written spelling: derived from the entries themselves, so a new entry is checked against the sample."""
    keys = {_letters(e) for e in sg._SCRIPT} | {_letters(x) for f in sg._SCRIPT_FORMS.values() for x in f.get("written", ())}
    out = {}
    for lang, words in SAMPLE.items():
        L = [(w, _letters(w)) for w in words]
        joins = []
        for a, la in L:
            for b, lb in L:
                if any(k in la + lb and k not in la and k not in lb for k in keys):
                    joins.append((a, b))
                for m, lm in L:
                    if any(k in la + lm + lb and k not in la + lm and k not in lm + lb for k in keys):
                        joins.append((a, m, b))
        out[lang] = joins
    return out


def test_boundary_joins_derived_from_the_entries_are_not_read():
    r = random.Random(SEED + 15)
    # the sample: ordinary words only (none holds a key, none is a word of a label)
    keys = [_letters(e) for e in sg._SCRIPT]
    assert all(not any(k in _letters(w) for k in keys) for ws in SAMPLE.values() for w in ws)
    joins = derived_joins()
    assert sum(len(j) for j in joins.values()) >= 200 and sum(1 for j in joins.values() if j) >= 7
    bad, own, glued_read = [], 0, []
    for lang, js in joins.items():
        for ws in js:
            # a join whose words glued ARE a label word (a declared form: "pa" + "rol'"): glued it is that word, and
            # with ONE space inside it it is read as written (decision 73, as ffd28ca8 read it); with "-" or "." not
            itself = flagged("".join(ws))
            own += itself
            for sep in (" ", "-", "", "."):
                t = sep.join(ws)
                if itself and sep in ("", " ") or lang == "th" and sep == "" and cbd.THAI in t:
                    continue                                   # (and residual (a): the Thai label as written)
                if flagged(t) or sep != "" and (masked(f"{t}: {value(r)}") or masked(f"{t}: ready today")):
                    bad.append((lang, js.index(ws), sep))
                elif sep == "" and (masked(f"{t}: {value(r)}") or masked(f"{t}: ready today")):
                    # OWNER DECISION (round 7, decision 75): glued, the join's letters END with a label, which is
                    # letter for letter a label with a word before it; before a value it is masked, as ffd28ca8
                    # masked it. Only such joins: the in-word pattern takes the glued text up to its end
                    seq, _glued = sg._INWORD["end"]
                    wv = sg._script_word_view(sg.normalise(t))
                    if not any(m.end() == len(wv) for m in seq.finditer(wv)):
                        bad.append((lang, js.index(ws), sep))
                    glued_read.append((lang, js.index(ws)))
    assert bad == [] and own > 0
    assert len(glued_read) == 24                                    # ru 12, uk 11, he 1


def test_a_new_bad_entry_is_caught_by_the_derived_joins(monkeypatch):
    """The derived test is not a snapshot: an entry the boundary rule would read across words (here the letters-only
    matching of cbdf5e78 put back) makes derived joins read."""
    monkeypatch.setattr(sg, "_SCRIPT_UNSPACED_RX", sg._SCRIPT_RX)
    prev.clear_guard_caches()
    joins = derived_joins()
    got = sum(flagged(" ".join(ws)) for js in joins.values() for ws in js)
    assert got >= 10


# ================================================================================================ NIT 1

def test_the_sigma_list_of_the_latin_form_shortcut_is_complete():
    """NIT 1: the shortcut that keeps the whole text's first pass is taken only without a character that gives the
    capital sigma (whose lower case depends on its context); every code point is checked: one whose NFKC, through
    the fold's early letters, holds the capital sigma is in ``_SIGMA_RX``."""
    early = sg._FOLD._early
    bad = [hex(o) for o in range(0x110000) if not 0xD800 <= o <= 0xDFFF
           and "\u03a3" in unicodedata.normalize("NFKC", chr(o)).translate(early)
           and sg._SIGMA_RX.search(chr(o)) is None]
    assert bad == []


def test_mutation_without_the_sigma_test_the_shortcut_is_not_the_definition(monkeypatch):
    monkeypatch.setattr(sg, "_SIGMA_RX", re.compile("(?!x)x"))
    prev.clear_guard_caches()
    nit1 = ["\u060c\u03a9\u064eS\xab\xe9\u039a\u2019\u03a3", "\u039a\u0419\u2019\u03a3\u05bc",
            "\u0964\u094d,\u0390'\u0441\u03c3^\u03a3"]
    assert sum(sg._label_norm(t) != cbd._latin_form_by_the_spans(monkeypatch, t) for t in nit1) >= 1


# ================================================================================================ NIT 2

def test_test_only_code_is_not_in_the_production_module():
    assert not hasattr(sg, "_one_script_spans_loop") and not hasattr(sg, "_TAG_RUN")


# ================================================================================================ the sample

# ordinary words of nine scripts (wordfreq 3.1.1 top 30,000; Thai: TNC and the pythainlp dictionary), chosen from
# the words that stand on either side of a label key across a boundary in the M2 candidate joins of this round, and
# frequent words; none holds a key or is a word of a label
SAMPLE = {
    "ru": (
        "\u0430", "\u0432", "\u0433\u0440\u0438\u043f\u043f\u0430", "\u0433\u0440\u0443\u043f\u043f\u0430",
        "\u0434\u043e\u0441\u0442\u0443\u043f\u0430", "\u0435\u0432\u0440\u043e\u043f\u0430", "\u0438",
        "\u043a\u0430\u043a", "\u043d\u0430", "\u043d\u0435", "\u043f\u0430", "\u043f\u0430\u043f\u0430",
        "\u043f\u0430\u0440", "\u043f\u043e", "\u043f\u0440\u0438\u043d\u0446\u0438\u043f\u0430", "\u0440",
        "\u0440\u043e\u043b\u0435\u0439", "\u0440\u043e\u043b\u0438", "\u0440\u043e\u043b\u0438\u043a",
        "\u0440\u043e\u043b\u0438\u043a\u0430", "\u0440\u043e\u043b\u0438\u043a\u0438",
        "\u0440\u043e\u043b\u0438\u043a\u043e\u0432", "\u0440\u043e\u043b\u043b", "\u0440\u043e\u043b\u044c",
        "\u0440\u043e\u043b\u044c\u044e", "\u0440\u043e\u043b\u044f\u0445", "\u0441", "\u0442.\u043d",
        "\u0442\u0438\u043f\u0430", "\u0442\u043e\u043b\u043f\u0430", "\u0442\u0440", "\u0443\u043f\u0430",
        "\u0444\u0438\u043b\u0438\u043f\u043f\u0430", "\u0447\u0435\u0440\u0435\u043f\u0430", "\u0447\u0442\u043e",
        "\u0448", "\u044d\u0442\u0430\u043f\u0430", "\u044f",
    ),
    "uk": (
        "\u0432", "\u0433\u0440\u0443\u043f\u0430", "\u0434\u043e", "\u0437", "\u0437\u043f",
        "\u043a\u0443\u043f\u0430", "\u043b", "\u043c\u0430\u043f\u0430", "\u043d\u0430",
        "\u043d\u0430\u0440\u0434\u0435\u043f\u0430", "\u043d\u0435",
        "\u043e\u043b\u0435\u043a\u0441\u0430\u043d\u0434\u0440",
        "\u043e\u043b\u0435\u043a\u0441\u0430\u043d\u0434\u0440\u0430", "\u043f", "\u043f\u0430",
        "\u043f\u0430\u043f\u0430", "\u043f\u0430\u0440", "\u043f\u0437", "\u0440",
        "\u0440\u043e\u043b\u0435\u0439", "\u0440\u043e\u043b\u0438\u043a", "\u0440\u043e\u043b\u0438\u043a\u0438",
        "\u0440\u043e\u043b\u044c", "\u0440\u043e\u043b\u044f\u0445", "\u0440\u043e\u043b\u0456",
        "\u0441\u0454\u043f\u0430\u0440", "\u0442\u0430", "\u0442\u0438\u043f\u0430", "\u0442\u0440",
        "\u0442\u0440\u0430\u043c\u043f\u0430", "\u0442\u0443\u043f\u0430", "\u0442\u0456\u043f\u0430", "\u0443",
        "\u0443\u043f\u0430", "\u0446\u0435", "\u0449\u043e", "\u0454\u0432\u0440\u043e\u043f\u0430", "\u0456",
    ),
    "el": (
        "1\u03b7", "1\u03bf", "2\u03b7", "3\u03b7", "4\u03b7", "t\u03bf", "\u03ae", "\u03b1\u03c0\u03cc",
        "\u03b3\u03b9\u03b1", "\u03b7", "\u03b8", "\u03b9\u03b7", "\u03ba\u03b1\u03b9", "\u03ba\u03b5\u03c0",
        "\u03bc\u03b5", "\u03bd\u03b1", "\u03bf", "\u03c0", "\u03c0\u03bf\u03c5", "\u03c1", "\u03c1\u03b9",
        "\u03c3", "\u03c3\u03c4", "\u03c4o", "\u03c4\u03b1", "\u03c4\u03b7\u03bd", "\u03c4\u03b7\u03c3",
        "\u03c4\u03bf", "\u03c4\u03bf\u03c5", "\u03c4\u03c1", "\u03c4\u03c3", "\u03c4\u03cc", "\u03c4\u1f78",
        "\u03c8", "\u03c9", "\u03cc", "\u03ce",
    ),
    "ar": (
        "\u0623\u0646", "\u0625\u0644\u0649", "\u0627\u0644\u0633\u0631\u0628", "\u0627\u0644\u0633\u0631\u062f",
        "\u0627\u0644\u0633\u0631\u0637\u0627\u0646", "\u0627\u0644\u0633\u0631\u0637\u0627\u0646\u064a\u0629",
        "\u0627\u0644\u0633\u0631\u0639\u0629", "\u0627\u0644\u0633\u0631\u0642\u0629",
        "\u0627\u0644\u0633\u0631\u0648\u0631", "\u0627\u0644\u0633\u0631\u064a",
        "\u0627\u0644\u0633\u0631\u064a\u0627\u0646\u064a\u0629", "\u0627\u0644\u0633\u0631\u064a\u0629",
        "\u0627\u0644\u0633\u0631\u064a\u0631", "\u0627\u0644\u0633\u0631\u064a\u0639",
        "\u0627\u0644\u0633\u0631\u064a\u0639\u0629", "\u0627\u0644\u0643\u0644\u0645\u0629",
        "\u0627\u0644\u0645\u0631\u0648\u0631\u064a\u0629", "\u0628\u0643\u0644\u0645\u0629", "\u0639\u0644\u0649",
        "\u0639\u0646", "\u0641\u064a", "\u0644\u0627", "\u0644\u0643\u0644\u0645\u0629",
        "\u0644\u0644\u0643\u0644\u0645\u0629", "\u0645\u0627", "\u0645\u0646", "\u0647\u0630\u0627", "\u0648",
        "\u0648\u0627\u0644\u0643\u0644\u0645\u0629", "\u0648\u0643\u0644\u0645\u0629",
    ),
    "fa": (
        "\u0627\u0632", "\u0627\u0633\u062a", "\u0627\u06cc\u0646", "\u0628\u0627", "\u0628\u0647",
        "\u062a\u0631\u0645\u0632", "\u062f\u0631", "\u0631\u0627", "\u0631\u0627\u0645\u0647\u0631\u0645\u0632",
        "\u0631\u0648", "\u0639\u0628\u0648\u0631\u06cc", "\u0642\u0631\u0645\u0632", "\u0647\u0631\u0645\u0632",
        "\u0648", "\u06a9\u0647",
    ),
    "he": (
        "\u05d0\u05e0\u05d9", "\u05d0\u05ea", "\u05d1\u05d1\u05e1\u05d9\u05e1", "\u05d1\u05e1\u05d9\u05e1",
        "\u05d2\u05dd", "\u05d4\u05d1\u05e1\u05d9\u05e1", "\u05d4\u05d5\u05d0", "\u05d6\u05d4",
        "\u05db\u05d1\u05e1\u05d9\u05e1", "\u05db\u05dc", "\u05dc\u05d0", "\u05dc\u05d1\u05e1\u05d9\u05e1",
        "\u05de\u05d4", "\u05de\u05d4\u05d1\u05d9\u05ea", "\u05de\u05d4\u05d5", "\u05de\u05d4\u05d5\u05d5\u05d4",
        "\u05de\u05d4\u05d9", "\u05de\u05d4\u05d9\u05d5\u05dd", "\u05de\u05d4\u05d9\u05e8",
        "\u05de\u05d4\u05d9\u05e8\u05d4", "\u05de\u05d4\u05d9\u05e8\u05d5\u05ea", "\u05de\u05d4\u05dc\u05da",
        "\u05de\u05d4\u05dd", "\u05de\u05d4\u05df", "\u05de\u05d4\u05e1\u05d5\u05d2", "\u05de\u05d4\u05e8",
        "\u05e2\u05dc", "\u05e2\u05dd", "\u05e4\u05e8\u05e0\u05e1\u05d9\u05e1", "\u05e9\u05dc",
    ),
    "hi": (
        "\u0906\u092a\u0938", "\u0906\u092a\u0938\u0940", "\u0906\u092a\u0938\u0947",
        "\u0906\u0938\u092a\u093e\u0938", "\u0914\u0930", "\u0915\u093e", "\u0915\u0940", "\u0915\u0947",
        "\u0915\u094b", "\u0924\u092a\u0938\u094d\u0935\u0940", "\u0924\u093e\u092a\u0938\u0940", "\u092a\u0930",
        "\u092a\u093e\u0938", "\u092a\u0940\u0938", "\u092a\u0948\u0938\u093e", "\u092a\u0948\u0938\u0947",
        "\u092a\u0948\u0938\u094b\u0902", "\u092e\u0947\u0902", "\u0930\u0902\u0921\u0940",
        "\u0930\u0921\u093e\u0930", "\u0930\u0940\u0921", "\u0930\u0940\u0921\u093f\u0902\u0917",
        "\u0930\u0947\u0921", "\u0930\u0947\u0921\u093f\u092f\u094b", "\u0930\u0947\u0921\u094d\u0921\u0940",
        "\u0930\u094b\u0921", "\u0930\u094b\u0921\u0935\u0947\u091c", "\u0930\u094b\u0921\u093c",
        "\u0935\u0930\u094d\u0921", "\u0935\u093e\u092a\u0938", "\u0935\u093e\u092a\u0938\u0940",
        "\u0935\u093e\u092a\u093f\u0938", "\u0935\u093e\u0930\u094d\u0921", "\u0935\u093e\u0930\u094d\u0921\u0928",
        "\u0935\u093e\u0930\u094d\u0921\u094b\u0902", "\u0938\u0947", "\u0939\u0948", "\u0939\u0948\u0902",
    ),
    "ko": (
        "\uac00", "\uace0", "\ub294", "\ub2e4", "\ub9ac\uc554", "\uba85\uc554", "\ubbf8\uc554", "\ubc1c\uc554",
        "\ubc88", "\ubc88\uac00", "\ubc88\uac08", "\ubc88\uac1c", "\ubc88\uac70", "\ubc88\ubcf5", "\ubc88\uc2dd",
        "\ubc88\uc5ed", "\ubc88\uc5ed\uac00", "\ubc88\uc601", "\ubc88\uc9c0", "\ubc88\uca4d", "\ubc88\ucc3d",
        "\ubc88\ud638\ud310", "\uc0c1\uc554", "\uc554", "\uc5d0", "\uc601\uc554", "\uc6a9\uc554", "\uc704\uc554",
        "\uc720\ubc29\uc554", "\uc740", "\uc744", "\uc758", "\uc774", "\ucdcc\uc7a5\uc554", "\ud3d0\uc554",
        "\ud558", "\ud56d\uc554", "\ud654\uac15\uc554",
    ),
    "th": (
        "\u0e01\u0e32\u0e23", "\u0e01\u0e32\u0e23\u0e40\u0e02\u0e49\u0e32\u0e23\u0e2b\u0e31\u0e2a",
        "\u0e01\u0e38\u0e0d\u0e41\u0e08\u0e23\u0e2b\u0e31\u0e2a", "\u0e02\u0e2d\u0e07",
        "\u0e02\u0e32\u0e19\u0e23\u0e2b\u0e31\u0e2a", "\u0e08\u0e30", "\u0e16\u0e2d\u0e14\u0e23\u0e2b\u0e31\u0e2a",
        "\u0e17\u0e35\u0e48", "\u0e1c\u0e32\u0e19", "\u0e1c\u0e48\u0e32\u0e19",
        "\u0e1c\u0e48\u0e32\u0e19\u0e01\u0e32\u0e23\u0e17\u0e14\u0e2a\u0e2d\u0e1a",
        "\u0e1c\u0e48\u0e32\u0e19\u0e1e\u0e1a", "\u0e1c\u0e48\u0e32\u0e19\u0e1e\u0e34\u0e20\u0e1e",
        "\u0e1c\u0e48\u0e32\u0e19\u0e1e\u0e49\u0e19", "\u0e1c\u0e48\u0e32\u0e19\u0e1f\u0e49\u0e32",
        "\u0e1c\u0e48\u0e32\u0e19\u0e1f\u0e49\u0e32\u0e25\u0e35\u0e25\u0e32\u0e28",
        "\u0e1c\u0e48\u0e32\u0e19\u0e20\u0e34\u0e20\u0e1e\u0e25\u0e35\u0e25\u0e32",
        "\u0e1c\u0e48\u0e32\u0e19\u0e21\u0e37\u0e2d", "\u0e1c\u0e48\u0e32\u0e19\u0e23\u0e32\u0e04\u0e32",
        "\u0e1c\u0e48\u0e32\u0e19\u0e28\u0e36\u0e01", "\u0e1c\u0e48\u0e32\u0e19\u0e2a\u0e32\u0e22\u0e15\u0e32",
        "\u0e1c\u0e49\u0e32\u0e19\u0e38\u0e48\u0e07", "\u0e21\u0e35", "\u0e23\u0e2b\u0e31\u0e2a",
        "\u0e40\u0e02\u0e49\u0e32\u0e23\u0e2b\u0e31\u0e2a", "\u0e40\u0e1b\u0e47\u0e19",
        "\u0e41\u0e1b\u0e25\u0e23\u0e2b\u0e31\u0e2a", "\u0e41\u0e25\u0e30", "\u0e43\u0e19",
        "\u0e43\u0e2a\u0e48\u0e23\u0e2b\u0e31\u0e2a", "\u0e44\u0e14\u0e49", "\u0e44\u0e21\u0e48",
    ),
}
