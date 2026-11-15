"""MP-C3b boundary / guard tests for marcus/link_extractor.py (marcus-pipeline-spec/1 B-MP-20, B-MP-21, B-MP-22,
B-MP-23). Each test pins one rule exactly at its limit. Synthetic data only."""

from __future__ import annotations

import json

from marcus import link_extractor as L

HOST = "http://example.com/"


# B-MP-22: remove_dot_segments per RFC 3986 5.2.4 - ".." at the root never eats the root (an empty segment after it
# is kept: "/..//a" -> "//a").
def test_B_MP_22_dot_segments_at_the_root_keep_the_following_empty_segment():
    assert L._remove_dots("/..//a") == "//a"
    assert L.normalise("http://example.com/..//a") == ("http://example.com//a", None)
    assert L.normalise("http://example.com/../a") == ("http://example.com/a", None)


# B-MP-22: more than 2,048 characters is dropped; exactly 2,048 is kept (input check).
def test_B_MP_22_url_length_edge_2048_kept_2049_dropped():
    url = HOST + "a" * (L.MAX_URL - len(HOST))
    assert len(url) == L.MAX_URL
    assert L.normalise(url) == (url, None)
    assert L.normalise(url + "a") == (None, "urls_too_long")


# B-MP-22: the same limit on the normalised output (percent-encoding can grow a URL past it).
def test_B_MP_22_normalised_length_edge_2048_kept_2049_dropped():
    url = HOST + "a" * (L.MAX_URL - len(HOST) - 3) + '"'           # '"' becomes %22: +2 characters
    assert len(url) == L.MAX_URL - 2
    out, why = L.normalise(url)
    assert why is None and len(out) == L.MAX_URL and out.endswith("%22")
    assert L.normalise(HOST + "a" * (L.MAX_URL - len(HOST) - 2) + '"') == (None, "urls_too_long")


# B-MP-22: a colon outside the brackets of a bracketed host is not a port separator: the authority is invalid, never
# read as the host before the colon.
def test_B_MP_22_bracketed_authority_after_a_colon_is_invalid_not_the_prefix_host():
    assert L.normalise("http://example.com:[v1.a]/") == (None, "urls_invalid")
    assert L.normalise("http://example.com:8080/x") == ("http://example.com:8080/x", None)      # control


# B-MP-23: the personal-data reader failing withholds (fail closed), never reads as "no finding".
def test_B_MP_23_personal_data_reader_error_fails_closed(monkeypatch):
    def boom(_text):
        raise RuntimeError("synthetic reader failure")

    monkeypatch.setattr(L.prompt_guard, "personal_data", boom)
    assert L._pd("plain words") is True
    assert L.scrub_url("http://example.com/plain") is None                # the host itself cannot be cleared


# B-MP-20: nesting deeper than 64 levels is skipped and counted; exactly 64 is parsed.
def test_B_MP_20_depth_edge_64_parsed_65_malformed():
    c = L.Counts()
    obj = L.parse_record(b"[" * L.MAX_DEPTH + b"]" * L.MAX_DEPTH, c)
    assert isinstance(obj, list) and c["records_malformed"] == 0
    assert L.parse_record(b"[" * (L.MAX_DEPTH + 1) + b"]" * (L.MAX_DEPTH + 1), c) is None
    assert c["records_malformed"] == 1


def _rec(uri, links):
    return {"Envelope": {"WARC-Header-Metadata": {"WARC-Type": "response", "WARC-Target-URI": uri},
                         "Payload-Metadata": {"HTTP-Response-Metadata": {"HTML-Metadata": {"Links": links}}}}}


# B-MP-20: a selected record needs BOTH a string target URI and a list of links; either missing skips it.
def test_B_MP_20_record_with_links_not_a_list_or_uri_not_a_string_is_skipped():
    c = L.Counts()
    assert L.page_links(_rec(HOST, []), c) == (HOST, None, [])             # control
    assert L.page_links(_rec(HOST, "not-a-list"), c) is None
    assert L.page_links(_rec(HOST, {"url": HOST}), c) is None
    assert L.page_links(_rec(12345, []), c) is None
    assert c["records_skipped"] == 3


# B-MP-21: a page whose own URL does not normalise is counted sources_invalid (not sources_withheld).
def test_B_MP_21_unparseable_source_url_counts_sources_invalid():
    c = L.Counts()
    line = json.dumps(_rec("not a url", [{"url": HOST + "x"}])).encode()
    assert list(L.extract_lines([line], c)) == []
    assert c["sources_invalid"] == 1 and c["sources_withheld"] == 0
    assert c["records_selected"] == 1


# B-MP-21: at most 10,000 links a page: exactly 10,000 has no excess recorded; 10,001 counts 1 over the cap.
def test_B_MP_21_links_cap_edge_10000_no_excess_10001_one_over():
    c = L.Counts()
    assert list(L._rows(HOST, HOST, [0] * L.MAX_LINKS, c, False)) == []
    assert c["links"] == L.MAX_LINKS and c["links_dropped"] == L.MAX_LINKS
    assert "links_over_cap" not in c                                        # nothing over the cap is recorded
    c = L.Counts()
    assert list(L._rows(HOST, HOST, [0] * (L.MAX_LINKS + 1), c, False)) == []
    assert c["links"] == L.MAX_LINKS and c["links_over_cap"] == 1
