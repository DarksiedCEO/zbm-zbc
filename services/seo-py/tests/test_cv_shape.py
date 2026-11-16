"""core-spec/2 CV-1..CV-4 (card C2-7): value-shape mask (VS) + joined-run rule (JR), label-independent.

Values come from the frozen generator VG (CV-9) or are built at run time; no literal secret, no label word of a
corpus. Edges named by G-6: VS length 7 vs 8, piece 24 vs 25, each J character, joiner between letters vs not,
two separators end a run, a word between ends a run."""
from __future__ import annotations

import hashlib
import random
import uuid

import pytest

from agents import callum, cv_shape as CV, secret_guard
from conformance import gen_values_v2 as VG


def _shown(text: str, a: int, b: int) -> bool:
    """True when some character of text[a:b] is outside every masked span."""
    sp = CV.value_spans(text)
    return any(not any(x <= k < y for x, y in sp) for k in range(a, b))


def test_q2_4_thresholds_and_character_sets():
    assert (CV.TOKEN_MIN, CV.PIECE_MAX, CV.RUN_MAX) == (8, 24, 64)
    assert CV.J == frozenset(" \u00a0\t-_./:+|,") and len(CV.J) == 11
    assert CV.JN == frozenset("-'\u2019._")
    assert CV.W_L == frozenset("\"'`([{<*") and CV.W_T == frozenset("\"'`)]}>*,.;:!?")


def test_cv1_canonical_view_and_span_map():
    v, src = CV.canonical("\uff21b\u200b1\u00a0\u00a0x\u2013y\n\tz")
    assert v == "Ab1 x-y z"
    assert src[0] == (0, 1) and src[2] == (3, 4) and src[3] == (4, 5)
    v, _ = CV.canonical("e\u0301te\u0301")                   # base + mark composed by NFKC, marks kept
    assert v == "\u00e9t\u00e9"
    t = "x \uff21\uff42\uff13\uff44\uff25\uff46\uff19\uff48 y"  # fullwidth value: span maps back to the input
    assert CV.value_spans(t) == [(2, 10)]


@pytest.mark.parametrize("n,masked", [(7, False), (8, True)])
def test_cv3_length_7_vs_8(n, masked):
    tok = ("ab1cd2ef3gh4")[:n]
    assert CV.valueish(tok) is masked
    assert bool(CV.value_spans("see " + tok + " here")) is masked


@pytest.mark.parametrize("tok,masked", [
    ("abcdefghij", False), ("abcd#efgh", True), ("1234#5678", True), ("abcd1234", True),
    ("\u03b1\u03b2\u03b3\u03b4\u03b5\u03b6\u03b71", True), ("\u03b1\u03b2\u03b3\u03b4\u03b5\u03b61", False), ("\u0915\u093f\u0924\u093e\u092c\u0967\u0968\u0969", True),
    ("rock-and-roll", False), ("don't-stop", False), ("don\u2019t.stop_it", False),
    ("-abcdefgh", True), ("abcdefg-", True),       # a trailing '-' is not between letters and not in W_T: a symbol
    ("abc--defg", True), ("abc-1defgh", True),     # C2-7b: these two cells were swallowed by the comment above
])
def test_cv3_two_of_three_classes_and_joiners(tok, masked):
    assert CV.valueish(tok) is masked, tok


def test_cv2_core_strips_wrap_characters_only():
    t = 'x ("Ab3dEf9h"), y'
    a = t.index("A")
    assert CV.value_spans(t) == [(a, a + 8)]
    assert CV.core("<*'abc'*>") == "abc" and CV.core("((") == ""


def test_cv3_uuid_and_long_hex_masked_in_cv():
    u = str(uuid.UUID(int=random.Random(5).getrandbits(128)))
    h = hashlib.sha1(b"cv-3 synthetic").hexdigest()
    for tok in (u, h, h.upper()):
        t = "id " + tok + " ok"
        assert CV.value_spans(t) == [(3, 3 + len(tok))], tok


def _greek_split(r: random.Random, sep: str) -> str:
    """A random string of capital Greek letters holding an inner sigma, split once right after it."""
    up = [chr(c) for c in range(0x391, 0x3aa) if c not in (0x3a2, 0x3a3)]
    w = "".join(r.choice(up) for _ in range(r.randint(2, 6))) + "\u03a3" + "".join(r.choice(up) for _ in range(4))
    k = w.index("\u03a3") + 1
    return w[:k] + sep + w[k:]


def _leads(r: random.Random, sep: str) -> list:
    """CV-10a lead variants for one separator: nothing, one ordinary word, capital Greek split by space / NBSP."""
    return [sep.lstrip(), "ordinary" + sep, _greek_split(r, " ") + sep, _greek_split(r, chr(0xA0)) + sep]


def _core_in(lead: str, val: str) -> tuple:
    a, b = CV.core_bounds(val)
    return len(lead) + a, len(lead) + b


@pytest.mark.parametrize("seed", VG.SEEDS)
def test_cv3_vg_values_masked_whatever_the_lead_and_span_identical(seed):
    r = random.Random(seed)
    for i, val in enumerate(VG.values(seed, 300)):
        rel = set()
        for lead in _leads(r, (": ", " = ", " is ")[i % 3]):
            t = lead + val + " end"
            sp = CV.value_spans(t)
            assert not _shown(t, *_core_in(lead, val)), (seed, i)
            rel.add(tuple((a - len(lead), b - len(lead)) for a, b in sp if b > len(lead)))
            assert all(b <= len(lead) + len(val) for a, b in sp if a >= len(lead))
        assert len(rel) == 1                                 # label independence: same span of the value


def test_cv3_label_is_never_masked_by_vs():
    r = random.Random(3)
    for val in VG.values(51, 50):
        lead = _greek_split(r, " ") + " is "
        t = lead + val
        assert not any(a < len(lead) - 1 for a, _b in CV.value_spans(t))


def test_f1_smoke_greek_upper_split_after_inner_sigma():
    r = random.Random(20261114)
    vals = VG.values(20261114, 60)
    k = 0
    for _spelling in range(10):
        for sp in (" ", "\u00a0"):
            for sep in (": ", " = ", " is "):
                lead = _greek_split(r, sp) + sep
                t = lead + vals[k]
                k += 1
                assert not _shown(t, *_core_in(lead, vals[k - 1]))
    assert k == 60


def test_cv4_run_over_spaces_masked_as_one_span():
    t = "code ab1 cd2 ef3 now"
    assert CV.value_spans(t) == [(5, 16)]
    assert CV.value_spans("ab1 cd2") == []                   # concatenation 6 < 8: not valueish


@pytest.mark.parametrize("j", sorted(CV.J))
def test_cv4_each_j_character_joins(j):
    t = "x ab1" + j + "cd2" + j + "ef3 y"
    v, _ = CV.canonical(t)
    a = v.index("ab1")
    assert CV.view_spans(v) == [(a, v.index("ef3") + 3)], repr(j)


@pytest.mark.parametrize("n,masked", [(24, True), (25, False)])
def test_cv4_piece_24_vs_25(n, masked):
    t = "ab1 " + "#" * n
    assert bool(CV.value_spans(t)) is masked


def test_cv4_two_separators_end_a_run():
    assert CV.value_spans("ab1 cd2 ef3") == [(0, 11)]
    assert CV.value_spans("ab1 -cd2 ef3") == []
    assert CV.value_spans("ab1 ,cd2 ef3") == []


def test_cv4_a_word_between_ends_a_run():
    assert CV.value_spans("ab1 cd2 ef3 gh4") == [(0, 15)]
    assert CV.value_spans("ab1 cd2 and ef3 gh4") == []


def test_cv4_run_cap_64():
    frags = ["a%d" % (i % 10) for i in range(32)]           # 32 x 2 = 64 -> masked; 33 x 2 = 66 -> not
    assert CV.value_spans(" ".join(frags))
    assert CV.value_spans(" ".join(frags + ["b1"])) == []


def test_cv4_f7_3_maximal_run_over_64_is_not_sub_searched_but_tokens_stay_under_vs():
    # lead ruling 15:12 PT F7-3: JR takes MAXIMAL runs only; a run whose concatenation is > 64 is not masked by JR and
    # no sub-run is searched, but each of its tokens is still under VS (CV-3).
    frags = ["a%d" % (i % 10) for i in range(30)]          # 30 x 2 = 60; any 4+ of them alone would be valueish
    tok = "ab1cd2ef3"                                      # 9 code points, letters + digits: valueish by VS
    t = " ".join(frags + [tok])                            # one maximal run, concatenation 69 > 64
    a = t.index(tok)
    assert CV.value_spans(t) == [(a, a + len(tok))]
    assert CV.value_spans(" ".join(frags[:4])) == [(0, 11)]  # the same pieces alone ARE a JR run (control)


def test_cv4_sub_token_exemption(monkeypatch):
    assert CV.value_spans("ab#1 cd#2") == [(0, 9)]
    monkeypatch.setattr(CV, "exempt_shape", lambda c: c in ("ab#1", "cd#2"))
    assert CV.value_spans("ab#1 cd#2") == []


def test_exempt_shape_is_the_single_entry_point(monkeypatch):
    monkeypatch.setattr(CV, "exempt_shape", lambda c: c == "abcd1234")
    assert CV.valueish("abcd1234") is False and CV.valueish("abcd1235") is True


def test_wired_into_client_view_and_display():
    val = VG.values(7, 1)[0]
    text = "the answer mentions " + val + " today"
    assert "value_shape" in secret_guard.client_view_findings(text)
    assert secret_guard.client_view_kind(["value_shape"]) == secret_guard.CREDENTIAL
    out = callum._display("answer", text)
    assert out["alert"] is True and val not in str(out["value"])
    assert secret_guard.client_view_findings("An ordinary sentence about shoes and hats.") == []
