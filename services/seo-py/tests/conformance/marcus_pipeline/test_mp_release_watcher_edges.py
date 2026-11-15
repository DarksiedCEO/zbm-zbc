"""marcus-pipeline-spec/1 section 1, Release Watcher guard edges (B-MP-01 strict validation, B-MP-03 / AC-12 the
not-connected port, AC-14 / CB-23 generated-mutant survivors of run 1, card MP-C3a). Synthetic values only."""

from __future__ import annotations

import json

import pytest

from conformance.marcus_pipeline import gen_cc
from conformance.marcus_pipeline.util import watch
from marcus import release_watcher as rw

CC = "https://index.commoncrawl.org/"


def _graph_id(n: int) -> str:
    """A grammar-valid graph id of exactly ``n`` characters (n in 20..111)."""
    gid = "cc-main-" + "a" * 12
    while len(gid) + 13 <= n:
        gid += "-" + "b" * 12
    if len(gid) < n:
        gid += "-" + "c" * (n - len(gid) - 1)
    assert len(gid) == n and rw.GRAPH_ID.fullmatch(gid), gid
    return gid


def _crawls(**over) -> bytes:
    return json.dumps([{**gen_cc.crawl_item("CC-MAIN-2026-40"), **over}]).encode()


def _graphs(items) -> bytes:
    return json.dumps(items).encode()


def test_B_MP_01_graph_id_length_edge_64_kept_65_refused():
    """GRAPH_ID_MAX is inclusive: a grammar-valid 64-character id is kept, 65 is refused (also through parse)."""
    assert rw.GRAPH_ID_MAX == 64
    ok, long = _graph_id(64), _graph_id(65)
    assert rw.valid_graph_id(ok) and not rw.valid_graph_id(long)
    rows = rw.parse(b"[]", _graphs([gen_cc.graph_item(gid=ok, kind="host_graph")]))
    assert [(r["kind"], r["id"]) for r in rows] == [("host_graph", ok)]
    with pytest.raises(rw.SourceInvalid):
        rw.parse(b"[]", _graphs([gen_cc.graph_item(gid=long, kind="host_graph")]))


def test_B_MP_01_crawl_url_length_edge_300_kept_301_refused():
    """URL_MAX is inclusive for both CC URLs of a crawl item; one character more quarantines the list."""
    assert rw.URL_MAX == 300
    u300, u301 = CC + "a" * (300 - len(CC)), CC + "a" * (301 - len(CC))
    assert len(u300) == 300 and len(u301) == 301
    for key in ("timegate", "cdx-api"):
        assert [r["id"] for r in rw.parse(_crawls(**{key: u300}), b"[]")] == ["CC-MAIN-2026-40"]
        with pytest.raises(rw.SourceInvalid):
            rw.parse(_crawls(**{key: u301}), b"[]")


@pytest.mark.parametrize("value", [7, None, ["https://index.commoncrawl.org/"], {"u": 1}])
def test_B_MP_01_non_string_crawl_url_is_quarantined_not_a_crash(value):
    with pytest.raises(rw.SourceInvalid):
        rw.parse(_crawls(timegate=value), b"[]")


def test_B_MP_01_list_byte_cap_edge():
    """A list of exactly MAX_LIST_BYTES bytes is read; one byte more is refused before parsing (either list)."""
    cap = rw.MAX_LIST_BYTES
    at, over = b"[]" + b" " * (cap - 2), b"[]" + b" " * (cap - 1)
    assert len(at) == cap and len(over) == cap + 1
    assert rw.parse(at, at) == []
    with pytest.raises(rw.SourceInvalid):
        rw.parse(over, b"[]")
    with pytest.raises(rw.SourceInvalid):
        rw.parse(b"[]", over)


@pytest.mark.parametrize("raw", ["x", 0, None])
def test_B_MP_01_non_bytes_list_is_quarantined(raw):
    with pytest.raises(rw.SourceInvalid):
        rw.parse(raw, b"[]")


@pytest.mark.parametrize("doc", [b"{}", b'{"a": 1}', b'""', b"0", b"null", b"true"])
def test_B_MP_01_a_list_document_that_is_not_a_json_array_is_quarantined(doc):
    """GP-1: each list file is a JSON array; an empty object (or any other top-level value) is another shape."""
    with pytest.raises(rw.SourceInvalid):
        rw.parse(doc, b"[]")
    with pytest.raises(rw.SourceInvalid):
        rw.parse(b"[]", doc)


def test_B_MP_01_list_item_cap_edge_5000_kept_5001_refused():
    n = rw.MAX_ITEMS
    assert n == 5000
    items = [gen_cc.graph_item(gid=f"cc-main-g{i}", kind="host_graph") for i in range(n + 1)]
    assert len(rw.parse(b"[]", _graphs(items[:n]))) == n
    with pytest.raises(rw.SourceInvalid):
        rw.parse(b"[]", _graphs(items))


def test_B_MP_01_oversized_list_file_is_quarantined_with_capped_size(mp):
    """An oversized fixture file is read only up to MAX_LIST_BYTES + 1 bytes (enough to prove it is over the cap);
    the quarantine facts record that read size and the keyed list hash of exactly those bytes."""
    import hashlib
    cap = rw.MAX_LIST_BYTES
    big = b"[]" + b" " * (cap + 50)
    (mp.fx / "collinfo.json").write_bytes(big)
    graphs = (mp.fx / "graphinfo.json").read_bytes()
    j = watch(mp)
    assert j["outcome"] == "QUARANTINED" and j["facts"]["reason"] == "SOURCE_SCHEMA_UNKNOWN", j
    assert j["facts"]["size"] == cap + 1 + len(graphs)
    assert j["facts"]["list_sha256"] == hashlib.sha256(big[:cap + 1] + b"\x00" + graphs).hexdigest()
    assert mp.svc.cc_releases == {}


def test_B_MP_01_list_file_one_byte_over_the_cap_is_quarantined(mp):
    (mp.fx / "collinfo.json").write_bytes(b"[]" + b" " * (rw.MAX_LIST_BYTES - 1))
    j = watch(mp)
    assert j["outcome"] == "QUARANTINED" and j["facts"]["reason"] == "SOURCE_SCHEMA_UNKNOWN", j
    assert mp.svc.cc_releases == {}


def test_B_MP_03_not_connected_port_fails_closed_if_read():
    """AC-12: the v1 live stand-in never returns data; a read raises instead of yielding an empty value."""
    port = rw.NotConnectedReleases()
    assert port.connected is False and port.doc_source is None and port.read_date is None
    with pytest.raises(RuntimeError, match="not connected"):
        port.read()
