"""The lead's probe of commit cbb6d7b7 (round 7): a script label inside a longer written word.

FINDING  ADR 0051 decision 73 listed as "no longer read, against ffd28ca8" a script label inside a longer written
         word: the Korean compound with a noun before the label ("administrator" + "password", one word, as Korean
         writes compounds), the Russian "administrator" + "password" glued, the Russian "password" + an adjective
         ending, a Hebrew or Arabic word with a prefix letter that is not a declared proclitic. ffd28ca8 and
         cbdf5e78 read them (the strict lane: the label's letters anywhere; the client view: the label's letters
         right before a separator and a value) and masked the value after them; cbb6d7b7 showed it to the owner.
         Disclosure is not an exemption (THE BAR, side 2).

The rule (``secret_guard._inword_value``, ADR 0051 decision 75): a script label inside a longer written word is read
in BOTH lanes by one helper, only when a separator and a value follow the word that holds it (the client view's own
reading of what follows a label). Per lane, as ffd28ca8 and cbdf5e78 read:
  strict       the label with any letters of its script before and after it in the same word (and words of the script
               joined to that word by "-");
  client view  the label with any letters before it, ending as the boundary rule ends it (no letter after it).
Without a value nothing changes: a word that holds a label's letters is an ordinary word (the joins the boundary rule
closed stay closed in their bare form). Marked (Hindi, Thai) and unspaced (Han, kana) entries are not concerned.

Every credential-shaped value is generated at run time; no assertion message prints a label or a value (indexes and
counts only). Non-ASCII text is written as escapes.
"""

from __future__ import annotations

import random
import re
import string
import unicodedata

import pytest

import test_review_2b26d980 as prev
import test_review_cbdf5e78 as cbd
import test_review_0334eeae as r033
from agents import secret_guard as sg

SEED = 20261116
LOWER = string.ascii_lowercase
KO = {"password": "\ube44\ubc00\ubc88\ud638", "pw": "\ube44\ubc88", "cipher": "\uc554\ud638", "token": "\ud1a0\ud070"}
KO_HEADS = ("\uad00\ub9ac\uc790", "\uacc4\uc815", "\uc0ac\uc6a9\uc790", "\uc11c\ubc84", "\uc640\uc774\ud30c\uc774")
RU = "\u043f\u0430\u0440\u043e\u043b\u044c"
RU_HEADS = ("\u0430\u0434\u043c\u0438\u043d", "\u0441\u0435\u0440\u0432\u0435\u0440", "\u0432\u0430\u0439\u0444\u0430\u0439")
UK_HEADS = ("\u0430\u0434\u043c\u0456\u043d", "\u0441\u0435\u0440\u0432\u0435\u0440", "\u043c\u043e\u0434\u0435\u043c")
RU_TAILS = ("\u043d\u044b\u0439", "\u043d\u0430\u044f", "\u043d\u043e")
RU_API = "\u043a\u043b\u044e\u0447 api"
AR = (r033.WORD, r033.SECRET)
AR_HEADS = ("\u0645\u062f\u064a\u0631", "\u062d\u0633\u0627\u0628")
FA = r033.PERSIAN
FA_HEADS = ("\u0645\u062f\u06cc\u0631", "\u06a9\u0627\u0631\u0628\u0631")
HE = r033.HEBREW
HE_HEADS = ("\u05de\u05e0\u05d4\u05dc", "\u05de\u05e9\u05ea\u05de\u05e9")
EL = cbd.GREEK_LABEL
EL_HEADS = ("\u03b4\u03b9\u03b1\u03c7\u03b5\u03b9\u03c1\u03b9\u03c3\u03c4\u03ae\u03c2", "\u03c7\u03c1\u03ae\u03c3\u03c4\u03b7\u03c2")
SEPARATORS = (": ", " = ", " is ", "=", " -> ")


@pytest.fixture(autouse=True)
def _fresh_guard_caches():
    prev.clear_guard_caches()
    yield
    prev.clear_guard_caches()


def value(r) -> str:
    return "".join(r.choice(LOWER) for _ in range(5)) + str(r.randint(10, 99)) + r.choice("!#%")


masked, flagged = cbd.masked, cbd.flagged


def lanes(form: str, r) -> tuple:
    """(flagged every time, masked every time) of ``form`` + each separator + a value, bare and in a sentence, NFC
    and NFD."""
    fl = ms = True
    for nf in ("NFC", "NFD"):
        f = unicodedata.normalize(nf, form)
        for sep in SEPARATORS:
            for t in (f"{f}{sep}{value(r)}", f"Note the {f}{sep}{value(r)} today."):
                fl = fl and flagged(t)
                ms = ms and masked(t)
    return fl, ms


def heads() -> list:
    """(name, text): a noun or a letter that is not a declared proclitic glued BEFORE a label of every spaced,
    unmarked script entry (the label as listed, as written, glued)."""
    out = []
    for k, e in KO.items():
        out += [(f"ko.{k}.{i}", h + e) for i, h in enumerate(KO_HEADS)]
    out += [(f"ko.password_apart.{i}", h + "\ube44\ubc00 \ubc88\ud638") for i, h in enumerate(KO_HEADS)]
    out += [(f"ru.{i}", h + RU) for i, h in enumerate(RU_HEADS)] + [(f"uk.{i}", h + RU) for i, h in enumerate(UK_HEADS)]
    out += [("ru.letter", "\u0437" + RU), ("ru_api", RU_HEADS[0] + RU_API)]
    for a, e in enumerate(AR):
        g = e.replace(" ", "")
        out += [(f"ar{a}.{i}", h + e) for i, h in enumerate(AR_HEADS)] + [(f"ar{a}.glued.{i}", h + g)
                                                                          for i, h in enumerate(AR_HEADS)]
        out += [(f"ar{a}.alef", "\u0627" + e), (f"ar{a}.alef_glued", "\u0627" + g), (f"ar{a}.al_glued", "\u0627\u0644" + g),
                (f"ar{a}.tatweel", AR_HEADS[0] + r033.TATWEEL + e)]
        out += [(f"ar{a}.{jn}", AR_HEADS[0] + j + e) for jn, j in r033.JOINERS.items()]
    out += [(f"fa.{i}", h + FA) for i, h in enumerate(FA_HEADS)] + [(f"fa.glued.{i}", h + FA.replace(" ", ""))
                                                                    for i, h in enumerate(FA_HEADS)]
    out += [(f"he.{i}", h + HE) for i, h in enumerate(HE_HEADS)]
    out += [(f"he.letter.{i}", c + HE) for i, c in enumerate("\u05d0\u05d2\u05d6\u05d7\u05e7")]
    out += [("he.four_proclitics", "\u05d5\u05db\u05e9\u05d4" + HE)]
    out += [(f"el.{i}", h + EL) for i, h in enumerate(EL_HEADS)] + [(f"el.glued.{i}", h + EL.replace(" ", ""))
                                                                    for i, h in enumerate(EL_HEADS)]
    out += [("el.letter", "\u03b1" + EL), ("el.unaccented", "\u03b1" + r033.GREEK_ENTRY + "\u03b7\u03c2"),
            ("el.ffd_stem", "\u03b1" + r033.GREEK_FFD_STEM)]
    out += [(f"{n}.{jn}", t[:2] + j + t[2:]) for n, t in list(out[:4]) for jn, j in r033.JOINERS.items()]
    return out


def tails() -> list:
    """(name, text): letters of the label's script glued AFTER it (the strict lane reads these with a value)."""
    out = [(f"ru.{i}", RU[:-1] + "\u044c" + x) for i, x in enumerate(RU_TAILS)]
    # a noun before a declared case form: the strict lane (the client view of ffd28ca8 and cbdf5e78 read only the
    # nominative before a separator, and neither does this one: glued joins ending in "-role" + a case ending are
    # ordinary words, "type" + "roles")
    out += [(f"ru.case.{i}", RU_HEADS[0] + RU[:-1] + x) for i, x in enumerate(("\u044f", "\u0435\u043c", "\u0456"))]
    out += [("ru.hyphen_compound", RU + "\u043d\u043e-\u0437\u0430\u0449\u0438\u0449\u0451\u043d\u043d\u044b\u0439")]
    out += [(f"ko.{k}.encryption", e + "\ud654") for k, e in KO.items()]
    out += [("ko.apart.hwa", "\ube44\ubc00 \ubc88\ud638\ud654"), ("he.ot", HE + "\u05d5\u05ea"),
            ("ar.iya", AR[0].replace(" ", "") + "\u064a\u0629"), ("el.ta", EL + "\u03c4\u03b1"),
            ("el.ffd_stem", r033.GREEK_FFD_STEM + "\u03b9\u03bf"), ("fa.i", FA + "\u06cc")]
    return out


# ================================================================================================ read again

def test_a_label_with_a_word_or_a_letter_before_it_is_read_in_both_lanes():
    r = random.Random(SEED)
    forms = heads()
    assert len(forms) >= 95
    bad = [n for n, t in forms if lanes(t, r) != (True, True)]
    assert bad == []


def test_a_label_with_letters_after_it_is_flagged_and_not_masked():
    """The strict lane of ffd28ca8 and cbdf5e78 read it (the label's letters anywhere); their client view did not
    (the label had to end before its separator), and neither does this one."""
    r = random.Random(SEED + 1)
    forms = tails()
    assert len(forms) >= 12
    assert [n for n, t in forms if lanes(t, r) != (True, False)] == []
    # a letter before AND after it: strict
    assert lanes(KO_HEADS[0] + KO["password"] + "\ud654", r) == (True, False)
    # a hyphenated compound after the label glued as written (the Arabic and Greek labels): strict
    for g in (AR[0].replace(" ", "") + "\u064a\u0629", EL.replace(" ", "") + "\u03c4\u03b1"):
        assert lanes(g + "-" + AR_HEADS[0] if g.startswith(AR[0][0]) else g + "-" + EL_HEADS[1], r) == (True, False)


def test_without_a_value_a_word_holding_a_label_is_an_ordinary_word():
    """The rule needs the separator and the value: bare, or with a word that is no value after it, none of these is
    flagged or masked (the boundary rule decides, as at cbb6d7b7)."""
    forms = heads() + tails()
    assert [n for n, t in forms if flagged(t) or flagged(f"Note the {t} today.") or masked(t)] == []
    # a separator with no value after it, and a natural-language verb before prose
    assert [n for n, t in forms if flagged(f"{t}:") or masked(f"{t}:") or masked(f"{t} was reset")] == []


def test_the_lead_probe_cells():
    """The cells of the finding: Korean "administrator", "account", "user" + "password" written as one word, and
    the Russian "administrator" + "password", with ": " and " = ": flagged and masked."""
    r = random.Random(SEED + 3)
    for t in [h + KO["password"] for h in KO_HEADS[:3]] + [RU_HEADS[0] + RU]:
        for sep in (": ", " = "):
            assert flagged(f"{t}{sep}{value(r)}") and masked(f"{t}{sep}{value(r)}"), (len(t), sep)


# ================================================================================================ stays closed

def test_the_pinned_collision_sets_stay_unread_with_a_value():
    """The Greek "code to bas-" phrases, the Thai joins without the label as written, the Arabic S-2 phrases and the
    Hindi glued joins: not read in either lane with a value after them (the in-word rule is not made for the marked
    or unspaced entries, and it reads the label's own words in a row, never across another word)."""
    r = random.Random(SEED + 4)
    texts = list(cbd.greek_phrases()) + [t for t, lit in cbd.thai_texts() if not lit] + list(cbd.ARABIC_PHRASES)
    texts += ["".join(p.split("+")) for p in cbd.HINDI_GLUED_PAIRS if "".join(p.split("+")) != cbd.HINDI]
    assert len(texts) >= 100 + 110 + 60 + 3
    bad = [i for i, t in enumerate(texts) for sep in (": ", " is ")
           if flagged(f"{t}{sep}{value(r)}") or masked(f"{t}{sep}{value(r)}")]
    assert bad == []


def test_words_of_a_label_never_stand_apart_by_another_word_or_a_comma():
    r = random.Random(SEED + 5)
    for e in AR + (FA,):
        for sep in (" \u0648 ", ", ", " / "):
            t = AR_HEADS[0] + e.replace(" ", sep)
            assert not flagged(f"{t}: {value(r)}") and not masked(f"{t}: {value(r)}")
    for sep in (", ", "/", "+"):
        t = KO_HEADS[0] + "\ube44\ubc00" + sep + "\ubc88\ud638"
        assert not masked(f"{t}: {value(r)}")


def test_the_client_view_reads_the_words_of_a_label_inside_a_word_apart_by_a_space_or_underscore_only():
    """ffd28ca8 and cbdf5e78 masked a label inside a longer word with its words apart by a space or "_", not "-";
    the strict lane read every form (Persian "Hormuz" + "crossing" with a hyphen)."""
    r = random.Random(SEED + 6)
    t = "\u0647\u0631\u0645\u0632-\u0639\u0628\u0648\u0631"
    assert flagged(f"{t}: {value(r)}") and not masked(f"{t}: {value(r)}")
    assert masked(f"{t.replace('-', '_')}: {value(r)}")


# ================================================================================================ mutations

def test_mutation_without_the_rule_the_lead_probe_cells_are_shown(monkeypatch):
    never = re.compile("(?!x)x")
    monkeypatch.setattr(sg, "_INWORD", {"any": (never, None), "end": (never, None)})
    prev.clear_guard_caches()
    r = random.Random(SEED + 7)
    got = [lanes(t, r) for _n, t in heads()]
    assert sum(1 for x in got if x == (False, False)) == len(got)


def test_mutation_the_client_view_reading_letters_after_the_label_masks_ordinary_words(monkeypatch):
    """"any" in the client view too: "encryption" (Korean "cipher" + a suffix) before a value would be masked."""
    monkeypatch.setattr(sg, "_INWORD", dict(sg._INWORD, end=sg._INWORD["any"]))
    prev.clear_guard_caches()
    r = random.Random(SEED + 8)
    assert masked(f"{KO['cipher']}\ud654: {value(r)}")


def test_mutation_the_strict_lane_without_letters_after_the_label_loses_the_suffixed_forms(monkeypatch):
    monkeypatch.setattr(sg, "_INWORD", dict(sg._INWORD, any=sg._INWORD["end"]))
    prev.clear_guard_caches()
    r = random.Random(SEED + 9)
    assert sum(lanes(t, r)[0] for _n, t in tails()) == 0


def test_mutation_without_the_value_test_bare_words_are_flagged(monkeypatch):
    """The value is load-bearing: read without it (the in-word pattern alone), the bare forms are flagged."""
    seq, glued = sg._INWORD["any"]
    monkeypatch.setattr(sg, "_inword_value", lambda v, inword: seq.search(sg._script_word_view(v)) is not None)
    prev.clear_guard_caches()
    assert sum(flagged(t) for _n, t in heads()) >= 50


# ================================================================================================ owner decision

def test_owner_decision_the_glued_and_head_joins_ffd28ca8_read_are_read_again():
    """OWNER DECISION (ADR 0051 decision 75): an ordinary phrase or glued join whose letters end with a label is
    letter for letter a label with a word before it; with a value after it, it is read again as ffd28ca8 read it
    (Persian "Hormuz" + "crossing"; the glued derived joins of the sample: "group" + "role" glued holds the Russian
    label). Without a value it is not (``tests/test_review_0334eeae.py``)."""
    r = random.Random(SEED + 10)
    hormuz = "\u0647\u0631\u0645\u0632 \u0639\u0628\u0648\u0631"
    assert masked(f"{hormuz}: ready today") and flagged(f"{hormuz}: {value(r)}") and not flagged(hormuz)
    group_role = "\u0433\u0440\u0443\u043f\u043f\u0430\u0440\u043e\u043b\u044c"
    assert masked(f"{group_role}: {value(r)}") and not flagged(group_role)


# ================================================================================================ review F1-F4

GREEK_CODE_PLAIN = "\u03ba\u03c9\u03b4\u03b9\u03ba\u03bf\u03c2"
GREEK_ACCESS = ("\u03c0\u03c1\u03cc\u03c3\u03b2\u03b1\u03c3\u03b7\u03c2", "\u03c0\u03c1\u03bf\u03c3\u03b2\u03b1\u03c3\u03b7\u03c2")
STRICT_JOINS = (".", "/", ":", ",", "+", "|", "--", "__", " - ", "- ", " -", "_ ", ". ", "=", ";")
STRICT_SPELLED = ("/", ",", ", ", "\u00b7", "\u2022", "|", "+", ":", " . ", " - ", " _ ", "=", ";")


def test_f1_the_greek_label_glued_with_one_word_accented_or_the_enclitic_accent():
    r = random.Random(SEED + 20)
    accented, plain = GREEK_ACCESS
    enclitic = accented[:-2] + "\u03ae\u03c2"
    forms = [cbd.GREEK_CODE + plain, GREEK_CODE_PLAIN + accented, cbd.GREEK_CODE + enclitic]
    forms += [f.upper() for f in forms] + [f.replace("\u03c2\u03c0", "\u03c3\u03c0") for f in forms]
    for f in forms:
        assert lanes(f, r) == (True, True), forms.index(f)
    # "code" + "to" + "basis" glued, accented or not: still another word (review cbdf5e78 MUST 1)
    for f in (cbd.GREEK_CODE + "\u03c0\u03c1\u03bf\u03c2\u03b2\u03ac\u03c3\u03b7\u03c2",
              GREEK_CODE_PLAIN + "\u03c0\u03c1\u03bf\u03c2\u03b2\u03b1\u03c3\u03b7"):
        assert not flagged(f"{f}: {value(r)}") and not masked(f"{f}: {value(r)}")


def test_f1_only_greek_letters_take_an_accent_hamza_and_madda_stay_other_letters():
    assert all(sg._first_name(k) == "GREEK" for k in sg._GREEK_ACCENTED)
    assert "\u0627" not in sg._GREEK_ACCENTED and "\u03b1" in sg._GREEK_ACCENTED
    assert "\u03ac" in sg._GREEK_ACCENTED["\u03b1"]


def test_f2_the_strict_lane_reads_the_words_of_a_label_joined_by_any_separator():
    r = random.Random(SEED + 21)
    labels = [AR[0], AR[1], FA, RU_API, "\ube44\ubc00 \ubc88\ud638", EL]
    bad = []
    for i, e in enumerate(labels):
        for j in STRICT_JOINS:
            t = e.replace(" ", j)
            for sep in (": ", " is "):
                if not flagged(f"{t}{sep}{value(r)}") or masked(f"{t}{sep}{value(r)}"):
                    bad.append((i, j, sep))
            if not flagged(t):
                bad.append((i, j, "bare"))
    assert bad == []
    # whole words only: a letter glued before the first word or after the last is not this join
    assert not flagged("\u0627" + AR[0].replace(" ", "/")) and not flagged(AR[0].replace(" ", "/") + "\u0627")


def test_f3_the_strict_lane_reads_a_label_spelled_with_any_separator():
    r = random.Random(SEED + 22)
    entries = [e for e in sg._SCRIPT]
    bad = []
    for i, e in enumerate(entries):
        units = e.replace(" ", "")
        if sg._marked_entry(e):
            continue                                        # (the Hindi and Thai: their marks, below)
        for s in STRICT_SPELLED:
            t = s.join(units)
            if not flagged(f"{t}: {value(r)}") or not flagged(t):
                bad.append((i, s))
    assert bad == []
    thai = cbd.THAI
    for s in STRICT_SPELLED:
        assert flagged(s.join(("\u0e23", "\u0e2b\u0e31", "\u0e2a", "\u0e1c\u0e48", "\u0e32", "\u0e19")))
    # the stem ffd28ca8 listed (a declared written spelling), and the words joined by a digit
    assert all(flagged(s.join(r033.GREEK_FFD_STEM)) for s in STRICT_SPELLED)
    assert flagged(AR[0].replace(" ", "2")) and flagged(AR[0].replace(" ", " 2 "))
    # a two-letter label so spelled is read only as a word of its own
    two = KO["pw"]
    assert flagged(two[0] + "/" + two[1]) and not flagged("\uac00" + two[0] + "/" + two[1])
    assert thai


def test_f2_f3_mutation_without_the_strict_joins_they_are_lost(monkeypatch):
    monkeypatch.setattr(sg, "_STRICT_SEQ", re.compile("(?!x)x"))
    monkeypatch.setattr(sg, "_MARKED_STRICT", re.compile("(?!x)x"))
    prev.clear_guard_caches()
    assert not flagged(AR[0].replace(" ", "/")) and not flagged("/".join(RU))


def test_f4_the_module_compiles_on_python_3_11():
    """Review of cbb6d7b7 F4: an f-string expression with a backslash is a SyntaxError on Python 3.11 (``import api``
    failed). Compiled by a Python 3.11 interpreter when one is on the PATH; on any Python, no f-string expression of
    the module holds a backslash."""
    import ast
    import pathlib
    import shutil
    import subprocess
    src = pathlib.Path(sg.__file__)
    tree = ast.parse(src.read_text(encoding="utf-8"))
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FormattedValue):
            seg = ast.get_source_segment(src.read_text(encoding="utf-8"), node.value) or ""
            if "\\" in seg:
                bad.append(node.lineno)
    assert bad == []
    py311 = shutil.which("python3.11")
    if py311:
        p = subprocess.run([py311, "-c", "import sys; compile(open(sys.argv[1], encoding='utf-8').read(), sys.argv[1], 'exec')",
                            str(src)], capture_output=True, text=True, timeout=60)
        assert p.returncode == 0, p.stderr[-200:]


KO_NOT_PARTICLES = ("\uc694", "\uc57c", "\uc544", "\uace0", "\uba70", "\uba74", "\uc11c", "\uaed8", "\uaed8\uc11c", "\ud55c\ud14c",
                    "\ucc98\ub7fc", "\ubcf4\ub2e4", "\ub9c8\ub2e4", "\uc870\ucc28", "\ubc16\uc5d0", "\uc774\uba70", "\uc774\uace0",
                    "\uc774\uba74", "\uc778", "\uc784", "\uc774\uc57c", "\uc774\uc694", "\uc600\ub2e4", "\uc774\uc5c8\ub2e4", "\ub85c\uc11c",
                    "\uc73c\ub85c\uc11c", "\uc5d0\ub2e4", "\ud55c\ud14c\uc11c", "\uc640\uc758", "\uacfc\uc758", "\uc5d0\ub3c4", "\uc744\uae4c")


def test_the_korean_particles_are_a_closed_list_every_common_other_ending_is_another_word():
    """Mutant (review of cbb6d7b7 SHOULD a): an open list (or one more ending, "-yo"). Each common particle or ending
    that is not declared: bare, not the label; before a value, not masked (the client view reads a declared ending
    only); the strict lane reads it before a value as a label with letters after it (decision 75)."""
    r = random.Random(SEED + 23)
    assert not set(KO_NOT_PARTICLES) & set(sg._KO_PARTICLES)
    for e in KO.values():
        for x in KO_NOT_PARTICLES:
            assert not flagged(e + x) and not masked(f"{e}{x}: {value(r)}"), (len(e), KO_NOT_PARTICLES.index(x))
            assert flagged(f"{e}{x}: {value(r)}")


def test_the_korean_compound_words_joined_by_a_dot_strict_only():
    """Review of cbb6d7b7 SHOULD a: "secret" + "." + "number" was flagged by ffd28ca8 and cbdf5e78 (the letters in a
    row) and masked by neither: read so."""
    r = random.Random(SEED + 24)
    for j in (".", ". ", " . "):
        t = "\ube44\ubc00" + j + "\ubc88\ud638"
        assert flagged(t) and flagged(f"{t}: {value(r)}") and not masked(f"{t}: {value(r)}")


def test_inside_a_word_the_client_view_reads_only_the_forms_it_read_before_a_separator():
    """BAR 1 (M2 of this round): a glued join ending in a label + a DECLARED ending ("type" + "roles" holds the Russian
    stem + a case ending; a Greek word + the label's other case) was masked by no release; the client view reads a
    label inside a longer word only bare or as a full form ffd28ca8 / cbdf5e78 read (the Russian nominative, the
    Greek genitive). The strict lane reads them before a value, as ffd28ca8 did."""
    r = random.Random(SEED + 25)
    type_roles = "\u0442\u0438\u043f\u0430\u0440\u043e\u043b\u0438"
    greek_other_case = "\u03b1" + r033.GREEK_ENTRY.replace(" ", "") + "\u03b7"
    for t in (type_roles, greek_other_case, KO_HEADS[0] + KO["pw"] + "\uac00"):
        assert not masked(f"{t}: {value(r)}") and flagged(f"{t}: {value(r)}") and not flagged(t)
    assert masked(f"{RU_HEADS[0]}{RU}: {value(r)}") and masked(f"\u03b1{r033.GREEK_ENTRY.replace(' ', '')}\u03b7\u03c2: {value(r)}")


def test_the_stem_ffd28ca8_listed_inside_a_word_only_with_the_medial_sigma():
    """M2 of this round: "code" (final sigma) + a word in "prosv-" ("insult"), glued, was read by no release."""
    r = random.Random(SEED + 26)
    insult = "\u03ba\u03c9\u03b4\u03b9\u03ba\u03cc\u03c2\u03c0\u03c1\u03bf\u03c3\u03b2\u03bf\u03bb\u03ae"
    assert not flagged(f"{insult}: {value(r)}") and not masked(f"{insult}: {value(r)}")
    # (nor with the medial sigma after the accented letter: the stem is read without accents, as ffd28ca8 listed it)
    insult_medial = insult.replace("\u03c2", "\u03c3")
    assert not flagged(f"{insult_medial}: {value(r)}") and not flagged(insult_medial[:12])
    assert flagged(f"{r033.GREEK_FFD_STEM}\u03b9\u03bf: {value(r)}")
    # with the final sigma, unaccented: letters after it are other words in the strict lane (ffd28ca8 did not read
    # them); the client view reads the stem right before a separator with a letter before it, as ffd28ca8 did
    final = r033.GREEK_FFD_STEM.replace("\u03c3\u03c0", "\u03c2\u03c0")
    assert not flagged(f"{final}\u03bf\u03bb\u03b7: {value(r)}")
    assert masked(f"\u03b1{final}: {value(r)}") and not flagged(f"\u03b1{final}")


def test_inside_a_word_the_strict_lane_reads_a_value_after_a_space_or_any_short_gap():
    """The lead's probe of fea61bcf: "administrator" / "account" / "user" + "password" written as one word (Korean),
    "administrator" + "password" (Russian), then ONE SPACE and a value: ffd28ca8 flagged them (the label's letters,
    whatever followed). The strict lane reads a value-looking token after one to three characters that are no letter
    (space, tab, line break, punctuation); a word of prose is no value; the client view never read a bare space."""
    r = random.Random(SEED + 27)
    forms = [h + KO["password"] for h in KO_HEADS[:3]] + [RU_HEADS[0] + RU, "\u0627" + AR[0], AR_HEADS[0] + FA,
                                                           HE_HEADS[0] + HE, EL_HEADS[0] + EL]
    for f in forms:
        for gap in (" ", "\t", "\n", "  ", ", ", "; ", " / ", " -- "):
            assert flagged(f"{f}{gap}{value(r)}") and flagged(f"Note the {f}{gap}{value(r)} today."), (forms.index(f), gap)
            assert not masked(f"{f}{gap}{value(r)}") or gap in (", ",), (forms.index(f), gap)
        assert not flagged(f"{f} ready today") and not flagged(f"{f}, more here") and not flagged(f)


def test_mutation_without_the_gap_value_the_space_cells_are_lost(monkeypatch):
    monkeypatch.setattr(sg, "_INWORD_GAP_VALUE", re.compile("(?!x)x"))
    prev.clear_guard_caches()
    r = random.Random(SEED + 28)
    assert not any(flagged(f"{h}{KO['password']} {value(r)}") for h in KO_HEADS[:3])
