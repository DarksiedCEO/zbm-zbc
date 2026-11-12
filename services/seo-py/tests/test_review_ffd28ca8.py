"""The independent review of commit ffd28ca8 (the fix round for the review of 98b76cde), closed.

MUST 1  the client view showed a labelled credential (a label, a separator, a value) to the owning tenant when the
        label was the Polish word in upper case with the stroked L, or the Hindi or the Thai entry of the script list
        in any form (the strict lane missed those two as well).

The owner asked for the CAUSE to be fixed, not the three cases. The cause: a label list entry and the text it is
looked for in did not go through the same normalisation. Now both do (``secret_guard._label_norm``; the client
view's cased text view of the Latin labels is checked against it for every entry at import), the matching tables
are derived from the lists through it at import, and this file proves the property for EVERY entry of every
credential-label list of both lanes, in every spelling: as listed, lower, UPPER, Title, with its natural diacritics
or marks present and absent, NFC and NFD, before a colon, an equals sign and each verb in every case mix.

Corrected by the review of cd0a6c7d (``tests/test_review_cd0a6c7d.py``, ADR 0051 decision 72): "marks absent" was
demanded of every script, which made the Japanese and Hindi keys looser than the text views (an ordinary Japanese
loanword and ordinary Hindi word pairs read as the label). The demand is withdrawn for the scripts whose marks
distinguish words: the Japanese label without its voicing marks and the Hindi label without its vowel signs are
other words and are no longer spellings of the entry (``MARKS_SPELL_THE_WORD``). The key of a script entry is now the
entry read by the function that reads the text, and the assertions on the internals below say so.

Corrected by the review of cbdf5e78 (``tests/test_review_cbdf5e78.py``, ADR 0051 decision 73): a script label is
matched by ONE pattern in both lanes, as a sequence of whole words (the boundary rule); the multi-word entries are
listed with their words apart; the Hindi and Thai entries are read on the marked views (their non-spacing marks
kept), so the Thai label without its marks is another word too (``MARKS_SPELL_THE_WORD``); a stem's declared case
endings are read before the separator in the client view as well (the Russian inflected forms are masked now).

Every credential-shaped value is generated at run time; no assertion message prints a label or a value (list names,
indexes and counts only). Non-ASCII text is written as escapes.
"""

from __future__ import annotations

import itertools
import json
import random
import re
import string
import time
import unicodedata

import pytest

import test_review_2b26d980 as prev
import test_vault_wiring as vw
from agents import secret_guard as sg
from helpers import rid
from ports import ProviderAnswer

SEED = 20261112
LOWER = string.ascii_lowercase


@pytest.fixture(autouse=True)
def _fresh_guard_caches():
    prev.clear_guard_caches()
    yield
    prev.clear_guard_caches()


def rs(r, chars: str, n: int) -> str:
    return "".join(r.choice(chars) for _ in range(n))


def value(r) -> str:
    """A value-like value (letters, two digits, a symbol): what follows a label and its separator."""
    return rs(r, LOWER, 5) + str(r.randint(10, 99)) + r.choice("!#%")


# ================================================================================================ the spellings

# the natural spelling(s) of an entry that is listed without its diacritics, spaces or marks (the lists hold the
# normal form of a Latin label; a script label is listed as its language writes it, or as a stem)
NATURAL = {
    "contrasena": ["contrase\u00f1a"], "motdepasse": ["mot de passe"], "losenord": ["l\u00f6senord"],
    "haslo": ["has\u0142o"], "katasandi": ["kata sandi"], "matkhau": ["m\u1eadt kh\u1ea9u"],
    "parolasi": ["parolas\u0131"], "jelszo": ["jelsz\u00f3"], "paroladordine": ["parola d'ordine", "parola d\u2019ordine"],
    "palavrapasse": ["palavra-passe"], "phrasedepasse": ["phrase de passe"], "sifre": ["\u015fifre"],
    "schlussel": ["schl\u00fcssel"], "cle": ["cl\u00e9"], "apikey": ["api key", "api_key"],
    "accesskey": ["access key"], "accesstoken": ["access token"], "secretkey": ["secret key"],
    "privatekey": ["private key"], "clientsecret": ["client secret"], "seedphrase": ["seed phrase"],
    "connectionstring": ["connection string"], "sharedaccesssignature": ["shared access signature"],
    "\u043f\u0430\u0440\u043e\u043b": ["\u043f\u0430\u0440\u043e\u043b\u044c"],
    # (review cbdf5e78: the multi-word entries are listed with their words apart; the glued spelling the list held
    # until then is a natural spelling of the Persian one only, written with the non-joiner, ``_SCRIPT_FORMS``)
    "\u0643\u0644\u0645\u0629 \u0627\u0644\u0645\u0631\u0648\u0631": ["\u0643\u064e\u0644\u0650\u0645\u064e\u0629 \u0627\u0644\u0645\u064f\u0631\u064f\u0648\u0631"],
    "\u0643\u0644\u0645\u0629 \u0627\u0644\u0633\u0631": ["\u0643\u0644\u0645\u0629 \u0627\u0644\u0633\u0651\u0631"],
    "\u0631\u0645\u0632 \u0639\u0628\u0648\u0631": ["\u0631\u0645\u0632\u0639\u0628\u0648\u0631"],
    "\u05e1\u05d9\u05e1\u05de\u05d4": ["\u05e1\u05b4\u05d9\u05e1\u05b0\u05de\u05b8\u05d4"],
}
# the upper-case spelling a Turkish keyboard gives (the dotted capital I), beside ``str.upper``
TURKISH_UPPER = {"\u015fifre": "\u015e\u0130FRE"}
POLISH, HINDI, THAI = "haslo", "\u092a\u093e\u0938\u0935\u0930\u094d\u0921", "\u0e23\u0e2b\u0e31\u0e2a\u0e1c\u0e48\u0e32\u0e19"
JAPANESE = "\u30d1\u30b9\u30ef\u30fc\u30c9"
# review cd0a6c7d MUST A: in these entries the marks spell the word (the kana voicing marks, the Devanagari vowel
# signs): the entry with them absent is another word, not a spelling of the label (ADR 0051 decision 72). Review
# cbdf5e78 MUST 2: the Thai vowel and tone marks as well (read on the marked views; ADR 0051 decision 73)
MARKS_SPELL_THE_WORD = (JAPANESE, HINDI, THAI)


def strip_marks(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c)[0] != "M")


def spellings(entry: str) -> dict:
    """name -> text: every spelling of one entry (distinct texts; the first name of each is kept)."""
    out: dict = {}
    for bname, b in [("listed", entry)] + [(f"natural{i}", n) for i, n in enumerate(NATURAL.get(entry, []))]:
        forms = {"as_written": b, "lower": b.lower(), "upper": b.upper(), "title": b.title(), "capitalised": b.capitalize()}
        if b in TURKISH_UPPER:
            forms["upper_dotted"] = TURKISH_UPPER[b]
        if not b.isascii() and strip_marks(b) != unicodedata.normalize("NFD", b) and b not in MARKS_SPELL_THE_WORD:
            forms["marks_absent"] = unicodedata.normalize("NFC", strip_marks(b))
            forms["marks_absent_upper"] = forms["marks_absent"].upper()
        if any("\u0600" <= c <= "\u06ff" for c in b):
            forms["elongated"] = b[:2] + "\u0640" + b[2:]             # the tatweel a typeset Arabic word may hold
        for fname, f in forms.items():
            for nf in ("NFC", "NFD"):
                out[f"{bname}.{fname}.{nf}"] = unicodedata.normalize(nf, f)
    seen: set = set()
    return {k: v for k, v in out.items() if not (v in seen or seen.add(v))}


def case_mixes(word: str) -> list:
    return ["".join(p) for p in itertools.product(*[(c.lower(), c.upper()) if c.isalpha() else (c,) for c in word])]


VERBS = ("is", "was", "are", "would be", "will be", "remains")
SEPARATORS = [("colon", ": "), ("colon", ":"), ("equals", "="), ("equals", " = ")] + \
    [(v, f" {m} ") for v in VERBS for m in case_mixes(v)]


def label_lists() -> list:
    """(list name, index, entry) for every entry of every credential-label list of section 2a / section 5."""
    out = [("_JOINED", i, e) for i, e in enumerate(sg._JOINED)]
    out += [("_PIECES", i, e) for i, e in enumerate(sorted(sg._PIECES))]
    out += [("_SCRIPT", i, e) for i, e in enumerate(sg._SCRIPT)]
    out += [("_CV_SCRIPT_FULL", i, e) for i, e in enumerate(sg._CV_SCRIPT_FULL)]
    return out


def matrix(entries: list, separators: list, seed: int) -> dict:
    """Every entry x spelling x separator with a run-time value: the cells, and the misses of each lane by (list,
    index, spelling)."""
    r = random.Random(seed)
    out = {"entries": len(entries), "spellings": 0, "cells": 0, "client_view": {}, "strict": {}}
    for lname, i, e in entries:
        sp = spellings(e)
        out["spellings"] += len(sp)
        for sname, text in sp.items():
            for _sep_name, sep in separators:
                t = f"{text}{sep}{value(r)}"
                out["cells"] += 1
                if "label_value" not in sg.client_view_findings(t):
                    out["client_view"][(lname, i, sname)] = out["client_view"].get((lname, i, sname), 0) + 1
                if sg.check(t) != "a_label":
                    out["strict"][(lname, i, sname)] = out["strict"].get((lname, i, sname), 0) + 1
    return out


# ================================================================================================ MUST 1: one normalisation

def test_the_matching_tables_are_the_lists_through_the_one_normalisation():
    def keys(entries, latin):
        if latin:
            return tuple(dict.fromkeys("".join(c for c in sg._label_norm(e) if c.isalpha()) for e in entries))
        # review cbdf5e78: a script key is the letters of the entry's inspection view (the PREFILTER of both lanes;
        # every key is letters only); the words the boundary rule matches are ``_SCRIPT_SPELL`` (below)
        return tuple(dict.fromkeys(sg._script_letters(sg.normalise(e)) for e in entries))
    assert sg._JOINED_KEYS == keys(sg._JOINED, True)
    assert sg._PIECE_KEYS == frozenset(keys(sg._PIECES, True))
    assert sg._SCRIPT_KEYS == keys(sg._SCRIPT, False) and all(k.isalpha() for k in sg._SCRIPT_KEYS)
    assert len(sg._JOINED_KEYS) == len(set(sg._JOINED)) and len(sg._SCRIPT_KEYS) == len(set(sg._SCRIPT))
    # the words of each entry: each word through the word view of its inspection view (of its MARKED view for the
    # two entries spelled with a non-spacing mark), and nothing else
    marked = [e for e in sg._SCRIPT if sg._marked_entry(e)]
    assert marked == [HINDI, THAI]
    for e, spell in zip(sg._SCRIPT, sg._SCRIPT_SPELL):
        clean = sg._clean_marked if e in marked else sg.normalise
        assert spell[0] == (e in marked) and spell[2] == tuple(sg._script_word_view(clean(w)) for w in e.split(" "))
    # the prefilters of both lanes hold every key, and nothing else
    assert sorted(sg._JOINED_RX.pattern.split("|")) == sorted(sg._JOINED_KEYS)
    assert sorted(sg._PIECES_IN.pattern.split("|")) == sorted(sg._PIECE_KEYS)
    key = {e: sg._script_letters(sg.normalise(e)) for e in sg._SCRIPT}
    assert sorted(sg._SCRIPT_RX.pattern.split("|")) == sorted(re.escape(key[e]) for e in sg._SCRIPT if e not in marked)
    assert sorted(sg._MARKED_RX.pattern.split("|")) == sorted(re.escape(key[e]) for e in marked)
    for k in sorted(set(sg._JOINED_KEYS) | sg._PIECE_KEYS):
        assert sg._CV_LABEL.fullmatch(k) and sg._CV_LABEL.fullmatch(k.upper()), len(k)
    # the one pattern both lanes read takes every entry and every listed full form whole, in every case
    for e in sg._SCRIPT + sg._CV_SCRIPT_FULL:
        rx, clean = (sg._MARKED_SEQ, sg._clean_marked) if e in marked else (sg._SCRIPT_SEQ, sg.normalise)
        for form in (e, e.upper(), e.title()):
            assert rx.fullmatch(sg._script_word_view(clean(form))), len(e)
    # the normal form is a fixed point, and a key of one alphabet is never a key of the other
    every = [(e, True) for e in sg._JOINED + tuple(sg._PIECES)] + [(e, False) for e in sg._SCRIPT + sg._CV_SCRIPT_FULL]
    assert all(sg._label_norm(sg._label_norm(e, latin), latin) == sg._label_norm(e, latin) for e, latin in every)
    assert all(k.isascii() and k.isalpha() for k in set(sg._JOINED_KEYS) | sg._PIECE_KEYS)
    assert not any(k.isascii() for k in sg._SCRIPT_KEYS)
    # what the sink guard reads is unchanged by the derivation (every Latin entry was already its own key)
    assert sg._LABEL_WORDS == frozenset(sg._JOINED) | sg._PIECES | {"apikey", "apikeys", "accesstokens", "authtokens"}


def test_the_text_views_of_both_lanes_are_made_by_the_same_function():
    r = random.Random(SEED + 1)
    alphabet = "aZ \u00e9 \u0141 \u0142 \u0131 \u0130 \u00df \u03a3 \u03c2 \u03cc \u0439 \u0451 \u093e \u094d \u0e31 \u0e48 \u30d1 \u30c9 \ud55c \u0645 \u064e \u0640 \u05d4 \u05b4 -_:=9"
    for _ in range(400):
        t = rs(r, alphabet, r.randint(1, 24))
        v = sg.normalise(t)
        # strict lane: the letters views are the letters of the normal form
        assert sg._letters_view(v) == "".join(c for c in sg._label_norm(v) if c.isalpha())
        assert sg._script_letters(v) == "".join(c for c in sg._label_norm(v, False) if c.isalpha())
        # both lanes, script labels (review cbdf5e78): the word view is the same fold one character at a time, its
        # letters the letters-only view
        assert "".join(c for c in sg._script_word_view(v) if c.isalpha()) == sg._script_letters(v)
    for p in ("\u0141", "\u00e9", "\u03c2", "\u0130"):
        assert sg._fold_piece(p + "x") == sg._label_norm(p + "x")
    # client view, Latin labels: its cased view reads every character of every spelling of every Latin entry as the
    # normal form does (the keys are the lists through ``_label_norm``; ``_cv_latin_keys`` checks the entries at
    # import, this checks their natural spellings)
    chars = {c for e in sg._JOINED + tuple(sg._PIECES) for t in spellings(e).values() for c in t if not c.isascii()}
    assert len(chars) >= 20 and "\u0141" in chars and "\u0130" in chars
    for c in sorted(chars):
        if unicodedata.category(c)[0] == "M" or c == "\u2019":
            continue                                           # (NFD spellings: the views drop a mark before this)
        assert sg._cv_label_view(c).lower() == sg._label_norm(c), hex(ord(c))
    for e in sg._JOINED + tuple(sg._PIECES):
        for t in spellings(e).values():
            v = sg.normalise(t)
            key = "".join(c for c in sg._label_norm(e) if c.isalpha())
            assert "".join(c for c in sg._cv_label_view(v) if c.isalpha()).lower() == key, len(e)
            assert sg._letters_view(v) == key, len(e)
    assert sg._cv_latin_keys("_JOINED", sg._JOINED) == sg._JOINED_KEYS
    assert frozenset(sg._cv_latin_keys("_PIECES", sorted(sg._PIECES))) == sg._PIECE_KEYS


def test_the_letters_nfkd_does_not_decompose():
    """The stroked / dotless letters that occur in a label language are folded in both lanes, and so is the sharp
    s (review cd0a6c7d MUST B: the client view left it as written and showed the German label spelled with it); the
    two ligatures by neither, and no entry or natural spelling needs those."""
    folded = {"\u0141": "l", "\u0142": "l", "\u00d8": "o", "\u00f8": "o", "\u0110": "d", "\u0111": "d", "\u0131": "i", "\u0130": "i"}
    for c, want in folded.items():
        assert unicodedata.normalize("NFKD", c) == c or c == "\u0130"
        assert sg._label_norm(c) == want, hex(ord(c))
        assert sg._cv_label_view(c) == (want.upper() if c.lower() != c else want), hex(ord(c))
    assert sg._label_norm("\u00df") == sg._label_norm("\u1e9e") == "ss"
    assert sg._cv_label_view("\u00df") == "ss" and sg._cv_label_view("\u1e9e") == "SS"
    for c in "\u00e6\u00c6\u0153\u0152":
        assert not sg._label_norm(c).isascii() and not sg._cv_label_view(c).isascii()
    texts = [e for _l, _i, e in label_lists()] + [n for ns in NATURAL.values() for n in ns]
    assert not any(c in t.lower() for t in texts for c in "\u00e6\u0153\u00f8\u0111")
    # the reported letter: the client view read the capital stroked L as a capital I. A lookalike of a capital I
    # from another script (the Greek iota, the Cyrillic palochka) still reads I
    assert sg._cv_label_view("HAS\u0141O") == "HASLO" and sg._cv_label_view("P\u0399N P\u04c0N") == "PIN PIN"
    # an entry the two lanes would read differently cannot be listed (the Greek capital eta: the strict lane reads
    # it through its lower-case form, an "n"; the client view reads the capital, an "H"). One with the sharp s can
    # be, now that both lanes read it ``ss``
    with pytest.raises(ValueError) as e:
        sg._cv_latin_keys("_TEST", ("toke\u0397",))
    assert "entry 0" in str(e.value)
    assert sg._cv_latin_keys("_TEST", ("stra\u00dfe",)) == ("strasse",)


def test_what_the_client_view_read_before_it_still_reads():
    """The client view's own text view is kept as it was but for the one corrected letter; what follows a script
    label is read on the view as written. Each form here was masked at ffd28ca8."""
    r = random.Random(SEED + 7)
    forms = ["\u03a4\u038c\u039a\u0395\u039d", "\u03a4\u039f\u039a\u0395\u039d", "\u03a1\u0391\u0405\u0405W\u039fRD", "p\u0430ssword", "S\u0395CR\u0395\u03a4"]
    assert [len(x) for x in forms if "label_value" not in sg.client_view_findings(f"{x}: {value(r)}")] == []
    korean = "\ube44\ubc00\ubc88\ud638"
    glued = rs(r, LOWER, 4) + "\u0926\u0941\u0915\u093e\u0928"                              # a value with a spacing mark in it
    assert "label_value" in sg.client_view_findings(f"{korean} was {glued}")
    assert sg._cv_script_ends("ab", "ab", list(re.finditer("b", "ab"))) == [2]
    # review cbdf5e78: the Thai and Hindi entries are read on the marked view (their non-spacing marks kept), what
    # follows them on that view as written; on a view without the marks neither is found
    for label in (THAI, HINDI):
        text = f"{label}: {value(r)}"
        mv = sg._clean_marked(text)
        wv = sg._script_word_view(mv)
        (end,) = sg._cv_script_ends(mv, wv, list(sg._MARKED_SEQ.finditer(wv)))
        assert mv[:end] == label and mv[end] == ":"
        assert list(sg._SCRIPT_SEQ.finditer(sg._script_word_view(sg.normalise(text)))) == []


@pytest.mark.parametrize("name,entries,latin,piece", [
    ("a Latin entry in another script", ("\u5bc6\u7801",), True, False),
    ("a Latin entry that keeps a non-ASCII letter", ("mot de p\u00e6sse",), True, False),
    ("a script entry in ASCII letters", ("parol",), False, False),
    ("a piece label of two words", ("pass word",), True, True),
    ("an entry of one letter", ("p",), True, False),
    ("an entry of marks only", ("\u094d\u093e",), False, False),
])
def test_an_entry_that_could_not_be_matched_stops_the_import(name, entries, latin, piece):
    with pytest.raises(ValueError) as e:
        sg._label_keys("_TEST", entries, latin, piece)
    assert "entry 0" in str(e.value) and entries[0] not in str(e.value)
    # an entry with its diacritics, capitals and spaces is taken: it is listed as its key
    assert sg._label_keys("_TEST", ("Contrase\u00f1a", "Mot de Passe"), True) == ("contrasena", "motdepasse")
    assert sg._label_keys("_TEST", ("\u039a\u03a9\u0394\u0399\u039a\u038c\u03a3",), False) == ("\u03ba\u03c9\u03b4\u03b9\u03ba\u03bf\u03c3",)


# ================================================================================================ MUST 1: the matrix

def test_every_label_entry_is_matched_in_every_spelling_in_both_lanes():
    """THE self-consistency property: entries x spellings x separators (every case mix of every verb), a run-time
    value after each: masked in the client view AND flagged as a label by the strict lane. 0 misses."""
    entries = label_lists()
    t = time.perf_counter()
    m = matrix(entries, SEPARATORS, SEED + 2)
    by_list = {n: sum(1 for x in entries if x[0] == n) for n in ("_JOINED", "_PIECES", "_SCRIPT", "_CV_SCRIPT_FULL")}
    print(f"label matrix: lists={by_list} entries={m['entries']} spellings={m['spellings']} "
          f"separators={len(SEPARATORS)} cells={m['cells']} client_view_misses={sum(m['client_view'].values())} "
          f"strict_misses={sum(m['strict'].values())} seconds={time.perf_counter() - t:.1f}")
    assert m["entries"] == len(sg._JOINED) + len(sg._PIECES) + len(sg._SCRIPT) + len(sg._CV_SCRIPT_FULL) >= 96
    assert m["spellings"] >= 390 and len(SEPARATORS) == 4 + 4 + 8 + 8 + 128 + 64 + 128
    assert m["cells"] == m["spellings"] * len(SEPARATORS)
    assert sorted(m["client_view"]) == [] and sorted(m["strict"]) == []


def test_the_three_reported_forms_are_masked_and_flagged():
    r = random.Random(SEED + 3)
    polish = [s for n, s in spellings(POLISH).items() if "\u0142" in s.lower()]
    assert "HAS\u0141O" in polish and len(polish) >= 3
    for label in polish + list(spellings(HINDI).values()) + list(spellings(THAI).values()):
        for sep in (": ", "=", " is ", " IS ", " Was "):
            t = f"{label}{sep}{value(r)}"
            assert "label_value" in sg.client_view_findings(t), (len(label), sep)
            assert sg.check(t) == "a_label", (len(label), sep)
    # the bare word in prose is not masked in the client view (a label needs its separator and a value)
    assert [len(x) for x in polish + [HINDI, THAI] if sg.client_view_findings(f"{x} {rs(r, LOWER, 6)}")] == []


def old_tables(monkeypatch) -> None:
    """Mutation: the matching of ffd28ca8 back (entries compared as written, the client view's own fold)."""
    class Old(dict):
        def __missing__(self, o):
            c = chr(o)
            return sg._fold_confusables("".join(x for x in unicodedata.normalize("NFKD", c)
                                                if not unicodedata.combining(x)))
    raw = sorted(set(s.replace(" ", "") for s in sg._SCRIPT), key=len, reverse=True)
    monkeypatch.setattr(sg, "_CV_LATIN_VIEW", Old())
    # (review cbdf5e78: the tables of that release in the names of this one: the entries as written, letters in a
    # row, the client view's "[ _]?" on the view as written, no marked views)
    monkeypatch.setattr(sg, "_SCRIPT_WORD", {})
    monkeypatch.setattr(sg, "_SCRIPT_SEQ", re.compile("|".join("[ _]?".join(re.escape(c) for c in s) for s in raw),
                                                      re.IGNORECASE))
    monkeypatch.setattr(sg, "_script_letters", lambda v: "".join(c for c in v.lower() if c.isalpha()))
    monkeypatch.setattr(sg, "_SCRIPT_RX", re.compile("|".join(re.escape(s) for s in raw)))
    monkeypatch.setattr(sg, "_SCRIPT_UNSPACED_RX", sg._SCRIPT_RX)
    monkeypatch.setattr(sg, "_MARKED_RX", None)


def test_mutation_with_the_old_tables_the_matrix_finds_the_reported_entries(monkeypatch):
    """The matrix is load-bearing: with entries compared as written again it reports exactly the reported gaps (the
    Polish entry in the client view; the Hindi and Thai entries in both lanes) and the forms no lane normalised."""
    old_tables(monkeypatch)
    entries = label_lists()
    seps = [s for s in SEPARATORS if s[0] in ("colon", "equals", "is")]
    m = matrix(entries, seps, SEED + 4)
    cv = {(n, i) for n, i, _s in m["client_view"]}
    st = {(n, i) for n, i, _s in m["strict"]}
    at = {e: (n, i) for n, i, e in entries}
    assert {at[POLISH], at[HINDI], at[THAI]} <= cv and {at[HINDI], at[THAI]} <= st and at[POLISH] not in st
    polish_misses = {s for n, i, s in m["client_view"] if (n, i) == at[POLISH]}
    assert polish_misses == {"natural0.upper.NFC"}, sorted(polish_misses)   # lower and Title were masked: only UPPER


# ================================================================================================ residuals (asserted)

def test_residual_an_inflected_form_of_a_stem_entry_is_flagged_but_not_masked():
    """Known limit (ADR 0051 decision 71), asserted, not hidden: two ``_SCRIPT`` entries are stems. Review cbdf5e78
    (decision 73): both lanes now read a stem with one of its DECLARED endings as one whole word, so the Russian case
    forms are masked as well as flagged. The Greek stem is in the label's SECOND word: an inflected FIRST word is not
    the label in either lane (as before) and is shown."""
    r = random.Random(SEED + 5)
    inflected = ["\u043f\u0430\u0440\u043e\u043b\u044f", "\u043f\u0430\u0440\u043e\u043b\u0435\u043c", "\u043f\u0430\u0440\u043e\u043b\u044e",
                 "\u03ba\u03c9\u03b4\u03b9\u03ba\u03bf\u03cd \u03c0\u03c1\u03cc\u03c3\u03b2\u03b1\u03c3\u03b7\u03c2", "\u03ba\u03c9\u03b4\u03b9\u03ba\u03cc \u03c0\u03c1\u03cc\u03c3\u03b2\u03b1\u03c3\u03b7\u03c2"]
    shown = 0
    for label in inflected:
        t = f"{label}: {value(r)}"
        shown += "label_value" not in sg.client_view_findings(t)
    russian = [sg.check(f"{x}: {value(r)}") for x in inflected[:3]]
    greek = [sg.check(f"{x}: {value(r)}") for x in inflected[3:]]
    print(f"inflected stem forms: shown in the client view {shown} of {len(inflected)}; strict lane: Russian "
          f"{russian}, Greek (the FIRST word inflected: the stem is not in it) {greek}")
    assert shown == 2 and russian == ["a_label"] * 3 and greek == [None] * 2
    assert all("label_value" in sg.client_view_findings(f"{x}: {value(r)}") for x in inflected[:3])


# ================================================================================================ end to end

def e2e_forms(r) -> list:
    """The three reported forms and 20 other entries in upper case."""
    reported = ["HAS\u0141O", HINDI, THAI]
    others = [e for _n, _i, e in label_lists() if e not in (POLISH, HINDI, THAI)]
    latin = [e for e in others if e.isascii()]
    script = [e for e in others if not e.isascii()]
    return reported + [e.upper() for e in r.sample(latin, 14) + r.sample(script, 6)]


def e2e_answers(r, forms: list) -> tuple:
    """One scripted answer per form: the form and a value in the answer text (next to the brand term), in the cited
    page's title and in its hint. Returns (answers, values)."""
    answers, values = [], []
    for i, label in enumerate(forms):
        url = f"https://site.test/guides/{vw.MARK}-note-{i}"
        vals = [rs(r, LOWER, 9) + str(r.randint(10, 99)) for _ in range(3)]
        a = ProviderAnswer("ANSWER", f"Z Best Media sellers note {i}. {label}: {vals[0]}", [url], "m", "1",
                           cite_from_text=False, provider="openai", asked_at="2026-10-09T18:00:00Z")
        a.citation_titles = {url: f"{label} = {vals[1]}"}
        a.citation_hints = {url: f"{label} is {vals[2]}"}
        answers.append(a)
        values.extend(vals)
    return answers, values


def e2e_run(tmp_path, r) -> tuple:
    forms = e2e_forms(r)
    answers, values = e2e_answers(r, forms)
    h = vw.harness(tmp_path)
    port = vw.Port(answers)
    h.ports.engines["openai"] = port
    h.ok(h.post("/tenants/zbm/domains", {"request_id": rid(), "domains": [vw.DOMAIN]}, andre=True))
    prompts = [f"who recovers marketplace revenue, question {i}" for i in range(8)]
    psid = h.ok(h.post("/tenants/zbm/prompt-sets", {
        "request_id": rid(), "name": "core", "brand_terms": ["Z Best Media"], "competitor_domains": ["competitor.test"],
        "prompts": prompts, "engines": ["openai"]}), 201)["prompt_set_id"]
    run = h.ok(h.post("/tenants/zbm/citation-runs", {"request_id": rid(), "domain": vw.DOMAIN,
                                                     "prompt_set_id": psid}), 201)
    assert run["status"] == "completed" and port.asked >= len(forms)
    view = h.ok(h.get(f"/tenants/zbm/citation-runs/{run['citation_run_id']}", caller="dashboard"))
    return forms, values, view


def shown_values(view, values: list) -> int:
    out = json.dumps(view, ensure_ascii=False) + json.dumps(view)
    return sum(1 for v in values if v in out)


def evidence_fields(view) -> list:
    return [(k, r["evidence"][k]) for r in view["result"]["facts"]["rows"] for k in ("excerpt", "title", "hint")
            if r.get("evidence") and r["evidence"].get(k)]


def test_a_citation_run_carrying_the_forms_is_read_by_its_owner_with_no_value_shown(tmp_path):
    r = random.Random(SEED + 6)
    forms, values, view = e2e_run(tmp_path, r)
    assert len(forms) == 23 and len(values) == 69 and len(set(values)) == 69
    fields = evidence_fields(view)
    alerts = [f for _k, f in fields if f["alert"]]
    print(f"end to end: forms={len(forms)} planted values={len(values)} evidence fields read={len(fields)} "
          f"masked with an alert={len(alerts)} values shown={shown_values(view, values)}")
    assert shown_values(view, values) == 0
    assert len(fields) >= 69 and len(alerts) == len(fields)
    assert {f["reason"] for f in alerts} == {"LOOKS_LIKE_CREDENTIAL"}
    assert vw.MARK in json.dumps(view)                         # the owner does read the run: a legitimate URL in full


def test_mutation_with_the_old_tables_the_owner_is_shown_the_reported_values(tmp_path, monkeypatch):
    old_tables(monkeypatch)
    r = random.Random(SEED + 6)
    _forms, values, view = e2e_run(tmp_path, r)
    assert shown_values(view, values[:9]) >= 3                 # the three reported forms lead the list


# ================================================================================================ no regression: prose

_SUBJECTS = """The shop|Our team|Each seller|A new customer|The warehouse|Every reviewer|The brand|A returning buyer|
The marketplace|Our support desk|The supplier|A small business|The agency|This guide|The report|Their newsletter|
The checkout page|A loyal subscriber|The product page|Our founder|The courier|An independent tester|The community|
A first-time visitor""".replace("\n", "").split("|")
_VERBS = """ships|compares|recommends|reviews|updates|tracks|explains|lists|restocks|packs|delivers|measures|
promotes|discounts|features|tests|returns|bundles|sources|photographs""".replace("\n", "").split("|")
_OBJECTS = """the blue widget|a set of ceramic mugs|organic dog treats|the winter catalogue|two cotton shirts|
a standing desk|the holiday bundle|refurbished laptops|a pair of trail shoes|the starter kit|scented candles|
a password manager|the cookie policy|its API keys guide|the token economy primer|a secret menu item|
the key takeaway|an authorization letter template|the PIN pad stand|a session of yoga|the pass rate chart|
garden tools|a leather wallet|the travel pillow|reusable bottles|a phone case|the summer range|wireless earbuds|
a cast iron pan|the loyalty programme""".replace("\n", "").split("|")
_TAILS = """every Monday morning|before the holiday rush|with free returns|for small sellers|at a fair price|
in under two days|across three regions|since the spring launch|after a short review|on the new storefront|
with a clear size chart|for first orders over $50|during the clearance sale|in recycled packaging|
without any hidden fees|as the survey suggested|by the end of the quarter|with detailed photos|
for 12 weeks in a row|at 9:00 and again at 17:30|in 2025 and 2026|to 1,250 subscribers|
with a rating of 4.8 out of 5|under order 112-1234567""".replace("\n", "").split("|")
_NOTES = """Shipping: free over $50 this week.|Colour: navy blue with white trim.|Note: orders placed today ship
 Monday.|Price: $12.99 for the pair.|The answer is simple and well tested.|Delivery was quick and the box arrived
 intact.|Sizes are listed below the photos.|Stock remains limited until March.|The result would be a faster
 checkout.|Returns will be accepted for 30 days.|Rating: 4.8 out of 5 from 312 reviews.|Material: recycled cotton
 and linen.""".replace("\n", "").split("|")


def english_units(n: int = 3200, seed: int = SEED + 10) -> list:
    """Neutral e-commerce prose: generated sentences (some hold a credential WORD in prose) and short notes with a
    colon or a verb and no label."""
    r = random.Random(seed)
    out: list = []
    seen: set = set()
    while len(out) < n:
        if len(out) % 8 == 7:
            t = r.choice(_NOTES) + " " + r.choice(_SUBJECTS) + " " + r.choice(_VERBS) + " " + r.choice(_OBJECTS) + "."
        else:
            t = f"{r.choice(_SUBJECTS)} {r.choice(_VERBS)} {r.choice(_OBJECTS)} {r.choice(_TAILS)}."
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _sentences(pools: list, n: int, seed: int, joiner: str = " ", end: str = ".") -> list:
    r = random.Random(seed)
    out: list = []
    seen: set = set()
    while len(out) < n:
        t = joiner.join(r.choice(p) for p in pools) + end
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


_POLISH = [
    "Sklep|Klient|Zam\u00f3wienie|Dostawa|Produkt|P\u0142atno\u015b\u0107|Opinia|Zesp\u00f3\u0142|Hurtownia|Oferta|Promocja|W\u0142a\u015bciciel|"
    "Sprzedawca|Kupuj\u0105cy|Paczka|\u0141\u00f3dzki magazyn".split("|"),
    "wysy\u0142a|oferuje|sprawdza|dostarcza|poleca|zamawia|op\u0142aca|ogl\u0105da|wybiera|por\u00f3wnuje|ocenia|zwraca|"
    "uk\u0142ada|wa\u017cy\u0142|zg\u0142asza".split("|"),
    "towar|paczk\u0119|ma\u0142e pude\u0142ko|ciep\u0142\u0105 kurtk\u0119|\u017c\u00f3\u0142ty sweter|bia\u0142\u0105 koszul\u0119|nowe buty|\u0142\u00f3\u017cko dla kota|\u015bwie\u017ce jab\u0142ka|"
    "d\u0142ugi kabel|zielon\u0105 herbat\u0119|s\u0142odkie ciasto|has\u0142a reklamowe|g\u0142\u00f3wn\u0105 stron\u0119|ca\u0142\u0105 list\u0119|z\u0142oty \u0142a\u0144cuszek".split("|"),
    "w poniedzia\u0142ek|ka\u017cdego dnia|przed \u015bwi\u0119tami|po po\u0142udniu|w zesz\u0142ym tygodniu|bez dodatkowych op\u0142at|"
    "z du\u017c\u0105 zni\u017ck\u0105|do ko\u0144ca miesi\u0105ca|w ca\u0142ej Polsce|przez internet|w \u0141odzi|w Gda\u0144sku|we Wroc\u0142awiu|"
    "w Bia\u0142ymstoku|wed\u0142ug opinii|dla sta\u0142ych klient\u00f3w".split("|"),
]
_HINDI = [
    "\u0926\u0941\u0915\u093e\u0928|\u0917\u094d\u0930\u093e\u0939\u0915|\u0915\u0902\u092a\u0928\u0940|\u091f\u0940\u092e|\u0935\u093f\u0915\u094d\u0930\u0947\u0924\u093e|\u0916\u0930\u0940\u0926\u093e\u0930|\u0935\u0947\u092c\u0938\u093e\u0907\u091f|\u092c\u093e\u091c\u093c\u093e\u0930|\u0917\u094b\u0926\u093e\u092e|\u0939\u092e\u093e\u0930\u0940 \u0938\u0947\u0935\u093e|\u0928\u092f\u093e \u0938\u094d\u091f\u094b\u0930|\u092a\u093e\u0938 \u0915\u093e \u092c\u093e\u091c\u093c\u093e\u0930".split("|"),
    "\u0939\u0930 \u0926\u093f\u0928|\u0906\u091c \u0938\u0941\u092c\u0939|\u0915\u0932 \u0936\u093e\u092e|\u0907\u0938 \u0939\u092b\u094d\u0924\u0947|\u092c\u093f\u0928\u093e \u0926\u0947\u0930\u0940 \u0915\u0947|\u0915\u092e \u0926\u093e\u092e \u092a\u0930|\u0926\u093f\u0932\u094d\u0932\u0940 \u092e\u0947\u0902|\u092e\u0941\u0902\u092c\u0908 \u092e\u0947\u0902|\u092a\u0942\u0930\u0947 \u092d\u093e\u0930\u0924 \u092e\u0947\u0902|\u0911\u0928\u0932\u093e\u0907\u0928|"
    "\u0924\u094d\u092f\u094b\u0939\u093e\u0930 \u0938\u0947 \u092a\u0939\u0932\u0947|\u0935\u093e\u0930\u094d\u0921 \u0928\u0902\u092c\u0930 \u092a\u093e\u0901\u091a \u092e\u0947\u0902".split("|"),
    "\u0928\u092f\u093e \u0938\u093e\u092e\u093e\u0928|\u0938\u0938\u094d\u0924\u093e \u0915\u092a\u0921\u093c\u093e|\u0905\u091a\u094d\u091b\u0940 \u0915\u093f\u0924\u093e\u092c|\u0924\u093e\u091c\u093c\u093e \u092b\u0932|\u0917\u0930\u092e \u091a\u093e\u092f|\u092e\u094b\u092c\u093e\u0907\u0932 \u092b\u094b\u0928|\u0932\u093e\u0932 \u091c\u0942\u0924\u0947|\u0938\u0942\u0924\u0940 \u0915\u092e\u0940\u091c\u093c|\u0932\u0915\u0921\u093c\u0940 \u0915\u0940 \u092e\u0947\u091c\u093c|"
    "\u092e\u0940\u0920\u093e \u092a\u0915\u0935\u093e\u0928|\u0938\u0935\u093e\u0930\u0940 \u0915\u093e \u091f\u093f\u0915\u091f|\u092a\u0938\u0902\u0926\u0940\u0926\u093e \u0909\u092a\u0939\u093e\u0930".split("|"),
    "\u092c\u0947\u091a\u0924\u0940 \u0939\u0948|\u0916\u0930\u0940\u0926\u0924\u093e \u0939\u0948|\u092d\u0947\u091c\u0924\u0940 \u0939\u0948|\u0926\u093f\u0916\u093e\u0924\u093e \u0939\u0948|\u092a\u0938\u0902\u0926 \u0915\u0930\u0924\u093e \u0939\u0948|\u0926\u0947\u0916\u0924\u093e \u0939\u0948|\u0932\u093e\u0924\u093e \u0939\u0948|\u0926\u0947\u0924\u0940 \u0939\u0948|\u091c\u093e\u0901\u091a\u0924\u093e \u0939\u0948|\u0932\u094c\u091f\u093e\u0924\u0940 \u0939\u0948".split("|"),
]
_THAI = [
    "\u0e23\u0e49\u0e32\u0e19\u0e04\u0e49\u0e32|\u0e25\u0e39\u0e01\u0e04\u0e49\u0e32|\u0e17\u0e35\u0e21\u0e07\u0e32\u0e19|\u0e1c\u0e39\u0e49\u0e02\u0e32\u0e22|\u0e1c\u0e39\u0e49\u0e0b\u0e37\u0e49\u0e2d|\u0e40\u0e27\u0e47\u0e1a\u0e44\u0e0b\u0e15\u0e4c|\u0e15\u0e25\u0e32\u0e14|\u0e04\u0e25\u0e31\u0e07\u0e2a\u0e34\u0e19\u0e04\u0e49\u0e32|\u0e1a\u0e23\u0e34\u0e01\u0e32\u0e23\u0e02\u0e2d\u0e07\u0e40\u0e23\u0e32|\u0e23\u0e49\u0e32\u0e19\u0e43\u0e2b\u0e21\u0e48".split("|"),
    "\u0e08\u0e31\u0e14\u0e2a\u0e48\u0e07|\u0e41\u0e19\u0e30\u0e19\u0e33|\u0e15\u0e23\u0e27\u0e08\u0e2a\u0e2d\u0e1a|\u0e40\u0e1b\u0e23\u0e35\u0e22\u0e1a\u0e40\u0e17\u0e35\u0e22\u0e1a|\u0e2a\u0e31\u0e48\u0e07\u0e0b\u0e37\u0e49\u0e2d|\u0e23\u0e35\u0e27\u0e34\u0e27|\u0e25\u0e14\u0e23\u0e32\u0e04\u0e32|\u0e04\u0e31\u0e14\u0e40\u0e25\u0e37\u0e2d\u0e01|\u0e1a\u0e23\u0e23\u0e08\u0e38|\u0e2a\u0e48\u0e07\u0e04\u0e37\u0e19".split("|"),
    "\u0e2a\u0e34\u0e19\u0e04\u0e49\u0e32\u0e43\u0e2b\u0e21\u0e48|\u0e40\u0e2a\u0e37\u0e49\u0e2d\u0e1c\u0e49\u0e32|\u0e23\u0e2d\u0e07\u0e40\u0e17\u0e49\u0e32|\u0e2d\u0e32\u0e2b\u0e32\u0e23\u0e2a\u0e14|\u0e40\u0e04\u0e23\u0e37\u0e48\u0e2d\u0e07\u0e14\u0e37\u0e48\u0e21|\u0e2b\u0e19\u0e31\u0e07\u0e2a\u0e37\u0e2d\u0e14\u0e35|\u0e23\u0e2b\u0e31\u0e2a\u0e2a\u0e34\u0e19\u0e04\u0e49\u0e32|\u0e23\u0e2b\u0e31\u0e2a\u0e44\u0e1b\u0e23\u0e29\u0e13\u0e35\u0e22\u0e4c|\u0e02\u0e2d\u0e07\u0e02\u0e27\u0e31\u0e0d|\u0e42\u0e17\u0e23\u0e28\u0e31\u0e1e\u0e17\u0e4c\u0e21\u0e37\u0e2d\u0e16\u0e37\u0e2d|"
    "\u0e0a\u0e32\u0e23\u0e49\u0e2d\u0e19|\u0e01\u0e23\u0e30\u0e40\u0e1b\u0e4b\u0e32\u0e2b\u0e19\u0e31\u0e07".split("|"),
    "\u0e17\u0e38\u0e01\u0e27\u0e31\u0e19|\u0e27\u0e31\u0e19\u0e19\u0e35\u0e49|\u0e1e\u0e23\u0e38\u0e48\u0e07\u0e19\u0e35\u0e49|\u0e17\u0e31\u0e48\u0e27\u0e1b\u0e23\u0e30\u0e40\u0e17\u0e28|\u0e17\u0e35\u0e48\u0e01\u0e23\u0e38\u0e07\u0e40\u0e17\u0e1e|\u0e17\u0e35\u0e48\u0e40\u0e0a\u0e35\u0e22\u0e07\u0e43\u0e2b\u0e21\u0e48|\u0e23\u0e32\u0e04\u0e32\u0e16\u0e39\u0e01|\u0e04\u0e38\u0e13\u0e20\u0e32\u0e1e\u0e14\u0e35|\u0e1c\u0e48\u0e32\u0e19\u0e01\u0e32\u0e23\u0e15\u0e23\u0e27\u0e08\u0e2a\u0e2d\u0e1a|\u0e2a\u0e48\u0e07\u0e40\u0e23\u0e47\u0e27|"
    "\u0e2b\u0e25\u0e31\u0e07\u0e01\u0e32\u0e23\u0e02\u0e32\u0e22|\u0e01\u0e48\u0e2d\u0e19\u0e40\u0e17\u0e28\u0e01\u0e32\u0e25".split("|"),
]
_JAPANESE = [
    "\u5e97\u8217\u306f|\u304a\u5ba2\u69d8\u306f|\u30c1\u30fc\u30e0\u306f|\u8ca9\u58f2\u8005\u306f|\u8cfc\u5165\u8005\u306f|\u30b5\u30a4\u30c8\u306f|\u5e02\u5834\u306f|\u5009\u5eab\u306f|\u30b5\u30fc\u30d3\u30b9\u306f|\u65b0\u3057\u3044\u5e97\u306f".split("|"),
    "\u65b0\u5546\u54c1\u3092|\u30d0\u30b9\u30b1\u30c3\u30c8\u3092|\u30cf\u30fc\u30c9\u30a6\u30a7\u30a2\u3092|\u30d1\u30b9\u30bf\u3092|\u30ef\u30fc\u30eb\u30c9\u30ac\u30a4\u30c9\u3092|\u30b9\u30dd\u30fc\u30c4\u30a6\u30a7\u30a2\u3092|\u30c7\u30fc\u30bf\u3092|"
    "\u30cf\u30a6\u30b9\u30ef\u30a4\u30f3\u3092|\u30d0\u30b9\u306e\u5207\u7b26\u3092|\u30dd\u30b9\u30c8\u30ab\u30fc\u30c9\u3092|\u30b9\u30ef\u30c3\u30d7\u54c1\u3092|\u6e29\u304b\u3044\u304a\u8336\u3092".split("|"),
    "\u6bce\u65e5|\u4eca\u65e5|\u660e\u65e5|\u5168\u56fd\u3067|\u6771\u4eac\u3067|\u5927\u962a\u3067|\u5b89\u3044\u4fa1\u683c\u3067|\u9ad8\u3044\u54c1\u8cea\u3067|\u30bb\u30fc\u30eb\u306e\u524d\u306b|\u30aa\u30f3\u30e9\u30a4\u30f3\u3067".split("|"),
    "\u914d\u9001\u3057\u307e\u3059|\u7d39\u4ecb\u3057\u307e\u3059|\u78ba\u8a8d\u3057\u307e\u3059|\u6bd4\u8f03\u3057\u307e\u3059|\u6ce8\u6587\u3057\u307e\u3059|\u30ec\u30d3\u30e5\u30fc\u3057\u307e\u3059|\u5272\u5f15\u3057\u307e\u3059|\u9078\u3073\u307e\u3059|\u5305\u307f\u307e\u3059|"
    "\u8fd4\u54c1\u3057\u307e\u3059".split("|"),
]
_RUSSIAN = [
    "\u041c\u0430\u0433\u0430\u0437\u0438\u043d|\u041a\u043b\u0438\u0435\u043d\u0442|\u0417\u0430\u043a\u0430\u0437|\u0421\u043a\u043b\u0430\u0434|\u041f\u0440\u043e\u0434\u0430\u0432\u0435\u0446|\u041f\u043e\u043a\u0443\u043f\u0430\u0442\u0435\u043b\u044c|\u0421\u0430\u0439\u0442|\u041d\u0430\u0448\u0430 \u043a\u043e\u043c\u0430\u043d\u0434\u0430|\u041d\u043e\u0432\u044b\u0439 \u043e\u0442\u0434\u0435\u043b|\u0421\u043b\u0443\u0436\u0431\u0430 \u0434\u043e\u0441\u0442\u0430\u0432\u043a\u0438".split("|"),
    "\u043e\u0442\u043f\u0440\u0430\u0432\u043b\u044f\u0435\u0442|\u043f\u0440\u0435\u0434\u043b\u0430\u0433\u0430\u0435\u0442|\u043f\u0440\u043e\u0432\u0435\u0440\u044f\u0435\u0442|\u0434\u043e\u0441\u0442\u0430\u0432\u043b\u044f\u0435\u0442|\u0440\u0435\u043a\u043e\u043c\u0435\u043d\u0434\u0443\u0435\u0442|\u0441\u0440\u0430\u0432\u043d\u0438\u0432\u0430\u0435\u0442|\u043e\u0446\u0435\u043d\u0438\u0432\u0430\u0435\u0442|\u0432\u043e\u0437\u0432\u0440\u0430\u0449\u0430\u0435\u0442|\u0443\u043f\u0430\u043a\u043e\u0432\u044b\u0432\u0430\u0435\u0442|"
    "\u0432\u044b\u0431\u0438\u0440\u0430\u0435\u0442".split("|"),
    "\u043d\u043e\u0432\u044b\u0439 \u0442\u043e\u0432\u0430\u0440|\u0442\u0451\u043f\u043b\u0443\u044e \u043a\u0443\u0440\u0442\u043a\u0443|\u0431\u0435\u043b\u0443\u044e \u0440\u0443\u0431\u0430\u0448\u043a\u0443|\u0441\u0432\u0435\u0436\u0438\u0435 \u044f\u0431\u043b\u043e\u043a\u0438|\u0434\u043b\u0438\u043d\u043d\u044b\u0439 \u043a\u0430\u0431\u0435\u043b\u044c|\u0437\u0435\u043b\u0451\u043d\u044b\u0439 \u0447\u0430\u0439|\u043a\u043b\u044e\u0447 \u043e\u0442 \u0441\u043a\u043b\u0430\u0434\u0430|"
    "\u043f\u043e\u0434\u0430\u0440\u043e\u0447\u043d\u044b\u0439 \u043d\u0430\u0431\u043e\u0440|\u043a\u043e\u0436\u0430\u043d\u044b\u0439 \u043a\u043e\u0448\u0435\u043b\u0451\u043a|\u0439\u043e\u0433\u0443\u0440\u0442|\u0451\u043b\u043e\u0447\u043d\u044b\u0435 \u0438\u0433\u0440\u0443\u0448\u043a\u0438".split("|"),
    "\u043a\u0430\u0436\u0434\u044b\u0439 \u0434\u0435\u043d\u044c|\u0441\u0435\u0433\u043e\u0434\u043d\u044f|\u0437\u0430\u0432\u0442\u0440\u0430|\u0432 \u041c\u043e\u0441\u043a\u0432\u0435|\u0432 \u041a\u0430\u0437\u0430\u043d\u0438|\u043f\u043e \u0432\u0441\u0435\u0439 \u0441\u0442\u0440\u0430\u043d\u0435|\u0441\u043e \u0441\u043a\u0438\u0434\u043a\u043e\u0439|\u0431\u0435\u0441\u043f\u043b\u0430\u0442\u043d\u043e|\u043f\u0435\u0440\u0435\u0434 \u043f\u0440\u0430\u0437\u0434\u043d\u0438\u043a\u0430\u043c\u0438|"
    "\u0447\u0435\u0440\u0435\u0437 \u0438\u043d\u0442\u0435\u0440\u043d\u0435\u0442".split("|"),
]
_GREEK = [
    "\u03a4\u03bf \u03ba\u03b1\u03c4\u03ac\u03c3\u03c4\u03b7\u03bc\u03b1|\u039f \u03c0\u03b5\u03bb\u03ac\u03c4\u03b7\u03c2|\u0397 \u03bf\u03bc\u03ac\u03b4\u03b1|\u039f \u03c0\u03c9\u03bb\u03b7\u03c4\u03ae\u03c2|\u0397 \u03b1\u03c0\u03bf\u03b8\u03ae\u03ba\u03b7|\u0397 \u03b9\u03c3\u03c4\u03bf\u03c3\u03b5\u03bb\u03af\u03b4\u03b1|\u0397 \u03b1\u03b3\u03bf\u03c1\u03ac|\u0397 \u03c5\u03c0\u03b7\u03c1\u03b5\u03c3\u03af\u03b1 \u03bc\u03b1\u03c2|"
    "\u039f \u03b1\u03b3\u03bf\u03c1\u03b1\u03c3\u03c4\u03ae\u03c2|\u03a4\u03bf \u03bd\u03ad\u03bf \u03c4\u03bc\u03ae\u03bc\u03b1".split("|"),
    "\u03c3\u03c4\u03ad\u03bb\u03bd\u03b5\u03b9|\u03c0\u03c1\u03bf\u03c3\u03c6\u03ad\u03c1\u03b5\u03b9|\u03b5\u03bb\u03ad\u03b3\u03c7\u03b5\u03b9|\u03c0\u03b1\u03c1\u03b1\u03b4\u03af\u03b4\u03b5\u03b9|\u03c0\u03c1\u03bf\u03c4\u03b5\u03af\u03bd\u03b5\u03b9|\u03c3\u03c5\u03b3\u03ba\u03c1\u03af\u03bd\u03b5\u03b9|\u03b1\u03be\u03b9\u03bf\u03bb\u03bf\u03b3\u03b5\u03af|\u03b5\u03c0\u03b9\u03c3\u03c4\u03c1\u03ad\u03c6\u03b5\u03b9|\u03c3\u03c5\u03c3\u03ba\u03b5\u03c5\u03ac\u03b6\u03b5\u03b9|"
    "\u03b5\u03c0\u03b9\u03bb\u03ad\u03b3\u03b5\u03b9".split("|"),
    "\u03c4\u03bf \u03bd\u03ad\u03bf \u03c0\u03c1\u03bf\u03ca\u03cc\u03bd|\u03ad\u03bd\u03b1 \u03b6\u03b5\u03c3\u03c4\u03cc \u03bc\u03c0\u03bf\u03c5\u03c6\u03ac\u03bd|\u03c6\u03c1\u03ad\u03c3\u03ba\u03b1 \u03bc\u03ae\u03bb\u03b1|\u03c4\u03bf\u03bd \u03ba\u03c9\u03b4\u03b9\u03ba\u03cc \u03c0\u03c1\u03bf\u03ca\u03cc\u03bd\u03c4\u03bf\u03c2|\u03ad\u03bd\u03b1 \u03b4\u03ce\u03c1\u03bf|\u03c0\u03c1\u03ac\u03c3\u03b9\u03bd\u03bf \u03c4\u03c3\u03ac\u03b9|"
    "\u03c4\u03b7\u03bd \u03c0\u03c1\u03cc\u03c3\u03b2\u03b1\u03c3\u03b7 \u03c3\u03c4\u03bf \u03ba\u03b1\u03c4\u03ac\u03c3\u03c4\u03b7\u03bc\u03b1|\u03ad\u03bd\u03b1 \u03b4\u03b5\u03c1\u03bc\u03ac\u03c4\u03b9\u03bd\u03bf \u03c0\u03bf\u03c1\u03c4\u03bf\u03c6\u03cc\u03bb\u03b9|\u03b4\u03cd\u03bf \u03b2\u03b9\u03b2\u03bb\u03af\u03b1|\u03c4\u03bf\u03bd \u03c4\u03b1\u03c7\u03c5\u03b4\u03c1\u03bf\u03bc\u03b9\u03ba\u03cc \u03ba\u03ce\u03b4\u03b9\u03ba\u03b1".split("|"),
    "\u03ba\u03ac\u03b8\u03b5 \u03bc\u03ad\u03c1\u03b1|\u03c3\u03ae\u03bc\u03b5\u03c1\u03b1|\u03b1\u03cd\u03c1\u03b9\u03bf|\u03c3\u03c4\u03b7\u03bd \u0391\u03b8\u03ae\u03bd\u03b1|\u03c3\u03c4\u03b7 \u0398\u03b5\u03c3\u03c3\u03b1\u03bb\u03bf\u03bd\u03af\u03ba\u03b7|\u03c3\u03b5 \u03cc\u03bb\u03b7 \u03c4\u03b7 \u03c7\u03ce\u03c1\u03b1|\u03bc\u03b5 \u03ad\u03ba\u03c0\u03c4\u03c9\u03c3\u03b7|\u03b4\u03c9\u03c1\u03b5\u03ac\u03bd|"
    "\u03c0\u03c1\u03b9\u03bd \u03b1\u03c0\u03cc \u03c4\u03b9\u03c2 \u03b3\u03b9\u03bf\u03c1\u03c4\u03ad\u03c2|\u03bc\u03ad\u03c3\u03c9 \u03b4\u03b9\u03b1\u03b4\u03b9\u03ba\u03c4\u03cd\u03bf\u03c5".split("|"),
]


def language_units(name: str, n: int = 320) -> list:
    """Ordinary text of one language, no credential in it (generated from word pools)."""
    pools, joiner, end = {
        "polish": (_POLISH, " ", "."), "hindi": (_HINDI, " ", " \u0964"), "thai": (_THAI, "", ""),
        "japanese": (_JAPANESE, "", "\u3002"), "russian": (_RUSSIAN, " ", "."), "greek": (_GREEK, " ", "."),
    }[name]
    return _sentences(pools, n, SEED + 20 + len(name), joiner, end)


CASINGS = (("as_written", str), ("title_case", str.title), ("upper_case", str.upper))
LANGUAGES = ("polish", "hindi", "thai", "japanese", "russian", "greek")


def false_masking(units: list) -> dict:
    """Units masked for a label and a value / masked for anything / flagged as a label by the strict lane / flagged
    by the strict lane at all, in each casing."""
    out = {}
    for name, f in CASINGS:
        cv = [sg.client_view_findings(f(u)) for u in units]
        st = [sg.check(f(u)) for u in units]
        out[name] = (sum("label_value" in x for x in cv), sum(bool(x) for x in cv),
                     sum(x == "a_label" for x in st), sum(x is not None for x in st))
    return out


def test_false_masking_on_neutral_prose_did_not_rise():
    """Measured at ffd28ca8 and at this commit on the same corpora (ADR 0051 decision 71): identical in every cell
    of the English corpus; the pinned figures are this commit's."""
    units = english_units()
    assert len(units) >= 3000 and len(set(units)) == len(units)
    got = false_masking(units)
    print(f"false masking, english units={len(units)} (label and value / any / strict label / strict any): {got}")
    assert {k: v[0] for k, v in got.items()} == {"as_written": 0, "title_case": 0, "upper_case": 0}
    assert {k: v[1] for k, v in got.items()} == {"as_written": 0, "title_case": 0, "upper_case": 0}
    # the strict lane (logs, outbound data) withholds a text holding a credential WORD, as it always did: the same
    # count in every casing
    assert len({v[2:] for v in got.values()}) == 1


@pytest.mark.parametrize("name", LANGUAGES)
def test_ordinary_text_of_the_label_languages_is_not_masked(name):
    units = language_units(name)
    assert len(units) >= 300 and len(set(units)) == len(units)
    got = false_masking(units)
    print(f"false masking, {name} units={len(units)} (label and value / any / strict label / strict any): {got}")
    assert all(v[0] == 0 and v[2] == 0 for v in got.values()), got          # no label finding in either lane
    # masked or flagged for anything: nothing as written and in Title Case. In UPPER CASE only printed: an
    # all-capitals sentence can read as a strong-key shape (the AWS key-id rule on the text with its spaces removed),
    # the same number of units at ffd28ca8 (39 of the 320 Polish ones); not a label finding, not changed here
    assert got["as_written"][1] == got["title_case"][1] == 0 and got["as_written"][3] == got["title_case"][3] == 0


def test_a_changed_entry_does_not_match_ordinary_words_of_its_language():
    """The entries whose key differs from the entry as listed (the marks the inspection views drop): the key alone,
    looked for as the strict lane looks for it, in ordinary text of the same language; and every script key in
    every corpus. (Review cd0a6c7d: the Japanese key is the entry as listed again; the Japanese corpus is still
    read, with its key.)"""
    changed = [(i, e) for i, e in enumerate(sg._SCRIPT) if sg._SCRIPT_KEYS[i] != e.replace(" ", "")]
    # (review cbdf5e78: and the Greek entry, its final sigma read as a sigma; it has no corpus here)
    assert sorted(len(e) for _i, e in changed) == sorted(len(x) for x in (HINDI, THAI, sg._SCRIPT[-1]))
    assert sg._SCRIPT_KEYS[sg._SCRIPT.index(JAPANESE)] == JAPANESE
    rates = {}
    for i, e in [(i, e) for i, e in changed if e in (HINDI, THAI)] + [(sg._SCRIPT.index(JAPANESE), JAPANESE)]:
        lang = {HINDI: "hindi", THAI: "thai"}.get(e, "japanese")
        units = language_units(lang)
        # review cbdf5e78: the entry looked for as the strict lane looks for it (the marked step for the two marked
        # entries, the letters-only view for the unspaced Japanese one)
        if e in (HINDI, THAI):
            rates[lang] = (sum(sg._marked_hit(f(u)) for u in units for _n, f in CASINGS), 3 * len(units))
        else:
            key = sg._SCRIPT_KEYS[i]
            rates[lang] = (sum(key in sg._script_letters(f(u)) for u in units for _n, f in CASINGS), 3 * len(units))
    every = sum(sg._script_hit(f(u)) for lang in LANGUAGES for u in language_units(lang) for _n, f in CASINGS)
    print(f"script keys in ordinary text: changed entries {rates}; any script key in any corpus: {every}")
    assert sorted(rates) == ["hindi", "japanese", "thai"] and all(hit == 0 for hit, _n in rates.values()) and every == 0


# ================================================================================================ cost

def test_reading_what_follows_a_script_label_is_one_pass_over_the_view(monkeypatch):
    """``_cv_script_ends`` maps every label found on the script view back to the view as written with ONE cursor:
    the table is asked a bounded number of times per character (once to make the view, at most twice by the
    cursor) however many labels the text holds (counted, not timed)."""
    asked = [0]

    class Counting(type(sg._SCRIPT_WORD)):
        def __getitem__(self, o):
            asked[0] += 1
            return super().__getitem__(o)
    monkeypatch.setattr(sg, "_SCRIPT_WORD", Counting())
    for n in (50, 400):
        asked[0] = 0
        text = " ".join([THAI, "\u0926\u0941\u0915\u093e\u0928"] * n)                  # n labels, none followed by a separator
        mv = sg._clean_marked(text)                            # (review cbdf5e78: the Thai entry is read on its marked view)
        sv = sg._script_word_view(mv)
        ends = list(sg._cv_script_ends(mv, sv, list(sg._MARKED_SEQ.finditer(sv))))
        assert mv == text and len(ends) == n and all(text[e] == " " for e in ends)
        assert asked[0] <= 3 * len(text), (asked[0], len(text))
        assert sg.client_view_findings(text) == [] and sg.check(text) == "a_label"
