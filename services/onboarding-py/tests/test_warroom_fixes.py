"""
War room fixes (ADR 0018; devtools/warroom/findings.md): WR-F003, a final sigma in a legal name split one person's
1099 total. The no-churn tests pin that every name the fix does not concern keeps its pk2- key.
"""

from __future__ import annotations

import datetime as dt
import unicodedata

import pytest

import name_key
from name_key import name_key_text
from service import person_key

DOB = dt.date(1990, 1, 2)


@pytest.mark.parametrize("variant", [
    "ᴊօsé ɡαrςía",               # war room replay onboarding-py/R0001
    "JoᏚé ʛaгϲíα",               # replay R0002: the lunate sigma, NFKC'd to the final sigma
    "José Garςía",
    "JOSÉ GARςÍA",
    "José\u200b Garςía",
])
def test_wr_f003_a_final_sigma_for_c_keys_as_c(variant):
    assert name_key_text(variant) == "jose garcia"
    assert person_key(variant, DOB) == person_key("José García", DOB)


def test_wr_f003_a_greek_name_keys_alike_in_capitals_and_lower_case():
    assert name_key_text("ΓΙΏΡΓΟΣ ΠΑΠΑΔΌΠΟΥΛΟΣ") == name_key_text("Γιώργος Παπαδόπουλος")


# pk2- keys computed by the code before the fix (b467f24): already-normalised names keep their key
PINNED = {
    "José García": "pk2-88303f762601cccef4c1984f9bd5e3a1",
    "Jane Doe": "pk2-ffb146a343b55ec45e81210694e8b908",
    "Mary-Kate O’Neil": "pk2-c675b7a8be2c845feed908ddf691ce0b",
    "Bjørn Sæther": "pk2-f0395ecbad4c9a4ae5cd498962413e90",
    "Zoë Œuvre": "pk2-30960cfa3b5b7c1c7001846d4031e1da",
    "Łukasz Żółć": "pk2-d85e04bf72655eca9031410ca69e3292",
    "Nguyễn Thị Minh Khai": "pk2-0a823b61277286a57c75df7d2a1ef826",
    "Seán Ó Briain": "pk2-4f2c93c978f945ca1fdf376afe7bf4ae",
    "Pаt Smith": "pk2-be827eb215b92419c062c3856bef0ca2",
    "Иван Петров": "pk2-2045cf222a48f7f090ee9523f311bb05",
}


@pytest.mark.parametrize("name", sorted(PINNED))
def test_wr_f003_no_key_churn_for_names_without_a_final_sigma(name):
    assert person_key(name, DOB) == PINNED[name]


_PRE_FIX_FOLDS = str.maketrans({**name_key._generated_latin_folds(), **name_key.CONFUSABLES})


def _pre_fix_name_key_text(legal_name: str) -> str:
    """name_key_text as it was before the fix (b467f24), from the module's unchanged parts."""
    t = "".join(ch for ch in legal_name if ch not in name_key._IGNORABLE and unicodedata.category(ch) != "Cf")
    t = unicodedata.normalize("NFKC", t).casefold()
    t = t.translate(name_key._PUNCT_FOLD).translate(_PRE_FIX_FOLDS)
    t = unicodedata.normalize("NFKC", t)
    out, prev_space = [], True
    for ch in t:
        if ch.isalnum() or ch in "'-":
            out.append(ch)
            prev_space = False
        elif not prev_space:
            out.append(" ")
            prev_space = True
    return "".join(out).strip()


def test_wr_f003_only_the_sigma_family_changes_among_letters_the_old_key_folded():
    """Every code point that the pre-fix key folded to ASCII, inside a word and at its end, keys the same way now,
    except the final sigma family (the fix itself)."""
    changed = set()
    for cp in range(0x80, 0x30000):
        if 0xD800 <= cp < 0xE000:
            continue
        for probe in (f"a{chr(cp)}b", f"a{chr(cp)}"):
            old = _pre_fix_name_key_text(probe)
            if old.isascii() and name_key_text(probe) != old:
                changed.add(chr(cp))
    assert changed and all(unicodedata.normalize("NFKC", c).lower() in ("σ", "ς") for c in changed)
