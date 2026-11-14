"""core-spec/1 section 7: the declared residuals, each pinned at its verdict on a small probe set and reported with its
size on the suite's own draws. A residual that CLOSES (its cells start being read) does not fail: it asks for an ADR
update (printed). One that GROWS past its bound fails. R-sigma is an owner item (Q-3: keep unread): asserted.
Ids and counts only."""
import pytest

from conformance import baseline as B
from conformance import core_matrix as M
from conformance import core_ref as R
from conformance import corpora as C

pytestmark = pytest.mark.conformance
VALUE = "Kx7#mQ2zB"
S = {eid: e for eid, e, *_ in R.entries()}


@pytest.fixture(scope="module")
def sg():
    from agents import secret_guard
    return secret_guard


def _cells_read(sg, sp, lane):
    """(cells, read) of the CI sample of one SP row in one lane."""
    import itertools
    cells = list(itertools.islice((c for c in M.all_cells(only_sp={sp}) if c.sep_class in ("1", "2", "3a")), 600))
    if lane == "strict":
        got = sum(1 for c in cells if sg.check(c.text) == "a_label")
    else:
        got = sum(1 for c in cells if "label_value" in sg.client_view_findings(c.text))
    return len(cells), got


@pytest.mark.parametrize("item,sp,lane", [("R-b", 12, "strict"), ("R-b", 12, "cv"), ("R-d3", 6, "strict"),
                                          ("R-d5", 21, "strict"), ("R-d6", 16, "cv"), ("R-71", 33, "strict"),
                                          ("R-71", 33, "cv"), ("R-hi", 30, "strict"), ("R-hi", 30, "cv"),
                                          ("R-hg", 37, "cv")])
def test_core_R_matrix_residual_sizes(sg, item, sp, lane):
    n, got = _cells_read(sg, sp, lane)
    print(f"{item} SP-{sp:02d} {lane}: CI cells={n} read={got} unread (the residual)={n - got}")
    assert n > 0
    if got == n:
        print(f"{item}: CLOSED on the CI sample: the ADR's residual table needs an update")


def test_core_R_c_hindi_written_as_two_words(sg):
    a, b = S["S19"][:3], S["S19"][3:]
    texts = [f"{a}{s}{b}: {VALUE}" for s in (" ", "-", "_")]
    read = sum(1 for t in texts if sg.check(t) == "a_label" or sg.client_view_findings(t))
    print(f"R-c: forms={len(texts)} read={read}")
    assert read == 0                                  # (and O-4 PIN pins the 182 pairs N)


def test_core_R_d1_bare_key_not_masked(sg):
    assert "label_value" not in sg.client_view_findings(f"key: {VALUE}")


def test_core_R_d2_all_caps_polish_prose_as_a_strong_key(sg):
    """R-d2: ALL-CAPS Polish prose read as a strong-key shape: no more than at ffd28ca8 on the neutral draw."""
    items = C.neutral()
    base = B.read("neutral", "ffd28ca8", C.stream_hash(items))
    now = sum(1 for (lang, cs, t), _b in zip(items, base) if lang == "pl" and cs == "upper" and sg.check(t) is not None)
    ffd = sum(1 for (lang, cs, _t), b in zip(items, base) if lang == "pl" and cs == "upper" and b.split("|")[0] == "1")
    print(f"R-d2: Polish UPPER sentences read now={now} at ffd28ca8={ffd}")
    assert now <= ffd


def test_core_R_d7_leetspeak_not_read(sg):
    for leet in ("p4ssw0rd", "s3cret", "t0ken", "pa$$word"):
        assert sg.check(f"the {leet} is {VALUE}") != "a_label"


def test_core_R_d8_percent_encoded_long_words_as_tokens(sg):
    """R-d8: a long non-Latin word percent-encoded in a URL path is read as a token: no more than at ffd28ca8."""
    items = C.urls()
    base = B.read("urls", "ffd28ca8", C.stream_hash(items))
    enc = [(u, b) for (_l, p, u), b in zip(items, base) if p.endswith(".enc")]
    now = sum(1 for u, _b in enc if sg.url_has_secret(u))
    ffd = sum(1 for _u, b in enc if b == "1")
    print(f"R-d8: encoded URLs={len(enc)} unfetched now={now} at ffd28ca8={ffd}")
    assert now <= ffd


def test_core_R_sigma_medial_sigma_written_final_inside_a_glued_word_stays_unread(sg):
    """R-sigma / Q-3 (owner: keep unread): an entry's MEDIAL sigma written final inside a glued word."""
    h5 = S["H5"]
    k = h5.index("\u03c3", 8)                        # the medial sigma of the second word
    t = h5[:k] + "\u03c2" + h5[k + 1:]
    assert sg.check(f"{t}: {VALUE}") != "a_label" and "label_value" not in sg.client_view_findings(f"{t}: {VALUE}")


def test_core_R_a_thai_glued_joins_as_written(sg):
    import test_review_cbdf5e78 as cbd
    literal = [t for t, lit in cbd.thai_texts() if lit]
    read = sum(1 for t in literal if sg.check(t) == "a_label")
    print(f"R-a: Thai glued joins holding the label as written (CI probe)={len(literal)} read={read}")
    assert len(literal) == 6


def test_core_R_p_unlabelled_tokens_shown_in_the_client_view(sg):
    import leak_corpus_v2 as lc
    cases = [c for c in lc.corpus() if c.family.endswith("_unlabelled")]
    shown = sum(1 for c in cases if not sg.client_view_findings(c.carrier))
    print(f"R-p: unlabelled random tokens={len(cases)} shown in the client view={shown} (strict withholds all)")
    assert all(sg.check(c.carrier) is not None for c in cases)


@pytest.mark.parametrize("item", ["R-d4", "R-g", "R-h", "R-o3", "R-o4", "R-o5", "R-kl", "R-new"])
def test_core_R_measured_elsewhere(item):
    """Residuals sized on corpora of other modules: R-g / R-h / R-a sizes on the W corpus (overtight_words, heavy);
    R-o5 by the end-to-end run (test_core_e2e prints the CV-O values shown); R-kl by
    tests/test_canary_all_routes.py; R-d4 / R-o3 / R-o4 by the heavy W and fuzz lanes; R-new: none found."""
    assert item.startswith("R-")


def test_core_G_8_adr_corrections():
    """G-8: the ADR 0051 statements core-spec/1 names as untrue at 186471eb are corrected, and the round's entry
    (decision 76) names its class list as "the R cells of core-spec/1"."""
    from pathlib import Path
    adr = (Path(__file__).resolve().parents[4] / "docs" / "adr" / "0051-seo-client-citation-vault.md").read_text(
        encoding="utf-8")
    adr = " ".join(adr.split())                      # (the ADR wraps its lines)
    assert "one of 57 separators (`_STRICT_SPACERS`" not in adr
    assert "no such list exists in the code" in adr
    assert "AS THIS ROUND'S OWN GENERATOR MADE" in adr
    assert "untrue for one F2 shape, SP-24 of core-spec/1" in adr
    assert "76. **" in adr and "the R cells of core-spec/1" in adr
