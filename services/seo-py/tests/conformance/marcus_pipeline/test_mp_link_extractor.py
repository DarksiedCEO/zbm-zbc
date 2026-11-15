"""marcus-pipeline-spec/1 section 3: the Link Extractor (B-MP-20..27), A-MP-02 (hostile URLs), O-MP-1..3."""

from __future__ import annotations

import json
import random

import pytest

from conformance.marcus_pipeline import gen_cc
from conformance.marcus_pipeline.util import all_rows, build, watch

REGISTRY = {"cc_link_extractor": {
    "assignment": "turn WAT records into normalised, personal-data-scrubbed link rows",
    "inputs": ["WAT records (GP-2) delivered by the WAT Fetcher"],
    "outputs": ["link rows {src, dst, anchor, path, rel}"],
    "name_pending": True, "proposed_name": "Esther"}}


def _x(lines, rel_verified=False):
    from marcus import link_extractor
    c = link_extractor.Counts()
    rows = list(link_extractor.extract_lines(iter(lines), c, rel_verified=rel_verified))
    return rows, c


def _rec(target_uri="https://src.example/p", links=(), wtype="response", html=True, base=None):
    meta = {"Links": list(links)}
    if base:
        meta["Head"] = {"Base": base}
    env = {"WARC-Header-Metadata": {"WARC-Type": wtype, "WARC-Target-URI": target_uri}}
    if html:
        env["Payload-Metadata"] = {"HTTP-Response-Metadata": {"HTML-Metadata": meta}}
    return gen_cc.line({"Envelope": env})


# ---------------------------------------------------------------------------------------------- B-MP-20

def test_B_MP_20_selects_html_response_records_with_link_metadata_only():
    ln = [{"path": "A@/href", "url": "https://acme.test/x", "text": "x"}]
    lines = [_rec(links=ln), _rec(links=ln, wtype="request"), _rec(links=ln, wtype="metadata"),
             _rec(links=ln, html=False), gen_cc.line({"Envelope": {"WARC-Header-Metadata": {}}}), b"",
             gen_cc.line({"Envelope": {"WARC-Header-Metadata": {"WARC-Type": "response", "WARC-Target-URI": 7}}})]
    rows, c = _x(lines)
    assert len(rows) == 1 and c["records"] == 6 and c["records_selected"] == 1


@pytest.mark.parametrize("bad,counter", [
    (b"{not json", "records_malformed"), (b'{"a":' * 65 + b"1" + b"}" * 65, "records_malformed"),
    (b"[" * 100_000 + b"]" * 100_000, "records_malformed"), (b'{"a": Infinity}', "records_malformed"),
    (b'{"a":1,"a":2}', "records_malformed"), (b"\xc3\x28", "records_malformed"),
    (b'{"a":"' + b"y" * (16 * 1024 * 1024) + b'"}', "records_too_large")])
def test_B_MP_20_malformed_deep_or_large_records_skipped_and_counted(bad, counter):
    good = [_rec(f"https://src{i}.example/p", links=[{"path": "A@/href", "url": "https://acme.test/x"}])
            for i in range(2)]
    rows, c = _x([good[0], bad, good[1]])
    assert len(rows) == 2 and c[counter] == 1


def test_B_MP_20_depth_64_is_accepted():
    from marcus import link_extractor
    assert link_extractor.MAX_DEPTH == 64 and link_extractor.MAX_RECORD == 16 * 1024 * 1024
    rec = json.loads(_rec(links=[{"path": "A@/href", "url": "https://acme.test/x"}]))
    rec["pad"] = json.loads(b'{"a":' * 60 + b"1" + b"}" * 60)          # depth 61 + the record's own levels
    rows, c = _x([gen_cc.line(rec)])
    assert c["records_malformed"] == 0 and len(rows) == 1


# ---------------------------------------------------------------------------------------------- B-MP-21

@pytest.mark.parametrize("seed", [21, 22, 23])
def test_B_MP_21_rows_equal_the_generators_well_formed_links_exactly(seed):
    files, truth = gen_cc.corpus(seed, 3, 20)
    lines = [gen_cc.line(p.record()) for f in files for p in f]
    rows, c = _x(lines)
    assert rows == truth
    assert c["links_dropped"] == 0


def test_B_MP_21_only_http_https_targets_counted_by_scheme_and_link_cap():
    from marcus import link_extractor
    assert link_extractor.MAX_LINKS == 10_000
    links = [{"path": "A@/href", "url": u} for u in ("mailto:x@y.example", "javascript:void(0)", "ftp://f.example/a",
                                                    "tel:5550100", "data:text/plain,hi", "file:///etc/x",
                                                    "gopher://g.example/", "https://acme.test/ok")]
    rows, c = _x([_rec(links=links)])
    assert [r["dst"] for r in rows] == ["https://acme.test/ok"]
    assert c["links_scheme:mailto"] == 1 and c["links_scheme:javascript"] == 1 and c["links_scheme:ftp"] == 1
    assert c["links_scheme:tel"] == 1 and c["links_scheme:data"] == 1 and c["links_scheme:file"] == 1
    assert c["links_scheme:other"] == 1
    many = [{"path": "A@/href", "url": f"https://acme.test/p{i % 7}/q"} for i in range(10_007)]
    rows, c = _x([_rec(links=many)])
    assert len(rows) == 10_000 and c["links_over_cap"] == 7


# ---------------------------------------------------------------------------------------------- B-MP-22

def _norm(u, base=None):
    from marcus import link_extractor
    return link_extractor.normalise(u, base)[0]


@pytest.mark.parametrize("seed", range(5))
def test_B_MP_22_normalisation_properties(seed):
    rng = random.Random(seed)
    for _ in range(400):
        h = gen_cc.host(rng)
        segs = gen_cc.slug(rng)
        path = "/" + "/".join(segs)
        want = f"https://{h}{path}"
        variants = [f"HTTPS://{h.upper()}{path}", f"https://{h}.{path}", f"https://{h}:443{path}",
                    f"https://{h}{path}#frag", f"https://{h}{path}?utm_source=a&gclid=b&fbclid=c",
                    f"https://user:pw@{h}{path}", f"https://{h}{path}?utm_medium=x#y", f"HtTpS://{h}.:443{path}"]
        for v in variants:
            assert _norm(v) == want, v
        assert _norm(f"http://{h}:80{path}") == f"http://{h}{path}"
        assert _norm(f"https://{h}:8443{path}") == f"https://{h}:8443{path}"
        assert _norm(path, f"https://{h}/a/b") == want
        assert _norm(f"https://{h}/a/../{segs[0]}") == f"https://{h}/{segs[0]}"
    assert _norm("https://b\u00fccher.example/x") == "https://xn--bcher-kva.example/x"
    assert _norm("https://acme.test/" + "a" * 2100) is None
    assert _norm("https://acme.test/a?keep=1&utm_x=2") == "https://acme.test/a?keep=1"


def test_B_MP_22_userinfo_never_kept_and_long_urls_counted():
    links = [{"path": "A@/href", "url": "https://someone:hunter2x@acme.test/a"},
             {"path": "A@/href", "url": "https://acme.test/" + "z" * 2049}]
    rows, c = _x([_rec(links=links)])
    assert [r["dst"] for r in rows] == ["https://acme.test/a"] and c["urls_too_long"] == 1
    assert "someone" not in json.dumps(rows) and "hunter2x" not in json.dumps(rows)


# ---------------------------------------------------------------------------------------------- B-MP-24 / 25 / 26

def test_B_MP_24_rel_unknown_unless_a_spike_is_recorded(mp):
    from marcus import link_extractor
    links = [{"path": "A@/href", "url": "https://acme.test/a", "rel": "nofollow"},
             {"path": "A@/href", "url": "https://acme.test/b", "rel": "sponsored ugc"}]
    rows, _ = _x([_rec(links=links)])
    assert [r["rel"] for r in rows] == ["UNKNOWN", "UNKNOWN"]
    rows, _ = _x([_rec(links=links)], rel_verified=True)
    assert [r["rel"] for r in rows] == ["nofollow", "sponsored ugc"]
    assert link_extractor.REL_TOKENS >= {"nofollow", "sponsored", "ugc", "follow"}
    watch(mp)
    j = build(mp)
    man = mp.svc.index_store.read_manifest(j["snapshot_id"])
    assert man["builder_version"]["rel_capture"] == "UNVERIFIED"
    assert {r["rel"] for r in all_rows(mp, j["snapshot_id"])} == {"UNKNOWN"}
    with pytest.raises(Exception):
        mp.svc.record_cc_spike({"request_id": __import__("helpers").rid(), "rel_capture": "VERIFIED"}, andre=False)
    mp.svc.record_cc_spike({"request_id": __import__("helpers").rid(), "rel_capture": "VERIFIED",
                            "approval_ref": "a" * 64}, andre=True)
    gen_cc.write_crawl(mp.fx, "CC-MAIN-2026-40", [[gen_cc.Page("src.example", ("p",), [gen_cc.Link(
        "acme.test", ("a",), rel="nofollow")])]])
    j2 = build(mp)
    assert mp.svc.index_store.read_manifest(j2["snapshot_id"])["builder_version"]["rel_capture"] == "VERIFIED"
    assert {r["rel"] for r in all_rows(mp, j2["snapshot_id"])} == {"nofollow"}


def test_B_MP_25_deterministic_same_file_same_rows_same_order():
    from marcus import link_extractor
    files, _ = gen_cc.corpus(25, 2, 30)
    lines = [gen_cc.line(p.record()) for f in files for p in f]
    a, _ = _x(lines)
    b, _ = _x(lines)
    assert [link_extractor.row_hash(r) for r in a] == [link_extractor.row_hash(r) for r in b]
    assert link_extractor.row_hash(a[0]) == link_extractor.row_hash(json.loads(json.dumps(a[0])))


def test_B_MP_26_throughput_reported_not_certified(mp):
    watch(mp)
    j = build(mp)
    t = j["facts"]["throughput"]
    assert set(t) == {"records_per_s", "links_per_page", "label"} and t["label"] == "measured"
    assert isinstance(t["records_per_s"], (int, float)) and t["links_per_page"] > 0
    assert "throughput is reported, not certified" in " ".join(j["limitations"])


def test_B_MP_27_registry_byte_for_byte():
    import svc_manager
    for agent, block in REGISTRY.items():
        shown = {**svc_manager.REGISTRY[agent], **svc_manager.NAMES[agent]}       # naming beside the block (Q-MP-1)
        assert set(svc_manager.REGISTRY[agent]) == {"assignment", "inputs", "outputs"}
        assert json.dumps(shown, sort_keys=True) == json.dumps(block, sort_keys=True)
        assert agent in svc_manager.AGENTS and agent not in svc_manager.NOT_BUILT_AGENTS
