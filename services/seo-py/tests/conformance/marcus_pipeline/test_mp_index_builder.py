"""marcus-pipeline-spec/1 section 4: the Index Builder (B-MP-30..36; GP-4), AC-10, AC-13."""

from __future__ import annotations

import base64
import json
import os

import pytest

from conformance.marcus_pipeline import gen_cc
from conformance.marcus_pipeline.util import all_rows, build, kinds_logged, post_job, snapshot, watch
from helpers import rid

C1, C2 = gen_cc.CRAWLS
REGISTRY = {"cc_index_builder": {
    "assignment": "build one snapshot: the link slices for the registered target domains, sharded, encrypted, "
                  "manifested",
    "inputs": ["link rows from the Link Extractor", "the registered domains of every tenant at build time",
               "the previous snapshot's slices (crawled_sources)"],
    "outputs": ["marcus.index_snapshot/1 (manifest) and its encrypted shards"],
    "name_pending": True, "proposed_name": "Ivan"}}


def _key(r):
    return json.dumps(r, sort_keys=True)


# ---------------------------------------------------------------------------------------------- B-MP-30

LOOKALIKES = ("acme.test.evil.test", "xn--cme-5cd.test", "\u0430cme.test", "acme.tes", "acmetest.example", "evilacme.test",
              "acme.test-evil.test", "globex.test.acme.example")


def test_B_MP_30_target_slices_match_registered_domains_never_lookalikes(mp):
    pages = []
    for i, h in enumerate(("acme.test", "www.acme.test", "shop.acme.test", "a.b.globex.test") + LOOKALIKES):
        pages.append(gen_cc.Page(f"ref{i}.example", ("p",), [gen_cc.Link(h, ("x",), anchor=f"a{i}")]))
    pages.append(gen_cc.Page("ref.example", ("q",), [
        gen_cc.Link("evil.test", ("x",)), gen_cc.Link("acme.test", ("x",), noise=("port",))]))
    pages[-1].links[0].raw = lambda: "https://acme.test@evil.test/x"
    pages.append(gen_cc.Page("ref2.example", ("q",), [gen_cc.Link("evil.test", ("y",))]))
    pages[-1].links[0].raw = lambda: "https://acme.test:443@evil.test/y"
    gen_cc.write_crawl(mp.fx, C1, [pages])
    watch(mp)
    j = build(mp)
    s = snapshot(mp, j["snapshot_id"])
    doms = sorted(sh["domain"] for sh in s["manifest"]["shards"])
    assert doms == ["acme.test", "globex.test"]
    hosts = sorted(r["dst"].split("/")[2] for r in all_rows(mp, j["snapshot_id"]))
    assert hosts == sorted(["acme.test", "www.acme.test", "shop.acme.test", "a.b.globex.test", "acme.test"])
    assert j["facts"]["links_lookalike"] >= 2


@pytest.mark.parametrize("seed", [30, 31])
def test_B_MP_30_oracle_over_a_generated_corpus(mp, seed):
    files, truth = gen_cc.corpus(seed, 4, 20)
    gen_cc.write_crawl(mp.fx, C1, files)
    watch(mp)
    j = build(mp)
    want = [r for r in truth if r["dst"].split("/")[2].split(":")[0] in ("acme.test", "globex.test") or
            r["dst"].split("/")[2].split(":")[0].endswith((".acme.test", ".globex.test"))]
    assert sorted(map(_key, all_rows(mp, j["snapshot_id"]))) == sorted(map(_key, want))


# ---------------------------------------------------------------------------------------------- B-MP-31

def test_B_MP_31_crawled_sources_evidence_for_the_previous_slices(mp):
    p = [gen_cc.Page(f"src{i}.example", ("a",), [gen_cc.Link("acme.test", ("t", str(i)))]) for i in range(6)]
    gen_cc.write_crawl(mp.fx, C1, [p])
    watch(mp)
    s1 = build(mp, crawls=(C1,))
    recrawled = [p[0], p[2], p[4]]
    recrawled[1].links = []                                          # crawled again, its link gone (LOST evidence)
    new = gen_cc.Page("fresh.example", ("b",), [gen_cc.Link("acme.test", ("n",))])
    gen_cc.write_crawl(mp.fx, C2, [recrawled + [new]])
    s2 = build(mp, crawls=(C2,))
    man = mp.svc.index_store.read_manifest(s2["snapshot_id"])
    cs = man["crawled_sources"]
    assert cs["previous_snapshot_id"] == s1["snapshot_id"] and cs["crawled"] == 3 and cs["not_crawled"] == 3
    doc = mp.svc.index_store.read_sources(s2["snapshot_id"], cs["sha256"])
    hk = lambda u: mp.svc.vault.field_hash("zbm", "crawled_source", u)[1]          # noqa: E731
    assert doc["crawled"] == sorted(hk(x.url()) for x in recrawled)
    assert doc["not_crawled"] == sorted(hk(x.url()) for x in (p[1], p[3], p[5]))
    assert not any(x.host in json.dumps(doc) for x in p)                       # a keyed-hash set, no URL
    first = mp.svc.index_store.read_manifest(s1["snapshot_id"])["crawled_sources"]
    assert first["previous_snapshot_id"] is None and first["crawled"] == 0


# ---------------------------------------------------------------------------------------------- B-MP-32

def test_B_MP_32_shards_per_registrable_domain_encrypted_and_hashed(mpd, monkeypatch):
    from marcus import index_builder, index_store
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    import vault
    assert index_builder.SHARD_MAX_ROWS == 500_000 and index_store.HKDF_INFO == b"marcus-index/v1"
    monkeypatch.setattr(index_builder, "SHARD_MAX_ROWS", 7)
    p = [gen_cc.Page(f"s{i}.example", ("a",), [gen_cc.Link("acme.test", ("t", f"x{k}")) for k in range(4)])
         for i in range(5)]
    gen_cc.write_crawl(mpd.fx, C1, [p])
    watch(mpd)
    j = build(mpd)
    man = mpd.svc.index_store.read_manifest(j["snapshot_id"])
    acme = [s for s in man["shards"] if s["domain"] == "acme.test"]
    assert [s["rows"] for s in acme] == [7, 7, 6] and len({s["shard_id"] for s in man["shards"]}) == len(man["shards"])
    raw = base64.urlsafe_b64decode(open(mpd.tmp / "vault.key").read().strip() + "==")
    aead = AESGCM(vault._derive(raw, b"marcus-index/v1"))                    # GP-4, computed by the test itself
    for sh in acme:
        path = mpd.svc.index_store.object_path("shard", sh["shard_id"])
        blob = open(path, "rb").read()
        import hashlib
        assert hashlib.sha256(blob).hexdigest() == sh["sha256"]
        assert b"acme.test" not in blob and b"s0.example" not in blob           # unreadable without the key
        plain = aead.decrypt(*index_store.split_blob(blob, "shard", sh["shard_id"]))
        assert b"acme.test" in plain
        bad = bytearray(blob)
        bad[-5] ^= 1
        with open(path, "wb") as f:
            f.write(bytes(bad))
        with pytest.raises(index_store.StoreCorrupt):
            mpd.svc.index_store.read_shard(j["snapshot_id"], sh["shard_id"], None)   # tamper -> GCM failure
        with pytest.raises(index_store.StoreCorrupt):
            mpd.svc.index_store.read_shard(j["snapshot_id"], sh["shard_id"], sh["sha256"])
        with open(path, "wb") as f:
            f.write(blob)
    other = index_store.IndexStore(None, index_store.Keyring.ephemeral())
    with pytest.raises(index_store.StoreCorrupt):
        other.decrypt(open(mpd.svc.index_store.object_path("shard", acme[0]["shard_id"]), "rb").read(), "shard",
                      acme[0]["shard_id"])


@pytest.mark.lead_gate
def test_B_MP_32_lead_real_split_at_500000(mp):
    pages = [gen_cc.Page(f"s{i}.example", ("a",), [gen_cc.Link("acme.test", ("t", f"x{k}")) for k in range(1000)])
             for i in range(501)]
    gen_cc.write_crawl(mp.fx, C1, [pages[i:i + 100] for i in range(0, 501, 100)])
    watch(mp)
    j = build(mp)
    man = mp.svc.index_store.read_manifest(j["snapshot_id"])
    assert [s["rows"] for s in man["shards"] if s["domain"] == "acme.test"] == [500_000, 1000]


# ---------------------------------------------------------------------------------------------- B-MP-33 / AC-13

class Crash(BaseException):
    pass


STEPS = ("release_read", "file_open:0", "file_records:0", "stage_written:0", "file_done:0", "file_open:1",
         "stage_written:1", "file_done:1", "shards_written", "shard_verified:0", "manifest_built", "manifest_written",
         "recorded")


@pytest.mark.parametrize("step", STEPS)
def test_B_MP_33_AC_13_crash_at_every_step_leaves_no_complete_manifest(make_mp, step):
    h = make_mp(data=True, sub=step.replace(":", "_"))
    watch(h)
    seen = []

    def hook(s):
        seen.append(s)
        if s == step:
            raise Crash()
    h.svc._mp_hook = hook
    try:
        h.svc.link_index_start("dashboard", {"request_id": rid(), "service": "cc_index_builder", "crawl_ids": [C1]})
        crashed = False
    except Crash:
        crashed = True
    assert step in seen
    h2 = h.restart()
    published = [s for s in h2.svc.index_snapshots.values() if s.get("status") == "published"]
    if crashed and step != "recorded":
        assert published == [] and h2.svc.complete_snapshots() == []
        assert h2.svc.index_store.names("manifest") == []                 # no complete manifest left behind
    else:
        assert len(published) == 1
    h2.svc.run_job("interrupted-audits", {"request_id": rid()})
    j = build(h2)
    assert j["outcome"] == "OK" and len(h2.svc.complete_snapshots()) == 1 + (step == "recorded")


def test_B_MP_33_complete_only_after_every_shard_reread_and_hash_checked(mpd, monkeypatch):
    from marcus import index_store
    real = index_store.IndexStore.put
    calls = []

    def flaky(self, kind, name, data):
        calls.append(kind)
        sha = real(self, kind, name, data)
        if kind == "shard" and calls.count("shard") == 1:
            p = self.object_path(kind, name)
            b = bytearray(open(p, "rb").read())
            b[20] ^= 0xFF                                                   # the disk returns other bytes
            open(p, "wb").write(bytes(b))
        return sha
    monkeypatch.setattr(index_store.IndexStore, "put", flaky)
    manifests_at_reread = []
    real_read = index_store.IndexStore.read_shard

    def read(self, *a, **k):
        manifests_at_reread.append(len(self.names("manifest")))
        return real_read(self, *a, **k)
    monkeypatch.setattr(index_store.IndexStore, "read_shard", read)
    watch(mpd)
    j = build(mpd)
    assert manifests_at_reread and set(manifests_at_reread) == {0}          # nothing complete before the check
    assert j["outcome"] == "FAILED" and j["facts"]["reason"] == "SHARD_VERIFY_FAILED"
    assert mpd.svc.index_store.names("manifest") == [] and mpd.svc.complete_snapshots() == []


# ---------------------------------------------------------------------------------------------- B-MP-34

def _tiny(h, i):
    gen_cc.write_crawl(h.fx, C1, [[gen_cc.Page(f"r{i}.example", ("a",), [gen_cc.Link("acme.test", (f"v{i}",))])]])
    return build(h)


def test_B_MP_34_retention_keeps_the_last_6_deletes_whole_manifest_first(mpd, monkeypatch):
    from marcus import index_store
    watch(mpd)
    order = []
    real = index_store.IndexStore.delete
    monkeypatch.setattr(index_store.IndexStore, "delete",
                        lambda self, kind, name: order.append((kind, name)) or real(self, kind, name))
    sids = [_tiny(mpd, i)["snapshot_id"] for i in range(8)]
    live = [s["snapshot_id"] for s in mpd.svc.complete_snapshots()]
    assert live == sids[2:]
    retired = [r["data"]["snapshot_id"] for r in mpd.svc.log.iter_records() if r["kind"] == "index_snapshot_retired"]
    assert retired == sids[:2]
    for sid in retired:                                   # deleted whole: the manifest first, then its shards
        i = order.index(("manifest", sid))
        assert order[i + 1][0] == "shard" and ("shard", order[i + 1][1]) not in order[:i]
    assert all(not os.path.exists(mpd.svc.index_store.object_path("manifest", s)) for s in sids[:2])


def test_B_MP_34_a_snapshot_referenced_by_a_report_is_kept_until_its_retention_ends(mp):
    from datetime import timedelta
    from clock import iso
    watch(mp)
    first = _tiny(mp, 0)["snapshot_id"]
    mp.svc.backlink_reports["rpt"] = {"snapshot_id": first, "retain_until": iso(mp.clock.now() + timedelta(days=30))}
    for i in range(1, 8):
        _tiny(mp, i)
    live = [s["snapshot_id"] for s in mp.svc.complete_snapshots()]
    assert first in live and len(live) == 7
    mp.clock.advance(days=31)
    _tiny(mp, 9)
    assert first not in [s["snapshot_id"] for s in mp.svc.complete_snapshots()]


def test_B_MP_34_snapshots_kept_setting(make_mp):
    h = make_mp(SEO_MARCUS_SNAPSHOTS_KEPT="2")
    watch(h)
    sids = [_tiny(h, i)["snapshot_id"] for i in range(4)]
    assert [s["snapshot_id"] for s in h.svc.complete_snapshots()] == sids[2:]
    import config as config_mod
    from helpers import base_env
    for bad in ("0", "x", "1000"):
        with pytest.raises(RuntimeError):
            config_mod.load(base_env(SEO_MARCUS_SNAPSHOTS_KEPT=bad))
    assert config_mod.load(base_env()).marcus_snapshots_kept == 6


# ---------------------------------------------------------------------------------------------- B-MP-35 / 36

def test_B_MP_35_a_new_tenant_domain_gets_its_slice_from_the_next_build_only(mp):
    p = [gen_cc.Page("r.example", ("a",), [gen_cc.Link("acme.test", ("x",)), gen_cc.Link("initech.test", ("y",))])]
    gen_cc.write_crawl(mp.fx, C1, [p])
    watch(mp)
    s1 = build(mp)["snapshot_id"]
    before = mp.svc.index_snapshots[s1]["manifest_sha256"]
    mp.tenant("initech", domains=("initech.test",))
    assert [s["domain"] for s in mp.svc.index_store.read_manifest(s1)["shards"]] == ["acme.test"]
    assert mp.svc.index_snapshots[s1]["manifest_sha256"] == before                # never back-filled
    s2 = build(mp)["snapshot_id"]
    assert sorted(s["domain"] for s in mp.svc.index_store.read_manifest(s2)["shards"]) == ["acme.test", "initech.test"]


def test_B_MP_36_registry_byte_for_byte():
    import svc_manager
    for agent, block in REGISTRY.items():
        shown = {**svc_manager.REGISTRY[agent], **svc_manager.NAMES[agent]}       # naming beside the block (Q-MP-1)
        assert set(svc_manager.REGISTRY[agent]) == {"assignment", "inputs", "outputs"}
        assert json.dumps(shown, sort_keys=True) == json.dumps(block, sort_keys=True)
        assert agent in svc_manager.AGENTS and agent not in svc_manager.NOT_BUILT_AGENTS


def test_AC_10_index_key_missing_build_refused_before_any_read(make_mp, monkeypatch):
    from marcus import wat_fetcher
    h = make_mp(data=True, SEO_VAULT_KEY_FILE=None, SEO_VAULT_KEY_ID=None)
    watch(h)
    opened = []
    monkeypatch.setattr(wat_fetcher.FixtureWatSource, "open", lambda self, p: opened.append(p))
    r = post_job(h, "cc_index_builder", crawl_ids=[C1])
    assert r.status_code == 503 and r.json()["detail"] == "INDEX_KEY_UNAVAILABLE"
    assert opened == [] and "link_index_job_requested" not in kinds_logged(h)[-1:]
    r = post_job(h, "cc_graph_importer", release_id=gen_cc.GRAPH, kind="host_graph")
    assert r.status_code == 503 and r.json()["detail"] == "INDEX_KEY_UNAVAILABLE"
