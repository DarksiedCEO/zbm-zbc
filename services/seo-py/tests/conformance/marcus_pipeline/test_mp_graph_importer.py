"""marcus-pipeline-spec/1 section 5: the Graph Importer (B-MP-40..43; GP-3) and A-MP-04 (graph hostility)."""

from __future__ import annotations

import json
import random
import subprocess
import sys
import textwrap

import pytest

from conformance.marcus_pipeline import gen_cc
from conformance.marcus_pipeline.util import all_rows, build, graphs, watch

REGISTRY = {"cc_graph_importer": {
    "assignment": "import the host / domain graph rank files of one release",
    "inputs": ["one recorded host_graph or domain_graph release (rank files, GP-3)",
               "the current snapshot's slices (the hosts and domains to keep)"],
    "outputs": ["marcus.graph_ranks/1 (encrypted at rest)"],
    "name_pending": True, "proposed_name": "Gideon"}}


def _slice_hosts(h, sid):
    hs = set()
    for r in all_rows(h, sid):
        hs.add(r["src"].split("/")[2].split(":")[0])
        hs.add(r["dst"].split("/")[2].split(":")[0])
    return hs


def test_B_MP_40_42_host_ranks_imported_for_the_slices_only_encrypted(mp):
    watch(mp)
    sid = build(mp)["snapshot_id"]
    j = graphs(mp)
    assert j["outcome"] == "OK", j
    rec = mp.svc.graph_ranks[(gen_cc.GRAPH, "host_graph")]
    doc = mp.svc.index_store.read_graph(rec["name"], rec["sha256"])
    assert set(doc) == {"contract", "release", "kind", "nodes", "snapshot_id", "rows"}
    assert doc["contract"] == "marcus.graph_ranks/1" and doc["release"] == gen_cc.GRAPH and doc["snapshot_id"] == sid
    want = _slice_hosts(mp, sid)
    assert set(doc["rows"]) == want
    assert all(set(v) == {"hc_rank", "pr_rank"} and isinstance(v["hc_rank"], int) for v in doc["rows"].values())
    total = sum(1 for ln in __import__("gzip").open(mp.fx / gen_cc.rank_path(gen_cc.GRAPH, "host_graph"), "rt")
                if not ln.startswith("#"))
    assert doc["nodes"] == total and j["facts"]["rows_kept"] == len(want)
    raw = b"".join(mp.svc.index_store.raw_objects())
    assert not any(hh.encode() in raw for hh in want)                      # encrypted at rest (B-MP-42)
    d = graphs(mp, kind="domain_graph")
    assert d["outcome"] == "OK"
    rd = mp.svc.graph_ranks[(gen_cc.GRAPH, "domain_graph")]
    ddoc = mp.svc.index_store.read_graph(rd["name"], rd["sha256"])
    assert set(ddoc["rows"]) <= {".".join(x.split(".")[-2:]) for x in want} and ddoc["rows"]


BAD_ROWS = {
    "non_numeric": "x\t1.0\t1\t1.0\tcom.example.a",
    "negative": "-3\t1.0\t1\t1.0\tcom.example.a",
    "negative_value": "3\t-1.0\t1\t1.0\tcom.example.a",
    "float_rank": "3.5\t1.0\t1\t1.0\tcom.example.a",
    "nan": "3\tnan\t1\t1.0\tcom.example.a",
    "too_few": "3\t1.0\t1\tcom.example.a",
    "too_many": "3\t1.0\t1\t1.0\tcom.example.a\textra",
    "rank_over_nodes": "999999\t1.0\t1\t1.0\tcom.example.a",
    "pr_over_nodes": "1\t1.0\t999999\t1.0\tcom.example.a",
    "bad_host": "3\t1.0\t1\t1.0\tcom..example",
    "upper_host": "3\t1.0\t1\t1.0\tCOM.EXAMPLE.A",
    "space_host": "3\t1.0\t1\t1.0\tcom.exa mple",
    "zero_rank": "0\t1.0\t1\t1.0\tcom.example.a",
}


@pytest.mark.parametrize("case", sorted(BAD_ROWS))
def test_B_MP_40_strict_rows_quarantined_SOURCE_ROW_INVALID(mp, case):
    from marcus import graph_importer
    c = graph_importer.Counts()
    lines = [gen_cc.RANK_HEADER, "1\t9.0\t2\t0.5\texample.a", "2\t8.0\t1\t0.6\texample.b", BAD_ROWS[case]]
    nodes, rows = graph_importer.parse_ranks(iter(x + "\n" for x in lines), {"a.example", "b.example"}, c, nodes=3)
    assert c["rows_invalid"] == 1 and c["quarantine:SOURCE_ROW_INVALID"] == 1, (case, dict(c))
    assert rows == {"a.example": {"hc_rank": 1, "pr_rank": 2}, "b.example": {"hc_rank": 2, "pr_rank": 1}}


def test_B_MP_40_duplicate_kept_host_quarantined_and_header_required():
    from marcus import graph_importer
    c = graph_importer.Counts()
    lines = [gen_cc.RANK_HEADER, "1\t9\t1\t1\texample.a", "2\t8\t2\t1\texample.a", "3\t7\t3\t1\texample.c"]
    _, rows = graph_importer.parse_ranks(iter(x + "\n" for x in lines), {"a.example"}, c, nodes=3)
    assert rows == {"a.example": {"hc_rank": 1, "pr_rank": 1}} and c["rows_duplicate"] == 1
    with pytest.raises(graph_importer.SourceInvalid) as e:
        graph_importer.parse_ranks(iter(["1\t9\t1\t1\texample.a\n"]), {"a.example"}, graph_importer.Counts(), nodes=1)
    assert e.value.code == "SOURCE_SCHEMA_UNKNOWN"


def test_A_MP_04_reversed_host_tricks_never_map_onto_a_slice_host(mp):
    from marcus import graph_importer
    keep = {"client.test", "www.client.test"}
    tricks = ["com.evil.test.client", "test.client.evil", "test.client", "test.clientx", "test.client.",
              ".test.client", "test.client\u200b", "test.xn--clent-zbb", "test\\.client", "test.client:80"]
    c = graph_importer.Counts()
    lines = [gen_cc.RANK_HEADER] + [f"{i + 1}\t1\t{i + 1}\t1\t{t}" for i, t in enumerate(tricks)]
    _, rows = graph_importer.parse_ranks(iter(x + "\n" for x in lines), keep, c, nodes=len(tricks))
    assert rows == {"client.test": {"hc_rank": 3, "pr_rank": 3}}
    assert graph_importer.unreverse("com.evil.test.client") == "client.test.evil.com"


@pytest.mark.parametrize("seed", range(20))
def test_A_MP_04_generated_hostility_fast(mp, seed):
    from marcus import graph_importer
    rng = random.Random(seed)
    hosts = [f"h{i}.example" for i in range(200)]
    rows = gen_cc.rank_rows(hosts, seed)
    text = gen_cc.rank_text(rows).splitlines()
    bad = 0
    for _ in range(rng.randint(1, 15)):
        i = rng.randrange(1, len(text))
        text[i] = rng.choice(list(BAD_ROWS.values()))
        bad += 1
    c = graph_importer.Counts()
    _, kept = graph_importer.parse_ranks(iter(x + "\n" for x in text), set(hosts[:50]), c, nodes=len(text) - 1)
    assert c["rows_invalid"] + c["rows_duplicate"] == bad or c["rows_invalid"] <= bad
    assert all(h in hosts[:50] for h in kept)


@pytest.mark.lead_gate
def test_B_MP_41_A_MP_04_streaming_peak_under_64_MiB_on_10e8_rows():
    code = textwrap.dedent("""
        import resource, sys
        sys.path.insert(0, %r)
        from marcus import graph_importer
        N = 10 ** 8
        def stream():
            yield "#harmonicc_pos\\tharmonicc_val\\tpr_pos\\tpr_val\\thost_rev\\n"
            for i in range(1, N + 1):
                yield "%%d\\t1.5\\t%%d\\t0.25\\texample.h%%d\\n" %% (i, N + 1 - i, i)
        base = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        c = graph_importer.Counts()
        nodes, rows = graph_importer.parse_ranks(stream(), {"h7.example", "h99999999.example"}, c, nodes=N)
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - base
        print(nodes, len(rows), peak)
    """) % str(__import__("pathlib").Path(graph_importer_src()))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=7200)
    nodes, kept, peak_kib = out.stdout.split()
    assert int(nodes) == 10 ** 8 and int(kept) == 2 and int(peak_kib) * 1024 <= 64 * 1024 * 1024, out.stdout


def graph_importer_src():
    import marcus
    return __import__("pathlib").Path(marcus.__file__).resolve().parents[1]


def test_B_MP_41_streaming_bound_fast():
    import tracemalloc
    from marcus import graph_importer
    n = 300_000

    def stream():
        yield gen_cc.RANK_HEADER + "\n"
        for i in range(1, n + 1):
            yield f"{i}\t1.5\t{n + 1 - i}\t0.25\texample.h{i}\n"
    tracemalloc.start()
    try:
        nodes, rows = graph_importer.parse_ranks(stream(), {"h7.example"}, graph_importer.Counts(), nodes=n)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert nodes == n and rows == {"h7.example": {"hc_rank": 7, "pr_rank": n - 6}} and peak < 8 * 1024 * 1024


def test_B_MP_42_needs_a_published_snapshot(mp):
    watch(mp)
    j = graphs(mp)
    assert j["outcome"] == "INSUFFICIENT_EVIDENCE" and j["facts"]["reason"] == "NO_COMPLETE_SNAPSHOT"
    assert mp.svc.graph_ranks == {}


def test_B_MP_43_registry_byte_for_byte():
    import svc_manager
    for agent, block in REGISTRY.items():
        shown = {**svc_manager.REGISTRY[agent], **svc_manager.NAMES[agent]}       # naming beside the block (Q-MP-1)
        assert set(svc_manager.REGISTRY[agent]) == {"assignment", "inputs", "outputs"}
        assert json.dumps(shown, sort_keys=True) == json.dumps(block, sort_keys=True)
        assert agent in svc_manager.AGENTS and agent not in svc_manager.NOT_BUILT_AGENTS
