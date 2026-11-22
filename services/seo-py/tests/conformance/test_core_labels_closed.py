"""core-spec/1 section 1: the closed label lists (L-1..L-8). The suite's own escaped copy (``core_ref``) must equal
the module's lists; the declared sub-lists are closed. Entries are named by id, never printed."""
import pytest

from conformance import core_ref as R

pytestmark = pytest.mark.conformance
VALUE = "Kx7#mQ2zB"


@pytest.fixture(scope="module")
def sg():
    from agents import secret_guard
    return secret_guard


def test_core_L_1_the_lists_are_closed(sg):
    """L-1: the set the import check validates is exactly the frozen set (44 + 27 + 23 + 2)."""
    assert tuple(sg._JOINED) == R.JOINED
    assert frozenset(sg._PIECES) == frozenset(R.PIECES)
    assert tuple(sg._SCRIPT) == tuple(e for e, _s, _g in R.SCRIPT)
    assert tuple(sg._CV_SCRIPT_FULL) == R.CV_SCRIPT_FULL
    # the import check reads every list entry (a list the check would not see cannot hold an entry)
    seen = set(sg._JOINED_KEYS) | set(sg._PIECE_KEYS) | set(sg._SCRIPT_KEYS)
    assert len(sg._JOINED_KEYS) == 44 and len(sg._PIECE_KEYS) == 27 and len(sg._SCRIPT_KEYS) == 23
    assert len(seen) == 94


def test_core_L_2_joined_44(sg):
    assert len(R.JOINED) == 44 and len(set(R.JOINED)) == 44 and tuple(sg._JOINED) == R.JOINED


def test_core_L_3_pieces_27(sg):
    assert len(R.PIECES) == 27 and sorted(sg._PIECES) == list(R.PIECES)


def test_core_L_4_script_23_and_declared_forms(sg):
    """L-4: the 23 script entries and their declared forms (_SCRIPT_FORMS)."""
    assert len(R.SCRIPT) == 23
    forms = sg._SCRIPT_FORMS
    e = [x for x, _s, _g in R.SCRIPT]
    assert tuple(forms[e[0]]["endings"]) == R.RU_ENDINGS
    assert forms[e[1]] == {"glued": True} and forms[e[18]] == {"glued": True}
    assert tuple(forms[e[12]]["endings"]) == R.KO_PARTICLES and forms[e[12]]["apart"] == R.KO_APART
    for i in (13, 14, 15):
        assert tuple(forms[e[i]]["endings"]) == R.KO_PARTICLES
    for i in (16, 17):
        assert tuple(forms[e[i]]["prefix"]) == R.AR_PROCLITICS
    assert tuple(forms[e[21]]["prefix"]) == R.HE_PROCLITICS
    assert tuple(forms[e[19]]["endings"]) == R.HI_ENDINGS
    assert tuple(forms[e[22]]["endings"]) == R.EL_ENDINGS and tuple(forms[e[22]]["written"]) == R.EL_WRITTEN
    assert set(forms) == {e[i] for i in (0, 1, 12, 13, 14, 15, 16, 17, 18, 19, 21, 22)}
    scripts = [s for _x, s, _g in R.SCRIPT]
    assert scripts.count("CJK") == 8 and scripts.count("HANGUL") == 4 and scripts.count("ARABIC") == 3


def test_core_L_5_full_forms_matched_whole_by_both_lanes(sg):
    """L-5: F1 and F2 are matched whole by both lanes (as listed, UPPER, Title), before a separator and a value."""
    for f in R.CV_SCRIPT_FULL:
        for form in (f, f.upper(), f.title()):
            assert sg.check(f"{form}: {VALUE}") == "a_label"
            assert "label_value" in sg.client_view_findings(f"{form}: {VALUE}")
            assert sg._SCRIPT_SEQ.fullmatch(sg._script_word_view(sg.normalise(form))) is not None


def test_core_L_6_the_matrix_entry_set_is_101(sg):
    """L-6: E = 44 + 27 + 23 + 2 + 5 = 101; each historical form (H1..H5) is read glued as written by both lanes."""
    ids = [eid for eid, *_ in R.entries()]
    assert len(ids) == R.ENTRY_COUNT == 101 and len(set(ids)) == 101
    assert len(R.HISTORY) == 5
    for _h, e, _g in R.HISTORY:
        assert sg.check(f"{e}: {VALUE}") == "a_label"
        assert "label_value" in sg.client_view_findings(f"{e}: {VALUE}")


def test_core_L_7_declared_sub_lists_are_closed():
    """L-7: 13 Russian endings, 33 Korean particles, 6 Greek endings, 2 Hindi endings, 7 Hebrew proclitics (up to
    3), 5 Arabic proclitics (up to 2)."""
    assert len(R.RU_ENDINGS) == 13 and len(set(R.RU_ENDINGS)) == 13
    assert len(R.KO_PARTICLES) == 33 and len(set(R.KO_PARTICLES)) == 33
    assert len(R.EL_ENDINGS) == 6 and len([x for x in R.HI_ENDINGS if x]) == 2
    assert len(R.HE_PROCLITICS[0]) == 7 and R.HE_PROCLITICS[1] == 3
    assert len(R.AR_PROCLITICS[0]) == 5 and R.AR_PROCLITICS[1] == 2


def test_core_L_7_a_particle_outside_the_list_is_no_declared_form(sg):
    """L-7: a particle / proclitic outside the lists is not a declared form: bare (no value) it is no label word
    (the in-word class SP-24 / 26..29 reads it only before a separator and a value)."""
    ko = [x for x, _s, _g in R.SCRIPT][12]
    other = "\ub791\ub3c4"                       # two particles in a row: not a listed particle
    assert other not in R.KO_PARTICLES
    assert not sg._SCRIPT_SEQ.search(sg._script_word_view(sg.normalise(ko + other)))
    he = [x for x, _s, _g in R.SCRIPT][21]
    assert not sg._SCRIPT_SEQ.search(sg._script_word_view(sg.normalise("\u05d5\u05d4\u05d1\u05dc" + he)))


def test_core_L_8_not_entries(sg):
    """L-8: the bare word ``key`` is no label (its own strict rule needs ``:`` / ``=`` and a token-shaped value; the
    client view never masks it: R-d1); the leetspeak spellings are no label finding (R-d7)."""
    assert sg.check("the key takeaway is simple") is None
    assert sg.check(f"key: {VALUE}") == "a_label"
    assert "label_value" not in sg.client_view_findings(f"key: {VALUE}")
    for leet in ("p4ssw0rd", "s3cret", "t0ken", "pa$$word"):
        assert sg.check(f"the {leet}: {VALUE}") != "a_label"
        assert "label_value" not in sg.client_view_findings(f"the {leet}: {VALUE}")
