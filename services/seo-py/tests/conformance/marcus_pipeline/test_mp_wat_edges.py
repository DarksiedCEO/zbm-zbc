"""MP-C3b boundary / guard tests for marcus/wat_fetcher.py (marcus-pipeline-spec/1 B-MP-10, B-MP-11, B-MP-12, B-MP-20).

Each test pins one rule exactly at its limit, so an off-by-one or a dropped guard fails a named test. Synthetic data
only (no network, no real crawl data)."""

from __future__ import annotations

import gzip
import io

import pytest

from marcus import wat_fetcher as W

CRAWL = "CC-MAIN-2024-10"
GOOD = ("crawl-data/CC-MAIN-2024-10/segments/1707947473347.0/wat/"
        "CC-MAIN-20240220211055-20240221001055-00000.warc.wat.gz")


def _lines(payload: bytes, **kw) -> list:
    return list(W.gz_lines(io.BytesIO(payload), **kw))


# B-MP-11: the path grammar check refuses anything that is not a str, too long or not ASCII - never a crash.
def test_B_MP_11_check_path_refuses_a_non_string_path_with_path_not_allowed():
    W.check_path(CRAWL, GOOD)                              # control: a real data path of that crawl is kept
    for bad in (12345, None, 3.5, b"bytes-path"):
        with pytest.raises(W.PathNotAllowed):
            W.check_path(CRAWL, bad)


def test_B_MP_11_check_path_refuses_over_200_characters_and_non_ascii():
    with pytest.raises(W.PathNotAllowed):
        W.check_path(CRAWL, "a" * 201)
    with pytest.raises(W.PathNotAllowed):
        W.check_path(CRAWL, GOOD.replace("wat/", "w\u00e4t/"))


# B-MP-10 / B-MP-11: a gzip-looking path list that does not decompress is refused, never read as an empty list.
def test_B_MP_10_a_corrupt_gzip_path_list_is_refused_path_not_allowed():
    assert W.parse_path_list(gzip.compress((GOOD + "\n").encode()), CRAWL) == [GOOD]     # control
    with pytest.raises(W.PathNotAllowed):
        W.parse_path_list(b"\x1f\x8b" + b"not really gzip at all", CRAWL)


# B-MP-10: the fixture source falls back from wat.paths to wat.paths.gz (CC publishes the gzip one).
def test_B_MP_10_fixture_source_reads_wat_paths_gz_when_plain_list_is_absent(tmp_path):
    d = tmp_path / "crawl-data" / CRAWL
    d.mkdir(parents=True)
    blob = gzip.compress((GOOD + "\n").encode())
    (d / "wat.paths.gz").write_bytes(blob)
    src = W.FixtureWatSource(str(tmp_path))
    assert src.paths(CRAWL) == blob
    assert W.parse_path_list(src.paths(CRAWL), CRAWL) == [GOOD]


def test_B_MP_10_fixture_source_with_no_list_is_quarantined_path_list_missing(tmp_path):
    (tmp_path / "crawl-data" / CRAWL).mkdir(parents=True)
    with pytest.raises(W.Quarantined) as e:
        W.FixtureWatSource(str(tmp_path)).paths(CRAWL)
    assert e.value.code == "PATH_LIST_MISSING"


# B-MP-20: a line of exactly max_line bytes is kept; one byte more is the TOO_LARGE marker (skipped, counted).
def test_B_MP_20_line_length_edge_max_kept_max_plus_1_too_large():
    got = _lines(gzip.compress(b"12345678\n123456789\nok\n"), max_line=8)
    assert got == [b"12345678", W.TOO_LARGE, b"ok"]


# B-MP-20: a decoded chunk that starts with a newline ends the current line there (an empty line, then the rest).
def test_B_MP_20_a_stream_starting_with_a_newline_yields_an_empty_line_first():
    assert _lines(gzip.compress(b"\nabc\n")) == [b"", b"abc"]
    assert _lines(gzip.compress(b"\n\nz")) == [b"", b"", b"z"]


# B-MP-20: an over-long line that ends exactly at a chunk edge stops being skipped at the next chunk's first byte
# (a newline): the following line is kept, not swallowed. Two gzip members make the chunk edge exact.
def test_B_MP_20_skipped_long_line_ending_at_a_chunk_edge_does_not_swallow_the_next_line():
    payload = gzip.compress(b"x" * 20) + gzip.compress(b"\nkeep\n")
    assert _lines(payload, max_line=10) == [W.TOO_LARGE, b"keep"]
    payload = gzip.compress(b"x" * 20) + gzip.compress(b"y" * 5) + gzip.compress(b"\nkeep\n")
    assert _lines(payload, max_line=10) == [W.TOO_LARGE, b"keep"]


# B-MP-12: the decoded budget is inclusive: exactly cap decoded bytes is read; cap + 1 quarantines TOO_LARGE.
def test_B_MP_12_decoded_cap_edge_exactly_cap_kept_cap_plus_1_too_large():
    blob = gzip.compress(b"abcdefghi\n")                   # 10 decoded bytes
    stats = {}
    assert list(W.gz_lines(io.BytesIO(blob), cap=10, stats=stats)) == [b"abcdefghi"]
    assert stats["decoded"] == 10
    with pytest.raises(W.Quarantined) as e:
        _lines(blob, cap=9)
    assert e.value.code == W.TOO_LARGE
