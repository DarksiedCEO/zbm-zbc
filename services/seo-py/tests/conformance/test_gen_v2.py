"""core-spec/2 CV-9 / CV-10c: self-tests of the frozen generators VG (gen_values_v2) and FS (gen_split_v2).

Values exist only at run time; failures print counts, lengths and indices, never a value. The FS acceptance predicate
(CV-3 / CV-4) is INJECTED: the stubs below are test doubles of a fragment / valueish predicate, not the CV rule (the CV
module of card C2-7 supplies the real one)."""
import string
import unicodedata
from collections import Counter

import pytest

from conformance import gen_split_v2 as FS
from conformance import gen_values_v2 as VG
from conformance.core_matrix import SEPS
from conformance.corpora import WORDS

N = 10_000


def _stub_fragment(p: str) -> bool:
    return any(c.isdigit() or c in string.punctuation for c in p)


def _stub_valueish(v: str) -> bool:
    cls = (any(c.isalpha() for c in v), any(c.isdigit() for c in v), any(c in string.punctuation for c in v))
    return len(v) >= 8 and sum(cls) >= 2


# ------------------------------------------------------------------------------------------------ VG (CV-9)

def test_cv9_frozen_constants():
    assert VG.SEEDS == (20261114, 51, 7)
    assert VG.SYMBOLS == string.punctuation and len(set(VG.SYMBOLS)) == 32
    assert (VG.LEN_MIN, VG.LEN_MAX) == (8, 24)


@pytest.mark.parametrize("seed", VG.SEEDS)
def test_cv9_values_deterministic_per_seed(seed):
    a, b = VG.values(seed, N), VG.values(seed, N)
    assert a == b and len(a) == N
    assert VG.values(seed, 100) == a[:100]            # a prefix of the stream is the same set


def test_cv9_seeds_give_different_streams():
    s = [tuple(VG.values(x, 200)) for x in VG.SEEDS]
    assert len(set(s)) == 3


@pytest.mark.parametrize("seed", VG.SEEDS)
def test_cv9_values_match_the_stated_distribution(seed):
    lens, digs, syms, bad = Counter(), Counter(), Counter(), []
    upper = lower = 0
    for i, v in enumerate(VG.values(seed, N)):
        n = len(v)
        d = sum(c in string.digits for c in v)
        s = [c for c in v if c in string.punctuation]
        letters = [c for c in v if c in string.ascii_letters]
        ok = (8 <= n <= 24 and len(s) == 1 and 1 <= d <= max(1, n // 4) and len(letters) == n - 1 - d)
        if not ok:
            bad.append((i, n, d, len(s)))
        lens[n] += 1
        digs[(n, d)] += 1
        syms[s[0] if s else None] += 1
        upper += any(c.isupper() for c in letters)
        lower += any(c.islower() for c in letters)
    assert not bad, bad[:10]
    assert sorted(lens) == list(range(8, 25))                      # every length drawn
    assert all(lens[x] > N / 17 * 0.7 for x in lens)               # roughly uniform
    for n in range(8, 25):                                         # every digit count of the range drawn
        assert {d for (m, d) in digs if m == n} == set(range(1, max(1, n // 4) + 1)), n
    assert set(syms) == set(string.punctuation)                    # all 32 symbols at least once over 10,000
    assert min(syms.values()) > N / 32 * 0.6
    assert upper > N * 0.9 and lower > N * 0.9                     # mixed case


@pytest.mark.parametrize("seed", VG.SEEDS)
def test_cv9_symbol_position_is_shuffled(seed):
    pos = Counter(v.index(next(c for c in v if c in string.punctuation)) for v in VG.values(seed, N))
    assert pos[0] > 0 and len(pos) >= 20                           # first and late positions occur


def test_cv9_prose_pool_is_the_v1_samples_lower_case_4_letters():
    pool = VG.prose_pool()
    langs = {lang for lang, ws in WORDS.items() if len(ws) >= 220}
    assert VG.PROSE_LANGS == tuple(sorted(langs))
    src = {w for lang in langs for w in WORDS[lang]}
    assert pool and set(pool) <= src and len(pool) == len(set(pool))
    for w in pool:
        assert w == w.lower() and sum(c.isalpha() for c in w) >= 4
        assert all(c.isalpha() or unicodedata.category(c)[0] == "M" for c in w)
    for lang in langs:                                             # every script contributes
        assert any(w in pool for w in WORDS[lang]), lang


@pytest.mark.parametrize("seed", VG.SEEDS)
def test_cv9_prose_deterministic_and_drawn_from_pool(seed):
    a, b = VG.prose(seed, 2_000), VG.prose(seed, 2_000)
    assert a == b and set(a) <= set(VG.prose_pool())
    assert len(set(a)) > 500


def test_cv9_manifest_frozen():
    got = VG.manifest()
    assert got == VG.MANIFEST, sorted(k for k in got if got[k] != VG.MANIFEST.get(k))


# ------------------------------------------------------------------------------------------------ FS (CV-10c)

def test_cv10c_frozen_constants():
    assert FS.J == (" ", "\u00a0", "\t", "-", "_", ".", "/", ":", "+", "|", ",")
    assert FS.KS == (2, 3, 4) and FS.N_VALUES == 6_000 and FS.PER_SEED * len(VG.SEEDS) == 6_000
    assert FS.PLACEMENTS == ("bare", "sep1", "sep2", "sentence")
    assert FS.N_CANDIDATES == 6_000 * 3 * 11 * 4


@pytest.mark.parametrize("seed", VG.SEEDS)
def test_cv10c_candidates_deterministic_per_seed(seed):
    a = FS.digest(FS.candidates((seed,)))
    b = FS.digest(FS.candidates((seed,)))
    assert a == b and a[0] == FS.PER_SEED * 3 * 11 * 4


@pytest.mark.parametrize("seed", VG.SEEDS)
def test_cv10c_candidate_structure(seed):
    vals = VG.values(seed, FS.PER_SEED)
    sep1 = {s for _i, s in SEPS["1"]}
    sep2 = {s for _i, s in SEPS["2"]}
    modes, seen_vals, bad = Counter(), set(), []
    for c in FS.candidates((seed,)):
        v = vals[c["vi"]]
        seen_vals.add(c["vi"])
        run = c["text"][c["span"][0]:c["span"][1]]
        pieces, seps = c["pieces"], c["seps"]
        ok = ("".join(pieces) == v and len(pieces) == c["k"] and len(seps) == c["k"] - 1
              and all(p for p in pieces) and all(s in FS.J for s in seps) and seps[0] == FS.J[c["sep"]]
              and run == pieces[0] + "".join(s + p for s, p in zip(seps, pieces[1:])))
        if c["mode"] == "uniform":
            ok = ok and len(set(seps)) == 1
        pre = c["text"][:c["span"][0]]
        if c["placement"] == "bare":
            ok = ok and pre == "" and c["span"][1] == len(c["text"])
        elif c["placement"] == "sep1":
            ok = ok and any(pre.endswith(s) and len(pre) > len(s) for s in sep1)
        elif c["placement"] == "sep2":
            ok = ok and any(pre.endswith(s) and len(pre) > len(s) for s in sep2)
        else:
            ok = ok and pre.endswith(" ") and c["text"][c["span"][1]] == " " and len(pre) > 1
        if not ok:
            bad.append(c["id"])
        modes[c["mode"]] += 1
    assert not bad, bad[:10]
    assert seen_vals == set(range(FS.PER_SEED))
    assert modes["uniform"] > 0 and modes["mixed"] > 0


def test_cv10c_rejection_sampling_uses_the_injected_predicate():
    seeds = (VG.SEEDS[2],)
    calls = Counter()

    def frag(p):
        calls["f"] += 1
        return _stub_fragment(p)

    def vish(v):
        calls["v"] += 1
        return _stub_valueish(v)

    rep = FS.select(frag, vish, seeds=seeds)
    acc = list(FS.accepted(frag, vish, seeds=seeds))
    assert calls["f"] > 0 and calls["v"] > 0
    total = FS.PER_SEED * 3 * 11 * 4
    assert rep["candidates"] == total and rep["accepted"] + rep["rejected"] == total
    assert rep["accepted"] == len(acc) and rep["rejected_structural"] <= rep["rejected"]
    for c in acc:
        assert all(_stub_fragment(p) for p in c["pieces"]) and _stub_valueish("".join(c["pieces"]))
        assert all(len(s) == 1 and s in FS.J for s in c["seps"])
        assert not any(ch in FS.J for p in c["pieces"] for ch in p)  # single J characters only
    assert 0 < rep["accepted"] < total
    none = FS.select(lambda p: False, lambda v: True, seeds=seeds)
    assert none["accepted"] == 0 and none["rejected"] == total
    allp = FS.select(lambda p: True, lambda v: True, seeds=seeds)
    assert allp["accepted"] == total - allp["rejected_structural"]


def test_cv10c_manifest_frozen():
    got = FS.manifest()
    assert got == FS.MANIFEST
