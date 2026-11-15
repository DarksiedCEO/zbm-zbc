"""MP-C3b guard tests for marcus/index_builder.validate_manifest (marcus-pipeline-spec/1 B-MP-33, CB-19: the manifest
is validated strictly by every consumer and refused whole). Synthetic manifests only."""

from __future__ import annotations

import pytest

from marcus import index_builder as B

H40, S64 = "a" * 40, "b" * 64
BAD64 = "z" * 64                                      # right length, not lower-case hex


def _manifest() -> dict:
    return {"contract": B.CONTRACT, "snapshot_id": "seo-snp-" + H40, "crawls": ["CC-MAIN-2024-10"],
            "built_at": "2026-11-14T00:00:00Z",
            "builder_version": {"version": B.BUILDER_VERSION, "rel_capture": "UNVERIFIED", "spike_ref": None},
            "shards": [{"shard_id": "seo-shd-" + H40, "sha256": S64, "domain": "example.com", "rows": 1}],
            "counts": {"rows": 1}, "licence_ack": B.LICENCE_ACK, "complete": True, "previous_snapshot_id": None,
            "crawled_sources": {"previous_snapshot_id": None, "sha256": S64, "crawled": 0, "not_crawled": 0},
            "key_id": "k1"}


def _refused(doc) -> None:
    with pytest.raises(B.ContractInvalid):
        B.validate_manifest(doc)


def test_B_MP_33_control_the_synthetic_manifest_is_valid():
    assert B.validate_manifest(_manifest())["snapshot_id"] == "seo-snp-" + H40


# B-MP-24 / B-MP-33: a VERIFIED rel capture needs a spike_ref that is a sha256 (lower-case hex), not any string.
def test_B_MP_33_verified_spike_ref_must_be_a_sha256_not_any_string():
    doc = _manifest()
    doc["builder_version"].update(rel_capture="VERIFIED", spike_ref=S64)
    B.validate_manifest(doc)                                               # control
    doc["builder_version"]["spike_ref"] = BAD64
    _refused(doc)


# B-MP-32 / B-MP-33: every shard's sha256 is a sha256; a string that is not one refuses the whole manifest.
def test_B_MP_33_shard_sha256_must_be_hex_and_the_rest_still_checked():
    doc = _manifest()
    doc["shards"][0]["sha256"] = BAD64
    _refused(doc)
    doc = _manifest()
    doc["shards"][0].update(sha256=BAD64, domain="com", rows=B.SHARD_MAX_ROWS + 1)
    _refused(doc)


# B-MP-31 / B-MP-33: the crawled-sources evidence block is checked in full (hash and counts), not just its link.
def test_B_MP_33_crawled_sources_hash_and_counts_checked_in_full():
    doc = _manifest()
    doc["crawled_sources"]["sha256"] = BAD64
    _refused(doc)
    doc = _manifest()
    doc["crawled_sources"]["crawled"] = -1
    _refused(doc)


class _Unreadable(dict):
    """A mapping that lists the manifest keys but cannot produce one of them (a broken reader / proxy object)."""

    def __getitem__(self, key):
        if key == "contract":
            raise KeyError(key)
        return dict.__getitem__(self, key)


# CB-19 / B-MP-33: a manifest the validator cannot read is refused (ContractInvalid), never returned as None.
def test_B_MP_33_CB_19_an_unreadable_manifest_is_refused_not_passed_through():
    doc = _Unreadable(_manifest())
    with pytest.raises(B.ContractInvalid):
        B.validate_manifest(doc)
