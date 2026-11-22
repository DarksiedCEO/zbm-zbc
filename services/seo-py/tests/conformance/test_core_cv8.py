"""core-spec/2 CV-8 (O cap, client-view mask rate) [OWNER DEFAULT Q2-2: <= 2.0%]: share of texts with >= 1 CV-3 / CV-4
span that 3120f0b0's client view did not mask. Judged: (a) O-4 N neutral corpus; (b) word classes plain_text /
question / local / multilingual of rv-priya18 ord.jsonl and rv-priya19 oq19.jsonl, each class. (c) / (d) printed
(disclosed / reported), not asserted. CI lane = fixed 1,000-text sample per corpus; heavy lane (lead gate) = full
corpora. The frozen corpora live outside the repo (CV8_N / CV8_ORD / CV8_OQ19, md5-pinned): without them the row is
NOT TESTED (skipped), never PASS. Counts and shape classes only."""
import pytest

from conformance import cv8_tool as T

pytestmark = pytest.mark.conformance


def _run(n, samples=False):
    if not samples and T.corpus_paths() is None:
        pytest.skip("CV-8 NOT TESTED: set CV8_N / CV8_ORD / CV8_OQ19 to the frozen corpora")
    res = T.run(n, samples=samples)
    print(T.table(res))
    return res


def _assert_caps(res):
    judged = res["judged"]
    assert "N/N" in judged, "CV-8 (a): O-4 N not measured"
    for name in ("ord18", "oq19"):
        for c in T.WORD_CLASSES:
            assert f"{name}/{c}" in judged, f"CV-8 (b): {name}/{c} not measured"
    assert not res["over_cap"], f"CV-8 cap {T.CAP_PCT}% exceeded: {res['over_cap']}"


def test_core_CV_8_mask_rate_sample_1000():
    """CI lane: (a) and (b) <= 2.0% on the fixed 1,000-text sample of each frozen corpus, committed md5-pinned under
    tests/conformance/data/cv8 (lead ruling 21:05 PT), so it runs on every clean checkout (no env, never skipped)."""
    _assert_caps(_run(None, samples=True))


def test_core_CV_8_committed_samples_are_the_fixed_draw():
    """The committed samples are exactly ``sample(full, 1000)`` of the frozen corpora (checked when they are present)."""
    full = T.corpus_paths()
    if full is None:
        pytest.skip("frozen corpora absent (CV8_*): draw equality checked in the lead gate only")
    for name, p in T.sample_paths().items():
        assert T.load(name, p, T.SAMPLES[name][2]) == T.sample(T.load(name, full[name]), 1000), name


@pytest.mark.conformance_heavy
def test_core_CV_8_mask_rate_full_corpora():
    """Heavy lane (lead gate): (a) N 45,600 and (b) each word class of ord 22,600 / oq19 24,400 <= 2.0%."""
    _assert_caps(_run(None))
