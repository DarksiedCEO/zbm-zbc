"""Unit tests for the Priya gate canonical view and safe shapes (priya-spec/2 2.1 CAN-1..CAN-6, 2.5; CL-1, CL-2,
CL-5..CL-7). ASCII-only source: every non-ASCII input is a \\u escape."""

from __future__ import annotations

import hashlib

import pytest

from agents import priya_gate as g


def covered(q: str) -> list[str]:
    """The canonical tokens of q that lie inside a safe-shape span, in order."""
    text = g.canonical(q)
    toks = g.tokens(q)
    out: list[str] = []
    for i, j in g.safe_spans(text):
        out.extend(toks[i:j])
    return out


def all_covered(q: str) -> bool:
    return covered(q) == g.tokens(q) and g.tokens(q) != []


# ---- confusables data (CL-5) ----
def test_confusables_pinned_and_vendored():
    assert (
        g.CONFUSABLES_SHA256
        == "8289f833e4cf78fde56b2080dc0e42934ef5182c9c3f4dd1fbdf2bced69fd5ed"
    )
    assert (
        hashlib.sha256(g.CONFUSABLES_PATH.read_bytes()).hexdigest()
        == g.CONFUSABLES_SHA256
    )


def test_confusables_mismatch_fails_closed(tmp_path, monkeypatch):
    bad = tmp_path / "c.txt"
    bad.write_bytes(b"0430 ;\t0061 ;\tMA\n")
    monkeypatch.setattr(g, "CONFUSABLES_PATH", bad)
    monkeypatch.setattr(g, "_SKELETON", None)
    with pytest.raises(g.ConfusablesMismatch):
        g.canonical("p\u0430ypal")


# ---- CAN-1 NFKC + casefold ----
def test_can1_nfkc_casefold():
    assert g.canonical("\uff21\uff22C") == "abc"
    assert g.canonical("STRA\u00dfE") == "strasse"
    assert g.canonical("\uff20") == "@"


def test_can1_negative_plain_unchanged():
    assert g.canonical("abc def") == "abc def"


# ---- CAN-2 drop Cc Cf Mn Me ----
@pytest.mark.parametrize(
    "q",
    [
        "ab\u200bc",
        "ab\u00adc",
        "\ufeffabc",
        "ab\u0007c",
        "abc\u20dd",
        "a\u200eb\u202ec",
    ],
)
def test_can2_drops_invisible_and_marks(q):
    assert g.canonical(q) == "abc"


def test_can2_negative_space_and_letters_kept():
    assert g.canonical("a b") == "a b"


# ---- CL-1 accent folding (NFKD, drop Mn, NFC) ----
def test_cl1_accent_fold():
    assert g.canonical("caf\u00e9") == "cafe"
    assert g.canonical("CAF\u00c9") == "cafe"
    assert g.canonical("espan\u0303a") == g.canonical("espa\u00f1a") == "espana"


def test_cl1_negative_hangul_roundtrips():
    assert g.canonical("\ud55c\uad6d") == "\ud55c\uad6d"


def test_cl1_kana_voicing_mark_is_folded():
    # literal CL-1: the dakuten (U+3099, Mn) is dropped after NFKD, so ga folds to ka
    assert g.canonical("\u304c") == "\u304b"


# ---- CAN-3 Nd -> ASCII ----
def test_can3_digits_to_ascii():
    assert g.canonical("\u0661\u0662\u0663") == "123"
    assert g.canonical("\u0967\u0968") == "12"
    assert g.canonical("\uff19") == "9"


def test_can3_negative_non_nd_numbers_untouched():
    assert (
        g.canonical("\u2160") == "i"
    )  # NFKC folds ROMAN NUMERAL ONE (Nl) to a letter; no digit mapping
    assert g.canonical("42") == "42"


# ---- CAN-4 skeleton to Latin for a mixed-script token; CL-2 Highly Restrictive ----
def test_can4_mixed_latin_cyrillic_to_latin():
    assert g.canonical("p\u0430ypal login") == "paypal login"
    assert g.canonical("g\u03bfogle") == "google"


def test_can4_negative_single_script_untouched():
    assert (
        g.canonical("\u043f\u0440\u0438\u0432\u0435\u0442")
        == "\u043f\u0440\u0438\u0432\u0435\u0442"
    )
    assert g.canonical("\u0434\u043e\u043c paypal") == "\u0434\u043e\u043c paypal"
    assert g.canonical("\u0439") == "\u0438"  # CL-1 folds the breve (Mn) too


@pytest.mark.parametrize(
    "tok", ["abc\u6f22\u3072\u30ab", "a\u6f22\u3105", "ab\ud55c\u6f22", "\u6f22\u3072"]
)
def test_cl2_highly_restrictive_sets_are_one_script(tok):
    assert not g.mixed_script(tok)
    assert g.canonical(tok) == tok


@pytest.mark.parametrize(
    "tok", ["\ud55c\u3072", "\u3105\u3072", "a\u0434", "\u0434\u05d0"]
)
def test_cl2_other_mixes_are_mixed(tok):
    assert g.mixed_script(tok)


def test_can4_unmappable_mix_stays_mixed():
    # Cyrillic de has no Latin prototype: the token still mixes scripts after CAN-4 (OG-2 fires on it)
    out = g.canonical("ab\u0434")
    assert g.mixed_script(out)


def test_can4_common_and_digits_ignored():
    assert not g.mixed_script("abc123")
    assert not g.mixed_script("\u043f\u0440\u0438123")


# ---- CAN-5 tokens ----
def test_can5_tokenise():
    assert g.tokens("foo_bar-baz  qux.") == ["foo", "bar", "baz", "qux"]
    assert g.tokens("\u0939\u093f\u0902\u0926\u0940 x") == [
        "\u0939\u093f\u0926\u0940",
        "x",
    ]  # Mc kept in token


def test_can5_negative_empty():
    assert g.tokens("") == []
    assert g.tokens(" ._-@ ") == []


# ---- CAN-6: V / T and queries share one canonical function ----
def test_can6_idempotent():
    for q in ("Caf\u00e9 \uff21", "p\u0430ypal", "\u0661\u0662", "\u304c\u3072"):
        assert g.canonical(g.canonical(q)) == g.canonical(q)


# ---- 2.5 safe shapes (CL-7: safe_spans on the canonical text, token-index spans) ----
POS = {
    "year": ["1900", "2024", "2039"],
    "date": [
        "2024-03-05",
        "3/5/2024",
        "5.3.2024",
        "05/03/2024",
        "2024/3/5",
        "3-5-2024",
        "20240305",
    ],
    "month_day": ["march 5 2024", "5 march", "dec 31", "enero 2", "oktober 3 1999"],
    "time": ["10:30", "9:05:59 pm", "7:15am", "23:59:00"],
    "tire": [
        "225/45r17",
        "225 45 17",
        "p205 55 16",
        "lt265 70 17",
        "225/45zr17",
        "225 45 zr17",
        "225-45-17",
        "205/55 r16 94h",
        "225/45r17 xl",
        "225 45r17 116s",
        "lt265/70r17 121/118s",
        "215/60/16 95h",
    ],
    "version": ["v1.2.3", "3.12", "1.0.4.7", "v10.0"],
    "dimension": ["10x12", "2.5 x 4 x 6 ft", "10 x 20cm", "4x8"],
    "price": [
        "$19.99",
        "19 usd",
        "\u20ac5",
        "5 eur",
        "\u00a31,299",
        "\u00a5999",
        "\uff0499",
    ],
    "quantity": ["64gb", "16 gb", "5 lb", "1080p", "100 mph", "3 yd", "12v", "2 kmh"],
    "ordinal": ["21st", "3rd", "2nd", "4th"],
    "model": ["s24", "ddr5", "i7", "4k", "8k", "rtx4090ti", "abcd123456"],
}
NEG = {
    "year": ["1899", "2040"],
    "date": ["2024-13-05", "20241305", "3/5/24x"],
    "month_day": ["march 45", "foo 5"],
    "time": ["25:61", "10:7"],
    "tire": ["22/45r17", "225/4r17"],
    "version": ["v.1", "1."],
    "dimension": ["10 x", "x 12"],
    "price": ["19 gbp", "$", "\u20ac"],
    "quantity": ["64zz", "5 qq"],
    "ordinal": ["3xx", "st"],
    "model": ["abcde5", "abcd1234567", "5abc"],
}


@pytest.mark.parametrize("shape,q", [(k, q) for k, v in POS.items() for q in v])
def test_safe_shape_positive(shape, q):
    assert all_covered(q), (shape, q, covered(q))


@pytest.mark.parametrize("shape,q", [(k, q) for k, v in NEG.items() for q in v])
def test_safe_shape_negative(shape, q):
    assert not all_covered(q), (shape, q, covered(q))


def test_safe_spans_inside_query():
    q = "flight 3/5 10:30 delta"
    assert covered(q) == ["10", "30"]
    q = "galaxy s24 ultra 256gb"
    assert g.safe_spans(g.canonical(q)) == [(1, 2), (3, 4)]


def test_safe_spans_ordinary_words_not_covered():
    assert g.safe_spans(g.canonical("carpet cleaning near me")) == []


# CAL-5 (spec 2.5 v2.1 owner-parameter update): one positive and one negative set per added shape
CAL5_POS = {
    "clock_ampm": ["5pm", "11 am", "12pm", "07am"],
    "clock_24h": ["17h30", "9h05", "23h59"],
    "iso_datetime": ["2024-03-05t10:30", "2024-03-05t10:30:59z"],
    "month_year": ["march 2024", "2024 dec"],
    "code": ["ab12c34", "abcd12e3", "1a234", "xy123ab45c"],
    "hyphen_code": ["abc-1234", "xq-55"],
}
CAL5_NEG = {
    "clock_ampm": ["13pm", "0am", "5 xm"],
    "clock_24h": ["17h", "17 h 60"],
    "iso_datetime": ["2024-13-05t10:30", "2024-03-05t25:30"],
    "month_year": ["march 1899", "foo 2024"],
    "code": [
        "a3f9c2e1",
        "abcde12f3",
        "abcd12345e6789",
        "1abcd2",
        "a1b2c3",
        "a1b2",
        "1a2",
    ],
    "hyphen_code": ["abcde-1234", "abc-1234567", "a-12", "abc-12x"],
}


@pytest.mark.parametrize("shape,q", [(k, q) for k, v in CAL5_POS.items() for q in v])
def test_cal5_safe_shape_positive(shape, q):
    assert all_covered(q), (shape, q, covered(q))


@pytest.mark.parametrize("shape,q", [(k, q) for k, v in CAL5_NEG.items() for q in v])
def test_cal5_safe_shape_negative(shape, q):
    assert not all_covered(q), (shape, q, covered(q))


def test_cl6_resolution_alternative_is_narrow():
    assert g.MODEL_RE.fullmatch("4k") and g.MODEL_RE.fullmatch("12k")
    assert not g.MODEL_RE.fullmatch("123k")


# ==== P2-2b: stage-1 rules OG-1..OG-8 (priya-spec/2 2.2, F-3, F-10, OW-3; spec 2.2 number words, P2-2b-fix) ====
from agents import priya_numwords as nw  # noqa: E402

# tiny fixture V (canonical words; never the real artifact) and T
V = frozenset(
    "carpet cleaning near me usa best pizza at dot home dinner matrix printer galaxy ultra one two three seven "
    "wonders top in new york mail john gmail com uno dos tres phone flight delta tokyo".split()
)
T0: frozenset[str] = frozenset()


def fired(q: str, t: frozenset[str] = T0) -> list[str]:
    return g.og_fired(q, V, t)


def test_og_clean_query_passes():
    assert g.og("carpet cleaning near me", V) == ("PASS", None)
    assert g.og("galaxy s24 ultra 256gb", V, T0) == ("PASS", None)


@pytest.mark.parametrize(
    "rule,yes,no",
    [
        (
            "OG-1",
            ["mail\uff20gmail", "john@gmail", "x\ufe6by"],
            ["dinner at home", "carpet cleaning"],
        ),
        (
            "OG-2",
            ["pa\u0434", "\u0434\u043e\u043c5"],
            ["\u0434\u043e\u043c 5", "galaxy s24"],
        ),
        (
            "OG-3",
            [
                "john at gmail dot com",
                "jan malpa onet kropka pl",
                "x punkt y at-zeichen z",
            ],
            ["dinner at home", "dot matrix printer"],
        ),
        ("OG-4", ["k x q w", "a b c d"], ["u s a", "a b", "1 2 3 4", "10 x 20 cm"]),
        (
            "OG-5",
            [
                "one two three four",
                "eleven twenty hundred thousand",
                "uno deux tre vier",
                "dix-sept dix-huit trente cent",
                "zwei hundert tausend elf",
                "\u043e\u0434\u0438\u043d \u0434\u0432\u0430 \u0442\u0440\u0438 \u0447\u0435\u0442\u044b\u0440\u0435",
                "\u043e\u0434\u043d\u043e\u0439 \u0434\u0432\u0443\u043c\u044f \u0441\u043e\u0440\u043e\u043a\u0430 \u0441\u0442\u0430",
                "\u0938\u093e\u0924 \u0906\u0920 \u0928\u094c \u090f\u0915",
                "\u0625\u062b\u0646\u0627\u0646 \u062b\u0644\u0627\u062b\u0629 \u0623\u0631\u0628\u0639\u0629 \u0645\u0627\u0626\u0629",
                "\u4e00\u4e8c\u4e09\u56db",
                "\u4e24\u4e09\u56db\u4e94",
                "\u58f9\u8d30\u53c1\u8086",
                "\ud558\ub098 \ub458 \uc14b \ub137",
            ],
            [
                "one two three",
                "top seven wonders",
                "1 2 3 4",
                "dix-sept dix-huit",
                "\u6771\u4eac \u4e00 \u4e8c \u4e09",
                "oh oh oh oh",
                "drie vier vijf zes",
                "\u043f\u02bc\u044f\u0442\u044c \u0448\u0456\u0441\u0442\u044c \u0441\u0456\u043c \u043d\u0443\u043b\u044c",
            ],
        ),
        (
            "OG-6",
            ["carpet xqzv", "a1b2", "\u10d0\u10d1"],
            ["carpet cleaning", "galaxy s24", "a", "1234"],
        ),
        (
            "OG-7",
            [
                "abc\u6f22\u5b57",
                "\u65e5\u672c 1234567",
                "\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03",
            ],
            ["\u65e5\u672c 123456", "\u65e5\u672c abc", "1234567"],
        ),
    ],
)
def test_og_rule_fires_and_does_not(rule, yes, no):
    for q in yes:
        assert rule in fired(q), (rule, q)
    for q in no:
        assert rule not in fired(q), (rule, q)


def test_og4_and_og6_respect_tenant_list():
    t = frozenset(("xyz", "xqzv"))
    assert "OG-4" in fired("x y z") and "OG-4" not in fired("x y z", t)
    assert "OG-6" in fired("carpet xqzv") and "OG-6" not in fired("carpet xqzv", t)


def test_og8_length_boundary():
    assert "OG-8" not in fired("a" * 512)
    assert "OG-8" in fired("a" * 513)
    assert g.MAX_QUERY_CHARS == 512


def test_og_precedence_first_rule_in_table_order():
    assert g.og("x@y " + "a" * 513, V) == (
        "WITHHOLD",
        "OG-8",
    )  # I-17: over the bound OG-8 first
    assert g.og("x@y " + "a" * 508, V) == ("WITHHOLD", "OG-1")
    assert set(fired("one at two dot three four")) >= {"OG-3", "OG-5"}
    assert g.og("one at two dot three four", V) == ("WITHHOLD", "OG-3")
    assert g.og("a" * 513, V) == (
        "WITHHOLD",
        "OG-8",
    )  # I-17 (was OG-6 before the 12:00 ruling)
    assert g.og("a" * 512, V) == ("WITHHOLD", "OG-6")
    assert g.og("pa\u0434 1@2", V) == ("WITHHOLD", "OG-1")


def test_og_reason_codes_closed_set():
    assert g.OG_RULES == tuple(f"OG-{i}" for i in range(1, 9))


def test_c1_lists_equal_v1_lists():
    from agents import search_performance as sp

    assert g.AT_WORDS == sp.AT_WORDS and g.DOT_WORDS == sp.DOT_WORDS


def test_numwords_pinned_spec22():
    body = "\n".join(f"{lang}\t{w}" for lang, w in nw.NUMWORDS).encode("utf-8")
    pin = "bd89d1992a9d4890154d1c3dc3097600107004463247eb289c5654be90b9b0e8"
    assert hashlib.sha256(body).hexdigest() == pin == nw.NUMWORDS_SHA256
    langs = "en es fr de pt it ja zh ko ru ar hi".split()  # spec 2.2 X1-X12, exactly
    assert [lang for lang in dict.fromkeys(lang for lang, _ in nw.NUMWORDS)] == langs
    assert len(nw.NUMWORDS) == len(set(nw.NUMWORDS)) == 545
    assert ("en", "oh") not in nw.NUMWORDS  # CL-8 extras withdrawn (lead 11:05 PT)
    for lang, words in [
        ("en", "zero nine thirteen ninety hundred thousand"),
        ("de", "eins eine einem siebzehn hundert tausend"),
        ("fr", "une dix-sept soixante-dix quatre-vingt-dix cent mille"),
        ("ja", "\u3007 \u4e5d \u5341\u4e5d \u4e5d\u5341 \u767e \u5343 \u58f1"),
        ("zh", "\u96f6 \u4e24 \u5341\u4e5d \u4e00\u767e \u4e00\u5343 \u58f9"),
        ("ko", "\uc601 \uacf5 \ud558\ub098 \ud55c \uc5f4 \uc2a4\ubb3c \ubc31 \ucc9c"),
        (
            "ru",
            "\u043e\u0434\u043d\u0430 \u043e\u0434\u043d\u0443 \u0434\u0432\u0443\u043c\u044f \u0441\u043e\u0440\u043e\u043a\u0430 \u0441\u0442\u0430 \u0442\u044b\u0441\u044f\u0447\u0430",
        ),
    ]:
        for w in words.split():
            assert (lang, w) in nw.NUMWORDS, (lang, w)


# ---- P2-2c: V loader, sha256 pin (V-3 / OG-PIN), fail closed; NB-3 confusables mismatch ----
import gzip  # noqa: E402

_FIX_TEXT = "\n".join(sorted(V)).encode("utf-8") + b"\n"
_FIX_SHA = hashlib.sha256(_FIX_TEXT).hexdigest()
_QUERIES = ["running shoes", "best pizza", "carpet cleaning near me", "a b c d", ""]


def _write_v(path, text: bytes):
    path.write_bytes(gzip.compress(text, compresslevel=1, mtime=0))
    return path


def _load_fixture(path, sha=_FIX_SHA):
    return g.load_vocab(path, expected_sha256=sha, expected_n=len(V))


def test_vocab_pin_is_spec_23():
    assert (
        g.VOCAB_SHA256
        == "7239fa821cbec6631ea96ea2adb70fcc4e7fe6c6f7f266a8a4129195acec7c68"
    )
    assert g.VOCAB_N == 1861560
    assert g.VOCAB_PATH.name == "priya_vocab.txt.gz"


def test_real_v_loads_and_passes():
    v = g.load_vocab()
    assert v.status == "OK" and v.sha256 == g.VOCAB_SHA256
    assert len(v) == g.VOCAB_N
    assert "running" in v and "shoes" in v
    assert "" not in v and "zzqxjv" not in v and 7 not in v
    words = gzip.decompress(g.VOCAB_PATH.read_bytes()).split(b"\n")[:-1]
    assert all(a < b for a, b in zip(words, words[1:]))  # strictly sorted bytewise
    for w in (words[0], words[1], words[len(words) // 2], words[-2], words[-1]):
        assert w.decode("utf-8") in v
        assert w.decode("utf-8") + "\x01" not in v
    assert g.og("running shoes", v) == ("PASS", None)


def test_fixture_v_membership_is_exact(tmp_path):
    v = _load_fixture(_write_v(tmp_path / "v.gz", _FIX_TEXT))
    assert all(w in v for w in V)
    for w in ("", "a", "zz", "carpe", "carpets", "tokyo2", "cleaning near"):
        assert w not in v, w


def test_fixture_v_ok_pass_path(tmp_path):
    v = _load_fixture(_write_v(tmp_path / "v.gz", _FIX_TEXT))
    assert v.status == "OK" and v.sha256 == _FIX_SHA and len(v) == len(V)
    assert g.og("best pizza", v) == ("PASS", None)
    assert g.og("john@gmail.com", v) == ("WITHHOLD", "OG-1")


def _assert_all_withheld(v, status):
    assert v.status == status and len(v) == 0
    for q in _QUERIES:
        assert g.og(q, v) == ("WITHHOLD", "OG-6"), q
        assert g.og_fired(q, v) == ["OG-6"], q


def test_flipped_text_byte_withholds_every_query(tmp_path):
    bad = bytearray(_FIX_TEXT)
    bad[3] ^= 0x01
    _assert_all_withheld(
        _load_fixture(_write_v(tmp_path / "v.gz", bytes(bad))), "VOCAB_MISMATCH"
    )


def test_flipped_gzip_byte_withholds_every_query(tmp_path):
    p = _write_v(tmp_path / "v.gz", _FIX_TEXT)
    raw = bytearray(p.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    p.write_bytes(bytes(raw))
    _assert_all_withheld(_load_fixture(p), "VOCAB_MISMATCH")


def test_wrong_count_withholds(tmp_path):
    p = _write_v(tmp_path / "v.gz", _FIX_TEXT)
    v = g.load_vocab(p, expected_sha256=_FIX_SHA, expected_n=len(V) + 1)
    _assert_all_withheld(v, "VOCAB_MISMATCH")


def test_missing_v_withholds_every_query(tmp_path):
    _assert_all_withheld(_load_fixture(tmp_path / "absent.gz"), "VOCAB_MISMATCH")


def _tamper_confusables(monkeypatch, path):
    monkeypatch.setattr(g, "CONFUSABLES_PATH", path)
    monkeypatch.setattr(g, "_SKELETON", None)


def test_nb3_tampered_confusables_fails_closed_like_v3(tmp_path, monkeypatch):
    good = _write_v(tmp_path / "v.gz", _FIX_TEXT)
    bad = tmp_path / "c.txt"
    bad.write_bytes(g.CONFUSABLES_PATH.read_bytes()[:-1] + b"x")
    _tamper_confusables(monkeypatch, bad)
    _assert_all_withheld(_load_fixture(good), "CONFUSABLES_MISMATCH")


def test_nb3_missing_confusables_fails_closed(tmp_path, monkeypatch):
    good = _write_v(tmp_path / "v.gz", _FIX_TEXT)
    _tamper_confusables(monkeypatch, tmp_path / "absent.txt")
    _assert_all_withheld(_load_fixture(good), "CONFUSABLES_MISMATCH")


def test_nb3_no_exception_escapes_og_with_plain_container(tmp_path, monkeypatch):
    bad = tmp_path / "c.txt"
    bad.write_bytes(b"0430 ;\t0061 ;\tMA\n")
    _tamper_confusables(monkeypatch, bad)
    for q in _QUERIES:
        assert g.og(q, V) == ("WITHHOLD", "OG-6"), q
        assert g.og_fired(q, V) == ["OG-6"], q
    assert g.gate_status(V) == "CONFUSABLES_MISMATCH"


def test_gate_status_names_the_problem(tmp_path):
    assert g.gate_status(_load_fixture(_write_v(tmp_path / "v.gz", _FIX_TEXT))) == "OK"
    assert g.gate_status(_load_fixture(tmp_path / "absent.gz")) == "VOCAB_MISMATCH"
    assert g.gate_status(V) == "OK"


# ==== P2-VAN-B: OG-4 (b) (priya-spec/2.1 2.2, owner 2026-11-14 15:40 PT) - synthetic shapes only ====
# Every positive is chosen so that OG-4 (a) does NOT fire (concatenation in T, or digit keys break the (a) run):
# OG-4 comes from (b) alone, so the pinned mutant "(b) removed" loses OG-4 on every positive.
OG4B_YES = [
    (
        "seven three twoqz k j q z",
        ("twoqz", "kjqz"),
    ),  # spelled code + glued key + spaced keys
    ("seven 5 q 2 j", ()),  # digit keys inside the run
    ("seven 5 2 7", ()),  # digit-only keys only
    ("4155 q j z", ("qjz",)),  # digit-only code just before
    ("seven qzvr k j q", ("qzvr", "kjq")),  # code 2 tokens before the run
]
OG4B_NO = [
    ("seven 5 q 5 g", ()),  # safe-span key: "5 g" (quantity) cuts the run to 2
    ("seven q 2", ()),  # only 2 one-char tokens
    ("carpet 5 q 2", ()),  # no code before
    ("5 q 2 j", ()),  # nothing before
    ("seven qzvr wxyz 5 q 2", ("qzvr", "wxyz")),  # code 3 tokens before
    ("best pizza a b c", ("abc",)),  # OQ form after an ordinary word
]


@pytest.mark.parametrize("q,t", OG4B_YES)
def test_og4b_fires(q, t):
    assert fired(q, frozenset(t)) == ["OG-4"], q
    assert g.og(q, V, frozenset(t)) == ("WITHHOLD", "OG-4")


@pytest.mark.parametrize("q,t", OG4B_NO)
def test_og4b_does_not_fire(q, t):
    assert "OG-4" not in fired(q, frozenset(t)), q


def test_og4b_safe_span_key_is_covered():
    assert covered("seven 5 q 5 g") == ["5", "g"]


def test_og4b_pinned_mutant_b_removed_is_killed(monkeypatch):
    monkeypatch.setattr(g, "_og4b", lambda toks, safe: False)
    for q, t in OG4B_YES:
        assert "OG-4" not in fired(q, frozenset(t)), q


# ==== P2-ZP-B: 2.5 model token NOT safe when a decoding contains "@" (priya-spec/2.1, owner ~18:20 PT) ====
# Synthetic only: "ofys422aoe" = base32 (lower case, unpadded) of a made-up reversed 6-byte e-mail, same shape as the
# frozen case (a{4}d{3}a{3}, digits in 2-7, model-token shaped, not in V).
ZP_TOKEN = "ofys422aoe"
ZP_YES = ["mail ofys422aoe home", "ofys422aoe"]
ZP_SAFE_MODEL = ["ddr5", "i7", "4k", "s24", "8k", "12k", "rtx4090ti", "abcd123456"]


def test_zp_token_is_model_shaped_and_decodes_to_at():
    assert g.MODEL_RE.fullmatch(ZP_TOKEN)
    assert g._decodes_to_at(ZP_TOKEN)


@pytest.mark.parametrize("q", ZP_YES)
def test_zp_same_shape_token_not_safe_and_withheld(q):
    assert ZP_TOKEN not in covered(q)
    assert "OG-6" in fired(q, frozenset())
    assert g.og(q, V, frozenset()) == ("WITHHOLD", "OG-6")


def test_zp_reversed_token_branch():
    # the token reversed is not itself a decodable "@" string; its reverse is
    assert g._decodes_to_at(ZP_TOKEN[::-1])


def test_zp_base64_and_base64url_branches():
    assert g._decodes_to_at("YUBi")  # base64 of a 3-byte synthetic string with "@"
    assert g._decodes_to_at(
        "YUA-Pw"
    )  # base64url only ("-" is not in the base64 alphabet)
    assert not g._decodes_to_at("YWJj")  # decodes, no "@"


def test_zp_nonprintable_decoding_is_not_a_veto():
    # base32 "aaaa" decodes to bytes without "@"; "ia2a" (base32 -> "@4") is the minimal veto
    assert not g._decodes_to_at("aaaa")
    assert g._decodes_to_at("ia2a")


@pytest.mark.parametrize("t", ZP_SAFE_MODEL)
def test_zp_no_fire_on_model_examples(t):
    assert not g._decodes_to_at(t), t
    assert all_covered(t), (t, covered(t))
    assert "OG-6" not in fired(f"best {t} home", frozenset())


@pytest.mark.parametrize("shape,q", [(k, q) for k, v in POS.items() for q in v])
def test_zp_every_safe_shape_example_still_covered(shape, q):
    assert all_covered(q), (shape, q, covered(q))
    for t in g.tokens(q):
        if g.MODEL_RE.fullmatch(t):
            assert not g._decodes_to_at(t), (shape, t)


def test_zp_pinned_mutant_decode_check_removed_is_killed(monkeypatch):
    monkeypatch.setattr(g, "_decodes_to_at", lambda tok: False)
    assert ZP_TOKEN in covered(ZP_YES[0])
    assert g.og(ZP_YES[0], V, frozenset()) == ("PASS", None)
