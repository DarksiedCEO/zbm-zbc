"""MP-C3c boundary / refusal tests for marcus/graph_importer (marcus-pipeline-spec/1 B-MP-40, B-MP-42: the graph
document is validated strictly and refused whole) and marcus/index_store (B-MP-32, W-MP-2, GP-4, AC-10: no key ->
INDEX_KEY_UNAVAILABLE; only the encrypted store API names objects). Synthetic values only."""

from __future__ import annotations

import os
import pickle

import pytest

import vault as vault_mod
from marcus import graph_importer as G
from marcus import index_builder as B
from marcus import index_store as S

SID = "seo-snp-" + "a" * 40


def _graph(**over) -> dict:
    doc = {"contract": G.CONTRACT, "release": "cc-main-2026-jul-aug-sep", "kind": "host_graph", "nodes": 3,
           "snapshot_id": SID, "rows": {"www.example.com": {"hc_rank": 1, "pr_rank": 3}}}
    doc.update(over)
    return doc


def _refused(doc) -> None:
    with pytest.raises(B.ContractInvalid):
        G.validate_graph(doc)


def test_B_MP_42_control_the_synthetic_graph_document_is_valid():
    assert G.validate_graph(_graph())["nodes"] == 3


@pytest.mark.parametrize("over", [{"contract": "marcus.graph_ranks/2"}, {"release": "CC-MAIN-bad id"},
                                  {"kind": "crawl"}, {"nodes": -1}, {"nodes": True}, {"nodes": G.NODES_MAX + 1},
                                  {"snapshot_id": "seo-snp-xyz"}, {"snapshot_id": 7}, {"rows": []}])
def test_B_MP_42_each_header_field_is_checked_in_full(over):
    _refused(_graph(**over))


def test_B_MP_42_an_extra_or_missing_key_is_refused():
    _refused(dict(_graph(), extra=1))
    doc = _graph()
    del doc["rows"]
    _refused(doc)


def test_B_MP_42_node_count_edges_0_and_max_are_valid():
    assert G.validate_graph(_graph(nodes=0, rows={}))["nodes"] == 0        # a header-only rank file
    assert G.validate_graph(_graph(nodes=G.NODES_MAX))["nodes"] == G.NODES_MAX


def test_B_MP_40_rank_edges_1_and_nodes_kept_0_and_nodes_plus_1_refused():
    ok = {"www.example.com": {"hc_rank": 1, "pr_rank": 1}, "b.example.com": {"hc_rank": 3, "pr_rank": 3}}
    assert G.validate_graph(_graph(rows=ok))["rows"] == ok
    for bad in (0, 4):
        _refused(_graph(rows={"www.example.com": {"hc_rank": 1, "pr_rank": bad}}))


def test_B_MP_42_a_bad_row_key_or_value_is_refused():
    _refused(_graph(rows={"WWW.Example.com": {"hc_rank": 1, "pr_rank": 1}}))
    _refused(_graph(rows={"www.example.com": {"hc_rank": 1}}))


def test_B_MP_42_a_list_kind_or_a_null_rank_is_refused():
    _refused(_graph(kind=["host_graph"]))
    _refused(_graph(rows={"www.example.com": {"hc_rank": 1, "pr_rank": None}}))


def test_B_MP_42_a_lookup_error_inside_the_document_is_refused():
    class Raising(dict):
        def __getitem__(self, k):
            raise KeyError(k)
    _refused(Raising(_graph()))


def test_B_MP_40_a_duplicate_kept_host_is_counted_and_the_import_continues():
    lines = ["#" + "\t".join(G.HEADER), "1\t0.5\t1\t0.5\tcom.example.www", "2\t0.4\t2\t0.4\tcom.example.www",
             "3\t0.3\t3\t0.3\tcom.example.b"]
    counts = G.Counts()
    _, rows = G.parse_ranks(lines, {"www.example.com", "b.example.com"}, counts, 3)
    assert rows == {"www.example.com": {"hc_rank": 1, "pr_rank": 1}, "b.example.com": {"hc_rank": 3, "pr_rank": 3}}
    assert counts["rows_duplicate"] == 1 and counts["quarantine:SOURCE_ROW_INVALID"] == 1


# ---------------------------------------------------------------------------------------------- index_store


class _NoKeyVault:
    def index_keys(self):
        raise vault_mod.VaultUnavailable()


def test_AC_10_no_vault_key_is_index_key_unavailable():
    with pytest.raises(S.IndexKeyUnavailable):
        S.Keyring.from_vault(_NoKeyVault())


def test_AC_10_a_store_without_a_keyring_refuses_put_and_decrypt():
    st = S.IndexStore(None, None)
    with pytest.raises(S.IndexKeyUnavailable):
        st.put("stage", "a", b"x")
    blob = S.IndexStore(None, S.Keyring.ephemeral()).encrypt("stage", "a", b"x")
    with pytest.raises(S.IndexKeyUnavailable):
        st.decrypt(blob, "stage", "a")


def test_AC_10_the_keyring_is_never_pickled_so_no_key_is_written():  # renamed (lead 23:40, MP-C4b): GP-4 is not a spec id
    with pytest.raises(TypeError, match="never pickled"):
        pickle.dumps(S.Keyring.ephemeral())


@pytest.mark.parametrize("kind, name", [("bogus", "a"), ("stage", 5), ("stage", "A"), ("stage", "a..b"),
                                        ("stage", "../a"), ("stage", "")])
def test_W_MP_2_object_kind_and_name_are_checked(kind, name):
    st = S.IndexStore(None, S.Keyring.ephemeral())
    with pytest.raises(ValueError, match="index object name"):
        st.put(kind, name, b"x")
    with pytest.raises(ValueError, match="index object name"):
        st.object_path(kind, name)


def test_W_MP_2_names_of_an_unknown_kind_is_refused():
    for root in (None, "/nonexistent-mp-c3c"):
        with pytest.raises(ValueError, match="index object kind"):
            S.IndexStore(root, S.Keyring.ephemeral()).names("bogus")


def test_W_MP_2_names_lists_only_valid_names_and_never_temporary_files(tmp_path):
    st = S.IndexStore(str(tmp_path), S.Keyring.ephemeral())
    st.put("stage", "b", b"x")
    d = tmp_path / S.DIR_NAME / "stage"
    for junk in ("a.tmp", "Upper", "_x", ".hidden"):
        (d / junk).write_bytes(b"junk")
    assert st.names("stage") == ["b"]


def test_B_MP_32_an_unreadable_object_is_skipped_and_the_rest_still_listed(tmp_path):
    st = S.IndexStore(str(tmp_path), S.Keyring.ephemeral())
    st.put("stage", "b", b"x")
    st.put("stage", "c", b"y")
    os.symlink(str(tmp_path / "nowhere"), str(tmp_path / S.DIR_NAME / "stage" / "a"))   # refused: O_NOFOLLOW
    raw = st.raw_objects()
    assert len(raw) == 2 and raw == [st.get_blob("stage", "b"), st.get_blob("stage", "c")]
